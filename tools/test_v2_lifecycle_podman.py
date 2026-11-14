#!/usr/bin/env python3
"""Explicit V2-only HOST lifecycle acceptance; never a legacy six-stage pass.

Requires a regular rootless host, a clean source candidate and the separate,
unmodified pinned native executable (default 2.3.0). Uses private synthetic
history, config, selected encrypted vault, provider/MCP and Podman storage.
Full AND portable restores and reset run production paths. Resources and
encrypted evidence remain on failure; --cleanup removes verified resources
only. There is deliberately no resume/retry, V1 binary or fake migration.

Global-vault coverage belongs to the separate global-vault fixture: running
this profile neither runs nor establishes that gate. No interactive UI, real
provider, OAuth, network-containment or security acceptance is claimed.

CLI source binding assumes a trusted Python interpreter/site-packages and system
Git, a reviewed full-history checkout and no concurrent checkout writers. Invoke
this script directly, not through an unchecked package entrypoint. Before project
imports it rejects dirty/untracked source, checks tracked HEAD blobs and isolates
bytecode. Ordinary library imports establish no host-acceptance binding.
"""

import sys
import os

# Script-directory/PYTHONPATH entries inside the checkout must not shadow the
# stdlib used for preflight (for example tools/json.py). External interpreter
# startup/site-packages remain an explicit trusted prerequisite, not a sandbox.
if __name__ == "__main__":
    _startup_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    sys.path[:] = [
        item
        for item in sys.path
        if item
        and os.path.commonpath((os.path.realpath(item), _startup_root)) != _startup_root
    ]

import argparse
import hashlib
import importlib.machinery
import json
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import types
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
_CLI_SOURCE = None
_CLI_CACHE = None
_SOURCE_REQUIRED = frozenset(
    {
        "src/oc_box/__init__.py",
        "src/oc_box/oc_box.py",
        "src/oc_box/credentials.py",
        "tools/test_v2_lifecycle_podman.py",
        "tools/test_v2_podman.py",
        "tools/test_global_vault_podman.py",
        "tools/native_host_profile.py",
    }
)
_EGG_METADATA = frozenset(
    {
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    }
)


class SourceError(Exception):
    """Fixed pre-import source refusal; never expose Git output or paths."""


def _source_require(condition, category):
    if not condition:
        raise SourceError(category)


def _early_host_guard():
    _source_require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "host-only",
    )


def _source_path(path):
    path = Path(path).absolute()
    _source_require(
        ".." not in path.parts and not any(c in str(path) for c in "\0\r\n"),
        "source-path",
    )
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        _source_require(not stat.S_ISLNK(info.st_mode), "source-path")
    return path


def _source_git(source, *arguments):
    # Never use fixture selectors, ambient Git credential/config overrides or
    # executable hooks/fsmonitor from the candidate's repository configuration.
    binary = shutil.which("git", path=os.defpath)
    _source_require(binary and Path(binary).is_absolute(), "source-git")
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


