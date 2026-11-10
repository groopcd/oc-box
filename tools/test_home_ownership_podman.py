#!/usr/bin/env python3
"""Fresh HOST-only, image-free probe of the shared encrypted home component.

No workspace/container/image lifecycle, first mount, keep-id/auto/custom mapping
acceptance. Retain all evidence; only the separate --cleanup EXACT_ROOT removes
the two proven local volumes, without force. Never use the global fixture cleanup.
"""

import argparse
import contextlib
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import stat
import sys
import tempfile
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import backup_crypto as crypto, backups, oc_box as box
from tools import test_global_vault_podman as global_fixture
from tools import test_interactive_podman as interactive

require = global_fixture.require
FORMAT = "oc-box-shared-home-component-ownership"
PREFIX = global_fixture.PREFIX + "home-ownership-"
LABEL = "io.oc-box.home-component"
ROLES = ("source", "destination")
HOME_MARKERS = (
    "home-component.json",
    "home-ready.json",
    *("home-created-" + role + ".json" for role in ROLES),
)
FRESH_MARKERS = (
    *HOME_MARKERS,
    "fixture.json",
    "fixture-ids.json",
    "interactive.json",
    "interactive-*-report.json",
    "edges.json",
    "edges-result.json",
    "edges-*-report.json",
    "edges-*-baseline.json",
    "edges-*-after.json",
    "history-purpose.json",
    "history-result.json",
    "home-mapping.json",
    "home-run-*-report.json",
    "home-cleanup-*-report.json",
    "cleanup-purpose.json",
    "result.json",
    "retry-result.json",
    "full-restore-result.json",
    "downgrade-result.json",
    "seed-baseline.json",
)
INVOCATION_SOURCE = ContextVar("home_component_invocation_source", default=None)
LIMIT = 64 * 1024 * 1024
AUDITED = (
    "tools/acceptance_guards.py",
    "tools/test_home_ownership_podman.py",
    "tools/test_global_vault_podman.py",
    "tools/test_interactive_podman.py",
    "tools/test_v2_podman.py",
)
FILES = {
    ".config/synthetic.json": b'{"fixture":"synthetic-only","enabled":true}\n',
    "mapped-sentinel": b"SYNTHETIC_MAPPED_ONE\n",
}
# kind, namespace UID, namespace GID, mode. No times/ACL/xattr promises.
EXPECTED = {
    ".": ["directory", 0, 0, 0o700],
    ".local": ["directory", 0, 0, 0o750],
    ".config": ["directory", 0, 0, 0o710],
    ".local/history.db": ["file", 0, 0, 0o600],
    ".config/synthetic.json": ["file", 0, 0, 0o640],
    "mapped-sentinel": ["file", 1, 1, 0o644],
}

MAP_PROGRAM = """import json,os,pathlib
print(json.dumps({'euid':os.geteuid(),'egid':os.getegid(),
 'uid':pathlib.Path('/proc/self/uid_map').read_text(),
 'gid':pathlib.Path('/proc/self/gid_map').read_text()}))
"""

