#!/usr/bin/env python3
"""Bounded, opt-in HOST-only manual manager/TUI/web/persistent-shell observation.

Start a FRESH synthetic owner on an unprivileged host terminal:
  python3 -B tools/test_interactive_podman.py start --work-dir /path/to/fixtures \
      --opencode-v2 /path/to/unmodified/opencode-2.2.6
Use only the printed exact --fixture root for secondary manager/tui/terminal/web/stop.
Manager opens the actual Textual dashboard without a monitoring client lease;
install optional textual/pyte dependencies in the helper's Python environment.
No replay, resume, cleanup, real credentials or automatic manual-UI acceptance.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import stat
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
MODES = ("start", "manager", "tui", "terminal", "web", "stop")
FIXTURE_PREFIXES = (fixture.PREFIX, "oc-box-live-v2-", "oc-box-live-backup-")
FIXTURE_MARKERS = ("fixture.json", "interactive.json", "fixture-ids.json")
MARKERS = frozenset((fixture.SHELL_MARKER, fixture.MCP_MARKER, fixture.TOOL_MARKER))
HOST_SERVICE_FORMAT = "oc-box-interactive-private-host-service-port"
HOST_SERVICE_MARKER = "host-service-port.json"
HOST_SERVICE_CONFIG = "host-config-2/opencode/service.json"
HOST_SERVICE_REGISTRATION = "host-state-2/opencode/service.json"
HOST_SERVICE_MAX_BYTES = 65536
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
        "manager-unavailable",
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
        "host-service-existing",
        "host-service-selector",
        "host-service-environment",
        "host-service-port",
        "host-service-setter",
        "host-service-marker",
        "host-service-config",
        "host-service-registration",
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


def private_host_service_json(root, relative):
    """Bounded private metadata only; never return it to reports or diagnostics."""
    path = fixture.safe_path(root, relative)
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as stream:
        info = os.fstat(stream.fileno())
        fixture.require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and info.st_size <= HOST_SERVICE_MAX_BYTES,
            "unsafe-metadata",
        )
        content = stream.read(HOST_SERVICE_MAX_BYTES + 1)
    fixture.require(len(content) <= HOST_SERVICE_MAX_BYTES, "unsafe-metadata")
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        raise fixture.FixtureError("unsafe-metadata") from None


def private_host_service_roots(root):
    for relative in (
        "",
        "bin",
        "host-home",
        *("host-" + n + "-2" for n in ("config", "data", "state", "cache")),
    ):
        path = fixture.no_symlinks(root / relative)
        info = path.stat()
        fixture.require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "unsafe-metadata",
        )


def host_service_port_valid(port):
    # Kernel-selected ephemeral ports must not fall back to native's default.
    return type(port) is int and 1024 <= port <= 65535 and port != 49374


def host_service_metadata(root, source, binary, port):
    return {
        "format": HOST_SERVICE_FORMAT,
        "version": 1,
        "source": source,
        "hostname": "127.0.0.1",
        "port": port,
        "native_version": fixture.NATIVE_VERSION,
        "native_sha256": file_hash(binary),
        "wrapper_sha256": file_hash(root / "bin/opencode"),
        "profile": {
            "home": "host-home",
            "data": "host-data-2",
            "config": HOST_SERVICE_CONFIG,
            "registration": HOST_SERVICE_REGISTRATION,
        },
    }


def validate_host_service_port(root, source, binary):
    """File-only selector validation; no discovery, repair or service shutdown.

    Native 2.2.6's service schema has hostname/remote/env/cors/disabled controls;
    none belongs in this fixture profile. With hostname omitted, native binds
    127.0.0.1. Only its automatic opaque authentication addition is tolerated.
    """
    private_host_service_roots(root)
    marker = private_host_service_json(root, HOST_SERVICE_MARKER)
    port = marker.get("port") if isinstance(marker, dict) else None
    fixture.require(host_service_port_valid(port), "host-service-port")
    fixture.require(
        type(marker.get("version")) is int
        and marker == host_service_metadata(root, source, binary, port),
        "host-service-marker",
    )
    config = private_host_service_json(root, HOST_SERVICE_CONFIG)
    fixture.require(
        isinstance(config, dict)
        and set(config) in ({"port"}, {"port", "password"})
        and type(config.get("port")) is int
        and config["port"] == port
        and (
            "password" not in config
            or isinstance(config["password"], str)
            and 0 < len(config["password"]) <= 4096
        ),
        "host-service-config",
    )
    registration = fixture.no_symlinks(root / HOST_SERVICE_REGISTRATION)
    if registration.exists():
        record = private_host_service_json(root, HOST_SERVICE_REGISTRATION)
        fixture.require(
            isinstance(record, dict)
            and set(record) == {"id", "pid", "url", "version", "password"}
            and isinstance(record.get("id"), str)
            and 0 < len(record["id"]) <= 256
            and type(record.get("pid")) is int
            and record["pid"] > 1
            and record.get("url") == "http://127.0.0.1:" + str(port)
            and record.get("version") == fixture.NATIVE_VERSION
            and isinstance(record.get("password"), str)
            and 0 < len(record["password"]) <= 4096,
            "host-service-registration",
        )
    return port


def prepare_host_service_port(root, source, binary):
    """Set one fresh profile's supported native port; never start its service.

    The reservation closes after configuration, before any future UI Connect.
    A later release-to-bind collision must fail, not retry another port, fall
    back to 49374 or stop an unknown listener. No existing profile is reused.
    """
    private_host_service_roots(root)
    for relative in (
        HOST_SERVICE_CONFIG,
        HOST_SERVICE_REGISTRATION,
        HOST_SERVICE_MARKER,
    ):
        path = fixture.no_symlinks(root / relative)
        fixture.require(not path.exists(), "host-service-existing")
    wrapper = fixture.safe_path(root, "bin/opencode")
    info = wrapper.stat()
    fixture.require(
        info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o700
        and wrapper.read_text() == fixture.native_wrapper_source()
        and private_host_service_json(root, "executables.json") == {"2": str(binary)},
        "host-service-selector",
    )
    # Same positive maintenance allowlist used by host catalog discovery, from
    # the existing isolated fixture context (including its temporary placement).
    env = box.maintenance_host_env()
    fixture.require(
        env.get("HOME") == str(root / "host-home")
        and all(
            env.get("XDG_" + n.upper() + "_HOME") == str(root / n)
            for n in ("config", "data", "state", "cache")
        )
        and env.get("PATH", "").startswith(str(root / "bin") + ":"),
        "host-service-environment",
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.set_inheritable(False)
        reservation.bind(("127.0.0.1", 0))
        address, port = reservation.getsockname()
        fixture.require(
            address == "127.0.0.1" and host_service_port_valid(port),
            "host-service-port",
        )
        try:
            box.run(
                [str(wrapper), "service", "set", "port", str(port)],
                env=env,
                cwd=root / "source-a",
                stdin=subprocess.DEVNULL,
                capture=True,
                private=True,
                timeout=15,
            )
        except (box.BoxError, OSError, subprocess.SubprocessError):
            raise fixture.FixtureError("host-service-setter") from None
        fixture.require(
            not fixture.no_symlinks(root / HOST_SERVICE_REGISTRATION).exists(),
            "host-service-registration",
        )
        fixture.write_json_new(
            root / HOST_SERVICE_MARKER,
            host_service_metadata(root, source, binary, port),
        )
        return validate_host_service_port(root, source, binary)


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
    validate_host_service_port(root, source, binary)
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
    fixture.write_json_new(
        path, report_value(mode, source, outcome, category, code, state, secondary)
    )
    print("Interactive-only report: " + str(path), flush=True)


def print_commands(root):
    command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve())])
    print("Retained interactive-only root: " + str(root), flush=True)
    print("Wait for the owning TUI to be ready; in other HOST terminals:", flush=True)
    for mode in MODES[1:]:
        print(
            "  " + command + " " + mode + " --fixture " + shlex.quote(str(root)),
            flush=True,
        )
    print(
        "Use Build only, with one or two short synthetic prompts (12 provider requests total per owner window). "
        "Expect shell/MCP/permission-tool markers; no Plan/deny conversation support. "
        "Ctrl-] detaches the persistent shell; close attached clients before the owner. "
        "Manager needs optional textual/pyte dependencies; monitoring takes no client lease. "
        "Use explicit workspace Connect/terminal actions for leases; host sessions use only this fixture's private store. "
        "Its host catalog port is reserved only during setup; later bind collisions fail without port retry or default fallback. "
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
    outcome, category, code = "failed", "preparation", 1
    primary_failure = False
    secondary = []

    def failure(error):
        nonlocal outcome, category, code, primary_failure
        failed = error_outcome(error)
        if primary_failure:
            secondary.append(failed[1])
        else:
            outcome, category, code = failed
            primary_failure = True

    try:
        with fixture.private_output():
            fixture.prepare_root(root, args.distro or "tumbleweed")
            fixture.prepare_podman_cli(root, podman)
        with patch.dict(os.environ, fixture.isolated_env(root), clear=True):
            with contextlib.ExitStack() as stack:
                # Enter temporary placement privately, but NEVER redirect the UI.
                with fixture.private_output():
                    stack.enter_context(fixture.temporary_location(root, args.temp_dir))
                    fixture.validate_store(root)
                    fixture.prepare_native_cli(root, binary)
                    write_metadata(root, source, binary)
                    prepare_host_service_port(root, source, binary)
                    port = stack.enter_context(provider(state))
                    fixture.write_json_new(
                        root / "config/opencode/opencode.json",
                        fixture.fixture_config(port, root),
                    )
                    fixture.write_plugin(root)
                try:
                    with patch.dict(os.environ, state["values"]):
                        # The real main-thread run_tui path owns the provider lifetime.
                        code = box.main(
                            ["start", "--project", str(root / "source-a"), "--no-vault"]
                        )
                    outcome, category = (
                        ("returned", "launcher-return")
                        if code == 0
                        else ("failed", "launcher-exit")
                    )
                    primary_failure = code != 0
                except BaseException as error:
                    failure(error)
                # Preserve the first UI failure if journal/shutdown also fails.
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
                        failure(error)
                if state["errors"]:
                    failure(fixture.FixtureError("provider-contract"))
        # The real UI and owner lease drain, provider shutdown and resource
        # journal have finished. The report retains the original identity.
        if not primary_failure:
            require_same_source(source)
    except BaseException as error:
        failure(error)
    finally:
        save_report(root, "start", source, outcome, category, code, state, secondary)
    print(
        "interactive fixture: outcome="
        + outcome
        + " category="
        + category
        + "; manual observations NOT automatically passed",
        flush=True,
    )
    return code


def attach(args, source):
    # All checks before engine access are file-only. In-flight owners may not
    # have populated fixture-ids.json yet; record only after full resource proof.
    with fixture.private_output():
        root, binary = validate_interactive(args.fixture, source)
        fixture.require_runtime_boot(root)
    outcome, category, code = "failed", "validation", 1
    try:
        with patch.dict(os.environ, fixture.isolated_env(root), clear=True):
            with fixture.temporary_location(root):
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
                # The dashboard monitors without an outer client lease. Its
                # explicit workspace Connect/terminal actions take their own.
                # Native clients use authenticated attach, NEVER owner-capable
                # box.main or fallback startup/unlock after a service failure.
                client = (
                    contextlib.nullcontext()
                    if args.mode == "manager"
                    else fixture.workspace_service.attach(box, path, data)
                )
                with client as connection:
                    if args.mode == "manager":
                        try:
                            from oc_box import tui
                        except ImportError:
                            raise fixture.FixtureError("manager-unavailable") from None
                        code = tui.cli(box, ["--workspace", data["id"]])
                    elif args.mode == "tui":
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
                    outcome, category = (
                        ("returned", "client-return")
                        if code == 0
                        else ("failed", "client-exit")
                    )
        if code == 0:
            require_same_source(source)
    except BaseException as error:
        outcome, category, code = error_outcome(error)
    finally:
        save_report(root, args.mode, source, outcome, category, code)
    print(
        "interactive fixture: outcome="
        + outcome
        + " category="
        + category
        + "; manual observations NOT automatically passed",
        flush=True,
    )
    return code


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
