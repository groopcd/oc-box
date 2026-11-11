"""Bounded, provider-free production session-backup body transport smoke.

Run with the intended package selected by normal installation or PYTHONPATH:
  python -B tests/smoke_session_backup_transport_v2.py --opencode /path/to/opencode

All native responses and CLI subprocesses are real. Host discovery/environment
selection points only to this fixture's explicit private --stdio server. The
workspace registry/lease and Podman execution envelope are synthetic; the real
production client_command is inspected, then its native command executes locally
against that same private server. This is NOT normal host discovery, interactive
TTY review, real credentials, real Podman, or host acceptance. No runtime source
is copied, executed through runpy, replaced or patched.
"""

import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from oc_box import backup_crypto as crypto
from oc_box import oc_box as box
from oc_box import opencode_compat as compat
from oc_box import opencode_v2_io as bridge
from oc_box import session_backups as engine
from oc_box import tui_backend, tui_operations, tui_sessions, workspace_service


SCOPE = "OFFLINE_NATIVE_BODY_TRANSPORT_ONLY"
SENTINEL = "SYNTHETIC_SESSION_BODY_SENTINEL_"
LARGE_TEXT = 4 * 1024 * 1024 + 128
MOCK_BOUNDARIES = (
    "host_discovery_to_explicit_private_server",
    "host_environment_selector",
    "workspace_registry_selection",
    "workspace_owner_client_lease",
    "podman_exec_envelope_to_local_explicit_native_cli",
)


class FixtureFailure(RuntimeError):
    """Fixed fixture diagnostics only; never wraps native output."""


def require(condition):
    if not condition:
        raise FixtureFailure()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def runtime_manifest():
    package = Path(engine.__file__).resolve().parent
    result = {}
    for name, module in tuple(sys.modules.items()):
        if name != "oc_box" and not name.startswith("oc_box."):
            continue
        location = getattr(module, "__file__", None)
        if location:
            path = Path(location).resolve()
            if path.suffix == ".py" and path.is_relative_to(package):
                result[str(path.relative_to(package))] = digest(path)
    require(
        {
            "tui_operations.py",
            "tui_backend.py",
            "session_backups.py",
            "workspace_service.py",
            "opencode_v2_io.py",
            "backup_crypto.py",
        }
        <= set(result)
    )
    return result


