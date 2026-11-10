#!/usr/bin/env python3
"""Bounded, opt-in HOST-only manual TUI/web/persistent-shell observation.

Start a FRESH synthetic owner on an unprivileged host terminal:
  python3 -B tools/test_interactive_podman.py start --work-dir /path/to/fixtures \
      --opencode-v2 /path/to/unmodified/opencode-2.2.6
Use only the printed exact --fixture root for secondary tui/terminal/web/stop.
No replay, resume, cleanup, real credentials or automatic manual-UI acceptance.
"""

import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import terminal_ui
from tools import test_global_vault_podman as fixture

box = fixture.box
FORMAT = "oc-box-interactive-only-host-observation"
MODES = ("start", "tui", "terminal", "web", "stop")
FIXTURE_PREFIXES = (fixture.PREFIX, "oc-box-live-v2-", "oc-box-live-backup-")
FIXTURE_MARKERS = ("fixture.json", "interactive.json", "fixture-ids.json")
MARKERS = frozenset((fixture.SHELL_MARKER, fixture.MCP_MARKER, fixture.TOOL_MARKER))
ERROR_CATEGORIES = frozenset(
    {
        "host-only",
        "owner-options",
        "client-options",
        "interactive-tty-required",
        "clean-source-required",
        "source-changed",
        "fresh-parent-required",
        "interactive-format",
        "interactive-only",
        "native-executable",
        "native-podman",
        "native-selector",
        "native-changed",
        "workspace-allowlist",
        "live-owner-required",
        "provider-contract",
        "provider-cleanup-timeout",
        "runtime-boot-current",
        "runtime-boot-marker",
        "runtime-boot-mismatch",
        "unsafe-path",
        "placement",
        "fixture-root",
        "fixture-format",
        "unsafe-metadata",
        "storage-config",
        "podman-selector",
        "fixture-ids",
        "private-store",
        "workspace-state",
        "resource-allowlist",
        "private-volume",
        "published-ports",
        "resource-mounts",
        "interactive-config",
    }
)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "interactive fixture: category=invalid-options\n")


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument("mode", choices=MODES)
    value.add_argument(
        "--fixture",
        type=Path,
        help="Exact printed interactive root; secondary clients only",
    )
    value.add_argument(
        "--work-dir", type=Path, help="Existing disk-backed parent; fresh owner only"
    )
    value.add_argument(
        "--temp-dir",
        type=Path,
        help="Existing private-staging parent; fresh owner only",
    )
    value.add_argument(
        "--opencode-v2",
        type=Path,
        help="Explicit unmodified OpenCode 2.2.6; fresh owner only",
    )
    value.add_argument(
        "--distro",
        choices=tuple(box.DISTROS),
        help="Fresh owner only; default tumbleweed",
    )
    return value


def validate_options(args):
    if args.mode == "start":
        fixture.require(
            args.fixture is None
            and args.work_dir is not None
            and args.opencode_v2 is not None,
            "owner-options",
        )
    else:
        fixture.require(
            args.fixture is not None
            and all(
                getattr(args, name) is None
                for name in ("work_dir", "temp_dir", "opencode_v2", "distro")
            ),
            "client-options",
        )


def require_tty(mode):
    if mode != "stop":
        fixture.require(
            sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty(),
            "interactive-tty-required",
        )