def _source_snapshot(source):
    """Pure stdlib/Git verification against HEAD, not the mutable Git index."""
    source = _source_path(source)
    commit = (
        _source_git(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    )
    _source_require(
        re.fullmatch(r"[0-9a-f]{40}", commit) is not None
        and _source_git(source, "rev-parse", "--show-toplevel").decode().strip()
        == str(source)
        and _source_git(source, "rev-parse", "--is-shallow-repository").strip()
        == b"false"
        and not _source_git(source, "status", "--porcelain", "--untracked-files=all"),
        "clean-source-required",
    )
    ignored = _source_git(
        source,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    for item in filter(None, ignored):
        relative = item.decode()
        # A fresh pycache_prefix makes these ignored caches unreadable to the
        # importer. Legacy sourceless .pyc and ignored source/extensions refuse.
        _source_require(
            (relative.endswith(".pyc") and "/__pycache__/" in "/" + relative)
            or (
                relative.startswith("src/oc_box.egg-info/")
                and relative.rsplit("/", 1)[-1] in _EGG_METADATA
            ),
            "ignored-source",
        )
        _source_path(source / relative)
    records = _source_git(source, "ls-tree", "-r", "-z", "HEAD")
    hashes = {}
    for record in filter(None, records.split(b"\0")):
        header, name = record.split(b"\t", 1)
        mode, kind, blob = header.split()
        relative = name.decode()
        _source_require(
            mode in (b"100644", b"100755") and kind == b"blob", "source-entry"
        )
        path = _source_path(source / relative)
        actual_mode = path.stat().st_mode
        _source_require(
            stat.S_ISREG(actual_mode)
            and bool(actual_mode & 0o111) == (mode == b"100755"),
            "source-entry",
        )
        content = path.read_bytes()
        actual = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content
        ).hexdigest()
        _source_require(actual.encode() == blob, "source-changed")
        hashes[relative] = hashlib.sha256(content).hexdigest()
    _source_require(_SOURCE_REQUIRED <= hashes.keys(), "source-incomplete")
    return {
        "commit": commit,
        "commit_count": int(_source_git(source, "rev-list", "--count", "HEAD")),
        "sha256": hashes,
    }


def _source_import_guard(source, tracked):
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name == "tools" or name.startswith("tools."):
            expected = source / "tools"
        elif name == "oc_box" or name.startswith("oc_box."):
            expected = source / "src/oc_box"
        else:
            expected = None
        locations = [filename] if filename else list(getattr(module, "__path__", ()))
        if expected is not None:
            _source_require(
                locations
                and all(
                    Path(item).resolve().is_relative_to(expected) for item in locations
                ),
                "source-shadowing",
            )
        if filename:
            resolved = Path(filename).resolve()
            if resolved.is_relative_to(source):
                _source_require(
                    resolved.relative_to(source).as_posix() in tracked,
                    "source-shadowing",
                )


def _prepare_cli_imports(source):
    global _CLI_CACHE
    _early_host_guard()  # Before Git, project import, placement or native/engine calls.
    identity = _source_snapshot(source)
    _source_import_guard(source, identity["sha256"])
    _CLI_CACHE = tempfile.TemporaryDirectory(prefix="oc-box-v2-only-bytecode-")
    sys.dont_write_bytecode = True
    sys.pycache_prefix = _CLI_CACHE.name
    # tools is a namespace in this reviewed checkout. Do not let an unrelated
    # site-packages tools/__init__.py run while resolving its checked modules.
    _source_require("tools/__init__.py" not in identity["sha256"], "source-namespace")
    if "tools" not in sys.modules:
        namespace = types.ModuleType("tools")
        namespace.__path__ = [str(source / "tools")]
        namespace.__package__ = "tools"
        namespace.__spec__ = importlib.machinery.ModuleSpec(
            "tools", None, is_package=True
        )
        sys.modules["tools"] = namespace
    return identity


if __name__ == "__main__":
    try:
        _CLI_SOURCE = _prepare_cli_imports(APP)
    except BaseException as error:
        print(
            "V2-only lifecycle pre-import refusal: "
            + (str(error) if isinstance(error, SourceError) else "source-preflight")
            + ".",
            file=sys.stderr,
        )
        raise SystemExit(130 if isinstance(error, KeyboardInterrupt) else 1) from None

sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from tools import test_v2_podman as legacy
from tools import test_global_vault_podman as safety
from tools import native_host_profile as profiles

if _CLI_SOURCE is not None:
    try:
        _source_import_guard(APP, _CLI_SOURCE["sha256"])
        _source_require(_source_snapshot(APP) == _CLI_SOURCE, "source-changed")
    except BaseException:
        print("V2-only lifecycle pre-import refusal: source-imports.", file=sys.stderr)
        raise SystemExit(1) from None

box, backups, crypto = legacy.box, legacy.backups, legacy.crypto
vault, credentials, compat = legacy.vault, legacy.credentials, legacy.compat
workspace_service = legacy.workspace_service
require = safety.require
PREFIX = "oc-box-v2-only-host-"
FORMAT = "oc-box-v2-only-lifecycle-host-acceptance"
VERSIONS = profiles.NATIVE_VERSIONS
PROJECTS = ("source-a", "restored-full", "restored-portable")
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
HOME_FILES = (".local/share/opencode/oc-box-v2.db", "acceptance-home.txt")
OS_FILE = "/etc/oc-box-v2-fixture"
DIRECTORIES = (
    "host-home",
    "config/opencode",
    "config/oc-box",
    "config/containers",
    "state",
    "cache",
    "registry/workspaces",
    "temporary",
    "podman-run",
    "podman-tmp",
    "data/libpod",
    "data/containers/storage/volumes",
    "bin",
    "source-a",
)


def binary_hash(binary):
    return crypto.digest(binary)["sha256"]


def prepare_native_cli(root, binary, version):
    require(version in VERSIONS, "native-profile")
    binary = Path(binary).resolve(strict=True)
    require(binary.is_file() and os.access(binary, os.X_OK), "native-executable")
    wrapper = safety.prepare_native_cli(root, binary, native_version=version)
    profile = safety.recorded_native(root, expected=version, binary=binary)
    # Keep canonical native-profile.json byte/schema compatibility. The selector
    # interpreter is separate so cleanup need not use the preparation Python.
    safety.write_json_new(root / "selector.json", {"interpreter": sys.executable})
    return wrapper, profile


def prepare_root(root, distro, podman):
    for relative in DIRECTORIES:
        if relative == "config/containers":
            continue  # The shared storage writer creates this directory itself.
        (root / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
    legacy.storage_config(root)
    (root / "config/containers/containers.conf").write_text(
        safety.containers_config(root)
    )
    safety.write_json_new(
        root / "fixture.json",
        {
            "format": FORMAT,
            "version": 1,
            "projects": list(PROJECTS),
            "distro": distro,
        },
    )
    safety.write_json_new(root / "fixture-ids.json", {})
    safety.write_json_new(
        root / "config/oc-box/config.json",
        {
            "default_distro": distro,
            "auto_update": False,
        },
    )
    (root / "source-a/unfinished.txt").write_text("PROJECT_RETAINED\n")
    safety.prepare_podman_cli(root, podman)


def workspace_records(root):
    """Validate metadata and all project/resource identities BEFORE Podman use."""
    records = {}
    directory = safety.safe_path(root, "registry/workspaces", directory=True)
    profile = safety.private_json(root, "native-profile.json")
    for folder in sorted(directory.iterdir()):
        require(re.fullmatch(r"[0-9a-f]{32}", folder.name) is not None, "workspace-id")
        safety.no_symlinks(folder)
        path = folder / "workspace.json"
        data = safety.private_json(root, path.relative_to(root))
        require(isinstance(data, dict), "workspace-metadata")
        project = data.get("project")
        require(isinstance(project, str), "workspace-project")
        project = safety.no_symlinks(project)
        name = project.name
        require(
            project == root / name and name in PROJECTS and name not in records,
            "workspace-project",
        )
        incomplete = bool(data.get("restore_incomplete"))
        safety.safe_path(root, name, directory=True, optional=incomplete)
        require(
            data.get("id") == folder.name
            and data.get("version") == 2
            and data.get("container") == "oc-box-" + folder.name
            and data.get("home_volume") == "oc-box-home-" + folder.name
            and data.get("home") == str(root / "host-home")
            and data.get("distro")
            == safety.private_json(root, "fixture.json")["distro"]
            and data.get("opencode_version", profile["version"]) == profile["version"]
            and not data.get("devices")
            and not data.get("podman_args")
            and box.container_project(data) == str(root / "source-a"),
            "workspace-allowlist",
        )
        if "credentials" in data:
            require(
                data["credentials"]
                == {
                    "mode": "selected",
                    "env": [],
                    "vault": [legacy.KEY],
                },
                "workspace-policy",
            )
        if data.get("restore_staging_project"):
            staging = safety.no_symlinks(data["restore_staging_project"])
            require(
                staging.parent == root and staging.name.startswith(".oc-box-restore-"),
                "restore-staging",
            )
        for child in folder.rglob("*"):
            safety.no_symlinks(child)
        if project.exists() and not incomplete:
            marker = safety.safe_path(root, Path(name) / box.MARKER)
            require(marker.read_text().strip() == folder.name, "workspace-marker")
        records[name] = (path, data)
    for name in PROJECTS[1:]:
        if name in records:
            require("source-a" in records, "restore-origin")
            origin = records[name][1].get("restored_from", {})
            require(
                origin.get("workspace_id") == records["source-a"][1]["id"]
                and origin.get("kind") == name.removeprefix("restored-"),
                "restore-origin",
            )
    return records


def validate_fixture(root, *, allow_unrecorded=False):
    root = safety.no_symlinks(root)
    require(root.name.startswith(PREFIX) and root.is_dir(), "fixture-root")
    info = root.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "fixture-root")
    root = root.resolve(strict=True)
    metadata = safety.private_json(root, "fixture.json")
    require(
        isinstance(metadata, dict)
        and metadata.get("distro") in box.DISTROS
        and metadata
        == {
            "format": FORMAT,
            "version": 1,
            "projects": list(PROJECTS),
            "distro": metadata["distro"],
        },
        "fixture-format",
    )
    for relative in DIRECTORIES:
        safety.safe_path(root, relative, directory=True)
    require(
        safety.safe_path(root, "config/containers/storage.conf").read_text()
        == safety.expected_storage(root),
        "storage-config",
    )
    require(
        safety.safe_path(root, "config/containers/containers.conf").read_text()
        == safety.containers_config(root),
        "storage-config",
    )
    mapping = safety.private_json(root, "podman-executable.json")
    require(
        isinstance(mapping, dict) and set(mapping) == {"podman", "interpreter"},
        "native-podman",
    )
    for binary in mapping.values():
        safety.validate_native_podman(binary, root)
    require(
        safety.safe_path(root, "bin/podman").read_text()
        == safety.podman_wrapper_source(mapping["interpreter"]),
        "podman-selector",
    )
    safety.safe_path(root, "native-profile.json")  # No legacy missing-profile fallback.
    profile = safety.recorded_native(root)
    binary = Path(profile["path"])
    require(
        binary.is_absolute()
        and binary.resolve(strict=True) == binary
        and binary.is_file()
        and os.access(binary, os.X_OK)
        and binary_hash(binary) == profile["sha256"],
        "native-executable-changed",
    )
    require(
        safety.private_json(root, "executables.json") == {"2": str(binary)},
        "native-selector",
    )
    selector = safety.private_json(root, "selector.json")
    require(
        isinstance(selector, dict)
        and set(selector) == {"interpreter"}
        and isinstance(selector["interpreter"], str)
        and Path(selector["interpreter"]).is_absolute()
        and Path(selector["interpreter"]).resolve(strict=True).is_file(),
        "native-selector",
    )
    require(
        safety.safe_path(root, "bin/opencode").read_text()
        == safety.native_wrapper_source(selector["interpreter"]),
        "native-selector",
    )
    records = workspace_records(root)
    ids = safety.private_json(root, "fixture-ids.json")
    actual = {name: data["id"] for name, (_, data) in records.items()}
    require(
        isinstance(ids, dict)
        and all(actual.get(name) == ident for name, ident in ids.items()),
        "fixture-ids",
    )
    require(allow_unrecorded or actual == ids, "unrecorded-journal")
    return root


def validate_store_info(root, info, temporary):
    """Same fixed independent store proof, without the global profile's schema."""
    require(isinstance(info, dict), "private-store")
    host = info.get("host", {})
    require(host.get("security", {}).get("rootless") is True, "rootless")
    require(host.get("serviceIsRemote") is False, "remote-engine")
    store = info.get("store", {})
    require(store.get("graphDriverName") == "overlay", "storage-driver")
    for field, relative in (
        ("graphRoot", "data/containers/storage"),
        ("runRoot", "podman-run"),
        ("volumePath", "data/containers/storage/volumes"),
    ):
        expected = safety.safe_path(root, relative, directory=True)
        require(
            isinstance(store.get(field), str)
            and Path(store[field]) == expected
            and safety.no_symlinks(store[field]).resolve(strict=True) == expected,
            "private-store",
        )
    if "configFile" in store:
        require(
            isinstance(store["configFile"], str)
            and safety.no_symlinks(store["configFile"])
            == root / "config/containers/storage.conf",
            "store-info-shape",
        )
    else:
        require(
            info.get("version", {}).get("Version")
            in safety.PODMAN_CONFIGFILE_OMISSIONS,
            "store-info-shape",
        )
        validate_fixture(root, allow_unrecorded=True)
    temporary = safety.no_symlinks(temporary)
    info = temporary.stat()
    require(
        temporary.is_dir() and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "temporary-path",
    )
    require(
        isinstance(store.get("imageCopyTmpDir"), str)
        and Path(store["imageCopyTmpDir"]) == temporary
        and safety.no_symlinks(store["imageCopyTmpDir"]).resolve(strict=True)
        == temporary.resolve(strict=True),
        "image-temporary-path",
    )


def validate_store(root, *, fresh=False, allow_unrecorded=False, temporary=None):
    validate_fixture(root, allow_unrecorded=allow_unrecorded)
    records = workspace_records(root)
    safety.require_runtime_boot(root, allow_uninitialized=fresh and not records)
    env = safety.isolated_env(root)
    temporary = safety.existing_directory(temporary or root / "temporary")
    info = temporary.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "temporary-path")
    env["TMPDIR"] = str(temporary)
    with patch.dict(os.environ, env, clear=True):
        for selected in (env, box.maintenance_host_env(), crypto.environment()):
            result = box.run(
                ["podman", "info", "--format", "json"],
                env=selected,
                stdin=subprocess.DEVNULL,
                capture=True,
                private=True,
                timeout=30,
            )
            validate_store_info(root, json.loads(result.stdout), selected["TMPDIR"])
    # Once initialized, all later operations require the actual matching marker.
    safety.require_runtime_boot(root)


