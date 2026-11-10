#!/usr/bin/env python3
"""Bounded, opt-in HOST-only manual TUI/web/persistent-shell observation.

Start a FRESH synthetic owner on an unprivileged host terminal:
  python3 -I -B tools/test_interactive_podman.py start --work-dir /path/to/fixtures \
      --opencode-v2 /path/to/unmodified/opencode-2.2.6 --expect-commit FULL_COMMIT
Use only the printed exact --fixture root for secondary tui/terminal/web/stop.
No replay, resume, cleanup, real credentials or automatic manual-UI acceptance.
Acceptance entrypoints and externally authenticated bootstrap loaders require
an isolated (-I) interpreter BEFORE executing this file. Ordinary library imports
are permitted for unit tests, but cannot initialize host acceptance without -I.
"""

import sys

# sys is built in and already initialized by Python. Refuse an unsafe entry
# BEFORE even an apparent stdlib import: a sibling argparse.py must not execute.
# The canonical library name remains importable by normal offline unit tests;
# load_source separately refuses acceptance initialization without isolation.
if not sys.flags.isolated and __name__ != "tools.test_interactive_podman":
    sys.stderr.write(
        "interactive fixture: category=isolated-python-required; use python3 -I -B\n"
    )
    raise SystemExit(1)

import argparse
import contextlib
import hashlib
import importlib
from importlib.machinery import ModuleSpec
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
from unittest.mock import patch
from types import ModuleType