def source_identity():
    """Record exact clean checkout identity, not an installed/fallback version."""
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    with patch.dict(os.environ, env, clear=True):
        info = fixture.get_version_info(APP)
        status = subprocess.run(
            ["git", "-C", str(APP), "status", "--porcelain", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=10,
        )
    fixture.require(
        not status.stdout
        and info.get("dirty") is False
        and isinstance(info.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", info["commit"]) is not None
        and isinstance(info.get("commit_count"), int)
        and info["commit_count"] > 29
        and isinstance(info.get("version"), str)
        and info["version"].endswith("+g" + info["commit"][:7]),
        "clean-source-required",
    )
    return {
        "checkout": str(APP),
        **{key: info[key] for key in ("version", "commit", "commit_count", "dirty")},
    }


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_same_source(initial):
    """Never relabel a long-lived observation to a later/dirty checkout."""
    try:
        unchanged = source_identity() == initial
    except (fixture.FixtureError, OSError, ValueError, subprocess.SubprocessError):
        unchanged = False
    fixture.require(unchanged, "source-changed")


def native_binary(path):
    # npm's normal .bin entry is a symlink; pin and validate its final target.
    path = fixture.no_symlinks(path.expanduser().resolve(strict=True))
    fixture.require(path.is_file() and os.access(path, os.X_OK), "native-executable")
    return path.resolve(strict=True)


def fresh_parent(path):
    parent = fixture.existing_directory(path)
    # A completed (or unfinished/renamed) fixture is evidence, never a work or
    # staging parent. The three host fixture families share these markers.
    for ancestor in (parent, *parent.parents):
        fixture.require(
            not ancestor.name.startswith(FIXTURE_PREFIXES)
            and not any(
                (ancestor / name).exists() or (ancestor / name).is_symlink()
                for name in FIXTURE_MARKERS
            ),
            "fresh-parent-required",
        )
    return parent


def write_metadata(root, source, binary):
    fixture.write_json_new(
        root / "interactive.json",
        {
            "format": FORMAT,
            "version": 1,
            "projects": ["source-a"],
            "source": source,
            "native": {
                "version": fixture.NATIVE_VERSION,
                "path": str(binary),
                "sha256": file_hash(binary),
                "interpreter": sys.executable,
            },
        },
    )


def validate_interactive(root, source):
    """File-only validation, including the full helper's selector/registry guards."""
    fixture.require(root.is_absolute(), "fixture-root")
    root = fixture.validate_fixture(root, allow_unrecorded=True)
    metadata = fixture.private_json(root, "interactive.json")
    fixture.require(
        isinstance(metadata, dict)
        and set(metadata) == {"format", "version", "projects", "source", "native"}
        and metadata["format"] == FORMAT
        and metadata["version"] == 1
        and metadata["projects"] == ["source-a"],
        "interactive-format",
    )
    fixture.require(metadata["source"] == source, "source-changed")
    fixture.require(
        not (root / "result.json").exists()
        and not (root / "seed-baseline.json").exists(),
        "interactive-only",
    )
    native = metadata["native"]
    fixture.require(
        isinstance(native, dict)
        and set(native) == {"version", "path", "sha256", "interpreter"}
        and native["version"] == fixture.NATIVE_VERSION
        and isinstance(native["path"], str)
        and Path(native["path"]).is_absolute()
        and isinstance(native["interpreter"], str)
        and Path(native["interpreter"]).is_absolute(),
        "native-selector",
    )
    binary = native_binary(Path(native["path"]))
    fixture.require(
        not binary.is_relative_to(root) and file_hash(binary) == native["sha256"],
        "native-changed",
    )
    fixture.require(
        fixture.private_json(root, "executables.json") == {"2": str(binary)},
        "native-selector",
    )
    wrapper = fixture.safe_path(root, "bin/opencode")
    fixture.require(
        wrapper.read_text() == fixture.native_wrapper_source(native["interpreter"])
        and os.access(wrapper, os.X_OK),
        "native-selector",
    )
    records = fixture.workspace_records(root)
    fixture.require(set(records) == {"source-a"}, "workspace-allowlist")
    for name in ("config", "data", "state", "cache"):
        fixture.safe_path(root, "host-" + name + "-2", directory=True)
    fixture.require(
        fixture.private_json(root, "config/oc-box/config.json")
        == {
            "default_distro": records["source-a"][1]["distro"],
            "pass_env": [fixture.PASS_ENV],
            "auto_update": False,
        },
        "interactive-config",
    )
    config = fixture.private_json(root, "config/opencode/opencode.json")
    base = (
        config.get("providers", {})
        .get("fixture", {})
        .get("settings", {})
        .get("baseURL", "")
    )
    match = re.fullmatch(r"http://host\.containers\.internal:([0-9]{1,5})/v1", base)
    fixture.require(
        match is not None and 0 < int(match[1]) < 65536, "interactive-config"
    )
    fixture.require(
        config == fixture.fixture_config(int(match[1]), root), "interactive-config"
    )
    plugin = "config/opencode/plugins/fixture-global-vault-permission/"
    fixture.require(
        fixture.safe_path(root, plugin + "index.js").read_text() == fixture.TOOL_SOURCE
        and fixture.safe_path(root, plugin + "package.json").read_text()
        == '{"type":"module","main":"index.js"}\n',
        "interactive-config",
    )
    return root, binary


def provider_state():
    # Only _API_KEY is forwarded by the deliberately explicit --no-vault owner.
    # SIDE/ADDED are _SECRET and must remain absent for this provider contract.
    return {
        "values": {fixture.KEY: secrets.token_urlsafe(32)},
        "requests": 0,
        "before": 0,
        "markers": set(),
        "errors": 0,
    }


@contextlib.contextmanager
def provider(state):
    """The provider lives on the foreground owner stack, through lease draining."""
    server, worker = fixture.make_provider(state)
    try:
        yield server.server_port
    finally:
        # Even an unexpectedly failed serve_forever must not leave the host
        # terminal waiting forever in BaseServer.shutdown. UI execution itself
        # always stays on the foreground thread; this thread only closes HTTP.
        errors = []

        def shutdown():
            try:
                server.shutdown()
            except BaseException as error:
                errors.append(error)

        closer = threading.Thread(target=shutdown, daemon=True)
        try:
            closer.start()
            closer.join(timeout=3)
        finally:
            try:
                # The unchanged provider uses daemon request handlers with 10s
                # socket limits, so server_close does not join those handlers.
                server.server_close()
            finally:
                worker.join(timeout=3)
        fixture.require(
            not closer.is_alive() and not worker.is_alive(), "provider-cleanup-timeout"
        )
        if errors:
            raise errors[0]


def report_value(mode, source, outcome, category, code, state=None, secondary=()):
    result = {
        "format": FORMAT,
        "scope": "interactive-only",
        "mode": mode,
        "source": source,
        "outcome": outcome,
        "category": category,
        "exit_code": code,
        "manual_observations": "NOT_AUTOMATICALLY_PASSED",
        "secondary_errors": list(secondary),
    }
    if state is not None:
        result["provider"] = {
            "requests": state["requests"],
            "errors": state["errors"],
            "markers": sorted(state["markers"] & MARKERS),
        }
    return result


def save_report(root, mode, source, outcome, category, code, state=None, secondary=()):
    path = root / ("interactive-" + mode + "-" + secrets.token_hex(8) + "-report.json")
    # Announcing a destination is not success evidence. Do it before the final
    # write, so an output failure cannot leave a successful terminal report.
    print("Interactive-only report: " + str(path), flush=True)
    fixture.write_json_new(
        path, report_value(mode, source, outcome, category, code, state, secondary)
    )


class Failures:
    """Invocation-local precedence; never consult the caller's sys.exc_info()."""

    def __init__(self):
        self.primary = None
        self.secondary = []
        self.result = ("failed", "preparation", 1)

    def record(self, error, *, category=None):
        outcome, fixed, code = error_outcome(error)
        fixed = category or fixed
        if self.primary is None:
            self.primary = error
            self.result = outcome, fixed, code
        else:
            self.secondary.append(fixed)

    def returned(self, code, label):
        self.result = (
            ("returned", label + "-return", 0)
            if code == 0
            else ("failed", label + "-exit", code)
        )
        if code != 0:
            self.primary = code

    def enter(self, stack, manager):
        value = manager.__enter__()

        def finish():
            try:
                manager.__exit__(None, None, None)
            except BaseException as error:
                self.record(error)

        stack.callback(finish)
        return value

    @contextlib.contextmanager
    def capture(self):
        try:
            yield
        except BaseException as error:
            self.record(error)

    def report(self, root, mode, source, state=None):
        try:
            save_report(root, mode, source, *self.result, state, self.secondary)
        except BaseException as error:
            self.record(error, category="reporting-error")
            # Persistence may be unavailable. Only fixed categories escape;
            # never retry into the original evidence or print private errors.
            print("interactive fixture: category=reporting-error", file=sys.stderr)
        outcome, category, code = self.result
        print(
            "interactive fixture: outcome="
            + outcome
            + " category="
            + category
            + "; manual observations NOT automatically passed",
            flush=True,
        )
        return code


def print_commands(root):
    command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve())])
    print("Retained interactive-only root: " + str(root), flush=True)
    print("Wait for the owning TUI to be ready; in other HOST terminals:", flush=True)
    for mode in ("tui", "terminal", "web", "stop"):
        print(
            "  " + command + " " + mode + " --fixture " + shlex.quote(str(root)),
            flush=True,
        )
    print(
        "Use Build only, with one or two short synthetic prompts (12 provider requests total per owner window). "
        "Expect shell/MCP/permission-tool markers; no Plan/deny conversation support. "
        "Ctrl-] detaches the persistent shell; close attached clients before the owner. "
        "Web prints a private one-use pairing URL: open it manually, never share/record it. "
        "No vault prompt or operator credentials are needed. Manual UI observations are NOT automatically passed.",
        flush=True,
    )