def record_ids(root):
    records = workspace_records(root)
    actual = {name: data["id"] for name, (_, data) in records.items()}
    previous = safety.private_json(root, "fixture-ids.json")
    require(
        all(actual.get(name) == ident for name, ident in previous.items()),
        "fixture-ids",
    )
    box.atomic_json(root / "fixture-ids.json", actual)
    return actual


def source_identity():
    _source_require(_CLI_SOURCE is not None, "cli-preflight-required")
    checked = _source_snapshot(APP)
    _source_require(checked == _CLI_SOURCE, "source-changed")
    _source_import_guard(APP, checked["sha256"])
    build = legacy.get_version_info(APP)
    _source_require(
        build.get("commit") == checked["commit"]
        and build.get("commit_count") == checked["commit_count"]
        and build.get("dirty") is False,
        "source-version",
    )
    return {
        "build": build,
        "sha256": checked["sha256"],
        "binding": "CLI pre-import HEAD blobs, clean full history, isolated bytecode; trusted interpreter/Git and no concurrent writers required",
    }


def new_report(version):
    require(version in VERSIONS, "native-profile")
    return {
        "format": FORMAT,
        "version": 1,
        "profile": "v2-only",
        "requested_native_version": version,
        "native_versions": {"2": version},
        "status": "failed",
        "checks": {name: False for name in CHECKS},
        "excluded": list(EXCLUDED),
        "credential_scope": "selected",
        "global_vault_fixture_ran": False,
        "legacy_fixture_ran": False,
        "limits": "Scripted synthetic rootless host lifecycle only. No legacy six-stage acceptance.",
    }