APP = Path(__file__).resolve().parents[1]
# No candidate imports, path installation or integration initialization here.
# This bootstrap deliberately cannot use a candidate guard to prove its trust.
fixture = box = terminal_ui = guards = None
INITIAL_SOURCE = None
FORMAT = "oc-box-interactive-only-host-observation"
MODES = ("start", "tui", "terminal", "web", "stop")
DONORS = (
    "tools/test_interactive_podman.py",
    "tools/test_global_vault_podman.py",
    "tools/test_v2_podman.py",
    "tools/acceptance_guards.py",
)
FIXTURE_PREFIXES = ("oc-box-gv-host-", "oc-box-live-v2-", "oc-box-live-backup-")
FIXTURE_MARKERS = (
    "fixture.json",
    "interactive.json",
    "fixture-ids.json",
    "edges.json",
    "history-purpose.json",
    "home-purpose.json",
    "home-component.json",
    "home-ready.json",
    "home-created-*.json",
    "home-run-*-report.json",
    "home-cleanup-*-report.json",
    "result.json",
    "history-result.json",
)
EGG_INFO = frozenset(
    (
        b"PKG-INFO",
        b"SOURCES.txt",
        b"dependency_links.txt",
        b"entry_points.txt",
        b"requires.txt",
        b"top_level.txt",
    )
)
ERROR_CATEGORIES = frozenset(
    {
        "host-only",
        "isolated-python-required",
        "owner-options",
        "client-options",
        "interactive-tty-required",
        "clean-source-required",
        "source-changed",
        "source-shadowing",
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


class BootstrapError(Exception):
    """Fixed stdlib-preflight category, independent of candidate exceptions."""


def require(value, category):
    if not value:
        raise BootstrapError(category)


def host_guard():
    require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "host-only",
    )


def checked_source_path(path):
    path = Path(path).expanduser().absolute()
    require(
        ".." not in path.parts and not any(c in str(path) for c in "\0\r\n"),
        "clean-source-required",
    )
    for ancestor in (*reversed(path.parents), path):
        mode = ancestor.lstat().st_mode
        require(not stat.S_ISLNK(mode), "clean-source-required")
        if ancestor != path:
            require(stat.S_ISDIR(mode), "clean-source-required")
    return path


def git(source, *arguments):
    executable = shutil.which("git", path=os.defpath)
    require(executable is not None, "clean-source-required")
    return subprocess.run(
        [executable, "-C", str(source), *arguments],
        env={
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


def source_snapshot(source, expected):
    """Full clean self-hosted checkout and tracked bytes, BEFORE helper imports."""
    source = checked_source_path(source)
    require(
        source == APP
        and source.is_dir()
        and re.fullmatch(r"[0-9a-f]{40}", expected or ""),
        "clean-source-required",
    )
    require(
        git(source, "rev-parse", "--show-toplevel").decode().strip() == str(source)
        and git(source, "rev-parse", "HEAD").decode().strip() == expected
        and git(source, "rev-parse", "--is-shallow-repository").strip() == b"false"
        and not git(source, "status", "--porcelain", "--untracked-files=all"),
        "clean-source-required",
    )
    count = int(git(source, "rev-list", "--count", "HEAD"))
    require(count > 29, "clean-source-required")
    ignored = git(
        source,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    require(
        all(
            (name.endswith(b".pyc") and b"/__pycache__/" in b"/" + name)
            or (
                name.startswith(b"src/oc_box.egg-info/")
                and name.rsplit(b"/", 1)[-1] in EGG_INFO
            )
            for name in ignored
            if name
            and (
                name.startswith((b"src/", b"tools/"))
                or (
                    b"/" not in name
                    and name.endswith((b".py", b".pyc", b".pyo", b".so"))
                )
            )
        ),
        "clean-source-required",
    )
    hashes = {}
    # Bind immutable expected-commit blobs, not an index which could change
    # after the clean-status observation and before candidate imports.
    for record in filter(
        None, git(source, "ls-tree", "-r", "-z", "--full-tree", expected).split(b"\0")
    ):
        header, name = record.split(b"\t", 1)
        mode, kind, blob = header.split()
        require(
            mode in (b"100644", b"100755") and kind == b"blob", "clean-source-required"
        )
        relative = name.decode()
        path = checked_source_path(source / relative)
        require(path.is_relative_to(source) and path.is_file(), "clean-source-required")
        content = path.read_bytes()
        require(
            hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content)
            .hexdigest()
            .encode()
            == blob,
            "source-changed",
        )
        hashes[relative] = hashlib.sha256(content).hexdigest()
    require(
        set(DONORS) <= hashes.keys() and "src/oc_box/terminal_ui.py" in hashes,
        "clean-source-required",
    )
    return {
        "checkout": str(source),
        "commit": expected,
        "commit_count": count,
        "version": f"1.6.{count - 29}+g{expected[:7]}",
        "dirty": False,
        "sha256": hashes,
    }


def import_guard(source, tracked):
    """Require loaded candidate origins AND their actual bytes to be tracked."""
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name in ("oc_box", "tools") or name.startswith(("oc_box.", "tools.")):
            locations = (
                [filename] if filename else list(getattr(module, "__path__", ()))
            )
            require(
                locations
                and all(
                    Path(item).resolve().is_relative_to(source) for item in locations
                ),
                "source-shadowing",
            )
        if filename:
            path = Path(filename).absolute()
            if path.resolve().is_relative_to(source):
                checked_source_path(path)
                relative = path.relative_to(source).as_posix()
                require(
                    relative in tracked and file_hash(path) == tracked[relative],
                    "source-shadowing",
                )


def isolate_bytecode():
    # -B prevents WRITES only. A never-used private cache prefix prevents READS
    # of matching-mtime/size poisoned pycs, including checked-hash pycs.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = "/nonexistent-oc-box-bytecode-" + secrets.token_hex(16)
    importlib.invalidate_caches()


def load_source(source, expected):
    global fixture, box, terminal_ui, guards, INITIAL_SOURCE
    require(sys.flags.isolated, "isolated-python-required")
    initial = source_snapshot(source, expected)
    source = checked_source_path(source)
    import_guard(source, initial["sha256"])
    # Same-path modules may already have executed stale bytecode; __file__ and
    # current source hashes do not attest an existing in-memory implementation.
    # Load this entrypoint before candidate helpers/application modules.
    require(
        not any(
            name == "oc_box"
            or name.startswith("oc_box.")
            or (name.startswith("tools.") and name != __name__)
            for name in sys.modules
        ),
        "source-shadowing",
    )
    isolate_bytecode()
    # tools is a namespace in this checkout. Normal resolution can execute an
    # ignored root tools.py or a later foreign regular package BEFORE any
    # post-import origin check. Install only its verified namespace, not the
    # repository root as an ambient import path. No candidate code executes here.
    tools_path = checked_source_path(source / "tools")
    require(
        tools_path.is_dir() and "tools/__init__.py" not in initial["sha256"],
        "source-shadowing",
    )
    tools = ModuleType("tools")
    tools.__package__ = "tools"
    tools.__path__ = [str(tools_path)]
    tools.__spec__ = ModuleSpec("tools", loader=None, is_package=True)
    tools.__spec__.submodule_search_locations = tools.__path__
    sys.modules["tools"] = tools
    candidate_paths = {str(source), str(tools_path), str(source / "src")}
    sys.path[:] = [
        str(source / "src"),
        *(entry for entry in sys.path if entry not in candidate_paths),
    ]
    guards = importlib.import_module("tools.acceptance_guards")
    terminal_ui = importlib.import_module("oc_box.terminal_ui")
    fixture = importlib.import_module("tools.test_global_vault_podman")
    box = fixture.box
    import_guard(source, initial["sha256"])
    require(box.PACKAGE.resolve() == source / "src/oc_box", "source-shadowing")
    require(source_snapshot(source, expected) == initial, "source-changed")
    INITIAL_SOURCE = initial
    return initial


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "interactive fixture: category=invalid-options\n")


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument("mode", choices=MODES)
    value.add_argument(
        "--source",
        type=Path,
        default=APP,
        help="This entrypoint's checkout only (default: its checkout)",
    )
    value.add_argument(
        "--expect-commit", help="Full expected clean commit; required for a fresh owner"
    )
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
    """Re-read against the ORIGINAL verified commit, never repin changed bytes."""
    expected = (
        INITIAL_SOURCE["commit"]
        if INITIAL_SOURCE
        else git(APP, "rev-parse", "HEAD").decode().strip()
    )
    info = source_snapshot(APP, expected)
    fixture.require(
        info.get("dirty") is False
        and isinstance(info.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", info["commit"]) is not None
        and isinstance(info.get("commit_count"), int)
        and info["commit_count"] > 29
        and isinstance(info.get("version"), str)
        and info["version"].endswith("+g" + info["commit"][:7]),
        "clean-source-required",
    )
    return info


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
    except (
        BootstrapError,
        fixture.FixtureError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ):
        unchanged = False
    fixture.require(unchanged, "source-changed")


def native_binary(path):
    # Discovery may resolve a final npm-entry symlink, never a symlink ancestor.
    return guarded(
        guards.native_identity, path, outside=(APP,), resolve_entry=True
    ).path


def guarded(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except guards.GuardError as error:
        raise fixture.FixtureError(error.code) from None


def fresh_parent(path):
    return guarded(
        guards.fresh_parent, path, prefixes=FIXTURE_PREFIXES, markers=FIXTURE_MARKERS
    )


def native_identities(binary, podman):
    return {
        name: guarded(guards.native_identity, path, outside=(APP,), resolve_entry=True)
        for name, path in (
            ("opencode", binary),
            ("podman", podman),
            ("python", sys.executable),
        )
    }


def serialized_natives(natives):
    return {
        name: {"path": str(value.path), "sha256": value.sha256, "bytes": value.bytes}
        for name, value in natives.items()
    }


def recorded_natives(root):
    metadata = fixture.private_json(root, "interactive.json")
    values = metadata.get("executables")
    fixture.require(
        isinstance(values, dict) and set(values) == {"opencode", "podman", "python"},
        "native-selector",
    )
    result = {}
    for name, value in values.items():
        fixture.require(
            isinstance(value, dict)
            and set(value) == {"path", "sha256", "bytes"}
            and isinstance(value["path"], str)
            and Path(value["path"]).is_absolute()
            and isinstance(value["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            and type(value["bytes"]) is int
            and value["bytes"] > 0,
            "native-selector",
        )
        result[name] = guards.NativeIdentity(
            Path(value["path"]), value["sha256"], value["bytes"]
        )
        fixture.require(not result[name].path.is_relative_to(root), "native-selector")
    return result


def provenance_checkpoint(source, natives):
    try:
        require(source_snapshot(APP, source["commit"]) == source, "source-changed")
        import_guard(APP, source["sha256"])
    except (BootstrapError, KeyError, OSError, ValueError, subprocess.SubprocessError):
        raise fixture.FixtureError("source-changed") from None
    for value in natives.values():
        guarded(guards.check_native, value)
    fixture.require(
        Path(sys.executable).resolve(strict=True) == natives["python"].path,
        "native-changed",
    )


def write_metadata(root, source, binary, natives=None):
    if natives is None:
        selector = fixture.private_json(root, "podman-executable.json")
        natives = native_identities(binary, Path(selector["podman"]))
    fixture.write_json_new(
        root / "interactive.json",
        {
            "format": FORMAT,
            "version": 2,
            "projects": ["source-a"],
            "source": source,
            "native": {
                "version": fixture.NATIVE_VERSION,
                "path": str(binary),
                "sha256": natives["opencode"].sha256,
                "interpreter": sys.executable,
            },
            "executables": serialized_natives(natives),
        },
    )


def validate_interactive(root, source):
    """File-only validation, including the full helper's selector/registry guards."""
    fixture.require(root.is_absolute(), "fixture-root")
    root = fixture.validate_fixture(root, allow_unrecorded=True)
    metadata = fixture.private_json(root, "interactive.json")
    fixture.require(
        isinstance(metadata, dict)
        and set(metadata)
        == {"format", "version", "projects", "source", "native", "executables"}
        and metadata["format"] == FORMAT
        and metadata["version"] == 2
        and metadata["projects"] == ["source-a"],
        "interactive-format",
    )
    fixture.require(metadata["source"] == source, "source-changed")
    natives = recorded_natives(root)
    provenance_checkpoint(source, natives)
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
    binary = natives["opencode"].path
    fixture.require(
        Path(native["path"]) == binary
        and native["sha256"] == natives["opencode"].sha256,
        "native-changed",
    )
    fixture.require(
        fixture.private_json(root, "executables.json") == {"2": str(binary)},
        "native-selector",
    )
    fixture.require(
        fixture.private_json(root, "podman-executable.json")
        == {
            "podman": str(natives["podman"].path),
            "interpreter": str(natives["python"].path),
        }
        and Path(native["interpreter"]).resolve(strict=True) == natives["python"].path,
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
    primary = None
    try:
        yield server.server_port
    except BaseException as error:
        primary = error
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

        closer = None
        try:
            closer = threading.Thread(target=shutdown, daemon=True)
            closer.start()
            closer.join(timeout=3)
        except BaseException as error:
            errors.append(error)
        try:
            # Daemon request handlers have 10s socket limits; this does not join
            # those handlers. Try every mandatory finalizer independently.
            server.server_close()
        except BaseException as error:
            errors.append(error)
        try:
            worker.join(timeout=3)
        except BaseException as error:
            errors.append(error)
        try:
            fixture.require(
                (closer is None or not closer.is_alive()) and not worker.is_alive(),
                "provider-cleanup-timeout",
            )
        except BaseException as error:
            errors.append(error)
        if errors:
            if primary is None:
                primary = errors.pop(0)
            state.setdefault("finalization_errors", []).extend(
                error_outcome(error)[1] for error in errors
            )
    if primary is not None:
        raise primary


def report_value(
    mode, source, outcome, category, code, state=None, secondary=(), natives=None
):
    result = {
        "format": FORMAT,
        "version": 2,
        "scope": "interactive-only",
        "mode": mode,
        "source": source,
        "outcome": outcome,
        "category": category,
        "exit_code": code,
        "manual_observations": "NOT_AUTOMATICALLY_PASSED",
        "secondary_errors": list(secondary),
    }
    if natives is not None:
        result["executables"] = serialized_natives(natives)
    if state is not None:
        result["provider"] = {
            "requests": state["requests"],
            "errors": state["errors"],
            "markers": sorted(
                state["markers"]
                & {
                    fixture.SHELL_MARKER,
                    fixture.MCP_MARKER,
                    fixture.TOOL_MARKER,
                }
            ),
        }
    return result


def save_report(
    root, mode, source, outcome, category, code, state=None, secondary=(), natives=None
):
    path = root / ("interactive-" + mode + "-" + secrets.token_hex(8) + "-report.json")
    # Announcing a destination is not success evidence. Do it before the final
    # write, so an output failure cannot leave a successful terminal report.
    print("Interactive-only report: " + str(path), flush=True)
    fixture.write_json_new(
        path,
        report_value(mode, source, outcome, category, code, state, secondary, natives),
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

    def report(self, root, mode, source, state=None, natives=None):
        try:
            print(
                "interactive fixture: finalizing category="
                + self.result[1]
                + "; manual observations NOT automatically passed",
                flush=True,
            )
        except BaseException as error:
            self.record(error, category="reporting-error")
        try:
            save_report(
                root, mode, source, *self.result, state, self.secondary, natives
            )
        except BaseException as error:
            self.record(error, category="reporting-error")
            # Persistence may be unavailable. Only fixed categories escape;
            # never retry into the original evidence or print private errors.
            try:
                print("interactive fixture: category=reporting-error", file=sys.stderr)
            except BaseException:
                pass  # The failure is retained even if output is unavailable.
        # A persisted terminal report is last: no mandatory output/cleanup work
        # remains that could invalidate successful terminal evidence.
        return self.result[2]


def print_commands(root):
    command = shlex.join([sys.executable, "-I", "-B", str(Path(__file__).resolve())])
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
    natives = native_identities(binary, Path(discovered))
    podman = natives["podman"].path
    provenance_checkpoint(source, natives)
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
                        provenance_checkpoint(source, natives)
                        fixture.validate_store(root)
                        provenance_checkpoint(source, natives)
                        fixture.prepare_native_cli(root, binary)
                        provenance_checkpoint(source, natives)
                        write_metadata(root, source, binary, natives)
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
                        provenance_checkpoint(source, natives)
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
                            provenance_checkpoint(source, natives)
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
        # The real UI and owner lease drain, provider shutdown and resource
        # journal have finished. The report retains the original identity.
    except BaseException as error:
        failures.record(error)
    if state["errors"]:
        failures.record(fixture.FixtureError("provider-contract"))
    for category in state.get("finalization_errors", ()):
        failures.record(fixture.FixtureError(category), category=category)
    try:
        provenance_checkpoint(source, natives)
        if failures.primary is None:
            require_same_source(source)
    except BaseException as error:
        failures.record(error)
    return failures.report(root, "start", source, state, natives)


def attach(args, source):
    # All checks before engine access are file-only. In-flight owners may not
    # have populated fixture-ids.json yet; record only after full resource proof.
    with fixture.private_output():
        root, binary = validate_interactive(args.fixture, source)
        natives = recorded_natives(root)
        fixture.require_runtime_boot(root)
    failures = Failures()
    try:
        with patch.dict(os.environ, fixture.isolated_env(root), clear=True):
            with contextlib.ExitStack() as stack, failures.capture():
                failures.enter(stack, fixture.temporary_location(root))
                with fixture.private_output():
                    provenance_checkpoint(source, natives)
                    fixture.validate_store(root, allow_unrecorded=True)
                    provenance_checkpoint(source, natives)
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
                    provenance_checkpoint(source, natives)
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
                        provenance_checkpoint(source, natives)
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
    except BaseException as error:
        failures.record(error)
    try:
        provenance_checkpoint(source, natives)
        if failures.primary is None:
            require_same_source(source)
    except BaseException as error:
        failures.record(error)
    return failures.report(root, args.mode, source, natives=natives)


def error_outcome(error):
    if isinstance(error, KeyboardInterrupt):
        return "interrupted", "interrupted", 130
    # Exception messages, subprocess argv/output and control credentials are
    # deliberately never serialized, even when raised during preparation.
    category = (
        str(error)
        if (
            isinstance(error, BootstrapError)
            or (fixture is not None and isinstance(error, fixture.FixtureError))
        )
        and str(error) in ERROR_CATEGORIES
        else fixture.safe_error_class(error)
        if fixture is not None
        else (
            "os-error"
            if isinstance(error, OSError)
            else "subprocess-exit"
            if isinstance(error, subprocess.CalledProcessError)
            else "subprocess-error"
            if isinstance(error, subprocess.SubprocessError)
            else "operation-failed"
        )
    )
    return "failed", category, 1


def main(argv=None):
    args = parser().parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        host_guard()  # stdlib-only, BEFORE imports, Git/native probes or resources.
        require_tty_early = args.mode != "stop"
        if require_tty_early:
            require(
                sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty(),
                "interactive-tty-required",
            )
        expected = args.expect_commit
        if args.mode != "start" and expected is None:
            expected = git(APP, "rev-parse", "HEAD").decode().strip()
        source = load_source(args.source, expected)
        validate_options(args)
        require_tty(args.mode)
        fixture.require(
            args.distro is None or args.distro in box.DISTROS, "owner-options"
        )
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
