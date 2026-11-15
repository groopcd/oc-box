#!/usr/bin/env python3
"""Fresh-only HOST lifecycle adapter for the frozen 1.6.94 application.

Invoke this independent file with python3 -I -B, --source and --expect-commit.
Only the first validated source-workspace start gains --import-session. A fresh
stopped-home comparison accounts for the frozen archive's metadata policy,
without normalizing restored data or weakening exact source preservation. The
unchanged candidate runs all ten lifecycle checks and retains its own report.
The external receipt is atomically linked from closed private staging retained
as non-authoritative evidence; its two links stay inside the fresh private root.
There is no retry, retained-store inspection, cleanup, V1 or interactive gate.
Trusted interpreter/site-packages/Git and no concurrent writers are required.
Offline callback tests are NOT native or rootless Podman acceptance.
"""

import sys
import os

if __name__ == "__main__":
    if not sys.flags.isolated:
        print("External lifecycle refused: isolated-python-required.", file=sys.stderr)
        raise SystemExit(1)
    sys.dont_write_bytecode = True

import argparse
import contextlib
import hashlib
import importlib
import importlib.machinery
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import types
from unittest.mock import patch

COMMIT = "9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22"
NATIVE_SHA256 = "2e0e4656521ca56e5ba16a00d48429958b8105b3fa6466710a4350e68759381f"
FROZEN = {
    "tools/test_v2_lifecycle_podman.py": "2eb9d48889332b7a88d27884f1b2fad9e13e30238434b04379edbcc770cef010",
    "tools/test_global_vault_podman.py": "e7d665369b7e1563a633a838b0d8c36a67b89749d490d1c381f37cc2ea9550d8",
    "tools/test_v2_podman.py": "c54e74fe6307235bfa8886967a7002b45876152d6eee9522c557c73ffe3cdb3c",
    "tools/native_host_profile.py": "4420fb228af9db497bbac004189787172119a15764df6d2b718aa34593615221",
    "tools/test_session_sync_podman.py": "96ae30ae0b6e9b726443c507ca17e128bb9ef91bb1dab8a751ceaa06f8516087",
}
CHECKS = (
    "private-store-native-selector",
    "native-v2-host-seed",
    "native-v2-host-import",
    "first-owner-stopped-selected-tools",
    "same-session-restart-rotation-attach",
    "full-encrypted-restore",
    "portable-encrypted-restore",
    "wrong-password-before-deletion",
    "reset-project-home-history-os",
    "source-preserved",
)
EXCLUDED = (
    "legacy-v1-migration",
    "legacy-v1-downgrade-reopen",
    "global-vault-full-path-separate-fixture-required",
    "interactive-tui-web",
    "real-provider-inference",
    "oauth-refresh",
    "network-containment",
    "security-certification",
)
SEQUENCE = (
    ("start", "source-a"),
    ("start", "source-a"),
    ("start", "restored-full"),
    ("start", "restored-portable"),
    ("reset", "source-a"),
)
REPORT = "external-lifecycle-result.json"
REPORT_STAGE_PREFIX = ".external-lifecycle-stage-"
FORMAT = "oc-box-external-v2-lifecycle-v1"
# Exactly the donor's six snapshot sites, before reset. No diagnostic/retry API.
HOME_SITES = (
    ("source-a", 2),
    ("restored-full", 2),
    ("source-a", 3),
    ("restored-portable", 3),
    ("source-a", 4),
    ("source-a", 4),
)
# One aggregate-only read, with the donor's existing 30-second tool bound.
# Its exact digest uses the identical entry schema/serialization as the donor.
# Only the SOURCE's expected digest changes root mode and nonsymlink special
# bits. Actual restored entries are always compared through their exact digest.
HOME_PROGRAM = r"""
import hashlib,json,os,pathlib,stat,sys
root=sys.argv[1]
source_policy=sys.argv[2]=='source'
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
def checksum(name,parent,info):
    child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
    with os.fdopen(child,'rb') as stream:
        before=os.fstat(stream.fileno())
        if (before.st_dev,before.st_ino,before.st_mode,before.st_size)!=(info.st_dev,info.st_ino,info.st_mode,info.st_size):
            raise ValueError('changed-home')
        value=hashlib.sha256()
        for block in iter(lambda:stream.read(1024*1024),b''): value.update(block)
        after=os.fstat(stream.fileno())
        if (after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(before.st_size,before.st_mtime_ns,before.st_ctime_ns):
            raise ValueError('changed-home')
        return value.hexdigest()
def digest(entries):
    return hashlib.sha256(json.dumps(sorted(entries),ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
def fail(error): raise error
try:
    info=os.fstat(fd)
    entries=[['.',stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid]]
    expected=[entries[0][:]] if source_policy else None
    if source_policy: expected[0][2]=0o700
    special=0
    for parent,dirs,files,parent_fd in os.fwalk('.',dir_fd=fd,follow_symlinks=False,onerror=fail):
        for name in sorted(dirs+files):
            info=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
            entry=[str(pathlib.Path(parent,name)),stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid]
            if stat.S_ISREG(info.st_mode): entry.append(checksum(name,parent_fd,info))
            elif stat.S_ISLNK(info.st_mode): entry.append(os.readlink(name,dir_fd=parent_fd))
            elif not stat.S_ISDIR(info.st_mode): raise ValueError('unsupported-home-entry')
            entries.append(entry)
            if not stat.S_ISLNK(info.st_mode):
                special+=bool(entry[2]&0o7000)
            if source_policy:
                normalized=entry[:]
                if not stat.S_ISLNK(info.st_mode): normalized[2]&=0o777
                expected.append(normalized)
    print(json.dumps(dict(exact=digest(entries),expected=digest(expected) if source_policy else None,
                         root_mode=entries[0][2],root_owner_expected=entries[0][3:5]==[0,0],
                         child_special_bits=special)))
finally:
    os.close(fd)
"""
# All historical host families share these prefixes/markers. Check ancestors
# only: an ordinary common parent containing sibling fixtures remains usable.
FIXTURE_PREFIXES = (
    "oc-box-v2-only-host-",
    "oc-box-gv-host-",
    "oc-box-live-v2-",
    "oc-box-live-backup-",
)
FIXTURE_MARKERS = (
    "fixture.json",
    "fixture-ids.json",
    "interactive.json",
    "interactive-result.json",
    "edges.json",
    "edges-result.json",
    "history-purpose.json",
    "history-result.json",
    "home-component.json",
    "home-ready.json",
    "home-created-source.json",
    "home-created-destination.json",
    REPORT,
)
# Only exact literal public categories may leave the exception boundary.
CATEGORIES = frozenset(
    {
        "host-only",
        "source-path",
        "source-git",
        "clean-source-required",
        "source-entry",
        "source-changed",
        "source-incomplete",
        "ignored-source",
        "source-shadowing",
        "source-pin",
        "source-placement",
        "runner-changed",
        "native-executable",
        "native-profile",
        "native-changed",
        "placement",
        "fresh-root",
        "fresh-parent-required",
        "baseline-contract",
        "launch-contract",
        "import-once",
        "report-contract",
        "original-report",
        "provider-worker-alive",
        "provider-cleanup-contract",
        "workspace-state",
        "resource-allowlist",
        "private-volume",
        "published-ports",
        "resource-mounts",
        "workspace-version",
        "client-autoaccept",
        "selected-session",
        "private-owner",
        "first-import",
        "native-tools",
        "native-history",
        "attach-session",
        "owning-unlock-count",
        "stopped-session",
        "host-vault-retention",
        "unsafe-path",
        "unsafe-metadata",
        "workspace-project",
        "workspace-allowlist",
        "workspace-policy",
        "workspace-marker",
        "fixture-ids",
        "stopped-restore",
        "restored-project",
        "source-preservation",
        "wrong-password-category",
        "wrong-password-accepted",
        "wrong-password-deletion",
        "wrong-password-preservation",
        "reset-container",
        "reset-project",
        "incomplete-checks",
        "native-executable-changed",
        "incomplete-report",
        "launcher-exit",
        "home-snapshot-contract",
        "home-dual-read",
        "home-root-mapping",
        "restore-home-policy",
        "source-home-preservation",
    }
)