def home_snapshot(data):
    result = {name: legacy.digest_home(data, name) for name in HOME_FILES}
    volume = backups.owned_volume(box, data)
    require(volume is not None, "private-home")
    # Hash names, kinds, link targets, ownership/mode and every regular file
    # without following symlinks or printing private names/content. mtimes are
    # deliberately excluded: archive precision is not byte preservation.
    program = """import hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1]); info=root.lstat()
entries=[['.',stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid]]
def fail(error): raise error
def checksum(path):
 digest=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
 return digest.hexdigest()
for parent,dirs,files in os.walk(root,followlinks=False,onerror=fail):
 for name in sorted(dirs+files):
  path=pathlib.Path(parent,name); info=path.lstat()
  entry=[str(path.relative_to(root)),stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid]
  if stat.S_ISREG(info.st_mode):
   entry.append(checksum(path))
  elif stat.S_ISLNK(info.st_mode): entry.append(os.readlink(path))
  elif not stat.S_ISDIR(info.st_mode): raise ValueError('unsupported-home-entry')
  entries.append(entry)
print(hashlib.sha256(json.dumps(sorted(entries),ensure_ascii=True,separators=(',',':')).encode()).hexdigest())
"""
    digest = (
        crypto.small(
            [
                "podman",
                "unshare",
                sys.executable,
                "-B",
                "-c",
                program,
                volume["Mountpoint"],
            ]
        )
        .decode()
        .strip()
    )
    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "private-home-digest")
    result["complete-home-tree"] = digest
    return result


