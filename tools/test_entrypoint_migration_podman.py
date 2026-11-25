#!/usr/bin/env python3
r"""Opt-in real rootless Podman test, inside a fresh disposable VM (not oc-box).

Run as the VM's disposable unprivileged user, with normal Podman dependencies
and delegated resource controllers. For example:

    python3 -B tools/test_entrypoint_migration_podman.py \
      --fresh-isolated-guest --work-dir "$HOME/entrypoint-test" \
      --image docker.io/library/alpine:3.22

The work directory must not exist. For an offline run, use instead
--image-archive FILE (single-image `podman save` archive) or --rootfs-archive
FILE (root filesystem tar imported locally). Neither reads another store.
Use a trusted small image with /bin/sh, /bin/sleep (supporting `infinity`),
and /bin/{cat,chmod,chown,mkdir,stat}. The fixture removes only verified
resources in its store and retains a private report and store directories
for inspection. Dispose of the VM separately.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from oc_box import (
    entrypoint_migration,
    history_recovery,
    native_runtime_guard,
    oc_box as box,
    runtime_safeguard,
    workspace_control,
)


SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
HEX = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[0-9a-f]{32}")
IMAGE_REF = re.compile(
    r"(?:[a-z0-9.-]+\.[a-z0-9.-]+(?::[0-9]+)?|localhost(?::[0-9]+)?)/"
    r"(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]*|@sha256:[0-9a-f]{64})"
)
DB = ".local/share/opencode/opencode.db"
MAPPED = "mapped-owner"
PROJECT = "synthetic-project.txt"
SHADOW_RECEIPT = ".legacy-shadow-executed"
OS_MARKER = "/oc-box-fixture-os-marker"
SHADOW = "/opt/oc-box/runtime/bin/sleep"


class FixtureError(Exception):
    """Fixed fixture stage or failure category; no raw subprocess diagnostics."""


def require(condition, category):
    if not condition:
        raise FixtureError(category)


def source_identity():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/oc_box").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(os.fsencode(path.relative_to(ROOT)) + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "runtime_tree": digest.hexdigest(),
        "fixture": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def local_directory(path):
    require(
        path.is_absolute() and path.resolve(strict=True) == path and path.is_dir(),
        "noncanonical-directory",
    )
    return path


def make_root(path):
    path = path.expanduser().absolute()
    parent = local_directory(path.parent)
    info = parent.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o022, "work-parent")
    require(not any(c in str(path) for c in ":,\n\r"), "work-path")
    path.mkdir(mode=0o700)  # Deliberately no reuse, even if an old run looks empty.
    return path


def guest_preflight():
    require(os.getuid() == os.geteuid() and os.getuid() != 0, "rootless-user")
    require(
        os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "container-refused",
    )
    detector = shutil.which("systemd-detect-virt", path=SYSTEM_PATH)
    require(detector, "missing-virtualization-detector")
    for mode, expected in (("--container", 1), ("--vm", 0)):
        result = subprocess.run(
            [detector, mode, "--quiet"],
            env={"PATH": SYSTEM_PATH, "LANG": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        require(result.returncode == expected, "disposable-vm-required")
    podman = shutil.which("podman", path=SYSTEM_PATH)
    require(podman, "missing-local-podman")
    podman = Path(podman).resolve(strict=True)
    require(
        podman.stat().st_uid == 0 and not podman.stat().st_mode & 0o022, "podman-binary"
    )
    runtime = local_directory(Path("/run/user") / str(os.getuid()))
    info = runtime.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "user-runtime")
    return podman, runtime


def prepare_store(root, runtime):
    for name in (
        "graph",
        "run",
        "volumes",
        "libpod",
        "engine-tmp",
        "tmp",
        "host-home",
        "registry",
        "config",
        "data",
        "cache",
    ):
        (root / name).mkdir(mode=0o700)

    def quote(name):
        return json.dumps(str(root / name))

    storage = (
        '[storage]\ndriver = "overlay"\n'
        f"graphroot = {quote('graph')}\n"
        f"runroot = {quote('run')}\n"
    )
    containers = (
        '[engine]\nremote = false\nhooks_dir = []\nevents_logger = "none"\n'
        f"static_dir = {quote('libpod')}\n"
        f"tmp_dir = {quote('engine-tmp')}\n"
        f"volume_path = {quote('volumes')}\n"
        '[containers]\nnetns = "private"\nenv = []\nmounts = []\ndevices = []\n'
    )
    (root / "storage.conf").write_text(storage)
    (root / "containers.conf").write_text(containers)
    (root / "mounts.conf").touch(exist_ok=False)
    (root / "auth.json").write_text('{"auths":{}}\n')
    user = pwd.getpwuid(os.getuid()).pw_name
    env = {
        "PATH": SYSTEM_PATH,
        "LANG": "C",
        "USER": user,
        "LOGNAME": user,
        "HOME": str(root / "host-home"),
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "registry"),
        "OC_BOX_STATE_HOME": str(root / "registry"),
        "TMPDIR": str(root / "tmp"),
        "CONTAINERS_STORAGE_CONF": str(root / "storage.conf"),
        "CONTAINERS_CONF": str(root / "containers.conf"),
        "REGISTRY_AUTH_FILE": str(root / "auth.json"),
    }
    if (runtime / "bus").is_socket():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / 'bus'}"
    return env


def image_id(value):
    ident = value.removeprefix("sha256:")
    require(HEX.fullmatch(ident), "image-id")
    return ident


def manifest(directory):
    result = {}

    def scan(path):
        info = path.lstat()
        row = [
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
        ]
        if stat.S_ISDIR(info.st_mode):
            entry = row + ["directory"]
        else:
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_size <= 1024**2,
                "fixture-data-shape",
            )
            entry = row + ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
        result[str(path.relative_to(directory))] = entry
        if entry[5] == "directory":
            for child in sorted(path.iterdir()):
                require(len(result) < 16, "fixture-data-count")
                scan(child)

    scan(directory)
    return result


def seed_db(home):
    db = home / DB
    db.parent.mkdir(parents=True, mode=0o700)
    db.touch(mode=0o600, exist_ok=False)
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "CREATE TABLE fixture_conversations (id INTEGER PRIMARY KEY, message TEXT)"
        )
        conn.execute(
            "INSERT INTO fixture_conversations VALUES (1, 'synthetic conversation')"
        )


def check_db(home):
    uri = (home / DB).as_uri() + "?mode=ro&immutable=1"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        require(
            conn.execute("PRAGMA quick_check").fetchall() == [("ok",)],
            "sqlite-integrity",
        )
        require(
            conn.execute("SELECT id, message FROM fixture_conversations").fetchall()
            == [(1, "synthetic conversation")],
            "sqlite-conversation",
        )


class Engine:
    def __init__(self, path, podman, env):
        self.path, self.podman, self.env = path, podman, env
        self.verified = False
        self.projects = {}
        self.images = set()
        self.image_ref = None
        self.fail_commit = None
        self.failed_commits = 0
        self.real_commits = 0
        self.last_failure = None

    def run(self, *args, **kwargs):
        kwargs.setdefault("check", True)
        kwargs.setdefault("timeout", 30)
        kwargs.update(
            env=self.env, capture=True, private=True, stdin=subprocess.DEVNULL
        )
        result = box.run(
            [
                self.podman,
                "--remote=false",
                f"--default-mounts-file={self.path / 'mounts.conf'}",
                *args,
            ],
            **{**kwargs, "check": False},
        )
        if kwargs["check"] and result.returncode:
            self.last_failure = {"command": args[0], "exit": result.returncode}
            raise FixtureError("podman-command")
        return result

    def inspect(self, kind, selector, *, missing=False):
        r = self.run(kind, "inspect", selector, check=False)
        if r.returncode:
            if (
                missing
                and self.run(kind, "exists", selector, check=False).returncode == 1
            ):
                return None
            self.last_failure = {"command": f"{kind}-inspect", "exit": r.returncode}
            raise FixtureError("inspect")
        rows = json.loads(r.stdout)
        require(isinstance(rows, list) and len(rows) == 1, "inspect-shape")
        return rows[0]

    def listed(self, kind):
        commands = {
            "container": (
                "ps",
                "--all",
                "--external",
                "--no-trunc",
                "--format",
                "{{.ID}}",
            ),
            "volume": ("volume", "ls", "--format", "{{.Name}}"),
            "image": ("image", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"),
        }
        return set(self.run(*commands[kind]).stdout.split())

    def verify_store(self, *, fresh=False):
        info = json.loads(self.run("info", "--format=json").stdout)
        host, store = info["host"], info["store"]
        require(
            host["serviceIsRemote"] is False and host["security"]["rootless"] is True,
            "local-rootless",
        )
        for key, name in (
            ("graphRoot", "graph"),
            ("runRoot", "run"),
            ("volumePath", "volumes"),
        ):
            require(
                Path(store[key]) == local_directory(self.path / name), "private-store"
            )
        if "configFile" in store:
            require(
                Path(store["configFile"]) == self.path / "storage.conf",
                "storage-selector",
            )
        require(store.get("graphDriverName") == "overlay", "storage-driver")
        if fresh:
            require(
                all(not self.listed(kind) for kind in ("container", "volume", "image")),
                "nonempty-store",
            )
        self.verified = True
        return info["version"]["Version"]

    def conversion_podman(self, *args, **kwargs):
        forced_local = args[:1] == ("--remote=false",)
        if forced_local:
            args = args[1:]
        if args[0] in ("commit", "create", "rename") or args[:2] == ("container", "rm"):
            require(forced_local, "unscoped-conversion-mutation")
        # A conversion may inspect/replace the stopped source; it may never start,
        # exec in, or stop any container. All permitted calls are the real CLI
        # except the single confirmed nonzero commit reply for the rebuild case.
        require(
            args[0] in ("info", "ps", "create", "rename", "commit")
            or args[:2]
            in (
                ("container", "inspect"),
                ("container", "exists"),
                ("container", "rm"),
                ("volume", "inspect"),
                ("image", "inspect"),
                ("image", "exists"),
            ),
            "unexpected-conversion-command",
        )
        if args[0] == "create":
            require(
                not any(
                    arg == "--ulimit" or arg.startswith("--ulimit=") for arg in args
                ),
                "inherited-process-limits-replayed",
            )
        if args[0] == "commit":
            source, tag = args[-2:]
            require(HEX.fullmatch(source), "commit-source-id")
            item = self.inspect("container", source)
            workspace = item["Config"]["Labels"][box.LABEL]
            plan = self.projects[workspace]
            match = re.fullmatch(r"localhost/oc-box-entrypoint-([0-9a-f]{32})", tag)
            require(match and source == plan["source"], "commit-selector")
            plan["token"] = match[1]
            if self.fail_commit == source:
                require(
                    kwargs.get("check") is False, "commit-must-return-confirmed-status"
                )
                self.fail_commit = None
                self.failed_commits += 1
                return subprocess.CompletedProcess(args, 125, "", "")
            self.real_commits += 1
        r = self.run(*args, **{**kwargs, "check": False})
        if r.returncode and (kwargs.get("check", True) or args[0] == "commit"):
            self.last_failure = {"command": args[0], "exit": r.returncode}
        if kwargs.get("check", True) and r.returncode:
            raise box.BoxError("Fixture Podman command failed.")
        return r

    def prepare_image(self, args):
        if args.image is not None:
            self.image_ref = args.image
            self.run("pull", "--quiet", self.image_ref, timeout=300)
        else:
            archive = (
                (args.image_archive or args.rootfs_archive).expanduser().absolute()
            )
            require(
                archive.resolve(strict=True) == archive and archive.is_file(),
                "local-image-archive",
            )
            if args.image_archive:
                self.run("load", "--input", archive, timeout=300)
            else:
                self.image_ref = f"localhost/oc-box-entrypoint-base:{uuid.uuid4().hex}"
                self.run("import", archive, self.image_ref, timeout=300)
        images = {image_id(x) for x in self.listed("image")}
        require(len(images) == 1, "base-image-inventory")
        (ident,) = images
        require(
            image_id(self.inspect("image", self.image_ref or ident)["Id"]) == ident,
            "base-image-binding",
        )
        self.images.add(ident)
        return ident

    def cleanup(self, *, keep_base=None):
        # Revalidate the store before cleanup. Do not reset the store,
        # force-remove resources, or touch unrecognized objects.
        failures = []
        if not self.verified:
            return ["store-not-verified"]
        try:
            self.verify_store()
        except (FixtureError, box.BoxError, ValueError, KeyError, OSError):
            return ["cleanup-store-not-verified"]
        for kind, remove in (
            ("container", self.remove_container),
            ("volume", self.remove_volume),
            ("image", self.remove_image),
        ):
            try:
                selectors = self.listed(kind)
                keep = {
                    item
                    for item in selectors
                    if kind == "image" and image_id(item) == keep_base
                }
            except (FixtureError, box.BoxError, ValueError, KeyError, OSError):
                failures.append(f"cleanup-{kind}-inventory")
                continue
            # Remove fixture snapshots ahead of their base if both remain.
            for selector in sorted(
                selectors - keep, key=lambda x: x.removeprefix("sha256:") in self.images
            ):
                try:
                    remove(selector)
                except (FixtureError, box.BoxError, ValueError, KeyError, OSError):
                    failures.append(f"cleanup-{kind}")
            try:
                if self.listed(kind) != keep:
                    failures.append(f"remaining-{kind}")
            except (FixtureError, box.BoxError, ValueError, KeyError, OSError):
                failures.append(f"unverified-{kind}")
        return sorted(set(failures))

    def remove_container(self, ident):
        require(HEX.fullmatch(ident), "cleanup-container-id")
        row = self.inspect("container", ident)
        labels = row["Config"]["Labels"]
        wid = labels.get(box.LABEL)
        require(
            wid in self.projects and row["Id"] == ident, "cleanup-container-ownership"
        )
        plan = self.projects[wid]
        token = plan.get("token")
        if labels.get("io.oc-box.entrypoint-conversion") is None:
            require(
                plan.get("source") in (None, ident)
                and row["Name"] in (f"oc-box-{wid}", f"oc-box-entrypoint-{token}-old")
                and row["Config"]["Cmd"] == ["-c", "exec sleep infinity"],
                "cleanup-source-binding",
            )
        else:
            require(
                token
                and labels["io.oc-box.entrypoint-conversion"] == token
                and row["Name"] in (f"oc-box-{wid}", f"oc-box-entrypoint-{token}-new"),
                "cleanup-candidate-binding",
            )
        state = row["State"]
        if state["Running"] is True:
            self.run("stop", "--time", "3", ident)
        stopped = self.inspect("container", ident)["State"]
        require(
            stopped["Running"] is False and stopped["Paused"] is False,
            "cleanup-stopped",
        )
        self.run("container", "rm", ident)

    def remove_volume(self, name):
        wid = name.removeprefix("oc-box-home-")
        require(
            wid in self.projects and name == f"oc-box-home-{wid}", "cleanup-volume-name"
        )
        self.volume_identity(name, wid, unmounted=True)
        self.run("volume", "rm", name)

    def remove_image(self, value):
        ident = image_id(value)
        row = self.inspect("image", ident)
        require(image_id(row["Id"]) == ident, "cleanup-image-id")
        owned = ident in self.images or self.image_ref in (
            row.get("RepoTags") or []
        ) + (row.get("RepoDigests") or [])
        if not owned:
            token = (row["Config"].get("Labels") or {}).get(
                "io.oc-box.entrypoint-conversion"
            )
            owned = token is not None and any(
                p.get("token") == token for p in self.projects.values()
            )
        require(owned, "cleanup-image-ownership")
        self.run("image", "rm", ident)

    def volume_identity(self, name, wid, *, unmounted=False):
        row = self.inspect("volume", name)
        path = self.path / "volumes" / name / "_data"
        require(
            row["Driver"] == "local"
            and row["Name"] == name
            and row["Labels"].get(box.LABEL) == wid
            and not row.get("Options")
            and Path(row["Mountpoint"]) == path,
            "volume-identity",
        )
        if unmounted and not path.exists():
            return None  # An unused volume may not have initialized its payload.
        info = local_directory(path).stat()
        return path, (info.st_dev, info.st_ino, row["CreatedAt"])


def check_phase(report, mode, phase):
    report["phase"] = f"{mode}/{phase}"


def run_case(engine, root, base, mode, report):
    check_phase(report, mode, "setup")
    project = root / mode
    project.mkdir(mode=0o700)
    (project / PROJECT).write_text("synthetic project file\n")
    path, data = box.create_workspace(box.validate_project(project), "debian")
    wid, name, volume = data["id"], data["container"], data["home_volume"]
    require(TOKEN.fullmatch(wid), "workspace-id")
    data.update(image=base, ready=True)
    box.atomic_json(path, data, durable=True)
    engine.projects[wid] = {}
    engine.run("volume", "create", "--label", f"{box.LABEL}={wid}", volume)
    args = box.container_create_args(data, project)
    require(args[-1] == history_recovery.PASSIVE_COMMAND, "production-create-argv")
    source = engine.run(*args[:-1], "exec sleep infinity", timeout=60).stdout.strip()
    require(HEX.fullmatch(source), "source-id")
    engine.projects[wid]["source"] = source

    # This is the only intentional start of the legacy command. The first
    # writable-layer and shadow files do not exist yet.
    engine.run("start", source, timeout=60)
    home, volume_id = engine.volume_identity(volume, wid)
    home.chmod(0o700)
    seed_db(home)
    mapped = home / MAPPED
    mapped.write_text("synthetic mapped ownership\n")
    mapped.chmod(0o644)
    engine.run(
        "exec",
        source,
        "/bin/sh",
        "-ec",
        """
        test ! -e "$1"
        test ! -e "$2"
        /bin/chown 1:1 "$HOME/mapped-owner"
        printf 'fixture OS change\\n' > "$1"
        /bin/mkdir -p /opt/oc-box/runtime/bin
        printf '%s\\n' '#!/bin/sh' ': > .legacy-shadow-executed' 'exec /bin/sleep "$@"' > "$2"
        /bin/chmod 0755 "$2"
        test -x "$2" && test ! -e .legacy-shadow-executed
        """,
        "fixture",
        OS_MARKER,
        SHADOW,
    )
    expected_owner = "0:0:700\n0:0:600\n1:1:644\n"
    owner_args = (
        "/bin/sh",
        "-ec",
        '/bin/stat -c "%u:%g:%a" "$HOME" "$HOME/$1" "$HOME/$2"',
        "fixture",
        DB,
        MAPPED,
    )
    require(
        engine.run("exec", source, *owner_args).stdout == expected_owner,
        "source-namespace-owners",
    )
    engine.run("stop", "--time", "3", source)
    stopped = engine.inspect("container", source)
    require(
        stopped["Name"] == name
        and stopped["State"]["Status"] in ("exited", "stopped")
        and stopped["State"]["Running"] is False,
        "source-stopped",
    )
    history_recovery.legacy_migration_mounts(box, data, stopped)
    project_before, home_before = manifest(project), manifest(home)
    check_db(home)

    check_phase(report, mode, "conversion")
    with patch.object(box, "podman", engine.conversion_podman):
        if mode == "rebuild":
            engine.fail_commit = source
        with box.workspace_lock(path):
            token = workspace_control.capture(path)
            try:
                entrypoint_migration.convert(box, path, data, token, source)
            except box.BoxError as exc:
                if mode != "rebuild" or str(exc) != entrypoint_migration.OFFER_REBUILD:
                    raise
            else:
                require(mode != "rebuild", "rebuild-required-separate-approval")
            require(workspace_control.unchanged(path, token), "stop-token-changed")

        if mode == "rebuild":
            check_phase(report, mode, "confirmed-failure-preservation")
            _, persisted = box.find_workspace(project)
            intent = persisted[entrypoint_migration.FIELD]
            still = engine.inspect("container", source)
            require(
                engine.failed_commits == 1
                and engine.fail_commit is None
                and intent["mode"] == "snapshot-failed"
                and intent["source"] == source
                and intent["image"] is None
                and intent["candidate"] is None
                and still["Id"] == source
                and still["Name"] == name
                and still["State"] == stopped["State"],
                "confirmed-failure-source",
            )
            require(
                engine.listed("container") == {source}
                and {image_id(i) for i in engine.listed("image")} == {base},
                "confirmed-failure-no-replacement",
            )
            require(
                engine.volume_identity(volume, wid) == (home, volume_id)
                and home_before == manifest(home)
                and project_before == manifest(project),
                "confirmed-failure-data",
            )
            check_phase(report, mode, "separately-approved-rebuild")
            with box.workspace_lock(path):
                token = workspace_control.capture(path)
                entrypoint_migration.convert(
                    box, path, data, token, source, rebuild=True
                )
                require(workspace_control.unchanged(path, token), "stop-token-changed")

    check_phase(report, mode, "replacement-before-start")
    replacement = engine.inspect("container", name)
    original = engine.inspect("container", source)
    current = replacement["Id"]
    fresh = replacement["Config"]
    is_rebuild = mode == "rebuild"
    require(
        HEX.fullmatch(current)
        and current != source
        and replacement["Name"] == name
        and fresh["Cmd"] == ["-c", "exec /bin/sleep infinity"]
        and fresh["Entrypoint"] in ("/bin/sh", ["/bin/sh"])
        and entrypoint_migration.fresh_replacement(data, replacement)
        and entrypoint_migration.needs_bootstrap(data) is is_rebuild
        and entrypoint_migration.FIELD not in data
        and box.find_workspace(project) == (path, data)
        and image_id(replacement["Image"]) == image_id(data["image"])
        and (image_id(data["image"]) == base) is is_rebuild
        and fresh["Labels"].get("io.oc-box.entrypoint-conversion")
        == engine.projects[wid]["token"]
        and original["Name"].endswith("-old")
        and original["State"]["Running"] is False
        and data[entrypoint_migration.RETIRE]["authorized"] is False
        and engine.listed("container") == {source, current},
        "replacement-binding",
    )
    history_recovery.passive_workspace_mounts(box, data, replacement)
    with patch.object(box, "podman", engine.conversion_podman):
        guarded_resources = runtime_safeguard._inspect(
            box, path, data, running=False, engine_env=engine.env
        )
    guarded_id = guarded_resources[0]
    require(guarded_id == current, "replacement-guarded-inspection")
    require(
        engine.volume_identity(volume, wid) == (home, volume_id)
        and home_before == manifest(home)
        and project_before == manifest(project),
        "replacement-data-before-start",
    )

    check_phase(report, mode, "guarded-passive-start-and-retirement")

    class PassiveOnly:
        def command(self, _box):
            # Admission.start never dispatches the native host CLI. This
            # synthetic selector lets the real launcher spawn fence run.
            return "/no-native-cli-in-this-passive-fixture"

    def passive_podman(*args, **kwargs):
        if args == ("--remote=false", "start", current):
            require(
                engine.inspect("container", source)["State"]["Running"] is False,
                "parked-source-before-real-start",
            )
            result = engine.run(*args[1:], **kwargs)
            require(
                engine.inspect("container", source)["State"]["Running"] is False,
                "parked-source-after-real-start",
            )
            return result
        return engine.conversion_podman(*args, **kwargs)

    # This exercises actual Admission.start including the credential-free
    # guarded mount exec, without claiming native preflight or guard enrollment.
    scope = runtime_safeguard.Admission(
        path,
        PassiveOnly(),
        None,
        native_runtime_guard.Record(current, volume, "2.4.0", "a" * 64, False),
        guarded_resources,
        token,
        transition=True,
        engine_env=engine.env,
    )
    with box.workspace_lock(path), patch.object(box, "podman", passive_podman):
        marker = runtime_safeguard._ACTIVE.set(scope)
        try:
            scope.start(box, data)
        finally:
            runtime_safeguard._ACTIVE.reset(marker)
    require(
        entrypoint_migration.RETIRE not in data
        and engine.inspect("container", source, missing=True) is None
        and engine.listed("container") == {current},
        "retirement-completion",
    )
    require(
        engine.inspect("container", current)["State"]["Running"] is True,
        "replacement-running",
    )
    require(
        engine.run("exec", current, *owner_args).stdout == expected_owner,
        "replacement-namespace-owners",
    )
    engine.run(
        "exec",
        current,
        "/bin/sh",
        "-ec",
        """
        test ! -e .legacy-shadow-executed
        if test "$3" = snapshot; then
            test "$(/bin/cat "$1")" = 'fixture OS change'
            test -x "$2"
            test "$(/bin/cat "$2")" = '#!/bin/sh
