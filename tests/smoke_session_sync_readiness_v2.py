#!/usr/bin/env python3
"""Exact HOST-runner native readiness/background methods, without Podman.

Run with --source /clean/frozen/checkout --expect-commit FULL_SHA --opencode PATH
--native-version 2.3.0. The runner is loaded from this checkout, outside --source.
Only container execution/addressing, the synthetic FIFO location and
post-readiness lease/archive callbacks are adapted. Native API/CLI, provider, paired inventories and production history
waits are real, not response fakes. This is NOT host/lifecycle/archive acceptance.
All state is fresh synthetic data in a retained private temporary directory.
"""

import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / "tools/test_session_sync_podman.py"
spec = importlib.util.spec_from_file_location("native_readiness_runner", RUNNER)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def private_environment(root):
    env = {
        "PATH": os.defpath + ":/usr/local/bin",
        "LANG": "C.UTF-8",
        "SHELL": "/bin/bash",
        "TERM": "xterm-256color",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "OPENCODE_DB": "oc-box-readiness.db",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    }
    for key, name in (
        ("HOME", "home"),
        ("TMPDIR", "tmp"),
        *(
            (f"XDG_{kind}_HOME", kind.lower())
            for kind in ("CONFIG", "DATA", "CACHE", "STATE")
        ),
    ):
        directory = root / name
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    env["OPENCODE_TEST_HOME"] = env["HOME"]
    return env