def project_payload(project):
    return {
        name: digest
        for name, digest in safety.tree_digest(project).items()
        if name != box.MARKER
    }


def container_python(data, program, *arguments):
    return box.run(
        box.exec_command(data, ["python3", "-c", program, *arguments]),
        input="",
        capture=True,
        private=True,
        timeout=30,
    )


def sentinel_check_program(os_file=OS_FILE):
    return (
        "import os,pathlib,sys; "
        "assert pathlib.Path(sys.argv[1],'unfinished.txt').read_text()=='PROJECT_RETAINED\\n'; "
        "assert pathlib.Path(os.environ['HOME'],'acceptance-home.txt').read_text()=='HOME_RETAINED\\n'; "
        "sentinel=pathlib.Path(" + repr(str(os_file)) + "); "
        "assert (sentinel.read_text()=='OS_RETAINED\\n') if sys.argv[2]=='full' else not sentinel.exists()"
    )


def prepare_launch(root, binary, version, report):
    env = safety.isolated_env(root)
    env["TMPDIR"] = str(
        safety.existing_directory(os.environ.get("TMPDIR", str(root / "temporary")))
    )
    with patch.dict(os.environ, env, clear=True):
        wrapper, profile = prepare_native_cli(root, binary, version)
        report["native"] = profile
        # A refusal here must not reach backend probes, registry resources,
        # native history import or owning operations.
        validate_store(root, fresh=True, temporary=Path(env["TMPDIR"]))
        box.check_backend(feature="launch", env=env)
        crypto.tools()
        vault.crypto()
        report["checks"]["private-store-native-selector"] = True
        baseline = safety.seed_native_history(root, binary)
        report["checks"]["native-v2-host-seed"] = True
    return wrapper, profile, baseline, env