# Only fixed synthetic data is seeded. Archive creation/extraction is EXCLUSIVELY
# production backup_archive.py, not this small namespace inspection program.
DATA_PROGRAM = """import hashlib,json,os,pathlib,sqlite3,stat,sys
def need(condition):
    if not condition: raise ValueError('namespace-contract')
need(os.geteuid()==0 and os.getegid()==0)
action=sys.argv[1]; root=pathlib.Path(sys.argv[2])
for path in (*reversed(root.parents),root):
    need(stat.S_ISDIR(path.lstat().st_mode))
need((root.stat().st_uid,root.stat().st_gid)==(0,0))
expected=json.loads(sys.argv[3]); contents=json.loads(sys.argv[4])
if action in ('seed','empty'):
    need(not any(root.iterdir()))
if action=='seed':
    root.chmod(0o700)
    for name in ('.local','.config'):
        (root/name).mkdir(mode=0o700)
    with sqlite3.connect(root/'.local/history.db') as db:
        db.execute('CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT INTO fixture VALUES (1,'SYNTHETIC_CLOSED_HISTORY')")
    db.close()
    for name,content in contents.items(): (root/name).write_bytes(bytes.fromhex(content))
    for name,(kind,uid,gid,mode) in expected.items():
        os.chown(root/name,uid,gid,follow_symlinks=False); (root/name).chmod(mode)
manifest={}
def scan(path,name):
    info=path.lstat(); kind='directory' if stat.S_ISDIR(info.st_mode) else 'file'
    need(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
    need(kind=='directory' or info.st_nlink==1)
    item=[kind,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)]
    if kind=='file':
        need(info.st_size<=1024*1024)
        item += [hashlib.sha256(path.read_bytes()).hexdigest()]
    manifest[name]=item
    if kind=='directory':
        for child in sorted(path.iterdir()):
            scan(child,child.name if name=='.' else name+'/'+child.name)
scan(root,'.')
if action!='empty':
    need(set(manifest)==set(expected))
    for name,item in expected.items(): need(manifest[name][:4]==item)
    for name,content in contents.items(): need((root/name).read_bytes()==bytes.fromhex(content))
    with sqlite3.connect((root/'.local/history.db').as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        need(db.execute('PRAGMA quick_check').fetchall()==[('ok',)])
        need(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()==[
            ('table','fixture','fixture','CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')])
        need(db.execute('SELECT * FROM fixture').fetchall()==[(1,'SYNTHETIC_CLOSED_HISTORY')])
        need(db.execute('PRAGMA user_version').fetchall()==[(0,)])
    db.close()
print(json.dumps({'mapping':{'euid':os.geteuid(),'egid':os.getegid(),
 'uid':pathlib.Path('/proc/self/uid_map').read_text(),
 'gid':pathlib.Path('/proc/self/gid_map').read_text()},'manifest':manifest}))
"""


def source_snapshot():
    source = interactive.source_identity()
    paths = (
        *AUDITED,
        *sorted(
            str(path.relative_to(APP))
            for path in (APP / "src/oc_box").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ),
    )
    return {
        "identity": source,
        "audited_sha256": {name: interactive.file_hash(APP / name) for name in paths},
    }


def same_source(source):
    require(source_snapshot() == source, "source-changed")


@contextlib.contextmanager
def invocation_source(current):
    token = INVOCATION_SOURCE.set(current)
    try:
        yield
    finally:
        INVOCATION_SOURCE.reset(token)


def fresh_parent(path):
    parent = interactive.fresh_parent(path)
    for ancestor in (parent, *parent.parents):
        require(
            not any(any(ancestor.glob(pattern)) for pattern in FRESH_MARKERS),
            "fresh-parent-required",
        )
    return parent


def plan(root, token):
    return {
        role: {
            "name": "oc-box-home-ownership-" + token + "-" + role,
            "labels": {
                LABEL: token,
                LABEL + ".uid": str(os.getuid()),
                LABEL + ".role": role,
            },
            "path": str(
                root
                / "data/containers/storage/volumes"
                / ("oc-box-home-ownership-" + token + "-" + role)
                / "_data"
            ),
        }
        for role in ROLES
    }


def write_purpose(root, source):
    token = secrets.token_hex(16)
    mapping = global_fixture.private_json(root, "podman-executable.json")
    purpose = {
        "format": FORMAT,
        "schema": 1,
        "root": str(root),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "token": token,
        "roles": plan(root, token),
        "source": source,
        "native_sha256": {
            name: interactive.file_hash(Path(path)) for name, path in mapping.items()
        },
    }
    global_fixture.write_json_new(root / "home-component.json", purpose)
    return purpose