class Refusal(Exception):
    """Fixed public category, never an arbitrary private error message."""


def require(condition, category):
    if not condition:
        raise Refusal(category)


def home_probe(value, *, source=True):
    """Validate private aggregate output; no private data in refusal messages."""
    try:
        require(type(value) is bytes and len(value) <= 1024, "home-snapshot-contract")

        def unique(pairs):
            result = {}
            for key, item in pairs:
                require(key not in result, "home-snapshot-contract")
                result[key] = item
            return result

        result = json.loads(value, object_pairs_hook=unique)
        require(
            type(result) is dict
            and result.keys()
            == {
                "exact",
                "expected",
                "root_mode",
                "root_owner_expected",
                "child_special_bits",
            }
            and all(
                type(result[key]) is str
                and re.fullmatch(r"[0-9a-f]{64}", result[key]) is not None
                for key in (("exact", "expected") if source else ("exact",))
            )
            and (source or result["expected"] is None)
            and type(result["root_mode"]) is int
            and 0 <= result["root_mode"] <= 0o7777
            and type(result["root_owner_expected"]) is bool
            and type(result["child_special_bits"]) is int
            and 0 <= result["child_special_bits"] <= 2_000_000,
            "home-snapshot-contract",
        )
        return result
    except (ValueError, TypeError, RecursionError):
        raise Refusal("home-snapshot-contract") from None


class HomeSnapshot(dict):
    """Keep the original dictionary; delegate only role-bound comparisons."""

    def __init__(self, adapter, original, probe, role, ordinal):
        super().__init__(original)
        self.adapter, self.original, self.probe = adapter, dict(original), probe
        self.role, self.ordinal = role, ordinal

    def __eq__(self, other):
        # Never return NotImplemented: plain-dict fallback could waive the probe.
        return self.adapter.compare_home(self, other)

    def __ne__(self, other):
        return not self == other


def host_guard():
    require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "host-only",
    )