def run(root, binary, version, distro, report):
    safety.host_guard()
    source = source_identity()
    require(
        source["build"].get("commit") and source["build"].get("dirty") is False,
        "clean-source-required",
    )
    report["source"] = source
    wrapper, profile, baseline, env = prepare_launch(root, binary, version, report)
    password = secrets.token_urlsafe(32)
    state = {
        "key": "fixture-" + secrets.token_urlsafe(24),
        "markers": set(),
        "requests": 0,
        "errors": [],
    }
    server, worker = legacy.make_provider(state)
    active = {"name": "source-a", "stage": "first", "required": baseline}
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(vault, "vault_path", return_value=root / "vault/vault.json"),
            patch.object(Path, "home", return_value=root / "host-home"),
            patch.object(
                box,
                "detect_opencode_release",
                side_effect=lambda: legacy.check_native_version(
                    root, wrapper, "2", expected=version
                ),
            ),
            patch.object(
                box,
                "host_command",
                side_effect=lambda data, *args: compat.command(
                    data.get("opencode_version", version), str(wrapper), args
                ),
            ),
        ):
            safety.write_json_new(
                root / "config/opencode/opencode.json",
                legacy.fixture_config(server.server_port),
            )
            with vault.locked(vault.vault_path(), create=True):
                vault.write_encrypted(
                    vault.vault_path(),
                    {
                        legacy.KEY: state["key"],
                        legacy.UNSELECTED: "fixture-unselected-"
                        + secrets.token_urlsafe(24),
                    },
                    password,
                )
            path, data = box.create_workspace(root / "source-a", distro)
            credentials.configure(data, mode="selected", secrets=[legacy.KEY])
            box.atomic_json(path, data)
            record_ids(root)

            def owner(command):
                records = workspace_records(root)
                current_path, current = records[active["name"]]
                safety.verify_owned_resources(root, current)
                require(current.get("opencode_version") == version, "workspace-version")
                require("--auto" not in command, "client-autoaccept")
                ident = safety.selected_session(
                    command, current_path, baseline["info"]["id"]
                )
                connection = workspace_service.live_connection()
                require(connection is not None, "private-owner")
                previous = box.export_container_session(current, ident)
                legacy.verify_v2_progress(previous, active["required"])
                if active["stage"] == "first":
                    require(
                        previous["messages"] == baseline["messages"], "first-import"
                    )
                    report["checks"]["native-v2-host-import"] = True
                state["markers"].clear()
                native = legacy.run_fixture_session(
                    current, ident, state, server.server_port
                )
                require(
                    not state["errors"]
                    and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
                    and "HOST_V2_ACCEPTANCE_OK" in native.stdout,
                    "native-tools",
                )
                transcript = box.export_container_session(current, ident)
                legacy.verify_v2_progress(transcript, previous)
                require(
                    "HOST_V2_ACCEPTANCE_OK" in json.dumps(transcript), "native-history"
                )
                with (
                    patch.object(
                        vault, "hidden", side_effect=AssertionError("attach-unlock")
                    ),
                    patch.object(
                        box,
                        "launch_credentials",
                        side_effect=AssertionError("attach-refresh"),
                    ),
                    workspace_service.attach(box, current_path, current) as attached,
                ):
                    require(attached["session"] == ident, "attach-session")
                    workspace_service.api(box, current, attached, "GET", "/api/info")
                if active["stage"] == "first":
                    container_python(
                        current,
                        "import os,pathlib; pathlib.Path(os.environ['HOME'],'acceptance-home.txt').write_text('HOME_RETAINED\\n'); pathlib.Path("
                        + repr(OS_FILE)
                        + ").write_text('OS_RETAINED\\n')",
                    )
                else:
                    container_python(
                        current,
                        sentinel_check_program(),
                        box.container_project(current),
                        "full"
                        if active["stage"] in ("restart", "restored-full")
                        else "portable",
                    )
                active["transcript"] = transcript
                return 0

            def start(name, stage, *, reset=False):
                active.update(name=name, stage=stage)
                current_path, current = workspace_records(root)[name]
                with (
                    patch.object(vault, "hidden", return_value=password) as hidden,
                    patch.object(box, "run_tui", side_effect=owner),
                ):
                    safety.launcher(
                        ["reset" if reset else "start", "--workspace", current["id"]]
                    )
                require(hidden.call_count == 1, "owning-unlock-count")
                current_path, current = workspace_records(root)[name]
                safety.verify_owned_resources(root, current, stopped=True)
                require(
                    box.session_state(current_path)["last"] == baseline["info"]["id"],
                    "stopped-session",
                )
                require(
                    legacy.KEY not in os.environ
                    and not credentials.has_unlocked_values(),
                    "host-vault-retention",
                )
                return current_path, current

            path, data = start("source-a", "first")
            report["checks"]["first-owner-stopped-selected-tools"] = True
            active["required"] = active["transcript"]
            state["key"] = "fixture-" + secrets.token_urlsafe(24)
            with vault.locked(vault.vault_path()):
                vault.write_encrypted(
                    vault.vault_path(),
                    {
                        legacy.KEY: state["key"],
                        legacy.UNSELECTED: "fixture-unselected",
                    },
                    password,
                )
            path, data = start("source-a", "restart")
            report["checks"]["same-session-restart-rotation-attach"] = True
            active["required"] = active["transcript"]
            saved = home_snapshot(data)
            source_tree = safety.tree_digest(root / "source-a")
            source_selection = box.session_state(path)
            original_container = safety.verify_owned_resources(
                root, data, stopped=True
            )["Id"]
            identity = root / "age-identity.txt"
            descriptor = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(crypto.small(["age-keygen"]))
            recipient = (
                crypto.small(["age-keygen", "-y", str(identity)]).decode().strip()
            )
            for portable in (False, True):
                kind = "portable" if portable else "full"
                output, destination = (
                    root / (kind + ".ocbox"),
                    root / ("restored-" + kind),
                )
                with patch.object(
                    vault, "hidden", side_effect=AssertionError("backup-unlock")
                ):
                    backups.make_backup(
                        box,
                        argparse.Namespace(
                            project=root / "source-a",
                            workspace=None,
                            output=output,
                            portable=portable,
                            recipient=[recipient],
                        ),
                    )
                    backups.restore_backup(
                        box,
                        argparse.Namespace(
                            project=destination, file=output, identity=[identity]
                        ),
                    )
                record_ids(root)
                restored_path, restored = workspace_records(root)["restored-" + kind]
                safety.verify_owned_resources(root, restored, stopped=True)
                require(
                    restored["opencode_version"] == version
                    and restored["id"] != data["id"]
                    and restored["container"] != data["container"]
                    and restored["home_volume"] != data["home_volume"]
                    and box.container_project(restored) == str(root / "source-a")
                    and box.session_state(restored_path)["last"]
                    == baseline["info"]["id"]
                    and home_snapshot(restored) == saved,
                    "stopped-restore",
                )
                require(
                    project_payload(destination) == project_payload(root / "source-a"),
                    "restored-project",
                )
                start("restored-" + kind, "restored-" + kind)
                require(
                    home_snapshot(data) == saved
                    and safety.tree_digest(root / "source-a") == source_tree
                    and box.session_state(path) == source_selection
                    and safety.verify_owned_resources(root, data, stopped=True)["Id"]
                    == original_container,
                    "source-preservation",
                )
                report["checks"][kind + "-encrypted-restore"] = True
            with (
                patch.object(vault, "hidden", return_value=password + "-wrong"),
                patch.object(
                    box, "remove_resources", wraps=box.remove_resources
                ) as removal,
                patch.object(
                    box, "run_tui", side_effect=AssertionError("wrong-password-owner")
                ),
            ):
                try:
                    box.main(["reset", "--workspace", data["id"]])
                except box.BoxError as error:
                    require("Unable to unlock" in str(error), "wrong-password-category")
                else:
                    require(False, "wrong-password-accepted")
                require(removal.call_count == 0, "wrong-password-deletion")
            require(
                home_snapshot(data) == saved
                and safety.tree_digest(root / "source-a") == source_tree
                and safety.verify_owned_resources(root, data, stopped=True)["Id"]
                == original_container,
                "wrong-password-preservation",
            )
            report["checks"]["wrong-password-before-deletion"] = True
            _, reset = start("source-a", "reset", reset=True)
            require(
                safety.verify_owned_resources(root, reset, stopped=True)["Id"]
                != original_container,
                "reset-container",
            )
            require(
                safety.tree_digest(root / "source-a") == source_tree, "reset-project"
            )
            report["checks"]["reset-project-home-history-os"] = True
            report["checks"]["source-preserved"] = True
            report["fixture_ids"] = record_ids(root)
            require(all(report["checks"].values()), "incomplete-checks")
            require(source_identity() == source, "source-changed")
            require(
                binary_hash(binary) == profile["sha256"], "native-executable-changed"
            )
            report["synthetic_provider_requests"] = state["requests"]
            report["status"] = "passed"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def cleanup(root):
    safety.host_guard()
    root = validate_fixture(root, allow_unrecorded=True)
    # No engine command, even info, may touch stale retained runtime metadata.
    safety.require_runtime_boot(root)
    validate_store(root, allow_unrecorded=True)
    records = workspace_records(root)
    with patch.dict(os.environ, safety.isolated_env(root), clear=True):
        # Inspect the complete allowlist before removing the first resource.
        for path, data in records.values():
            with box.workspace_lock(path):
                box.refresh_workspace_identity(path, data)
                safety.verify_owned_resources(root, data, stopped=True)
        for path, data in records.values():
            with box.workspace_lock(path):
                box.refresh_workspace_identity(path, data)
                safety.verify_owned_resources(root, data, stopped=True)
                box.remove_resources(data)


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "V2-only lifecycle fixture refused: arguments.\n")


