#!/usr/bin/env python3
"""Bounded native-only regression check for the global-vault HOST fixture.

Usage from the repository root:
  python3 -B tests/smoke_global_vault_host_helpers.py --opencode-v2 /path/to/opencode

Uses an exact unmodified native profile, fixture helpers and private synthetic state. Provider
transport alone is adapted to host loopback instead of the Podman gateway. No
Podman, operator credentials/config/history, vault delivery, full backups or
interactive TUI are used or certified. Every generated root is outside the
project/HOME and retained for diagnosis; no host installations or updates occur.
"""

import argparse
from collections import Counter
import io
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tarfile
import tempfile
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

APP = Path(__file__).resolve().parents[1]


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "NATIVE_HELPER_FAILED_ARGUMENTS\n")


def temporary_parent(fixture, repo):
    """Prefer a trusted approved parent; never stage beneath project or HOME."""
    home = Path.home().resolve()

    def allowed(path):
        path = fixture.existing_directory(path)
        fixture.require(
            not path.is_relative_to(repo) and not path.is_relative_to(home),
            "temporary-placement",
        )
        return path

    preferred = Path("/tmp/opencode")
    try:
        # Check placement before creating anything. Do not chmod/replace an
        # existing shared or unfamiliar directory merely to make it usable.
        fixture.require(
            not preferred.is_relative_to(repo) and not preferred.is_relative_to(home),
            "temporary-placement",
        )
        fixture.no_symlinks(preferred)
        preferred.mkdir(mode=0o700, exist_ok=True)
        metadata = preferred.lstat()
        if (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid in (0, os.getuid())
            and not metadata.st_mode & 0o022
            and os.access(preferred, os.W_OK | os.X_OK)
        ):
            return allowed(preferred)
    except (OSError, fixture.FixtureError):
        pass
    return allowed(Path(tempfile.gettempdir()))