def safe_path(path):
    path = Path(path).absolute()
    require(
        ".." not in path.parts and not any(c in str(path) for c in "\0\r\n"),
        "source-path",
    )
    for ancestor in (*reversed(path.parents), path):
        require(not stat.S_ISLNK(ancestor.lstat().st_mode), "source-path")
    return path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fresh_parent(path):
    """Refuse retained evidence by lstat only; never read marker contents."""
    parent = safe_path(Path(path).expanduser())
    require(stat.S_ISDIR(parent.lstat().st_mode), "placement")
    for ancestor in (parent, *parent.parents):
        require(not ancestor.name.startswith(FIXTURE_PREFIXES), "fresh-parent-required")
        for name in FIXTURE_MARKERS:
            try:
                (ancestor / name).lstat()
            except FileNotFoundError:
                continue
            require(False, "fresh-parent-required")
    return parent


def git(source, *arguments):
    binary = shutil.which("git", path=os.defpath)
    require(binary and Path(binary).is_absolute(), "source-git")
    return subprocess.run(
        [
            binary,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(source),
            *arguments,
        ],
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


def snapshot(source, expected):
    """Pre-import HEAD-blob binding, independent of the candidate's importer."""
    source = safe_path(source)
    require(expected == COMMIT, "source-pin")
    require(
        git(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        == expected,
        "source-pin",
    )
    require(
        git(source, "rev-parse", "--show-toplevel").decode().strip() == str(source)
        and git(source, "rev-parse", "--is-shallow-repository").strip() == b"false"
        and not git(source, "status", "--porcelain", "--untracked-files=all"),
        "clean-source-required",
    )
    metadata = {
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    }
    for name in filter(
        None,
        git(
            source, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
        ).split(b"\0"),
    ):
        relative = name.decode()
        require(
            (relative.endswith(".pyc") and "/__pycache__/" in "/" + relative)
            or (
                relative.startswith("src/oc_box.egg-info/")
                and relative.rsplit("/", 1)[-1] in metadata
            ),
            "ignored-source",
        )
        safe_path(source / relative)
    hashes = {}
    for record in filter(None, git(source, "ls-tree", "-r", "-z", "HEAD").split(b"\0")):
        header, name = record.split(b"\t", 1)
        mode, kind, blob = header.split()
        relative = name.decode()
        require(mode in (b"100644", b"100755") and kind == b"blob", "source-entry")
        path = safe_path(source / relative)
        actual = path.stat().st_mode
        require(
            stat.S_ISREG(actual) and bool(actual & 0o111) == (mode == b"100755"),
            "source-entry",
        )
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
        all(hashes.get(name) == value for name, value in FROZEN.items()),
        "source-incomplete",
    )
    return {
        "commit": expected,
        "commit_count": int(git(source, "rev-list", "--count", "HEAD")),
        "sha256": hashes,
    }


def import_guard(source, identity, *, before=False):
    for name, module in tuple(sys.modules.items()):
        protected = (
            name == "tools"
            or name.startswith("tools.")
            or (name == "oc_box" or name.startswith("oc_box."))
        )
        require(not (before and protected), "source-shadowing")
        filename = getattr(module, "__file__", None)
        if protected:
            expected = source / (
                "tools" if name.split(".")[0] == "tools" else "src/oc_box"
            )
            locations = (
                [filename] if filename else list(getattr(module, "__path__", ()))
            )
            require(
                locations
                and all(
                    Path(item).resolve().is_relative_to(expected) for item in locations
                ),
                "source-shadowing",
            )
        if filename:
            resolved = Path(filename).resolve()
            if resolved.is_relative_to(source):
                require(
                    resolved.relative_to(source).as_posix() in identity["sha256"],
                    "source-shadowing",
                )


def load_candidate(source, identity, scratch):
    """Call only after all pre-import binding/placement checks; no checker bypass."""
    scratch = fresh_parent(scratch)
    require(not scratch.is_relative_to(source), "source-placement")
    import_guard(source, identity, before=True)
    # A new prefix prevents even timestamp-valid ignored bytecode being imported.
    cache = tempfile.TemporaryDirectory(
        prefix="oc-box-external-lifecycle-bytecode-",
        dir=scratch,
    )
    sys.dont_write_bytecode = True
    sys.pycache_prefix = cache.name
    namespace = types.ModuleType("tools")
    namespace.__path__ = [str(source / "tools")]
    namespace.__package__ = "tools"
    namespace.__spec__ = importlib.machinery.ModuleSpec("tools", None, is_package=True)
    sys.modules["tools"] = namespace
    sys.path[:0] = [str(source / "src"), str(source)]
    # Ignore both cached tempfile.tempdir and ambient TMPDIR for the original
    # checker's own allocation and any default scratch use while importing.
    with patch.object(tempfile, "tempdir", str(scratch)):
        fixture = importlib.import_module("tools.test_v2_lifecycle_podman")
        require(fixture.APP == source, "source-shadowing")
        # The original CLI checker remains authoritative, with its real snapshot.
        fixture._CLI_SOURCE = fixture._prepare_cli_imports(source)
    require(fixture._CLI_SOURCE == identity, "source-changed")
    import_guard(source, identity)
    require(snapshot(source, COMMIT) == identity, "source-changed")
    require(
        tuple(fixture.CHECKS) == CHECKS and tuple(fixture.EXCLUDED) == EXCLUDED,
        "report-contract",
    )
    return fixture, cache


def diagnostic(error, stage, fixture=None):
    label = "exception"
    kinds = [
        (Refusal, "refusal"),
        (subprocess.TimeoutExpired, "timeout"),
        (subprocess.CalledProcessError, "subprocess-exit"),
        (AssertionError, "assertion"),
        (OSError, "os-error"),
        ((ValueError, KeyError, TypeError), "invalid-data"),
        ((KeyboardInterrupt, SystemExit), "interrupted"),
    ]
    if fixture is not None:
        kinds[:0] = [
            (fixture.safety.FixtureError, "fixture-error"),
            (fixture.box.BoxError, "box-error"),
        ]
    for kind, value in kinds:
        if isinstance(error, kind):
            label = value
            break
    fixed = "other-fixed"
    public = isinstance(error, Refusal) or (
        fixture is not None and isinstance(error, fixture.safety.FixtureError)
    )
    if public and len(error.args) == 1 and type(error.args[0]) is str:
        if error.args[0] in CATEGORIES:
            fixed = error.args[0]
    code = getattr(error, "returncode", None)
    code = code if type(code) is int and -255 <= code <= 255 else None
    return {
        "substep": stage,
        "error_class": label,
        "category": fixed,
        "exit_code": code,
        "exit_zero": None if code is None else code == 0,
    }


class Adapter:
    """Forward donor operations, binding first import and stopped-home policy."""

    def __init__(self, fixture, parent):
        self.f = fixture
        self.parent = parent
        self.root = None
        self.baseline = None
        self.report = None
        self.primary = None
        self.cleanup_errors = []
        self.reporting_errors = []
        self.stage = "fixture"
        self.last_exit = None
        self.in_cleanup = False
        self.seen = []
        self.ledger = []
        self.imports = 0
        self.provider = False
        self.cleanup = {"shutdown": 0, "close": 0, "join": 0, "worker_alive": None}
        self.home_snapshots = []
        self.home_comparisons = {}
        self.source_preservations = []
        self.home_preserved_sites = set()

    def fail(self, error, stage=None, *, secondary=None):
        if any(previous is error for previous in self.seen):
            return
        self.seen.append(error)
        if secondary is None and self.in_cleanup:
            secondary = "cleanup"
        item = diagnostic(error, stage or self.stage, self.f)
        if item["exit_code"] is None and self.last_exit is not None:
            observed_stage, code = self.last_exit
            if observed_stage == item["substep"]:
                item.update(exit_code=code, exit_zero=code == 0)
        if secondary == "report":
            self.reporting_errors.append(item)
        elif secondary == "cleanup":
            self.cleanup_errors.append(item)
        elif self.primary is None:
            self.primary = item

    @contextlib.contextmanager
    def checkpoint(self, stage, *, cleanup=False):
        if cleanup:
            self.capture_unwinding()
        previous, self.stage = self.stage, stage
        previous_exit, self.last_exit = self.last_exit, None
        previous_cleanup, self.in_cleanup = self.in_cleanup, cleanup or self.in_cleanup
        try:
            yield
        except BaseException as error:
            self.fail(error, secondary="cleanup" if cleanup else None)
            raise
        finally:
            self.stage = previous
            self.last_exit = previous_exit
            self.in_cleanup = previous_cleanup

    def capture_unwinding(self):
        # __enter__ startup handlers and production finally blocks can invoke
        # cleanup before their original exception reaches a forwarding wrapper.
        # Do not promote an already seen cleanup/report exception to primary.
        active = sys.exc_info()[1]
        if active is not None and self.primary is None and not self.in_cleanup:
            self.fail(active, self.stage)

    def prepare_root(self, actual, root, *args):
        with self.checkpoint("fresh-root"):
            require(self.root is None, "fresh-root")
            fresh_parent(self.parent)
            root = safe_path(root)
            info = root.stat()
            require(
                root.parent == self.parent
                and root.name.startswith(self.f.PREFIX)
                and stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and not any(root.iterdir()),
                "fresh-root",
            )
            self.root = root
            return actual(root, *args)

    def prepare_launch(self, actual, root, binary, version, report):
        with self.checkpoint("preparation"):
            require(root == self.root and self.baseline is None, "baseline-contract")
            self.report = report
            result = actual(root, binary, version, report)
            baseline = result[2]
            self.f.box.validate_session_export(baseline, baseline["info"]["id"])
            require(
                self.f.compat.directory(baseline) == str(root / "source-a")
                and not baseline["info"].get("parentID")
                and result[1]["version"] == "2.3.0"
                and result[1]["sha256"] == NATIVE_SHA256,
                "baseline-contract",
            )
            self.baseline = baseline
            return result

    def launcher(self, actual, arguments):
        with self.checkpoint("owning-launch"):
            index = len(self.ledger)
            require(
                self.root is not None
                and self.baseline is not None
                and index < len(SEQUENCE),
                "launch-contract",
            )
            command, project = SEQUENCE[index]
            records = self.f.workspace_records(self.root)
            require(project in records, "launch-contract")
            _, data = records[project]
            require(data["project"] == str(self.root / project), "launch-contract")
            require(
                type(arguments) is list
                and arguments == [command, "--workspace", data["id"]],
                "launch-contract",
            )
            forwarded = list(arguments)
            if index == 0:
                require(self.imports == 0 and project == "source-a", "import-once")
                forwarded += ["--import-session", self.baseline["info"]["id"]]
                self.imports += 1
            item = {
                "ordinal": index + 1,
                "command": command,
                "project": project,
                "import_appended": index == 0,
                "completed": False,
            }
            self.ledger.append(item)
            result = actual(forwarded)
            item["completed"] = True
            return result

    def home_snapshot(self, actual, data):
        with self.checkpoint("home-snapshot"):
            index = len(self.home_snapshots)
            require(
                self.root is not None
                and self.baseline is not None
                and index < len(HOME_SITES),
                "home-snapshot-contract",
            )
            role, launches = HOME_SITES[index]
            require(
                len(self.ledger) == launches
                and all(item["completed"] for item in self.ledger),
                "home-snapshot-contract",
            )
            # The frozen validator binds project markers, private registry,
            # selected policy, resource names and restored_from source/kind.
            records = self.f.workspace_records(self.root)
            require(
                role in records
                and type(data) is dict
                and data == records[role][1]
                and data["project"] == str(self.root / role)
                and not data.get("restore_incomplete"),
                "home-snapshot-contract",
            )
            if role == "source-a":
                require(not data.get("restored_from"), "home-snapshot-contract")
            else:
                require(
                    data.get("restored_from", {}).get("workspace_id")
                    == records["source-a"][1]["id"]
                    and data["restored_from"]["kind"] == role.removeprefix("restored-"),
                    "home-snapshot-contract",
                )
            # No original critical-file read or collector read until the actual
            # owned container, mounts and private home volume are verified stopped.
            self.f.safety.verify_owned_resources(self.root, data, stopped=True)
            original = actual(data)
            require(
                type(original) is dict
                and original.keys() == {*self.f.HOME_FILES, "complete-home-tree"}
                and all(
                    type(value) is str
                    and re.fullmatch(r"[0-9a-f]{64}", value) is not None
                    for value in original.values()
                ),
                "home-snapshot-contract",
            )
            volume = self.f.backups.owned_volume(self.f.box, data)
            require(
                type(volume) is dict
                and volume.get("Name") == data["home_volume"]
                and (volume.get("Labels") or {}).get(self.f.box.LABEL) == data["id"]
                and self.f.safety.no_symlinks(volume["Mountpoint"])
                == self.root
                / "data/containers/storage/volumes"
                / data["home_volume"]
                / "_data",
                "private-volume",
            )
            probe = home_probe(
                self.f.crypto.small(
                    [
                        "podman",
                        "unshare",
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        HOME_PROGRAM,
                        volume["Mountpoint"],
                        "source" if index == 0 else "exact",
                    ],
                    timeout=30,
                ),
                source=index == 0,
            )
            require(probe["exact"] == original["complete-home-tree"], "home-dual-read")
            # The controlled fixture creates a root-owned container home. Do not
            # invent a generic arbitrary-root-owner mapping the archive lacks.
            require(probe["root_owner_expected"], "home-root-mapping")
            result = HomeSnapshot(self, original, probe, role, index)
            self.home_snapshots.append(result)
            return result

    def compare_home(self, left, right):
        with self.checkpoint("home-comparison"):
            require(
                type(left) is HomeSnapshot
                and type(right) is HomeSnapshot
                and left.adapter is self
                and right.adapter is self
                and all(
                    0 <= item.ordinal < len(self.home_snapshots)
                    and self.home_snapshots[item.ordinal] is item
                    and dict(item) == item.original
                    for item in (left, right)
                ),
                "home-snapshot-contract",
            )
            # Symmetric only for the same saved source baseline versus a later
            # snapshot. No restored/restored or plain-dictionary fallback.
            source, current = (left, right) if left.ordinal == 0 else (right, left)
            require(
                source.ordinal == 0
                and source.role == "source-a"
                and current.ordinal > 0,
                "home-snapshot-contract",
            )
            if current.role == "source-a":
                with self.checkpoint("source-home-preservation"):
                    exact = current.original == source.original and all(
                        current.probe[key] == source.probe[key]
                        for key in (
                            "exact",
                            "root_mode",
                            "root_owner_expected",
                            "child_special_bits",
                        )
                    )
                    if current.ordinal not in self.home_preserved_sites:
                        self.source_preservations.append(exact)
                        self.home_preserved_sites.add(current.ordinal)
                    kind = {2: "full", 4: "portable"}.get(current.ordinal)
                    if kind is not None:
                        require(kind in self.home_comparisons, "home-snapshot-contract")
                        self.home_comparisons[kind]["source_preservation_exact"] = exact
                    require(exact, "source-home-preservation")
                    return True
            with self.checkpoint("restore-home-policy"):
                kind = current.role.removeprefix("restored-")
                require(
                    (kind, current.ordinal) in (("full", 1), ("portable", 3)),
                    "home-snapshot-contract",
                )
                critical = all(
                    current.original[name] == source.original[name]
                    for name in self.f.HOME_FILES
                )
                tree = current.probe["exact"] == source.probe["expected"]
                private = current.probe["root_mode"] == 0o700
                owner = current.probe["root_owner_expected"]
                no_special = current.probe["child_special_bits"] == 0
                expected = critical and tree and private and owner and no_special
                self.home_comparisons[kind] = {
                    "exact_equal": current.original == source.original,
                    "policy_expected_matches": expected,
                    "root_mode_changed": current.probe["root_mode"]
                    != source.probe["root_mode"],
                    "source_has_special_bits": source.probe["child_special_bits"] > 0
                    or bool(source.probe["root_mode"] & 0o7000),
                    "restored_root_private": private,
                    "restored_root_owner_expected": owner,
                    "restored_no_child_special_bits": no_special,
                    "critical_files_exact": critical,
                    "inventory_content_ordinary_metadata_matches": tree,
                    "source_preservation_exact": self.home_comparisons.get(
                        kind, {}
                    ).get("source_preservation_exact"),
                }
                require(expected, "restore-home-policy")
                return True

    def make_provider(self, actual, *args, **kwargs):
        server, worker = actual(*args, **kwargs)
        require(not self.provider, "provider-cleanup-contract")
        self.provider = True
        adapter = self

        class Server:
            def __getattr__(self, name):
                return getattr(server, name)

            def shutdown(self, *a, **k):
                adapter.provider_cleanup("shutdown", server.shutdown, *a, **k)

            def server_close(self, *a, **k):
                adapter.provider_cleanup("close", server.server_close, *a, **k)

        class Worker:
            def join(self, *a, **k):
                adapter.provider_cleanup("join", worker.join, *a, **k)
                try:
                    alive = worker.is_alive()
                    require(type(alive) is bool, "provider-cleanup-contract")
                    adapter.cleanup["worker_alive"] = alive
                    require(not alive, "provider-worker-alive")
                except BaseException as error:
                    adapter.fail(error, "provider-join", secondary="cleanup")

        return Server(), Worker()

    def provider_cleanup(self, name, actual, *args, **kwargs):
        self.cleanup[name] += 1
        try:
            with self.checkpoint("provider-" + name, cleanup=True):
                return actual(*args, **kwargs)
        except BaseException:
            # Do not let shutdown mask an earlier error or skip close/join.
            return None

    @contextlib.contextmanager
    def installed(self):
        f = self.f
        with contextlib.ExitStack() as stack:
            actual_run = subprocess.run

            def observed_run(*args, **kwargs):
                try:
                    result = actual_run(*args, **kwargs)
                    code = result.returncode
                except BaseException as error:
                    code = getattr(error, "returncode", None)
                    self.last_exit = (
                        (self.stage, code)
                        if type(code) is int and -255 <= code <= 255 and code != 0
                        else None
                    )
                    raise
                self.last_exit = (
                    (self.stage, code)
                    if type(code) is int and -255 <= code <= 255 and code != 0
                    else None
                )
                return result

            # Exit data only: never read/stringify argv, stdout, stderr or env.
            stack.enter_context(patch.object(subprocess, "run", observed_run))
            for name in ("prepare_root", "prepare_launch"):
                actual = getattr(f, name)
                method = getattr(self, name)
                stack.enter_context(
                    patch.object(
                        f,
                        name,
                        side_effect=lambda *a, _m=method, _f=actual, **k: _m(
                            _f, *a, **k
                        ),
                    )
                )
            actual_launcher = f.safety.launcher
            stack.enter_context(
                patch.object(
                    f.safety,
                    "launcher",
                    side_effect=lambda args: self.launcher(actual_launcher, args),
                )
            )
            actual_provider = f.legacy.make_provider
            stack.enter_context(
                patch.object(
                    f.legacy,
                    "make_provider",
                    side_effect=lambda *a, **k: self.make_provider(
                        actual_provider, *a, **k
                    ),
                )
            )
            actual_require = f.safety.require

            def observed_require(condition, category):
                try:
                    return actual_require(condition, category)
                except BaseException as error:
                    self.fail(error)
                    raise

            stack.enter_context(patch.object(f.safety, "require", observed_require))
            stack.enter_context(patch.object(f, "require", observed_require))
            actual_home = f.home_snapshot
            stack.enter_context(
                patch.object(
                    f,
                    "home_snapshot",
                    side_effect=lambda data: self.home_snapshot(actual_home, data),
                )
            )
            actual_write = f.safety.write_json_new

            def write(path, value):
                try:
                    return actual_write(path, value)
                except BaseException as error:
                    if self.root is not None and path == self.root / "result.json":
                        self.fail(error, "original-report", secondary="report")
                    raise

            stack.enter_context(patch.object(f.safety, "write_json_new", write))
            # Scoped forwarding catches failures before production finally blocks.
            targets = (
                (f, "workspace_records", "workspace-metadata", False),
                (f.safety, "verify_owned_resources", "resources", False),
                (f.safety, "selected_session", "selected-session", False),
                (f.box, "export_container_session", "transcript", False),
                (f.box, "import_host_session", "host-import", False),
                (f.box, "verify_session_import", "import-verification", False),
                (f.legacy, "verify_v2_progress", "transcript", False),
                (f.legacy, "run_fixture_session", "native-tools", False),
                (f, "container_python", "sentinels", False),
                (f, "run", "fixture-run", False),
                (f.backups, "make_backup", "backup", False),
                (f.backups, "restore_backup", "restore", False),
                (f.box, "start_container", "container-start", False),
                (f.box, "prepare_runtime", "runtime-prepare", False),
                (f.workspace_service.Owner, "__enter__", "owner-start", False),
                (f.workspace_service.Owner, "shutdown", "owner-stop", True),
                (f.box, "sync_sessions", "history-sync", False),
                (f.box, "stop_container", "container-stop", True),
            )
            for target, name, stage, cleanup in targets:
                actual = getattr(target, name)

                def forward(*a, _f=actual, _s=stage, _c=cleanup, **k):
                    with self.checkpoint(_s, cleanup=_c):
                        return _f(*a, **k)

                # Functions, not MagicMock descriptors: Owner methods still bind.
                stack.enter_context(patch.object(target, name, forward))
            yield

    def complete(self, original, exit_code):
        require(
            type(exit_code) is int
            and exit_code == 0
            and original.get("status") == "passed"
            and isinstance(original.get("checks"), dict)
            and original["checks"].keys() == set(CHECKS)
            and all(value is True for value in original["checks"].values())
            and original.get("excluded") == list(EXCLUDED)
            and original.get("profile") == "v2-only"
            and original.get("credential_scope") == "selected"
            and original.get("global_vault_fixture_ran") is False
            and original.get("legacy_fixture_ran") is False
            and original.get("native", {}).get("version") == "2.3.0"
            and original.get("native", {}).get("sha256") == NATIVE_SHA256,
            "report-contract",
        )
        require(
            self.imports == 1
            and len(self.ledger) == len(SEQUENCE)
            and all(item["completed"] for item in self.ledger),
            "import-once",
        )
        require(
            len(self.home_snapshots) == len(HOME_SITES)
            and self.home_comparisons.keys() == {"full", "portable"}
            and all(
                item["policy_expected_matches"] is True
                and item["source_preservation_exact"] is True
                for item in self.home_comparisons.values()
            )
            and self.source_preservations == [True, True, True],
            "home-snapshot-contract",
        )
        require(
            self.provider
            and self.cleanup
            == {
                "shutdown": 1,
                "close": 1,
                "join": 1,
                "worker_alive": False,
            },
            "provider-cleanup-contract",
        )
        require(
            not self.primary and not self.cleanup_errors and not self.reporting_errors,
            "report-contract",
        )


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "External lifecycle refused: arguments.\n")