: > .legacy-shadow-executed
exec /bin/sleep "$@"'
        else
            test ! -e "$1"
            test ! -e "$2"
        fi
        """,
        "fixture",
        OS_MARKER,
        SHADOW,
        mode,
    )
    engine.run("stop", "--time", "3", current)
    final = engine.inspect("container", current)["State"]
    require(final["Running"] is False and final["Pid"] == 0, "replacement-stopped")
    require(
        engine.volume_identity(volume, wid) == (home, volume_id)
        and home_before == manifest(home)
        and project_before == manifest(project)
        and not (project / SHADOW_RECEIPT).exists(),
        "replacement-final-data",
    )
    check_db(home)
    report["cases"][mode] = {
        "confirmed_nonzero_commit_injected": is_rebuild,
        "same_project_and_volume_inode_bytes_ownership_modes": True,
        "source_retired": True,
        "original_retained_until_verified_boot_and_mount": True,
        "fresh_replacement_on_canonical_name": True,
        "guarded_inspection_of_stopped_replacement": True,
        "os_layer_preserved": not is_rebuild,
        "legacy_shadow_not_executed": True,
        "absolute_passive_start_and_stop": True,
        "bootstrap_required_by_receipt": is_rebuild,
    }


def interrupted(_number, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fresh-isolated-guest",
        action="store_true",
        help="attest fresh VM and disposable guest user",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="new directory under an owned, non-writable-by-others parent",
    )
    image = parser.add_mutually_exclusive_group(required=True)
    image.add_argument(
        "--image",
        help="explicit fully qualified tagged or digest image to pull",
    )
    image.add_argument(
        "--image-archive",
        type=Path,
        help="local single-image archive created by podman save",
    )
    image.add_argument(
        "--rootfs-archive", type=Path, help="local root filesystem tar to import"
    )
    args = parser.parse_args(argv)
    if not args.fresh_isolated_guest:
        parser.error(
            "--fresh-isolated-guest is required; do not run in a normal workspace"
        )
    if args.image is not None and not IMAGE_REF.fullmatch(args.image):
        parser.error("--image requires a registry-qualified tag or sha256 digest")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    root = None
    report = {
        "result": "FAIL",
        "phase": "guest-guard",
        "cases": {},
        "cleanup_failures": [],
        "scope": (
            "Disposable VM, real local rootless Podman and production stopped-container conversion. "
            "Synthetic SQLite data, not a native OpenCode conversation. The rebuild branch injects "
            "one nonzero commit status without running that commit. No OpenCode launch, bootstrap, "
            "CLI consent/routing, live history, or provider activity is exercised."
        ),
    }
    try:
        podman, runtime = guest_preflight()
        report["phase"] = "new-work-directory"
        root = make_root(args.work_dir)
        report["source_sha256"] = source_identity()
        engine = Engine(root, podman, prepare_store(root, runtime))
        with patch.dict(os.environ, engine.env, clear=True):
            report["phase"] = "verify-empty-isolated-store"
            try:
                report["podman_version"] = engine.verify_store(fresh=True)
                report["phase"] = "prepare-fixture-image"
                base = engine.prepare_image(args)
                report["base_image_id"] = base
                for mode in ("snapshot", "rebuild"):
                    run_case(engine, root, base, mode, report)
                    report["phase"] = f"{mode}/resource-cleanup"
                    failures = engine.cleanup(
                        keep_base=base if mode == "snapshot" else None
                    )
                    require(not failures, ",".join(failures))
                require(
                    engine.failed_commits == engine.real_commits == 1,
                    "commit-injection-counts",
                )
                require(source_identity() == report["source_sha256"], "source-changed")
                report.update(result="PASS", phase="complete")
            finally:
                if engine.last_failure is not None:
                    report["podman_failure"] = engine.last_failure
                if engine.verified:
                    try:
                        report["cleanup_failures"] = engine.cleanup()
                    except KeyboardInterrupt:
                        report["cleanup_failures"] = ["cleanup-interrupted"]
                if (
                    not engine.verified
                    and report["phase"] != "verify-empty-isolated-store"
                ):
                    report["cleanup_failures"] = ["store-not-verified"]
    except FixtureError as exc:
        report["failure"] = str(exc)
    except KeyboardInterrupt:
        report["failure"] = "interrupted"
    except (
        box.BoxError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        report["failure"] = type(exc).__name__
    finally:
        if report["cleanup_failures"] or "failure" in report:
            report["result"] = "FAIL"
        if report["phase"] == "complete" and report["cleanup_failures"]:
            report["phase"] = "final-resource-cleanup"
        if root is not None:
            try:
                with (root / "report.json").open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, indent=2, sort_keys=True)
                    stream.write("\n")
            except OSError:
                report.update(result="FAIL", report_write_failed=True)
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