def extract_config(fixture, archive, target):
    """Use stdlib's data filter when available, with a strict 3.10 fallback."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        if hasattr(tarfile, "data_filter"):
            source.extractall(target, filter="data")
            return
        # The generated fixture archive contains only files/directories. Avoid
        # unrestricted extractall on older Python; never accept links or devices.
        members = source.getmembers()
        for member in members:
            relative = Path(member.name)
            fixture.require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and (member.isfile() or member.isdir()),
                "config-archive",
            )
        for member in members:
            path = fixture.no_symlinks(target / member.name)
            fixture.require(
                path.is_relative_to(target) and path != target, "config-archive"
            )
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with source.extractfile(member) as reader, path.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
            path.chmod(member.mode & 0o700)


def completed_tools(fixture, exported):
    markers = {
        "shell": fixture.SHELL_MARKER,
        "execute": fixture.MCP_MARKER,
        fixture.ACTION: fixture.TOOL_MARKER,
    }
    found = Counter()
    for message in exported["messages"]:
        if message["type"] != "assistant":
            continue
        for part in message["content"]:
            name = part.get("name")
            state = part.get("state", {})
            if (
                part.get("type") == "tool"
                and name in markers
                and state.get("status") == "completed"
                and markers[name] in json.dumps(state)
            ):
                found[name] += 1
    return found


def run_tools(fixture, binary, request, ident, env, state, prior):
    """Use the exact NATIVE_PROBE, without the production Podman transport."""
    project = fixture.compat.directory(prior)
    fixture.assert_plugins(request, project)
    state.update(before=state["requests"], markers=set())
    command = [
        str(binary),
        "run",
        "--server",
        request.endpoint["url"],
        "--session",
        ident,
        "--agent",
        "build",
        "--model",
        "fixture/gpt-smoke",
        "Exercise the three synthetic global vault fixture tools.",
    ]
    # As in workspace_service.client_command, the native client receives only
    # its private roots and the ephemeral control password, not provider values.
    client = {
        key: env[key]
        for key in (
            "HOME",
            "PATH",
            "LANG",
            "TERM",
            "TMPDIR",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "OPENCODE_DB",
            "OPENCODE_DISABLE_MODELS_FETCH",
            "OPENCODE_DISABLE_AUTOUPDATE",
        )
        if key in env
    }
    client["PWD"] = project
    client["OPENCODE_PASSWORD"] = request.endpoint["password"]
    fixture.require(not any(name in client for name in fixture.KNOWN), "client-env")
    result = fixture.box.run(
        [
            sys.executable,
            "-c",
            fixture.NATIVE_PROBE,
            request.endpoint["url"],
            ident,
            *command,
        ],
        env=client,
        input="",  # Explicit EOF: upstream otherwise waits before inference.
        capture=True,
        private=True,
        timeout=215,
    )
    fixture.require(
        json.loads(result.stdout) == {"completed": True, "permission_asked": 0},
        "native-events",
    )
    fixture.require(
        state["errors"] == 0
        and state["markers"]
        == {fixture.SHELL_MARKER, fixture.MCP_MARKER, fixture.TOOL_MARKER},
        "native-tools",
    )
    actual = request("GET", "/api/experimental/session/" + ident + "/export")["data"]
    fixture.verify_v2_progress(actual, prior)
    before = completed_tools(fixture, prior)
    after = completed_tools(fixture, actual)
    fixture.require(
        all(
            after[name] == before[name] + 1
            for name in ("shell", "execute", fixture.ACTION)
        ),
        "native-completed-tools",
    )
    fixture.require(
        actual["info"].get("permissions") == fixture.ASK_RULES,
        "permission-preservation",
    )
    fixture.require(
        request("GET", "/api/session/" + ident + "/permission")["data"] == [],
        "pending-permission",
    )
    return actual


def check_cli_empty_responses(fixture, binary, request, data, primary):
    """Exercise production CLI parsing, not just the bridge's HTTP JSON reader."""
    original_client = fixture.workspace_service.client_command
    local = {**data, "container": "native-helper-no-container"}

    def local_client(box, workspace, connection, arguments, **kwargs):
        command = original_client(box, workspace, connection, arguments, **kwargs)
        begin = command.index(workspace["container"]) + 1
        return box.ContainerCommand(command[begin:], command.environment)

    with (
        patch.object(fixture.box, "CONTAINER_OPENCODE", str(binary)),
        patch.object(
            fixture.workspace_service, "client_command", side_effect=local_client
        ),
    ):

        def cli(method, target, value=None):
            try:
                return fixture.workspace_service.api(
                    fixture.box, local, request.endpoint, method, target, value
                )
            except json.JSONDecodeError as error:
                if not error.doc.strip():
                    raise fixture.FixtureError("cli-empty-success-unhandled") from None
                raise

        session = fixture.create_session(
            cli, data["project"], agent="plan", parent=primary
        )
        target = "/api/session/" + session["id"] + "/permission"
        result = cli(
            "POST", target, {"action": fixture.ACTION, "resources": ["ask-target"]}
        )["data"]
        fixture.require(result["effect"] == "ask", "cli-plan-ask")
        fixture.require(
            cli("POST", target + "/" + result["id"] + "/reply", {"decision": "reject"})
            is None,
            "cli-empty-permission-reply",
        )
        fixture.require(cli("GET", target)["data"] == [], "cli-permission-cleaned")
        fixture.require(
            cli("DELETE", "/api/session/" + session["id"]) is None,
            "cli-empty-session-delete",
        )
        fixture.require(cli("POST", "/api/location/reload") is None, "cli-empty-reload")
        fixture.assert_plugins(cli, data["project"])
        retained = cli("GET", "/api/session/" + primary)["data"]
        fixture.require(
            retained["id"] == primary and retained["permissions"] == fixture.ASK_RULES,
            "cli-primary-retained",
        )
        fixture.evaluate(cli, primary, "deny", agent="build", resource="session-denied")
        try:
            cli("GET", "/api/session/ses_cli_missing_fixture")
        except fixture.box.BoxError:
            pass
        else:
            raise fixture.FixtureError("cli-failed-command-suppressed")