def preflight(args, runner):
    host_guard()  # No Git, native, placement or allocation before host refusal.
    source = safe_path(args.source)
    identity = snapshot(source, args.expect_commit)
    require(not runner.is_relative_to(source), "source-placement")
    parent = fresh_parent(args.work_dir)
    temporary = fresh_parent(args.temp_dir) if args.temp_dir is not None else None
    for directory in (parent, temporary):
        if directory is not None:
            require(not directory.is_relative_to(source), "source-placement")
    import_guard(source, identity, before=True)
    binary = safe_path(args.opencode_v2.expanduser().resolve(strict=True))
    require(
        binary.is_file()
        and os.access(binary, os.X_OK)
        and digest(binary) == NATIVE_SHA256,
        "native-executable",
    )
    return source, identity, binary, parent, temporary


def write_report_stage(descriptor, value):
    """Own/close this newly allocated private descriptor before publication."""
    try:
        output = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with output:
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def publish_report(root, value):
    """Closed staging -> atomic no-replace link; retain the private alias."""
    root = safe_path(root)
    info = root.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and root.name.startswith("oc-box-v2-only-host-")
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077,
        "fresh-root",
    )
    descriptor, stage = tempfile.mkstemp(
        prefix=REPORT_STAGE_PREFIX,
        suffix=".json",
        dir=root,
    )
    # A late write/flush/fsync/close failure leaves only non-authoritative staging.
    # No cleanup on either outcome: unlink failure after link must not turn a
    # published pass into a nonzero invocation. The root is private and fresh.
    write_report_stage(descriptor, value)
    os.link(stage, root / REPORT, follow_symlinks=False)
    # Link is the final fallible publication operation. The final file and the
    # retained stage intentionally have nlink=2. Frozen private_json (nlink=1)
    # remains used only for original result.json, never for this new receipt.