def validate_purpose(root, current):
    require(root.is_absolute() and root.name.startswith(PREFIX), "purpose")
    root = global_fixture.validate_fixture(root)
    require(
        global_fixture.private_json(root, "fixture-ids.json") == {}
        and not any((root / "registry/workspaces").iterdir()),
        "empty-registry",
    )
    data = global_fixture.private_json(root, "home-component.json")
    require(
        isinstance(data, dict)
        and set(data)
        == {
            "format",
            "schema",
            "root",
            "uid",
            "gid",
            "token",
            "roles",
            "source",
            "native_sha256",
        },
        "purpose",
    )
    require(
        data["format"] == FORMAT
        and type(data["schema"]) is int
        and data["schema"] == 1
        and data["root"] == str(root)
        and data["uid"] == os.getuid()
        and data["gid"] == os.getgid()
        and isinstance(data["token"], str)
        and re.fullmatch(r"[0-9a-f]{32}", data["token"]) is not None,
        "purpose",
    )
    require(data["roles"] == plan(root, data["token"]), "purpose")
    source = data["source"]
    require(
        isinstance(source, dict)
        and set(source) == {"identity", "audited_sha256"}
        and isinstance(source["identity"], dict),
        "source-compatibility",
    )
    original = source["identity"]
    require(
        set(original) == {"checkout", "version", "commit", "commit_count", "dirty"}
        and original["checkout"] == str(APP)
        and original["dirty"] is False
        and isinstance(original["commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", original["commit"]) is not None
        and type(original["commit_count"]) is int
        and original["commit_count"] > 29
        and original["version"]
        == "1.6." + str(original["commit_count"] - 29) + "+g" + original["commit"][:7]
        and source["audited_sha256"] == current["audited_sha256"],
        "source-compatibility",
    )
    mapping = global_fixture.private_json(root, "podman-executable.json")
    require(
        data["native_sha256"]
        == {name: interactive.file_hash(Path(path)) for name, path in mapping.items()},
        "native-changed",
    )
    return data


@contextlib.contextmanager
def lifecycle(root):
    root = global_fixture.no_symlinks(root)
    info = root.stat()
    require(
        root.is_dir() and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "fixture-root",
    )
    fd = os.open(
        root / "home-component.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            "lifecycle-lock",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise global_fixture.FixtureError("lifecycle-lock") from None
        yield
    finally:
        os.close(fd)


def validate_private_store(root, *, initial=False):
    # validate_store deliberately permits an uninitialized empty registry. Limit
    # that exception to the FIRST fresh info call, not its later sanitized calls.
    original = box.run
    first = initial

    def guarded(command, **kwargs):
        nonlocal first
        require(command == ["podman", "info", "--format", "json"], "engine-command")
        engine_entry(root, initial=first)
        first = False
        return original(command, **kwargs)

    if not initial:
        global_fixture.require_runtime_boot(root)
    with patch.object(box, "run", guarded):
        global_fixture.validate_store(root)
    global_fixture.require_runtime_boot(root)


def engine_entry(root, *, initial=False):
    current = INVOCATION_SOURCE.get()
    require(current is not None, "invocation-context")
    same_source(current)  # current invocation, NEVER the retained run identity
    global_fixture.require_runtime_boot(root, allow_uninitialized=initial)
    global_fixture.validate_fixture(root)
    require(
        global_fixture.private_json(root, "fixture-ids.json") == {}, "empty-registry"
    )
    require(shutil.which("podman") == str(root / "bin/podman"), "podman-selector")


def volume_command(action, item):
    name = item["name"]
    if action == "create":
        return [
            "volume",
            "create",
            "--driver",
            "local",
            *[
                part
                for key, value in item["labels"].items()
                for part in ("--label", key + "=" + value)
            ],
            name,
        ]
    require(action in ("exists", "inspect", "rm"), "engine-command")
    return ["volume", action, name]


def engine(root, purpose, command, *, check=True):
    allowed = [
        volume_command(action, item)
        for item in purpose["roles"].values()
        for action in ("create", "exists", "inspect", "rm")
    ]
    allowed.append(
        [
            "volume",
            "ls",
            "--filter",
            "label=" + LABEL + "=" + purpose["token"],
            "--format",
            "{{.Name}}",
        ]
    )
    allowed.append(["unshare", sys.executable, "-B", "-c", MAP_PROGRAM])
    allowed.extend(
        [
            "unshare",
            sys.executable,
            "-B",
            "-c",
            DATA_PROGRAM,
            action,
            item["path"],
            json.dumps(EXPECTED),
            json.dumps({key: value.hex() for key, value in FILES.items()}),
        ]
        for role, item in purpose["roles"].items()
        for action in (
            ("seed", "inspect") if role == "source" else ("empty", "inspect")
        )
    )
    require(command in allowed, "engine-command")
    engine_entry(root)
    return backups.command(box, *command, check=check, timeout=60)


def inspect_volume(root, purpose, role):
    item = purpose["roles"][role]
    value = json.loads(engine(root, purpose, volume_command("inspect", item)).stdout)
    require(
        isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict),
        "volume-metadata",
    )
    info = value[0]
    require(
        info.get("Name") == item["name"]
        and info.get("Labels") == item["labels"]
        and info.get("Driver") == "local"
        and "Options" in info
        and info.get("Options") in ({}, None)
        and info.get("StorageID") in (None, "")
        and info.get("Scope", "local") == "local"
        and not info.get("Anonymous", False)
        and info.get("Mountpoint") == item["path"],
        "volume-metadata",
    )
    path = global_fixture.no_symlinks(item["path"])
    require(path.is_dir() and path.resolve(strict=True) == path, "volume-path")
    return path


def preflight(root, purpose):
    result = engine(
        root,
        purpose,
        [
            "volume",
            "ls",
            "--filter",
            "label=" + LABEL + "=" + purpose["token"],
            "--format",
            "{{.Name}}",
        ],
    )
    names = result.stdout.splitlines()
    require(
        len(names) == 2
        and set(names) == {item["name"] for item in purpose["roles"].values()},
        "volume-allowlist",
    )
    paths = {role: inspect_volume(root, purpose, role) for role in ROLES}
    require(
        paths["source"] != paths["destination"]
        and not os.path.samefile(paths["source"], paths["destination"]),
        "volume-path",
    )
    return paths


def parse_map(text, host):
    require(isinstance(text, str), "namespace-mapping")
    rows = []
    for line in text.splitlines():
        require(
            re.fullmatch(r"\s*\d+\s+\d+\s+\d+\s*", line) is not None,
            "namespace-mapping",
        )
        row = tuple(map(int, line.split()))
        require(
            row[2] > 0 and max(row[0] + row[2], row[1] + row[2]) <= 2**32 - 1,
            "namespace-mapping",
        )
        rows.append(row)
    require(0 < len(rows) <= 340, "namespace-mapping")
    for i, row in enumerate(rows):
        for other in rows[:i]:
            for axis in (0, 1):
                require(
                    row[axis] + row[2] <= other[axis]
                    or other[axis] + other[2] <= row[axis],
                    "namespace-mapping",
                )
    translated = []
    for ident in (0, 1):
        matches = [
            outside + ident - inside
            for inside, outside, count in rows
            if inside <= ident < inside + count
        ]
        require(len(matches) == 1, "namespace-mapping")
        translated.append(matches[0])
    require(
        translated[0] == host and translated[1] not in (0, host), "namespace-mapping"
    )
    return translated


def mapping(value):
    require(
        isinstance(value, dict)
        and set(value) == {"euid", "egid", "uid", "gid"}
        and type(value["euid"]) is int
        and value["euid"] == 0
        and type(value["egid"]) is int
        and value["egid"] == 0,
        "namespace-mapping",
    )
    return {
        "uid": parse_map(value["uid"], os.getuid()),
        "gid": parse_map(value["gid"], os.getgid()),
        "maps_sha256": fingerprint({key: value[key] for key in ("uid", "gid")}),
    }


def namespace(root, purpose, role, action, measured):
    path = inspect_volume(root, purpose, role)
    result = engine(
        root,
        purpose,
        [
            "unshare",
            sys.executable,
            "-B",
            "-c",
            DATA_PROGRAM,
            action,
            str(path),
            json.dumps(EXPECTED),
            json.dumps({key: value.hex() for key, value in FILES.items()}),
        ],
    )
    value = json.loads(result.stdout)
    require(mapping(value["mapping"]) == measured, "mapping-changed")
    manifest = value["manifest"]
    if action == "empty":
        require(
            set(manifest) == {"."} and manifest["."][:3] == ["directory", 0, 0],
            "destination-empty",
        )
    else:
        verify_manifest(manifest)
    return manifest


def verify_manifest(manifest):
    require(
        isinstance(manifest, dict) and set(manifest) == set(EXPECTED), "fixed-manifest"
    )
    for name, expected in EXPECTED.items():
        item = manifest[name]
        require(
            isinstance(item, list)
            and item[:4] == expected
            and len(item) == (5 if expected[0] == "file" else 4),
            "fixed-manifest",
        )
        if expected[0] == "file":
            require(
                isinstance(item[4], str)
                and re.fullmatch(r"[0-9a-f]{64}", item[4]) is not None,
                "fixed-manifest",
            )
    for name, content in FILES.items():
        require(
            manifest[name][4] == hashlib.sha256(content).hexdigest(), "fixed-manifest"
        )


def host_manifest(path, measured):
    # Independent read-only HOST metadata; all content IO/mutations use unshare.
    manifest = {}

    def scan(current, name):
        global_fixture.no_symlinks(current)
        info = current.lstat()
        require(
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "host-manifest"
        )
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
        require(
            info.st_uid in measured["uid"] and info.st_gid in measured["gid"],
            "host-ownership",
        )
        item = [
            kind,
            measured["uid"].index(info.st_uid),
            measured["gid"].index(info.st_gid),
            stat.S_IMODE(info.st_mode),
        ]
        if kind == "file":
            require(info.st_nlink == 1 and info.st_size <= 1024 * 1024, "host-manifest")
        manifest[name] = item
        if kind == "directory":
            for child in sorted(current.iterdir()):
                scan(child, child.name if name == "." else name + "/" + child.name)

    scan(path, ".")
    require(manifest == EXPECTED, "fixed-manifest")
    return manifest


def archive(root, purpose, action):
    role = "source" if action == "create" else "destination"
    path = inspect_volume(root, purpose, role)
    command = backups.archive_command(
        box,
        action,
        path,
        private=True,
        **({"limit": LIMIT} if action == "extract" else {}),
    )
    expected = [
        "podman",
        "unshare",
        sys.executable,
        str(box.PACKAGE / "backup_archive.py"),
        action,
        str(path),
    ]
    if action == "extract":
        expected += ["--limit", str(LIMIT), "--owners"]
    require(action in ("create", "extract") and command == expected, "archive-command")
    engine_entry(root)
    return command


def transport(root, purpose, measured):
    paths = preflight(root, purpose)
    seed = namespace(root, purpose, "source", "seed", measured)
    host_seed = {name: item[:4] for name, item in seed.items()}
    require(host_manifest(paths["source"], measured) == host_seed, "seed-host-proof")
    namespace(root, purpose, "destination", "empty", measured)
    private = crypto.small(["age-keygen"])
    recipient = crypto.small(["age-keygen", "-y"], data=private).decode().strip()
    crypto.validate_recipients([recipient])
    ciphertext = root / "home.age"
    crypto.encrypt_stream(archive(root, purpose, "create"), recipient, ciphertext)
    # Seed is independently checked BEFORE capture, not just compared to a copy.
    require(
        namespace(root, purpose, "source", "inspect", measured) == seed
        and host_manifest(paths["source"], measured) == host_seed,
        "source-preservation",
    )
    namespace(root, purpose, "destination", "empty", measured)
    crypto.decrypt_stream(ciphertext, private, archive(root, purpose, "extract"))
    del private
    destination = namespace(root, purpose, "destination", "inspect", measured)
    require(
        host_manifest(paths["destination"], measured) == host_seed
        and destination == seed,
        "destination-proof",
    )
    require(
        namespace(root, purpose, "source", "inspect", measured) == seed
        and host_manifest(paths["source"], measured) == host_seed,
        "source-preservation",
    )
    return {
        "ciphertext": crypto.digest(ciphertext),
        "source_manifest_sha256": fingerprint(seed),
        "destination_manifest_sha256": fingerprint(destination),
        "checks": {
            "fixed_seed": True,
            "host_and_namespace_ownership": True,
            "destination_matches_fixed_and_source": True,
            "source_preserved": True,
            "roots_private_0_0": True,
            "bounded_production_stream": True,
        },
    }


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def create_volumes(root, purpose):
    # Refuse BOTH pre-existing names before creating either one.
    for item in purpose["roles"].values():
        result = engine(root, purpose, volume_command("exists", item), check=False)
        require(result.returncode == 1, "volume-preexisting")
    for role, item in purpose["roles"].items():
        engine(root, purpose, volume_command("create", item))
        inspect_volume(root, purpose, role)
        global_fixture.write_json_new(root / ("home-created-" + role + ".json"), item)
    preflight(root, purpose)
    global_fixture.write_json_new(root / "home-ready.json", {"roles": purpose["roles"]})


def cleanup(root, current):
    purpose = validate_purpose(root, current)  # all file/source checks first
    global_fixture.require_runtime_boot(root)  # empty registry must NOT waive this
    for role, item in purpose["roles"].items():
        require(
            global_fixture.private_json(root, "home-created-" + role + ".json") == item,
            "creation-journal",
        )
    require(
        global_fixture.private_json(root, "home-ready.json")
        == {"roles": purpose["roles"]},
        "creation-journal",
    )
    validate_private_store(root)
    preflight(
        root, purpose
    )  # BOTH candidates + unexpected same-label volumes BEFORE first rm
    same_source(current)
    for item in purpose["roles"].values():
        engine(root, purpose, volume_command("rm", item))  # deliberately no force
    return {
        "removed_exact_volumes": True,
        "files_and_ciphertext_retained": True,
        "run_source": purpose["source"],
    }


def run_fresh(root, args, current):
    global_fixture.prepare_root(
        root, "tumbleweed"
    )  # compatibility layout; NO workspaces
    native = shutil.which(
        "podman", path=box.maintenance_host_env().get("PATH", os.defpath)
    )
    require(native is not None, "native-podman")
    global_fixture.prepare_podman_cli(root, Path(native).resolve(strict=True))
    purpose = write_purpose(root, current)
    with patch.dict(os.environ, global_fixture.isolated_env(root), clear=True):
        with global_fixture.temporary_location(root, args.temp_dir):
            validate_purpose(root, current)
            validate_private_store(root, initial=True)
            measured = mapping(
                json.loads(
                    engine(
                        root,
                        purpose,
                        ["unshare", sys.executable, "-B", "-c", MAP_PROGRAM],
                    ).stdout
                )
            )
            global_fixture.write_json_new(
                root / "home-mapping.json",
                {"sha256": fingerprint(measured), "covers_0_1": True},
            )
            create_volumes(
                root, purpose
            )  # mapping refusal precedes seeding AND volume creation
            return transport(root, purpose, measured)


def category(error):
    # Only fixture require() literals enter FixtureError; other exception text is
    # never retained (including crypto argv, private stderr, keys or environment).
    if isinstance(error, global_fixture.FixtureError) and re.fullmatch(
        r"[a-z][a-z-]{1,48}", str(error)
    ):
        return str(error)
    return global_fixture.safe_error_class(error)


def execute(root, mode, current, operation):
    primary = None
    secondary = []
    proof = {}
    with invocation_source(current), lifecycle(root):
        try:
            with global_fixture.private_output():
                proof = operation()
        except BaseException as error:
            primary = category(error)
        try:
            same_source(current)
        except BaseException as error:
            if primary is None:
                primary = category(error)
            else:
                secondary.append(category(error))
        report = {
            "format": FORMAT,
            "schema": 1,
            "scope": "shared-home-component-only",
            "mode": mode,
            "source": current,
            "outcome": "failed" if primary else "passed",
            "primary_failure": primary,
            "secondary_failures": secondary,
            "proof": proof,
        }
        try:
            global_fixture.write_json_new(
                root / ("home-" + mode + "-" + secrets.token_hex(8) + "-report.json"),
                report,
            )
        except BaseException as error:
            # Never replace the primary result with a late reporting exception.
            secondary.append(category(error))
            primary = primary or "report-failed"
        print(
            "home component: outcome="
            + ("failed" if primary else "passed")
            + " category="
            + (primary or "component-only")
            + (" secondary=" + ",".join(secondary) if secondary else ""),
            flush=True,
        )
    return 1 if primary else 0


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "home component: category=invalid-options\n")


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--cleanup", type=Path, metavar="EXACT_ROOT")
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        global_fixture.host_guard()  # before placement, source/native or engine
        require(sys.platform == "linux", "host-only")
        require(sys.flags.optimize == 0, "optimization-refused")
        require(
            (args.cleanup is None and args.work_dir is not None)
            or (
                args.cleanup is not None
                and args.work_dir is None
                and args.temp_dir is None
            ),
            "invalid-options",
        )
        # Bound child crash output as well as Python; no secret material in cores.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if args.cleanup is None:
            parent = fresh_parent(args.work_dir)
            if args.temp_dir is not None:
                fresh_parent(args.temp_dir)
        current = source_snapshot()
        if args.cleanup is not None:
            root = args.cleanup
            # validate placement/purpose BEFORE opening a retained lifecycle lock.
            validate_purpose(root, current)
            global_fixture.require_runtime_boot(root)

            def operation():
                with patch.dict(
                    os.environ, global_fixture.isolated_env(root), clear=True
                ):
                    with global_fixture.temporary_location(root):
                        return cleanup(root, current)

            return execute(root, "cleanup", current, operation)
        root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent))
        root.chmod(0o700)
        print("Retained shared-home component root: " + str(root), flush=True)
        return execute(root, "run", current, lambda: run_fresh(root, args, current))
    except BaseException as error:
        print("home component: category=" + category(error), file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    sys.exit(main())