def main(argv=None):
    parser = Arguments(description=__doc__)
    parser.add_argument("--repo", type=Path, default=APP)
    parser.add_argument("--opencode-v2", type=Path, required=True)
    parser.add_argument("--native-version", choices=("2.2.6", "2.3.0"), default="2.2.6")
    args = parser.parse_args(argv)
    phase = "PREPARE"
    old_umask = os.umask(0o077)
    previous = Path.cwd()
    output = os.fdopen(os.dup(1), "w", buffering=1)
    checks = []

    def mark(name):
        checks.append(name)
        print(name, file=output, flush=True)

    try:
        repo = args.repo.resolve(strict=True)
        if not (repo / "tools/test_global_vault_podman.py").is_file():
            raise ValueError("fixture unavailable")
        sys.path.insert(0, str(repo))
        sys.path.insert(0, str(repo / "src"))
        from tools import test_global_vault_podman as fixture

        binary = args.opencode_v2.expanduser().resolve(strict=True)
        fixture.require(
            binary.is_file() and os.access(binary, os.X_OK), "native-executable"
        )
        parent = temporary_parent(fixture, repo)
        root = Path(tempfile.mkdtemp(prefix="oc-box-gv-native-check-", dir=parent))
        root.chmod(0o700)
        runtime = root / "native-only-runtime"
        runtime.mkdir(mode=0o700)
        baseline_env = {
            "PATH": fixture.box.maintenance_host_env().get("PATH", "/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": str(runtime),
        }
        isolated = fixture.isolated_env(root, baseline_env)
        # A dispatcher tripwire makes accidental Podman subprocesses fail even
        # during future fixture preparation. It does not fake a Podman response.
        (root / "bin").mkdir(mode=0o700)
        with (root / "bin/podman").open("x") as dispatcher:
            dispatcher.write("#!" + sys.executable + "\nimport sys; sys.exit(97)\n")
        (root / "bin/podman").chmod(0o700)

        with fixture.private_output(), patch.dict(os.environ, isolated, clear=True):
            fixture.prepare_root(root, "tumbleweed")
            phase = "HOST_SEED"
            fixture.prepare_native_cli(root, binary, native_version=args.native_version)
            # Exact helper: direct native binary + native_env(root, '2') +
            # private bridge; never the selector that clears bridge auth.
            seed = fixture.seed_native_history(root, binary)
            ident = seed["info"]["id"]
            fixture.require(
                seed["messages"][0]["text"] == fixture.SEED_TEXT, "native-seed"
            )
            mark("NATIVE_HELPER_SEED_OK")

            # These are ordinary synthetic native-probe values, not operator
            # credentials or decrypted vault entries. Vault delivery is outside
            # this native-only check's scope.
            values = {
                fixture.KEY: "native-only-" + secrets.token_urlsafe(24),
                fixture.SIDE: "native-only-" + secrets.token_urlsafe(24),
            }
            state = {
                "values": values,
                "requests": 0,
                "before": 0,
                "markers": set(),
                "errors": 0,
            }
            server, worker = fixture.make_provider(state)
            try:
                phase = "COPIED_CONFIG"
                fixture.write_json_new(
                    root / "config/opencode/opencode.json",
                    fixture.fixture_config(server.server_port, root),
                )
                fixture.write_plugin(root)  # Exact TOOL_SOURCE, unchanged.
                home = root / "native-container-home"
                target = home / ".config/opencode"
                target.mkdir(parents=True, mode=0o700)
                data = {
                    "id": secrets.token_hex(16),
                    "home": str(home),
                    "project": str(root / "source-a"),
                    "opencode_version": args.native_version,
                }
                # Production archive generation performs the real plugin-path
                # rewrite and creates the generated Build helper/profile.
                archive = fixture.box.config_archive(data)
                extract_config(fixture, archive, target)
                for relative in (".local/share", ".local/state", ".cache"):
                    (home / relative).mkdir(parents=True, mode=0o700)
                content = fixture.box.container_build_config(
                    args.native_version, home=str(home)
                )
                # Sole transport adaptation: no Podman gateway exists here.
                # fixture_config, TOOL_SOURCE, MCP and NATIVE_PROBE are unchanged.
                content["providers"] = {
                    "fixture": {
                        "settings": {
                            "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                        }
                    }
                }
                native_env = {
                    **isolated,
                    "HOME": str(home),
                    "PWD": data["project"],
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(home / ".local/share"),
                    "XDG_STATE_HOME": str(home / ".local/state"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                    "OPENCODE_CONFIG": str(target / "oc-box-profile.json"),
                    "OPENCODE_CONFIG_CONTENT": json.dumps(content),
                    "OPENCODE_DB": "oc-box-v2.db",
                    **values,
                }
                with build_opener(ProxyHandler({})).open(
                    f"http://127.0.0.1:{server.server_port}/health", timeout=5
                ) as response:
                    fixture.require(
                        json.load(response) == {"fixture": "oc-box-global-vault"},
                        "provider-health",
                    )
                os.chdir(data["project"])
                with (
                    patch.dict(os.environ, native_env, clear=True),
                    fixture.bridge.server(str(binary)) as request,
                ):
                    phase = "NATIVE_IMPORT"
                    imported = request(
                        "POST",
                        "/api/experimental/session/import",
                        {
                            **seed,
                            "location": {"directory": data["project"]},
                        },
                    )["data"]
                    fixture.require(imported["id"] == ident, "native-import")
                    prior = request(
                        "GET", "/api/experimental/session/" + ident + "/export"
                    )["data"]
                    fixture.require(
                        prior["messages"] == seed["messages"], "native-import-messages"
                    )
                    fixture.verify_v2_progress(prior, seed)
                    fixture.assert_plugins(request, data["project"])
                    mark("NATIVE_HELPER_IMPORT_AND_COPIED_PLUGINS_OK")

                    phase = "CLI_EMPTY_RESPONSES"
                    check_cli_empty_responses(fixture, binary, request, data, ident)
                    mark("NATIVE_HELPER_CLI_REPLY_DELETE_RELOAD_EMPTY_OK")

                    phase = "FRESH_MATRIX"
                    fixture.permission_matrix(request, data["project"], ident)
                    mark("NATIVE_HELPER_BUILD_PLAN_DENIALS_UNKNOWN_FRESH_OK")
                    phase = "FRESH_TOOLS"
                    prior = run_tools(
                        fixture, binary, request, ident, native_env, state, prior
                    )
                    mark("NATIVE_HELPER_THREE_TOOLS_EVENT_BARRIER_FRESH_OK")

                    phase = "RELOAD"
                    request("POST", "/api/location/reload")
                    fixture.assert_plugins(request, data["project"])
                    phase = "RELOADED_MATRIX"
                    fixture.permission_matrix(request, data["project"], ident)
                    mark("NATIVE_HELPER_BUILD_PLAN_DENIALS_UNKNOWN_RELOAD_OK")
                    phase = "RELOADED_TOOLS"
                    prior = run_tools(
                        fixture, binary, request, ident, native_env, state, prior
                    )
                    mark("NATIVE_HELPER_THREE_TOOLS_EVENT_BARRIER_RELOAD_OK")

                    # As in the driver, this is the owner's final model-related
                    # step. No reverse/default-Build reload claim is made.
                    phase = "DEFAULT_PLAN"
                    fixture.default_plan_fallback(root, data, request)
                    final = request(
                        "GET", "/api/experimental/session/" + ident + "/export"
                    )["data"]
                    fixture.verify_v2_progress(final, prior)
                    fixture.verify_v2_progress(final, seed)
                    fixture.require(
                        not (root / "source-a/opencode.json").exists(),
                        "fixture-config-removal",
                    )
                    mark("NATIVE_HELPER_DEFAULT_PLAN_FALLBACK_OK")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

            phase = "REPORT"
            fixture.write_json_new(
                root / "native-helper-result.json",
                {
                    "status": "passed",
                    "checks": checks,
                    "native_version": args.native_version,
                    "native": fixture.recorded_native(
                        root, expected=args.native_version, binary=binary, verify=True
                    ),
                    "source_identity": fixture.get_version_info(repo),
                    "session_id": ident,
                    "scope": (
                        "Native-only helper validation with private synthetic state; "
                        "copied fixture config/plugins, seed/import, shell/MCP/tool "
                        "completion and event barriers, permissions and reload. "
                        "Loopback provider transport. No Podman, vault delivery, "
                        "full backup/restore, host lifecycle or interactive TUI pass."
                    ),
                },
            )
        mark("NATIVE_HELPER_CHECK_PASSED")
        return 0
    except BaseException as error:
        category = "operation-failed"
        error_class = "exception"
        if "fixture" in locals():
            error_class = fixture.safe_error_class(error)
            if (
                isinstance(error, fixture.FixtureError)
                and str(error) == "cli-empty-success-unhandled"
            ):
                category = "cli-empty-success-unhandled"
        print(
            "NATIVE_HELPER_CHECK_FAILED phase="
            + phase
            + " class="
            + error_class
            + " category="
            + category,
            file=output,
            flush=True,
        )
        return 1
    finally:
        os.chdir(previous)
        os.umask(old_umask)
        output.close()


if __name__ == "__main__":
    sys.exit(main())