def execute_fixture(fixture, parent, binary, distro, temporary, provenance, recheck):
    adapter = Adapter(fixture, parent)
    exit_code = None
    original = {}
    report_hash = None
    arguments = [
        "--opencode-v2",
        str(binary),
        "--native-version",
        "2.3.0",
        "--distro",
        distro,
        "--work-dir",
        str(parent),
    ]
    if temporary is not None:
        arguments += ["--temp-dir", str(temporary)]
    try:
        with adapter.installed(), fixture.safety.private_output():
            exit_code = fixture.main(arguments)
        recheck()
        require(adapter.root is not None, "fresh-root")
        try:
            original = fixture.safety.private_json(adapter.root, "result.json")
            report_hash = digest(adapter.root / "result.json")
        except BaseException as error:
            adapter.fail(error, "original-report", secondary="report")
            raise
        require(
            adapter.report is not None
            and original.get("source") == adapter.report.get("source")
            and original.get("source", {}).get("build", {}).get("commit") == COMMIT
            and original["source"]["sha256"] == provenance["application"]["sha256"],
            "report-contract",
        )
        if not (adapter.primary or adapter.cleanup_errors or adapter.reporting_errors):
            adapter.complete(original, exit_code)
    except BaseException as error:
        adapter.fail(error, "fixture-completion")
    if adapter.root is None:
        print(
            "External lifecycle failed before fresh fixture allocation.",
            file=sys.stderr,
        )
        return 1
    # Complete every integrity check before publishing a pass. There is no
    # post-write original-report read that could leave a failed invocation with
    # a parseable passed companion (trusted no-concurrent-writer prerequisite).
    if report_hash is not None:
        try:
            require(
                digest(adapter.root / "result.json") == report_hash, "original-report"
            )
        except BaseException as error:
            adapter.fail(error, "original-report-integrity", secondary="report")
    passed = (
        adapter.primary is None
        and not adapter.cleanup_errors
        and not adapter.reporting_errors
    )
    original_checks = original.get("checks", {}) if isinstance(original, dict) else {}
    if not isinstance(original_checks, dict):
        original_checks = {}
    result = {
        "format": FORMAT,
        "version": 1,
        "status": "passed" if passed else "failed",
        "provenance": provenance,
        "checks": {name: original_checks.get(name) is True for name in CHECKS},
        "excluded": list(EXCLUDED),
        "credential_scope": "selected",
        "legacy_fixture_ran": False,
        "global_vault_fixture_ran": False,
        "original_exit_code": exit_code
        if type(exit_code) is int and 0 <= exit_code <= 255
        else None,
        "original_exit_zero": type(exit_code) is int and exit_code == 0,
        "original_report_sha256": report_hash,
        "first_import_count": adapter.imports,
        "first_import_verified": original_checks.get("native-v2-host-import") is True,
        "launches": adapter.ledger,
        "provider_cleanup": adapter.cleanup,
        "home_comparisons": adapter.home_comparisons,
        "source_home_preservations_exact": adapter.source_preservations,
        "primary_failure": adapter.primary,
        "cleanup_failures": adapter.cleanup_errors,
        "reporting_failures": adapter.reporting_errors,
        "companion_publication": {
            "method": "closed-private-stage-link-no-replace",
            "staging_retained": True,
            "staging_authoritative": False,
        },
        "limits": "Fresh scripted synthetic V2-only host lifecycle; no legacy/global/UI/real-provider/OAuth/network/security acceptance.",
    }
    try:
        publish_report(adapter.root, result)
    except BaseException as error:
        adapter.fail(error, "external-report", secondary="report")
        print(
            "External lifecycle report publication failed; original evidence retained. "
            "Any staging is private and non-authoritative.",
            file=sys.stderr,
        )
        # Failed staging/publication is not an authoritative receipt; preserve
        # fixed primary/secondary diagnostics on stderr. Never replace a final
        # file that already existed; partial/complete staging stays private.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "primary_failure": adapter.primary,
                    "cleanup_failures": adapter.cleanup_errors,
                    "reporting_failures": adapter.reporting_errors,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        failures = [adapter.primary, *adapter.cleanup_errors, *adapter.reporting_errors]
        return (
            130
            if any(item and item["error_class"] == "interrupted" for item in failures)
            else 1
        )
    print(
        "External lifecycle "
        + result["status"]
        + "; fresh fixture retained: "
        + str(adapter.root)
    )
    failures = [adapter.primary, *adapter.cleanup_errors, *adapter.reporting_errors]
    return (
        0
        if passed
        else (
            130
            if any(item and item["error_class"] == "interrupted" for item in failures)
            else 1
        )
    )


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--opencode-v2", type=Path, required=True)
    parser.add_argument("--native-version", choices=("2.3.0",), default="2.3.0")
    parser.add_argument("--distro", choices=("tumbleweed",), default="tumbleweed")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path)
    args = parser.parse_args(argv)
    cache = None
    try:
        host_guard()
        runner = safe_path(Path(__file__).absolute())
        runner_hash = digest(runner)
        probe = safe_path(runner.parents[1] / "tests/test_v2_lifecycle_external.py")
        probe_hash = digest(probe)
        source, identity, binary, parent, temporary = preflight(args, runner)
        fixture, cache = load_candidate(source, identity, temporary or parent)
        # The original checker has now installed its own private cache prefix.
        # Dispose our unused prefix before any fixture allocation/reporting.
        cache.cleanup()
        cache = None
        provenance = {
            "application": identity,
            "frozen_helpers": FROZEN,
            "runner_sha256": runner_hash,
            "offline_probe_sha256": probe_hash,
            "native": {"version": "2.3.0", "sha256": NATIVE_SHA256},
        }

        def recheck():
            require(
                digest(runner) == runner_hash and digest(probe) == probe_hash,
                "runner-changed",
            )
            require(
                snapshot(source, COMMIT) == identity
                and fixture.source_identity()["sha256"] == identity["sha256"],
                "source-changed",
            )
            import_guard(source, identity)
            require(digest(binary) == NATIVE_SHA256, "native-changed")

        recheck()  # Before candidate main can allocate a fresh root.
        return execute_fixture(
            fixture, parent, binary, args.distro, temporary, provenance, recheck
        )
    except BaseException as error:
        category = diagnostic(error, "preflight")["category"]
        print("External lifecycle refused: " + category + ".", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        if cache is not None:
            try:
                cache.cleanup()
            except BaseException:
                print("External lifecycle bytecode cleanup failed.", file=sys.stderr)
                return 1


if __name__ == "__main__":
    sys.exit(main())