def command(arguments, env, cwd, *, text_input=None, timeout=30):
    process = subprocess.Popen(
        arguments,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE if text_input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(text_input, timeout=timeout)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise
    host.require(process.returncode == 0, "subprocess-error")
    return output


def run(args):
    # This never calls or weakens the HOST runner's main/host guard.
    loaded = host.load_source(args.source, args.expect_commit)
    box, service, bridge = loaded.box, loaded.service, loaded.fixture.bridge
    binary = args.opencode.resolve(strict=True)
    native_hash = host.file_hash(binary)
    root = Path(
        tempfile.mkdtemp(
            prefix="oc-box-native-readiness-", dir=os.environ.get("TMPDIR", "/tmp")
        )
    )
    root.chmod(0o700)
    project = root / "project"
    project.mkdir(mode=0o700)
    env = private_environment(root)
    provider = host.Provider()
    config = provider.config()
    config["providers"]["fixture"]["settings"]["baseURL"] = (
        f"http://127.0.0.1:{provider.server.server_port}/v1"
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    harness = host.Harness(
        loaded, root, binary, host.file_hash(RUNNER), native_version=args.native_version
    )
    harness.provider = provider
    harness.path = root / "workspace.json"
    harness.path.write_text(json.dumps({"opencode_version": args.native_version}))
    harness.data = {"project": str(project)}
    box.session_file(harness.path).write_text('{"synthetic":true}\n')
    primary = None
    failure_trace = []
    exit_info = {}
    previous = Path.cwd()
    native_calls = []
    sync_calls = []
    archive_calls = []

    def record(error):
        nonlocal primary, failure_trace
        if primary is None:
            primary, failure_trace = host.category(error), host.trace(error)
        else:
            harness.secondary.append(host.category(error))

    try:
        os.chdir(project)
        from oc_box import opencode_compat

        with (
            patch.dict(os.environ, env, clear=True),
            bridge.server(str(binary), exit_info=exit_info) as request,
        ):
            client_env = {**env, "OPENCODE_PASSWORD": request.endpoint["password"]}
            host.require(
                opencode_compat.version(
                    command([str(binary), "--version"], env, project)
                )
                == args.native_version,
                "native-changed",
            )
            harness.connection = request.endpoint

            def container_python(code, value, *, timeout=60):
                # The exact runner's native_run/batch/ShellGate scripts execute;
                # only Podman exec becomes a local private process.
                output = command(
                    [sys.executable, "-c", code],
                    client_env,
                    project,
                    text_input=json.dumps(value),
                    timeout=timeout,
                )
                return output

            harness.container_python = container_python
            native_run = harness.native_run

            def observed_native_run(*arguments):
                value = native_run(*arguments)
                native_calls.append(value)
                return value

            harness.native_run = observed_native_run

            class LocalShellGate(host.ShellGate):
                def __init__(self, owner, *, marker):
                    # Path/execution boundary only: command/release are inherited
                    # unchanged and the native shell really reads this FIFO.
                    self.harness, self.marker, self.released = owner, marker, False
                    self.path = str(root / "tmp/background.fifo")
                    owner.container_python(
                        "import json,os,sys\np=json.load(sys.stdin);os.mkfifo(p,0o600)\n",
                        self.path,
                    )

            def local_client(_box, data, connection, arguments, **options):
                host.require(
                    connection == request.endpoint
                    and data == harness.data
                    and arguments[:3] == [str(binary), "api", "get"]
                    and arguments[3]
                    in ("/api/session/active", "/api/experimental/job"),
                    "oracle",
                )
                return box.ContainerCommand(arguments, client_env)

            @contextlib.contextmanager
            def lease(_box, path, data):
                # Callback rig only: not production owner/socket acceptance.
                host.require(path == harness.path and data == harness.data)
                harness.owner.clients += 1
                try:
                    yield request.endpoint
                finally:
                    harness.owner.clients -= 1

            harness.owner = SimpleNamespace(
                clients=0, closing=False, child=SimpleNamespace(poll=lambda: None)
            )
            harness.wait_entered = threading.Event()
            harness.wait_entered.set()

            def sync():
                host.require(service.live_connection() == request.endpoint)
                sync_calls.append(True)

            harness.sync = sync
            harness.verify_archive = lambda ident: archive_calls.append(
                ident == "ses_history_background"
            )
            token = service.CONNECTION.set(request.endpoint)
            try:
                with (
                    patch.object(box, "CONTAINER_OPENCODE", str(binary)),
                    patch.object(service, "client_command", local_client),
                    patch.object(service, "attach", lease),
                    patch.object(host, "ShellGate", LocalShellGate),
                    patch.object(
                        loaded.fixture,
                        "verify_owned_resources",
                        return_value={"State": {"Running": True}},
                    ),
                    box.workspace_lock(harness.path),
                ):
                    selected = (
                        request(
                            "POST",
                            "/api/session",
                            {"location": {"directory": str(project)}},
                        )["data"]["id"]
                        if args.parent_mode == "native"
                        else harness.seed("ses_history_root")
                    )
                    host.require(harness.readiness(selected) == 0)
                    harness.lease.join(5)
                    if args.case == "all":
                        host.require(harness.background(selected) == 0)
            finally:
                service.CONNECTION.reset(token)
    except BaseException as error:
        record(error)
    finally:
        os.chdir(previous)
        try:
            provider.close()
        except BaseException as error:
            record(error)
        observations = (
            harness.background_observations
            if harness.background_observations["phase"] != "not-started"
            else harness.readiness_observations
        )
        observations.update(
            provider_requests=provider.requests,
            provider_errors=provider.errors,
            provider_failure=provider.failure,
        )
    for verify in (
        lambda: host.require(host.file_hash(binary) == native_hash, "native-changed"),
        lambda: host.require(
            host.source_snapshot(loaded.source, args.expect_commit) == loaded.identity,
            "source-changed",
        ),
        lambda: host.import_guard(loaded.source, loaded.identity["sha256"]),
        lambda: host.require(
            host.file_hash(RUNNER) == harness.runner, "source-changed"
        ),
    ):
        try:
            verify()
        except BaseException as error:
            record(error)
    if primary is None:
        try:
            host.require(native_calls == ([0, 0] if args.case == "all" else [0]))
            host.require(exit_info == {"code": 0, "forced": False})
            host.require(harness.owner.clients == 0)
            host.require(
                provider.errors == 0
                and provider.requests
                == (
                    (7 if args.native_version == "2.3.0" else 4)
                    if args.case == "all"
                    else (2 if args.native_version == "2.3.0" else 1)
                ),
                "provider-contract",
            )
            host.require(
                args.native_version != "2.3.0"
                or args.case != "all"
                or all(
                    host.additional_checks(
                        harness.readiness_observations, harness.background_observations
                    ).values()
                )
            )
        except BaseException as error:
            record(error)
    result = {
        "scope": "exact native readiness/background methods; NOT Podman/host acceptance",
        "source_identity": loaded.identity,
        "runner_sha256": harness.runner,
        "probe_sha256": host.file_hash(__file__),
        "runner_checkout_commit": host.git(RUNNER.parents[1], "rev-parse", "HEAD")
        .decode()
        .strip(),
        "runner_checkout_dirty": bool(
            host.git(
                RUNNER.parents[1], "status", "--porcelain", "--untracked-files=all"
            )
        ),
        "case": args.case,
        "parent_mode": args.parent_mode,
        "native": harness.native,
        "primary_category": primary,
        "secondary_categories": harness.secondary,
        "failure_trace": failure_trace if primary else [],
        "checks": harness.checks,
        "callback_only_checks": ["held_lease_shutdown"],
        "readiness_observations": harness.readiness_observations,
        "background_observations": harness.background_observations,
        "additional_checks": host.additional_checks(
            harness.readiness_observations, harness.background_observations
        )
        if args.native_version == "2.3.0"
        else {},
        "provider_requests": provider.requests,
        "provider_errors": provider.errors,
        "background_requests": provider.background_requests,
        "native_client_exits": native_calls,
        "private_server_exit": exit_info,
        "sync_callback_count": len(sync_calls),
        "archive_callback_count": len(archive_calls),
        "archive_callbacks_valid": all(archive_calls),
        "limitations": [
            "lease owner/resource callback rig only; no Podman status/socket acceptance",
            "sync/archive callbacks record context only; no host archive/lifecycle proof",
            "no native API/activity/job/provider response substitution",
        ],
    }
    result_path = root / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    result_path.chmod(0o600)
    print(json.dumps({"root": str(root), **result}, indent=2))
    return 0 if primary is None and not harness.secondary else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--native-version", choices=host.NATIVE_VERSIONS, required=True)
    parser.add_argument("--case", choices=("foreground", "all"), default="all")
    parser.add_argument("--parent-mode", choices=("native", "seeded"), default="native")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