def own(args, source):
    parent = fresh_parent(args.work_dir)
    if args.temp_dir is not None:
        fresh_parent(args.temp_dir)
    binary = native_binary(args.opencode_v2)
    discovered = shutil.which(
        "podman", path=box.maintenance_host_env().get("PATH", os.defpath)
    )
    fixture.require(discovered is not None, "native-podman")
    podman = Path(discovered).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix=fixture.PREFIX + "interactive-", dir=parent))
    root.chmod(0o700)
    print_commands(root)
    state = provider_state()
    failures = Failures()
    try:
        with fixture.private_output():
            fixture.prepare_root(root, args.distro or "tumbleweed")
            fixture.prepare_podman_cli(root, podman)
        with patch.dict(os.environ, fixture.isolated_env(root), clear=True):
            with contextlib.ExitStack() as stack:
                try:
                    # Capture preparation BEFORE unwinding either finalizer.
                    # Each callback records its own failure independently.
                    with fixture.private_output():
                        failures.enter(
                            stack, fixture.temporary_location(root, args.temp_dir)
                        )
                        fixture.validate_store(root)
                        fixture.prepare_native_cli(root, binary)
                        write_metadata(root, source, binary)
                        port = failures.enter(stack, provider(state))
                        fixture.write_json_new(
                            root / "config/opencode/opencode.json",
                            fixture.fixture_config(port, root),
                        )
                        fixture.write_plugin(root)
                except BaseException as error:
                    failures.record(error)
                if failures.primary is None:
                    try:
                        with patch.dict(os.environ, state["values"]):
                            code = box.main(
                                [
                                    "start",
                                    "--project",
                                    str(root / "source-a"),
                                    "--no-vault",
                                ]
                            )
                        failures.returned(code, "launcher")
                    except BaseException as error:
                        failures.record(error)
                    # Preserve the first UI failure if journal/shutdown fails.
                    with fixture.private_output():
                        try:
                            records = fixture.workspace_records(root)
                            fixture.require(
                                set(records) == {"source-a"}, "workspace-allowlist"
                            )
                            fixture.verify_owned_resources(
                                root, records["source-a"][1], stopped=True
                            )
                            fixture.record_ids(root)
                        except BaseException as error:
                            failures.record(error)
                    if state["errors"]:
                        failures.record(fixture.FixtureError("provider-contract"))
        # The real UI and owner lease drain, provider shutdown and resource
        # journal have finished. The report retains the original identity.
        if failures.primary is None:
            require_same_source(source)
    except BaseException as error:
        failures.record(error)
    return failures.report(root, "start", source, state)