def main(argv=None):
    parser = QuietParser(description=__doc__)
    parser.add_argument("--opencode-v2", type=Path)
    parser.add_argument("--native-version", choices=VERSIONS, default="2.3.0")
    parser.add_argument("--distro", choices=tuple(box.DISTROS), default="tumbleweed")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--cleanup", type=Path)
    args = parser.parse_args(argv)
    root = None
    report_written = False
    report = new_report(args.native_version)
    try:
        safety.host_guard()
        with safety.private_output():
            if args.cleanup:
                require(
                    not any((args.opencode_v2, args.work_dir, args.temp_dir)),
                    "arguments",
                )
                cleanup(args.cleanup)
                return 0
            require(args.opencode_v2 is not None, "arguments")
            # npm/fnm bin entries are ordinary symlinks. Pin the selected real
            # executable, then validate its canonical path, never the alias.
            binary = safety.no_symlinks(
                args.opencode_v2.expanduser().resolve(strict=True)
            )
            require(
                binary.is_file() and os.access(binary, os.X_OK), "native-executable"
            )
            parent = safety.existing_directory(args.work_dir) if args.work_dir else None
            if args.temp_dir:
                safety.existing_directory(args.temp_dir)
            podman = shutil.which("podman")
            require(podman is not None, "native-podman")
            root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent))
            root.chmod(0o700)
            prepare_root(root, args.distro, Path(podman).resolve(strict=True))
            with legacy.temporary_location(root, args.temp_dir):
                run(root, binary, args.native_version, args.distro, report)
                require(
                    report["status"] == "passed" and all(report["checks"].values()),
                    "incomplete-report",
                )
    except BaseException as error:
        # Fixed category only; never stringify private argv/output/env/errors.
        category = safety.safe_error_class(error)
        report["status"] = "failed"
        report["failure"] = category
        print("V2-only lifecycle fixture failed: " + category + ".", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        if root is not None:
            try:
                safety.write_json_new(root / "result.json", report)
                report_written = True
            except BaseException:
                print(
                    "V2-only lifecycle report unavailable; fixture retained.",
                    file=sys.stderr,
                )
            print("V2-only lifecycle fixture retained: " + str(root), flush=True)
    if not report_written:
        return 1
    print(
        "V2-only lifecycle profile passed; excluded legacy/global/UI gates were not run."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