def source_state(environment):
    # A wheel can have no surrounding Git checkout. Never discover/read user
    # profiles: all git configuration and environment here is fixture-private.
    package = Path(engine.__file__).resolve().parent
    result = subprocess.run(
        ["git", "-C", str(package), "rev-parse", "--show-toplevel"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    if result.returncode:
        return {"commit": None, "working_tree_dirty": None, "git_checkout": False}
    root = result.stdout.decode().strip()
    commit = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    status = subprocess.run(
        ["git", "-C", root, "status", "--porcelain"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    require(commit.returncode == 0 and status.returncode == 0)
    identity = commit.stdout.decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", identity) is not None)
    return {
        "commit": identity,
        "working_tree_dirty": bool(status.stdout),
        "git_checkout": True,
    }


def private_environment(root):
    home = root / "home"
    values = {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_TEST_HOME": str(home),
        "OPENCODE_DB": "oc-box-v2.db",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "plugins": [],
                "policies": [
                    {"action": "plugin.use", "resource": "*", "effect": "deny"}
                ],
            }
        ),
    }
    for name in (
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        Path(values[name]).mkdir(mode=0o700, parents=True, exist_ok=True)
    return values


class NativeRuntime:
    """Adapt only discovery/environment/Podman framing, not runtime API logic."""

    BoxError = box.BoxError
    ContainerCommand = box.ContainerCommand

    def __init__(self, binary, environment, endpoint, project, deadline):
        self.binary, self.environment, self.endpoint = binary, environment, endpoint
        self.project, self.deadline = project, deadline
        self.CONTAINER_OPENCODE = binary
        self.counts = {
            "host_cli": 0,
            "workspace_cli": 0,
            "host_imports": 0,
            "workspace_imports": 0,
            "host_large_imports": 0,
            "workspace_large_imports": 0,
            "workspace_envelopes": 0,
        }

    def maintenance_host_env(self):
        return {**self.environment, "OPENCODE_PASSWORD": self.endpoint["password"]}

    def container_project(self, data):
        require(data["project"] == str(self.project))
        return data["project"]

    def run(self, command, **kwargs):
        require(kwargs.get("capture") is True and kwargs.get("private") is True)
        original = [str(value) for value in command]
        require(
            all(
                SENTINEL not in value and self.endpoint["password"] not in value
                for value in original
            )
        )
        require("--standalone" not in original and "--data" not in original)
        mode = "host"
        if original[0] == "podman":
            mode = "workspace"
            require(
                original[:8]
                == [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    str(self.project),
                    "--env",
                    "OPENCODE_PASSWORD",
                    "fixture-container",
                ]
            )
            require("-t" not in original and "--tty" not in original)
            require(original[8:10] == ["python3", "-c"] and len(original) > 12)
            names = json.loads(original[11])
            require(
                "OPENCODE_PASSWORD" not in names and names["PWD"] == str(self.project)
            )
            require(
                names["HOME"] == self.environment["HOME"]
                and names["OPENCODE_DB"] == self.environment["OPENCODE_DB"]
            )
            inherited = getattr(command, "environment", None)
            require(
                isinstance(inherited, dict)
                and inherited.get("OPENCODE_PASSWORD") == self.endpoint["password"]
            )
            # Only replace the Podman/Python environment-and-exec envelope. Keep
            # the production native CLI arguments unchanged, including --server.
            actual = original[12:]
            require(actual[0] == self.binary and "--server" in actual)
            require(actual[actual.index("--server") + 1] == self.endpoint["url"])
            kwargs["env"] = {
                **self.environment,
                "OPENCODE_PASSWORD": inherited["OPENCODE_PASSWORD"],
            }
            self.counts["workspace_envelopes"] += 1
        else:
            require(original[0] == "opencode")
            actual = [self.binary, *original[1:]]
            require(
                kwargs.get("env", {}).get("OPENCODE_PASSWORD")
                == self.endpoint["password"]
            )
            if actual[1:4] != ["debug", "paths", "db"]:
                require("--server" not in actual)
                actual.extend(["--server", self.endpoint["url"]])
        self.counts[mode + "_cli"] += 1
        if actual[1:3] == ["session", "import"]:
            require(actual[3] == "/proc/self/fd/0" and "--directory" in actual)
            body = kwargs.get("input")
            require(isinstance(body, str) and "stdin" not in kwargs)
            self.counts[mode + "_imports"] += 1
            if len(body.encode()) > 4 * 1024 * 1024:
                self.counts[mode + "_large_imports"] += 1
        else:
            require("input" not in kwargs)
        remaining = self.deadline - time.monotonic()
        require(remaining > 0)
        kwargs["timeout"] = min(float(kwargs.get("timeout", 10)), 10, remaining)
        kwargs.setdefault("cwd", self.project)
        return box.run(actual, **kwargs)


def seed(request, ident, directory, *, parent=None, large=False):
    info = {
        "id": ident,
        "projectID": "global",
        "location": {"directory": str(directory)},
        "title": "Synthetic transport source",
        "time": {"created": 1, "updated": 2},
        "cost": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        "agent": "plan",
        "metadata": {"untrusted": "synthetic-only", "oc-box.archive": {"version": 1}},
    }
    if parent:
        info["parentID"] = parent
    value = {
        "info": info,
        "messages": [
            {
                "id": "msg_" + ident[4:],
                "type": "user",
                "time": {"created": 1},
                "text": SENTINEL + ("x" * LARGE_TEXT if large else "small"),
            }
        ],
        "location": {"directory": str(directory)},
    }
    require(request("POST", "/api/experimental/session/import", value) is None)


def catalog(connection):
    return tui_sessions.SessionCatalog(
        connection.request,
        connection.origin,
        connection.store,
        ownership=connection.ownership,
    )


def round_trip(
    connection, native, root, destination, recipient, identity, label, state
):
    state["stage"] = label + "-backup"
    selected = catalog(connection)
    baseline = {
        item["id"]: bridge.checked_export(native, item["id"])
        for item in selected.list()
    }
    archive = root / (label + ".ocsession")
    summary = engine.backup(
        selected, "ses_fixture_root", archive, recipients=[recipient]
    )
    require(summary["session_count"] == 2 and summary["message_count"] == 2)
    require(SENTINEL.encode() not in archive.read_bytes())
    require(archive.stat().st_mode & 0o777 == 0o600 and archive.stat().st_nlink == 1)
    state["stage"] = label + "-inspect"
    inspected = engine.inspect(archive, identities=[identity])
    require(inspected["session_count"] == 2 and SENTINEL not in json.dumps(inspected))
    state["stage"] = label + "-prepare"
    plan = engine.prepare_restore(
        selected, archive, str(destination), identities=[identity]
    )
    require(
        plan.review["destination"]
        == {
            "origin": connection.origin,
            "store": connection.store,
            "directory": str(destination),
        }
    )
    state["stage"] = label + "-restore"
    result = engine.restore(selected, plan)
    require(result["session_count"] == 2 and result["atomic"] is False)
    created = result["created_ids"]
    require(
        len(set(created)) == 2
        and all(re.fullmatch(r"ses_[0-9a-f]{32}", value) for value in created)
    )
    require(not set(created) & set(baseline))
    messages = {
        message["id"] for value in baseline.values() for message in value["messages"]
    }
    observed = [bridge.checked_export(native, ident) for ident in created]
    for original_id, restored in zip(
        ("ses_fixture_root", "ses_fixture_child"), observed
    ):
        original = baseline[original_id]
        require(restored["info"]["location"] == {"directory": str(destination)})
        require(restored["info"]["permissions"] == [])
        require(
            restored["info"]["metadata"]
            == {
                "oc-box.session-backup": {
                    "version": 1,
                    "source": {
                        "origin": connection.origin,
                        "store": connection.store,
                        "id": original_id,
                    },
                }
            }
        )
        require(
            not set(restored["info"]) & {"agent", "model", "fork", "revert", "subpath"}
        )
        require(
            restored["info"]["time"]["created"] == original["info"]["time"]["created"]
        )
        require(len(restored["messages"]) == len(original["messages"]))
        for source_message, target_message in zip(
            original["messages"], restored["messages"]
        ):
            new_id = target_message["id"]
            require(
                re.fullmatch(r"msg_[0-9a-f]{32}", new_id) is not None
                and new_id not in messages
            )
            messages.add(new_id)
            require(target_message == {**source_message, "id": new_id})
    require("parentID" not in observed[0]["info"])
    require(observed[1]["info"]["parentID"] == created[0])
    state["stage"] = label + "-conflict"
    conflicting = copy.deepcopy(observed[0])
    conflicting["location"] = {"directory": str(destination)}
    conflicting["messages"][0]["text"] += "_CONFLICT_MUST_NOT_REPLACE"
    try:
        connection.request("POST", "/api/experimental/session/import", conflicting)
    except tui_operations.OperationError as error:
        require(error.reason == "import-acknowledgement-unverified")
    else:
        raise FixtureFailure()
    require(bridge.checked_export(native, created[0]) == observed[0])
    state["stage"] = label + "-preservation"
    for ident, exported in baseline.items():
        require(bridge.checked_export(native, ident) == exported)
    require(
        {item["id"] for item in tui_sessions.list_sessions(native)}
        == set(baseline) | set(created)
    )
    return {
        "session_count": 2,
        "fresh_session_and_message_ids": True,
        "encrypted_inspection_metadata_only": True,
        "permissions_and_metadata_exact": True,
        "destination_exact": True,
        "parent_remapped_foreign_parent_removed": True,
        "source_and_preexisting_exports_unchanged": True,
        "inventory_expected_only": True,
        "native_conflict_acknowledgement_refused": True,
        "conflict_history_unchanged": True,
        "message_text_sha256": hashlib.sha256(
            observed[0]["messages"][0]["text"].encode()
        ).hexdigest(),
    }


def execute(binary, state, report):
    started = time.monotonic()
    deadline = started + 110
    binary = str(Path(binary).expanduser().resolve(strict=True))
    report["native_sha256"] = digest(binary)
    report["fixture_sha256"] = digest(__file__)
    report["runtime_files_before"] = runtime_manifest()
    with tempfile.TemporaryDirectory(
        prefix="oc-box-session-body-transport-"
    ) as temporary:
        root = Path(temporary)
        env = private_environment(root)
        report["runtime_source_before"] = source_state(env)
        source, host_target, workspace_target = (
            root / name for name in ("source", "host-target", "workspace-target")
        )
        for path in (source, host_target, workspace_target):
            path.mkdir(mode=0o700)
        with patch.dict(os.environ, env, clear=True):
            state["stage"] = "native-version"
            version = box.run(
                [binary, "--version"],
                env=env,
                capture=True,
                private=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
            )
            report["native_version"] = compat.version(version.stdout)
            require(report["native_version"] == "2.2.6")
            private = crypto.small(["age-keygen"])
            recipient = (
                crypto.small(["age-keygen", "-y"], data=private).decode().strip()
            )
            identity = root / "synthetic-age-identity"
            identity.write_bytes(private)
            identity.chmod(0o600)
            state["stage"] = "private-server"
            with bridge.server(binary) as native:
                pid = native("GET", "/api/info")["pid"]
                require(type(pid) is int and pid > 0)
                runtime = NativeRuntime(
                    binary, env, native.endpoint, workspace_target, deadline
                )
                backend = tui_operations._OperationBackend(runtime)
                state["stage"] = "host-connection"
                host = backend.session_connection()
                try:
                    state["stage"] = "seed-via-protected-cli-import"
                    seed(host.request, "ses_fixture_foreign", source)
                    seed(
                        host.request,
                        "ses_fixture_root",
                        source,
                        parent="ses_fixture_foreign",
                        large=True,
                    )
                    seed(
                        host.request,
                        "ses_fixture_child",
                        source,
                        parent="ses_fixture_root",
                    )
                    report["host"] = round_trip(
                        host,
                        native,
                        root,
                        host_target,
                        recipient,
                        identity,
                        "host",
                        state,
                    )
                    require(backend._expected_host_pid == pid)
                finally:
                    host.close()
                state["stage"] = "workspace-connection"
                workspace_id = "e" * 32
                registration = root / "synthetic-workspace.json"
                data = {
                    "id": workspace_id,
                    "project": str(workspace_target),
                    "home": env["HOME"],
                    "container": "fixture-container",
                    "opencode_version": "2.2.6",
                }
                workspace_backend = tui_operations._OperationBackend(runtime)
                lease_counts = {"entered": 0, "closed": 0}

                def selected(ident):
                    require(ident == workspace_id)
                    return registration, data

                @contextlib.contextmanager
                def lease(queries, path, value):
                    require(
                        queries._box is runtime
                        and path == registration
                        and value is data
                    )
                    lease_counts["entered"] += 1
                    try:
                        yield {**native.endpoint, "session": "ses_fixture_root"}
                    finally:
                        lease_counts["closed"] += 1

                with (
                    patch.object(workspace_backend, "selected", side_effect=selected),
                    patch.object(workspace_service, "attach", side_effect=lease),
                ):
                    connection = workspace_backend.session_connection(workspace_id)
                    try:
                        report["workspace"] = round_trip(
                            connection,
                            native,
                            root,
                            workspace_target,
                            recipient,
                            identity,
                            "workspace",
                            state,
                        )
                    finally:
                        connection.close()
                require(lease_counts == {"entered": 1, "closed": 1})
                require(native("GET", "/api/info")["pid"] == pid and native.alive())
                counts = runtime.counts
                require(
                    counts["host_large_imports"] >= 3
                    and counts["workspace_large_imports"] == 2
                )
                require(counts["workspace_envelopes"] == counts["workspace_cli"])
                report["transport_calls"] = counts
                report["server_pid_stable"] = True
                report["workspace_lease_balanced"] = True
                report["workspace_production_stdin_non_tty_envelopes_checked"] = True
                report["host_and_workspace_body_routes_used"] = True
                report["body_and_auth_absent_from_argv"] = True
                require(
                    report["host"]["message_text_sha256"]
                    == report["workspace"]["message_text_sha256"]
                )
            report["runtime_source_after"] = source_state(env)
            report["only_ciphertext_archive_files_outside_native_state"] = set(
                root.iterdir()
            ) == {
                root / "home",
                root / "tmp",
                source,
                host_target,
                workspace_target,
                identity,
                root / "host.ocsession",
                root / "workspace.ocsession",
            }
            require(report["only_ciphertext_archive_files_outside_native_state"])
    report["runtime_files_after"] = runtime_manifest()
    require(report["runtime_files_before"] == report["runtime_files_after"])
    require(report["native_sha256"] == digest(binary))
    report["runtime_file_hashes_stable"] = True
    report["native_file_unchanged"] = True
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    require(report["elapsed_seconds"] < 120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opencode",
        required=True,
        help="Explicit unmodified native OpenCode 2.2.6 executable",
    )
    args = parser.parse_args()
    report = {
        "scope": SCOPE,
        "mock_boundaries": list(MOCK_BOUNDARIES),
        "normal_host_discovery_tested": False,
        "real_podman_tested": False,
        "interactive_tty_review_tested": False,
        "host_acceptance": False,
        "provider_inference": False,
        "standalone_used": False,
    }
    state = {"stage": "initialization"}

    def expired(_signal, _frame):
        raise FixtureFailure()

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 110)
    try:
        execute(args.opencode, state, report)
        report["passed"] = True
    except BaseException:
        report.update(
            passed=False,
            failed_stage=state["stage"],
            failure_category="fixture-check-or-native-operation",
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