def attach(args, source):
    # All checks before engine access are file-only. In-flight owners may not
    # have populated fixture-ids.json yet; record only after full resource proof.
    with fixture.private_output():
        root, binary = validate_interactive(args.fixture, source)
        fixture.require_runtime_boot(root)
    failures = Failures()
    try:
        with patch.dict(os.environ, fixture.isolated_env(root), clear=True):
            with contextlib.ExitStack() as stack, failures.capture():
                failures.enter(stack, fixture.temporary_location(root))
                with fixture.private_output():
                    fixture.validate_store(root, allow_unrecorded=True)
                    fixture.check_native_version(root, binary, "2")
                    records = fixture.workspace_records(root)
                    fixture.require(set(records) == {"source-a"}, "workspace-allowlist")
                    path, data = records["source-a"]
                    info = fixture.verify_owned_resources(root, data)
                    fixture.require(
                        info["State"].get("Running") is True
                        and not info["State"].get("Paused"),
                        "live-owner-required",
                    )
                    fixture.require(
                        fixture.workspace_service.controller_running(box, data),
                        "live-owner-required",
                    )
                    fixture.record_ids(root)
                # Dispatch only native clients against this authenticated
                # connection, NEVER owner-capable box.main. A stop/service
                # failure can invalidate even a held lease; its client must
                # fail rather than falling through to owning startup/unlock.
                connection = failures.enter(
                    stack, fixture.workspace_service.attach(box, path, data)
                )
                try:
                    if args.mode == "tui":
                        command = fixture.workspace_service.client_command(
                            box,
                            data,
                            connection,
                            [
                                box.CONTAINER_OPENCODE,
                                box.container_project(data),
                                "--server",
                                connection["url"],
                                "--session",
                                connection["session"],
                            ],
                            interactive=True,
                        )
                        code = box.run_tui(command)
                    elif args.mode == "terminal":
                        command = fixture.workspace_service.client_command(
                            box,
                            data,
                            connection,
                            [
                                "python3",
                                "-c",
                                (box.PACKAGE / "session_terminal.py").read_text(),
                                connection["url"],
                                connection["session"],
                                box.container_project(data),
                            ],
                            interactive=True,
                        )
                        print(
                            "oc-box: Session shell terminal; Ctrl-] detaches and keeps it running, 'exit' closes the shell.",
                            flush=True,
                        )
                        code = terminal_ui.run_tui(
                            command, env=command.environment, show_loading=False
                        )
                    elif args.mode == "web":
                        web_args = argparse.Namespace(
                            no_open=True,
                            port=fixture.workspace_service.web_parser().get_default(
                                "port"
                            ),
                        )
                        code = fixture.workspace_service.web_session(
                            box, data, connection, web_args
                        )
                    else:  # stop: already verified the sole fixture-owned source.
                        box.stop_container(data)
                        with fixture.private_output():
                            fixture.verify_owned_resources(root, data, stopped=True)
                        code = 0
                        print(
                            "Container stopped; files, packages, and conversations retained.",
                            flush=True,
                        )
                    failures.returned(code, "client")
                except BaseException as error:
                    failures.record(error)
        if failures.primary is None:
            require_same_source(source)
    except BaseException as error:
        failures.record(error)
    return failures.report(root, args.mode, source)


def error_outcome(error):
    if isinstance(error, KeyboardInterrupt):
        return "interrupted", "interrupted", 130
    # Exception messages, subprocess argv/output and control credentials are
    # deliberately never serialized, even when raised during preparation.
    category = (
        str(error)
        if isinstance(error, fixture.FixtureError) and str(error) in ERROR_CATEGORIES
        else fixture.safe_error_class(error)
    )
    return "failed", category, 1


def main(argv=None):
    args = parser().parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        fixture.host_guard()  # BEFORE placement, Git/native probes or resources.
        validate_options(args)
        require_tty(args.mode)
        source = source_identity()
        return own(args, source) if args.mode == "start" else attach(args, source)
    except BaseException as error:
        outcome, category, code = error_outcome(error)
        print(
            "interactive fixture: outcome=" + outcome + " category=" + category,
            file=sys.stderr,
        )
        return code
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    sys.exit(main())
