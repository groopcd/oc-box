"""Confirmed disposal of an exact old guarded workspace, without startup."""

import contextlib
from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from types import SimpleNamespace
import time

if __package__:
    from . import (
        entrypoint_migration,
        podman_config_layers,
        private_memory,
        runtime_safeguard,
        uninstall_tty,
        workspace_control,
        workspace_identity,
        workspace_podman,
        workspace_service,
        workspace_uninstall,
    )
else:
    import entrypoint_migration
    import podman_config_layers
    import private_memory
    import runtime_safeguard
    import uninstall_tty
    import workspace_control
    import workspace_identity
    import workspace_podman
    import workspace_service
    import workspace_uninstall


_ID = re.compile(r"[a-f0-9]{64}")
_VOLUME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
_LIMIT = 128
_BYTES = workspace_podman._INSPECT
_MOUNTINFO_BYTES = 1024 * 1024
_MOUNT_ESCAPE = re.compile(rb"\\(040|011|012|134)")
_NET_NAMESPACE = re.compile(rb"net:\[[0-9]{1,20}\]")
_CONFIG_LIMIT = 256 * 1024
_MUTATION_SECONDS = 180
_PROFILE = "6.2.0"
_CONFIG_FILES = podman_config_layers.FILES
_PREEXEC_MARKER = "/etc/containers/podman_preexec_hooks.txt"
_NAMESPACE_PROBE = """\
import json, os, re, stat, sys
stamps = []
for index, path in enumerate(sys.argv[1:]):
    mode = os.O_DIRECTORY if index >= 2 else 0
    fd = os.open(path, os.O_PATH | mode | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if index < 2 and not stat.S_ISREG(info.st_mode):
            raise SystemExit(1)
        with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
            ids = re.findall(rb"^mnt_id:\\s*([0-9]{1,20})$", stream.read(4097), re.M)
        if len(ids) != 1:
            raise SystemExit(1)
        stamps.append([info.st_dev, info.st_ino, int(ids[0])])
    finally:
        os.close(fd)
with open("/proc/self/mountinfo", "rb") as stream:
    mountinfo = stream.read(1048577)
sys.stdout.buffer.write(json.dumps(stamps).encode("ascii") + b"\\n" + mountinfo)
"""


class Refused(ValueError):
    """Fixed diagnostic only."""


def _require(condition, message):
    if not condition:
        raise Refused(message)


def _absolute(path):
    return (
        isinstance(path, str)
        and path.startswith("/")
        and path != "/"
        and len(path) <= 4096
        and not any(ch in path for ch in "\0\r\n")
        and str(Path(path)) == path
        and ".." not in Path(path).parts
    )


def _identity(path, *, persistent=False):
    uid = os.geteuid()
    with workspace_podman._path_fd(str(path), uid, directory=True) as (fd, info):
        _require(
            info.st_uid == uid,
            "An owned storage directory could not be verified.",
        )
        stamp = info.st_dev, info.st_ino, workspace_uninstall._mount_id(fd)
        if not persistent:
            return stamp
        observed, stable = workspace_identity.snapshot_fd(fd)
        _require(
            list(stamp[:2]) == observed,
            "The original private home filesystem identity could not be verified.",
        )
        return stamp, stable


def _absent(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    return False


def _mount_path(value):
    escapes = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}
    path = os.fsdecode(_MOUNT_ESCAPE.sub(lambda match: escapes[match[1]], value))
    _require(path == "/" or _absolute(path), "A host mount path could not be verified.")
    return Path(path)


def _mounts():
    with open("/proc/self/mountinfo", "rb") as stream:
        return _parse_mounts(stream.read(_MOUNTINFO_BYTES + 1))


def _parse_mounts(raw):
    _require(
        raw.endswith(b"\n") and len(raw) <= _MOUNTINFO_BYTES,
        "The host mount table could not be verified.",
    )
    result = {}
    for line in raw.split(b"\n")[:-1]:
        before, separator, after = line.partition(b" - ")
        fields = before.split()
        filesystem = after.split()
        _require(
            separator and len(fields) >= 6 and len(filesystem) >= 3,
            "The host mount table is invalid.",
        )
        mount = int(fields[0])
        major, minor = (int(value) for value in fields[2].split(b":"))
        _require(
            mount > 0 and mount not in result, "The host mount table is ambiguous."
        )
        # A live Podman network can pin an nsfs object. Its kernel root is
        # net:[inode], not a backing pathname. Retain the mountpoint for
        # nested-mount checks; it must not share the storage device.
        root = (
            None
            if filesystem[0] == b"nsfs" and _NET_NAMESPACE.fullmatch(fields[3])
            else _mount_path(fields[3])
        )
        result[mount] = (
            os.makedev(major, minor),
            root,
            _mount_path(fields[4]),
        )
    return result


def _exclusive_storage(mounts, path, stamp):
    """Refuse visible aliases and nested mounts into an engine removal tree."""
    _check_mount_backing(mounts, path, stamp[2])


def _check_mount_backing(mounts, path, anchor):
    location = Path(path)
    device, root, point = mounts[anchor]
    # Mount IDs (not st_dev) bind the open path to this mountinfo row.
    # The kernel can report a distinct st_dev for a filesystem subtree.
    _require(
        root is not None and location.is_relative_to(point),
        "The original workspace storage mount could not be verified.",
    )
    backing = root / location.relative_to(point)
    for ident, (other_device, other_root, other_point) in mounts.items():
        if ident == anchor:
            continue
        if other_root is None:
            _require(
                other_device != device and not other_point.is_relative_to(location),
                "A namespace mount overlaps the workspace storage.",
            )
            continue
        # A namespace may stack a bind of the store over its original
        # location. A covering record already projecting the same leaf to
        # the same path is not a second alias. A mount at/below the removal
        # tree or a projection to any other path is still a refusal.
        covers_at_same_path = (
            backing.is_relative_to(other_root)
            and other_point / backing.relative_to(other_root) == location
        )
        _require(
            not other_point.is_relative_to(location)
            and not (
                other_device == device
                and workspace_uninstall._overlap(str(backing), str(other_root))
                and not covers_at_same_path
            ),
            "A host mount or bind alias overlaps the workspace storage.",
        )


def _config_bytes(path):
    uid = os.geteuid()
    with workspace_podman._path_fd(str(path), uid) as (fd, info):
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_size <= _CONFIG_LIMIT,
            "A Podman configuration file could not be verified.",
        )
        expected = workspace_uninstall._file_stamp(info)
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            data = stream.read(_CONFIG_LIMIT + 1)
        _require(
            len(data) <= _CONFIG_LIMIT
            and expected == workspace_uninstall._file_stamp(os.fstat(fd))
            and expected == workspace_uninstall._file_stamp(path.lstat()),
            "A Podman configuration file changed during review.",
        )
        return data


def _sealed_config(stack, data):
    fd = private_memory.create("oc-box-discard-config", allow_sealing=True)
    stack.callback(os.close, fd)
    _require(
        os.write(fd, data) == len(data), "The Podman configuration could not be copied."
    )
    private_memory.seal(fd)
    _require(
        private_memory.is_sealed(fd), "The Podman configuration could not be sealed."
    )
    return fd


def _check_config_values(containers, storage, environment):
    _require(
        podman_config_layers.canonical_fields(containers)
        and podman_config_layers.canonical_fields(storage),
        "Podman configuration contains an unsupported field spelling.",
    )
    _require(
        set(storage) <= {"storage"} and "storage" not in containers,
        "The Podman configuration files cannot be bound as one profile.",
    )
    config = containers.get("engine", {})
    _require(
        type(config) is dict
        and not config.get("env")
        and not config.get("namespace")
        and config.get("remote", False) is False,
        "Guarded disposal requires a local Podman configuration without environment or namespace overrides.",
    )
    static_dir = config.get("static_dir", "")
    _require(
        static_dir == "" or _absolute(static_dir) and "?" not in static_dir,
        "The Podman database location is unsupported.",
    )
    storage_settings = storage.get("storage", {})
    _require(
        type(storage_settings) is dict,
        "The Podman storage configuration is unsupported.",
    )
    inputs = tuple(
        storage_settings.get(key, "")
        for key in ("graphroot", "runroot", "rootless_storage_path", "imagestore")
    ) + tuple(
        environment.get(key, "")
        for key in ("HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "USER", "LOGNAME")
    )
    # Libpod uses a SQLite DSN, not a quoted filename. Its driver splits
    # at the first '?'; refuse such a selector before the first native call.
    _require(
        all(type(value) is str and "?" not in value for value in inputs),
        "The Podman database location is unsupported.",
    )
    return static_dir


def _engine_config(stack, environment):
    _require(
        "STORAGE_OPTS" not in environment
        and not any(
            environment.get(key)
            for key in (
                "CONTAINERS_CONF_OVERRIDE",
                "CONTAINERS_CONF_MODULES",
                "CONTAINERS_STORAGE_CONF_OVERRIDE",
                "STORAGE_DRIVER",
            )
        ),
        "Guarded disposal does not support Podman configuration overrides.",
    )
    try:
        import tomllib
    except ImportError:
        raise Refused(
            "Guarded disposal requires a host Python with TOML support (3.11 or later)."
        ) from None
    if all(environment.get(key) for key in _CONFIG_FILES):
        explicit = [environment[key] for key in _CONFIG_FILES]
        _require(
            all(_absolute(path) for path in explicit),
            "An explicit Podman configuration path is unsupported.",
        )
        container_config, storage_config = (
            _config_bytes(Path(path)) for path in explicit
        )
    else:
        try:
            possible = podman_config_layers.candidates(environment, _config_bytes)
            # Validate each selected declaration before flattening it, even
            # when a later file overrides the value.
            _check_config_values({}, {}, environment)
            for family, blob in possible.values():
                values = tomllib.loads(blob.decode("utf-8"))
                _check_config_values(
                    values if family == "containers" else {},
                    values if family == "storage" else {},
                    environment,
                )
            container_config, storage_config = (
                podman_config_layers.flatten(
                    [blob for family, blob in possible.values() if family == name],
                    tomllib.loads,
                )
                for name in podman_config_layers.NAMES
            )
            _require(
                podman_config_layers.candidates(environment, _config_bytes) == possible,
                "The installed Podman configuration changed during review.",
            )
        except podman_config_layers.ConfigError as exc:
            raise Refused(str(exc)) from None
    containers = tomllib.loads(container_config.decode("utf-8"))
    storage = tomllib.loads(storage_config.decode("utf-8"))
    static_dir = _check_config_values(containers, storage, environment)

    # Both native decoders accept one TOML file and ignore the other's tables.
    combined = container_config + b"\n" + storage_config
    _require(
        tomllib.loads(combined.decode("utf-8")) == {**containers, **storage},
        "The Podman configuration files cannot be bound as one profile.",
    )
    result = {
        key: value for key, value in environment.items() if key not in _CONFIG_FILES
    }
    result.update(
        PATH="/usr/sbin:/usr/bin:/sbin:/bin",
        CONTAINERS_CONF="/proc/self/fd/0",
        CONTAINERS_STORAGE_CONF="/proc/self/fd/0",
    )
    # stdin is a sealed regular file, never the confirmation terminal. The
    # Podman rootless re-exec and operand-free unshare pass standard input;
    # each config decoder opens its own read handle in its own namespace.
    settings = containers.get("engine", {}), storage.get("storage", {})
    return result, static_dir, _sealed_config(stack, combined), settings


def _database_stamp(fd, info):
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid(),
        "The existing Podman database could not be verified.",
    )
    return info.st_dev, info.st_ino, workspace_uninstall._mount_id(fd)


def _storage_options(observed):
    """Explicit graphroot/driver flags suppress config-derived driver options."""
    if observed == {}:
        return ()
    # info projects a map rather than a lossless option vector. Replay only
    # the audited distro default, not arbitrary strings or external stores.
    _require(
        observed == {"overlay.mountopt": "nodev"},
        "The native Podman storage options could not be reproduced safely.",
    )
    return ("--storage-opt=overlay.mountopt=nodev",)


def _store_path(value, environment):
    _require(type(value) is str, "A configured Podman store path is unsupported.")
    # Admit only a known leading path variable, with the exact same value the
    # sanitized Podman child receives. No derived SQLite DSN can contain '?'.
    prefix = re.match(
        r"^\$(?:\{(HOME|XDG_DATA_HOME|XDG_RUNTIME_DIR)\}|(HOME|XDG_DATA_HOME|XDG_RUNTIME_DIR))(?=/|$)",
        value,
    )
    if prefix:
        selected = environment.get(prefix.group(1) or prefix.group(2), "")
        _require(
            _absolute(selected), "A configured Podman store path could not be resolved."
        )
        value = selected + value[prefix.end() :]
    _require(
        _absolute(value) and "$" not in value and "?" not in value,
        "A configured Podman store path is unsupported.",
    )
    return value


def _store_layout(environment, static_dir, settings):
    engine, storage = settings
    runtime_base = _store_path(environment.get("XDG_RUNTIME_DIR", ""), {})
    data_base = _store_path(
        environment.get("XDG_DATA_HOME")
        or (_store_path(environment.get("HOME", ""), {}) + "/.local/share"),
        {},
    )
    graph = _store_path(
        storage.get("graphroot")
        or storage.get("rootless_storage_path")
        or data_base + "/containers/storage",
        environment,
    )
    runroot = _store_path(
        storage.get("runroot") or runtime_base + "/containers", environment
    )
    volume = _store_path(engine.get("volume_path") or graph + "/volumes", {})
    temp = _store_path(engine.get("tmp_dir") or runtime_base + "/libpod/tmp", {})
    static = _store_path(static_dir or graph + "/libpod", {})
    _require(
        Path(volume).is_relative_to(graph)
        and volume != graph
        and Path(static).is_relative_to(graph)
        and static != graph
        and not workspace_uninstall._overlap(graph, runroot),
        "The configured Podman store layout is unsupported.",
    )
    _require(
        storage.get("driver", "") in ("", "overlay")
        and storage.get("transient_store", False) is False
        and storage.get("driver_priority", []) in ([], ["overlay"])
        and not storage.get("imagestore")
        and engine.get("database_backend", "") in ("", "sqlite"),
        "Only a persistent local Podman overlay/SQLite store is supported.",
    )
    _require(
        not engine.get("events_logfile_path"),
        "A custom Podman event-log path is not supported by guarded disposal.",
    )
    options = storage.get("options", {})
    _require(
        type(options) is dict, "The configured Podman storage options are unsupported."
    )
    overlay = options.get("overlay", {})
    _require(
        type(overlay) is dict
        and overlay.get("mountopt", "") in ("", "nodev")
        and not any(value for key, value in overlay.items() if key != "mountopt")
        and not any(
            value
            for key, value in options.items()
            if key not in ("overlay", "pull_options", "vfs", "btrfs", "zfs")
        ),
        "The configured Podman overlay options are unsupported.",
    )
    graph_options = {"overlay.mountopt": "nodev"} if overlay.get("mountopt") else {}
    database = str(Path(static if static_dir else graph) / "db.sql")
    return graph, runroot, volume, static, temp, database, graph_options


def _verify_database_metadata(database, expected):
    """Verify only the existing Podman 6.2 DBConfig row, never open it writable."""
    try:
        import sqlite3
    except ImportError:
        raise Refused("Guarded disposal requires host Python SQLite support.") from None

    uid = os.geteuid()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            with workspace_podman._path_fd(database + suffix, uid) as (fd, info):
                _database_stamp(fd, info)
                _require(
                    suffix != "-journal" or info.st_size == 0,
                    "The Podman database has an unsettled rollback journal.",
                )
        except FileNotFoundError:
            pass
    uri = Path(database).as_uri() + "?mode=ro&immutable=1"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA trusted_schema = OFF")
            _require(
                db.execute(
                    "SELECT type FROM sqlite_master WHERE name='DBConfig'"
                ).fetchmany(2)
                == [("table",)],
                "The existing Podman database does not have the expected configuration table.",
            )
            rows = db.execute(
                "SELECT ID, SchemaVersion, OS, StaticDir, TmpDir, GraphRoot, RunRoot, GraphDriver, VolumeDir FROM DBConfig"
            ).fetchmany(2)
    except sqlite3.Error:
        raise Refused(
            "The existing Podman database configuration could not be verified."
        ) from None
    _require(
        len(rows) == 1
        and rows[0][:3] == (1, 1, "linux")
        and all(value in ("", wanted) for value, wanted in zip(rows[0][3:], expected)),
        "The existing Podman database config does not match the reviewed store.",
    )
    return rows[0][7]


class Engine:
    def __init__(self, box):
        self.environment = entrypoint_migration.engine_environment(box)
        self.environment["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
        # This native selector is not allowed to leak into the sealed profile.
        if "CONTAINERS_STORAGE_CONF_OVERRIDE" in os.environ:
            self.environment["CONTAINERS_STORAGE_CONF_OVERRIDE"] = os.environ[
                "CONTAINERS_STORAGE_CONF_OVERRIDE"
            ]
        # A fixed packaged path also lets the rootless mount report re-enter
        # this same executable in Podman's own user/mount namespace.
        self.path = str(Path("/usr/bin/podman").resolve(strict=True))
        _require(
            Path(self.path).is_relative_to("/usr"),
            "The packaged Podman executable could not be located safely.",
        )
        self.flags = ("--remote=false",)
        self.config_fd = subprocess.DEVNULL
        self.bound = None
        self.database = None
        self.alive = None

    def __enter__(self):
        with contextlib.ExitStack() as stack:
            for parent in Path(self.path).parents:
                info = parent.stat()
                _require(
                    info.st_uid == 0 and not info.st_mode & 0o022,
                    "The packaged Podman executable is not in a trusted directory.",
                )
            self.executable = workspace_podman._pin(
                stack, self.path, os.geteuid(), owner=0, directory=False
            )
            mode = self.executable.stamp[4]
            _require(
                mode & 0o111
                and not mode & 0o6000
                and workspace_podman._read_pin(self.executable, 4) == b"\x7fELF",
                "The local packaged Podman executable could not be verified.",
            )
            self.environment, static_dir, self.config_fd, settings = _engine_config(
                stack, self.environment
            )
            graph, runtime, volume, static, temp, self.database_path, options = (
                _store_layout(self.environment, static_dir, settings)
            )
            self.roots = tuple(
                (path, _identity(path)) for path in (graph, runtime, volume)
            )
            _identity(static)
            _identity(temp)
            fd, info = stack.enter_context(
                workspace_podman._path_fd(self.database_path, os.geteuid())
            )
            self.database = fd, _database_stamp(fd, info)
            _require(
                self.database[1][::2] == self.roots[0][1][::2],
                "The Podman database crosses a storage boundary.",
            )
            stored_driver = _verify_database_metadata(
                self.database_path, (static, temp, graph, runtime, "overlay", volume)
            )
            try:
                self.alive = workspace_podman._pin(
                    stack,
                    temp + "/alive",
                    os.geteuid(),
                    owner=os.geteuid(),
                    directory=False,
                )
            except FileNotFoundError:
                raise Refused(
                    "The local Podman store is not initialized. Run 'podman info' on the host before retrying."
                ) from None
            with open("/proc/sys/kernel/random/boot_id", "rb") as stream:
                boot = stream.read(64)
            _require(
                re.fullmatch(rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n", boot)
                and workspace_podman._read_pin(self.alive, 65) == boot,
                "The Podman runtime is not verified for this boot. Run 'podman info' on the host before retrying.",
            )
            # The packaged binary initializes its default runtime directory
            # and checks the pre-exec marker even during --version.
            with workspace_podman._path_fd(
                self.environment["XDG_RUNTIME_DIR"] + "/libpod",
                os.geteuid(),
                directory=True,
            ) as (_, info):
                _require(
                    info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o1700,
                    "The default Podman runtime directory is not initialized privately.",
                )
            self._verify_version(stack)
            # Use explicit checked roots and the preserved driver options on
            # the very first runtime-opening command, not just during rm.
            self.flags = (
                "--remote=false",
                f"--root={graph}",
                f"--runroot={runtime}",
                f"--volumepath={volume}",
                f"--tmpdir={temp}",
                # Native SQLite validation substitutes the configured default
                # for an empty stored driver. Forcing overlay here can make
                # libpod reject a legitimate automatic-driver store before
                # opening it. Scope still requires actual overlay before use.
                *(("--storage-driver=overlay",) if stored_driver else ()),
                *_storage_options(options),
                "--transient-store=false",
                "--namespace=",
            )
            self.bound = self.scope()
            _require(
                self.bound[:3] == self.roots and self.bound[3][2] == options,
                "Explicit Podman selectors did not preserve the reviewed store.",
            )
            self.check_namespace(())
            self.stack = stack.pop_all()
        return self

    def __exit__(self, *_):
        self.stack.close()

    def _verify_version(self, stack):
        self._check_preexec()
        environment = {
            key: value
            for key, value in self.environment.items()
            if not key.startswith(("CONTAINER_", "CONTAINERS_", "STORAGE_"))
        }
        environment.update({key: "/proc/self/fd/0" for key in _CONFIG_FILES})
        code, raw = workspace_podman._capture_pinned(
            self.executable,
            ("--remote=false", "--version"),
            environment,
            time.monotonic() + 8,
            1024,
            stdin=_sealed_config(stack, b""),
        )
        self.executable.recheck()
        _require(
            code == 0 and raw == f"podman version {_PROFILE}\n".encode("ascii"),
            "Guarded disposal requires the verified Podman 6.2.0 executable.",
        )

    def call(self, *arguments):
        self._check_preexec()
        self.executable.recheck()
        self.check_database()
        self.alive.recheck()
        _require(
            all(_identity(path) == stamp for path, stamp in self.roots),
            "The reviewed Podman store paths changed.",
        )
        timeout = (
            _MUTATION_SECONDS
            if arguments[:2]
            in (
                ("container", "rm"),
                ("volume", "rm"),
            )
            else workspace_podman._COMMAND_SECONDS
        )
        code, output = workspace_podman._capture_pinned(
            self.executable,
            (*self.flags, *arguments),
            self.environment,
            time.monotonic() + timeout,
            _BYTES,
            command_seconds=timeout,
            stdin=self.config_fd,
        )
        self.executable.recheck()
        self.check_database()
        self.alive.recheck()
        return subprocess.CompletedProcess(
            arguments, code, output.decode("utf-8", errors="strict")
        )

    @staticmethod
    def _check_preexec():
        _require(
            _absent(_PREEXEC_MARKER),
            "Podman pre-exec hooks are enabled; guarded disposal did not open the store.",
        )

    def check_database(self):
        if self.database is None:
            return
        fd, stamp = self.database
        with workspace_podman._path_fd(self.database_path, os.geteuid()) as (
            named,
            info,
        ):
            _require(
                _database_stamp(named, info)
                == stamp
                == _database_stamp(fd, os.fstat(fd)),
                "The reviewed Podman database identity changed.",
            )

    def read(self, *arguments):
        result = self.call(*arguments)
        _require(
            result.returncode == 0
            and isinstance(result.stdout, str)
            and len(result.stdout.encode("utf-8")) <= _BYTES,
            "The local Podman inventory could not be verified.",
        )
        return result.stdout

    def scope(self):
        return self._inspect_scope(self.read("info", "--format=json"))

    @staticmethod
    def _inspect_scope(raw):
        data = workspace_podman._json(raw)
        host, store = data["host"], data["store"]
        graph, runtime, volume = (
            store[key] for key in ("graphRoot", "runRoot", "volumePath")
        )
        _require(
            host["serviceIsRemote"] is False
            and host["security"]["rootless"] is True
            and store["graphDriverName"] == "overlay"
            and store["transientStore"] is False
            and host["databaseBackend"] == "sqlite"
            and data["version"]["Version"] == _PROFILE
            and all(
                _absolute(item) and "?" not in item for item in (graph, runtime, volume)
            )
            and Path(volume).is_relative_to(graph)
            and volume != graph
            and not workspace_uninstall._overlap(graph, runtime),
            "Disposal requires the verified local rootless Podman 6.2.0 SQLite/overlay profile.",
        )
        return (
            *((item, _identity(item)) for item in (graph, runtime, volume)),
            (
                host["databaseBackend"],
                store.get("configFile"),
                store["graphOptions"],
                host.get("networkBackend"),
                host.get("cgroupManager"),
                host.get("ociRuntime"),
            ),
        )

    def mounted(self, rows):
        # With no operands, --latest or --all, this is a catalog, not a
        # mount request. Rootless overlay needs the engine's user namespace.
        report = self.read(
            "unshare", self.path, *self.flags, *workspace_podman._MOUNT_LIST
        )
        return dict(
            workspace_podman._mount_catalog(
                report,
                [SimpleNamespace(ident=row["Id"], name=row["Name"]) for row in rows],
                [SimpleNamespace(path=path) for path, _ in self.bound[:2]],
            )
        )

    def check_namespace(self, targets):
        # A native mount namespace can shadow the database or boot marker
        # without shadowing either directory that would be removed.
        subject = (
            (self.database_path, self.database[1]),
            (self.alive.path, self.alive.stamp),
            *targets,
        )
        text = self.read(
            "unshare",
            "/usr/bin/python3",
            "-IB",
            "-c",
            _NAMESPACE_PROBE,
            *(path for path, _ in subject),
        )
        line, _, raw = text.partition("\n")
        observed = workspace_podman._json(line)
        _require(
            type(observed) is list and len(observed) == len(subject),
            "Podman did not verify the storage in its namespace.",
        )
        mounts = _parse_mounts(raw.encode("utf-8")) if targets else None
        for index, ((path, expected), stamp) in enumerate(zip(subject, observed)):
            _require(
                type(stamp) is list
                and len(stamp) == 3
                and all(type(value) is int and value >= 0 for value in stamp)
                and tuple(stamp[:2]) == expected[:2],
                "The Podman namespace does not use the reviewed database or boot marker."
                if index < 2
                else "The Podman namespace does not use the same backing storage.",
            )
            if index >= 2:
                _check_mount_backing(mounts, path, stamp[2])

    def catalog(self, kind):
        if kind == "container":
            args = ("ps", "--all", "--external", "--no-trunc", "--format", "{{.ID}}")
            pattern = _ID
        else:
            args = ("volume", "ls", "--format", "{{.Name}}")
            pattern = _VOLUME
        rows = workspace_podman._catalog(self.read(*args).encode("utf-8"), pattern)
        _require(
            len(rows) <= _LIMIT, "The Podman resource list exceeds the review limit."
        )
        return rows

    def inspect(self, kind, selectors):
        if not selectors:
            return ()
        key = "Id" if kind == "container" else "Name"
        return workspace_podman._listed(
            self.read(kind, "inspect", "--", *selectors), key, selectors
        )

    def exists(self, kind, target, *, present):
        report = self.call(kind, "exists", "--", target)
        _require(
            report.returncode == (0 if present else 1),
            "The exact Podman selector did not agree with the inventory.",
        )

    def labeled_networks(self, ident):
        _require(
            re.fullmatch(r"[a-f0-9]{32}", ident), "The network selector is invalid."
        )
        return workspace_podman._catalog(
            self.read(
                "network",
                "ls",
                "--filter",
                f"label=io.oc-box.workspace={ident}",
                "--format",
                "{{.Name}}",
            ).encode("utf-8"),
            _VOLUME,
        )

    def remove(self, kind, target):
        # Both selectors are taken from the rechecked in-memory plan, never
        # raw user argv. Do not retry an uncertain acknowledgement.
        _require(
            isinstance(target, str)
            and (
                kind == "container"
                and _ID.fullmatch(target)
                or kind == "volume"
                and re.fullmatch(r"oc-box-home-[a-f0-9]{32}", target)
            ),
            "The reviewed removal selector is invalid.",
        )
        result = self.call(kind, "rm", "--", target)
        _require(
            result.returncode == 0 and result.stdout == target + "\n",
            "Podman removal was not confirmed. Inspect what remains; no further removal was attempted.",
        )


@dataclass(frozen=True)
class Plan:
    registration: object
    guard: object
    control: tuple
    scope: tuple
    container: tuple | None
    volume: tuple | None
    other_resources: tuple


def _old_container(data, container_id, rows):
    name = data["container"]
    _require(
        all(
            isinstance(row["Name"], str) and not _ID.fullmatch(row["Name"])
            for row in rows
        )
        and len({row["Name"] for row in rows}) == len(rows),
        "A container selector is ambiguous; no disposal was admitted.",
    )
    targets = [
        row
        for row in rows
        if row["Id"] == container_id
        or row["Name"] == name
        or (row["Config"].get("Labels") or {}).get("io.oc-box.workspace") == data["id"]
    ]
    if not targets:
        return None
    _require(len(targets) == 1, "More than one container matches this workspace.")
    row = targets[0]
    labels = row["Config"]["Labels"]
    _require(
        (container_id is None or row["Id"] == container_id)
        and row["Name"] == name
        and labels.get("io.oc-box.workspace") == data["id"]
        and labels.get("io.oc-box.managed") == "true",
        "The old container's ownership or full ID could not be verified.",
    )
    _require(
        entrypoint_migration._stopped(
            row, ("created", "configured", "exited", "stopped")
        )
        and not row.get("Pod")
        and not row.get("Dependencies")
        and row.get("IsInfra") is False
        and row.get("IsService") is False
        and row["HostConfig"]["RestartPolicy"]["Name"] in ("", "no"),
        "The selected container is not verified stopped and standalone. Stop it explicitly before disposal.",
    )
    mounts = row["Mounts"]
    _require(
        isinstance(mounts, list)
        and len(mounts) == 2
        and any(
            mount.get("Type") == "bind"
            and mount.get("Source") == data["project"]
            and mount.get("Destination")
            == data.get("container_project", data["project"])
            for mount in mounts
        )
        and any(
            mount.get("Type") == "volume"
            and mount.get("Name") == data["home_volume"]
            and mount.get("Destination") == data["home"]
            for mount in mounts
        ),
        "The old container's original project and private-home mounts could not be verified.",
    )
    return row


def _writable(scope, selected):
    if selected is None:
        return None
    details = selected["GraphDriver"]
    upper = details["Data"].get("UpperDir")
    graph, stamp = scope[0]
    prefix = graph + "/overlay/"
    _require(
        details["Name"] == "overlay"
        and _absolute(upper)
        and upper.startswith(prefix)
        and upper.endswith("/diff")
        and _ID.fullmatch(upper[len(prefix) : -len("/diff")]),
        "The old container's writable layer could not be verified.",
    )
    layer = str(Path(upper).parent)
    identity = _identity(layer)
    upper_identity = _identity(upper)
    _require(
        all(
            item[0] == stamp[0] and item[2] == stamp[2] and item != stamp
            for item in (identity, upper_identity)
        ),
        "The old writable layer crosses a storage boundary.",
    )
    return layer, identity, upper_identity


def _local_volume(volume, base):
    name = volume["Name"]
    directory = Path(base) / name
    mount = directory / "_data"
    _require(
        _VOLUME.fullmatch(name)
        and volume["Driver"] == "local"
        and volume["Scope"] == "local"
        and volume["Options"] in (None, {})
        and type(volume.get("Anonymous", False)) is bool
        and volume.get("StorageID", "") == ""
        and workspace_podman._creation_time(volume["CreatedAt"])
        and volume["Mountpoint"] == str(mount)
        and workspace_podman._uint(volume["MountCount"]),
        "A named volume uses unverified backing storage.",
    )
    return directory, mount


def _volume(data, scope, volume, selected, known, *, stale=False):
    name = data["home_volume"]
    base = Path(scope[2][0])
    directory = base / name
    if volume is None:
        _require(
            selected is None and _absent(directory),
            "The original private home is missing or retains unverified storage.",
        )
        return None
    else:
        directory, mount = _local_volume(volume, base)
        _require(
            volume["Name"] == name
            and volume.get("Anonymous", False) is False
            and (volume.get("Labels") or {}).get("io.oc-box.workspace") == data["id"]
            and volume["MountCount"] == 0,
            "The original unused private home could not be verified.",
        )
        receipt = data.get(entrypoint_migration.RECEIPT)
        if receipt is not None and entrypoint_migration.HOME_IDENTITY in data:
            stamp, stable = _identity(mount, persistent=True)
        else:
            stamp, stable = _identity(mount), None
        parent = _identity(directory)
        binding = (name, stamp, volume["CreatedAt"], parent)
        _require(
            all(
                item[0] == scope[2][1][0] and item[2] == scope[2][1][2]
                for item in (stamp, parent)
            )
            and (
                stale or selected is not None or receipt is not None or known == binding
            )
            and (
                receipt is None
                or entrypoint_migration.volume_identity_matches(data, stamp[:2], stable)
            ),
            "The original private home filesystem identity could not be verified.",
        )
        if selected is not None:
            _require(
                any(
                    item.get("Name") == name and item.get("Source") == str(mount)
                    for item in selected["Mounts"]
                ),
                "The old container does not use the verified private home.",
            )
        return binding


def _other_consumers(
    data,
    container_id,
    rows,
    volume_rows,
    selected,
    targets,
    scope,
    *,
    missing_bind=None,
):
    name = data["home_volume"]
    for volume in volume_rows:
        if volume["Name"] == name:
            continue
        directory, mount = _local_volume(volume, scope[2][0])
        _require(
            all(_identity(path)[2] == scope[2][1][2] for path in (directory, mount))
            and not any(
                workspace_uninstall._overlap(str(directory), path) for path in targets
            ),
            "Another volume may share the old workspace storage.",
        )
    for row in rows:
        if row is selected:
            continue
        _require(
            container_id not in (row.get("Dependencies") or []),
            "Another container depends on the selected container.",
        )
        other = _writable(scope, row)
        _require(
            not any(workspace_uninstall._overlap(other[0], path) for path in targets),
            "Another container may share the old writable layer.",
        )
        for item in row["Mounts"]:
            _require(
                item.get("Name") != name,
                "Another container still references the original private home.",
            )
            kind = item["Type"]
            _require(
                kind in ("bind", "volume", "tmpfs"),
                "Another container uses unsupported mount storage.",
            )
            if kind in ("bind", "volume"):
                source = item["Source"]
                _require(
                    source == "/" or _absolute(source),
                    "An overlapping mount could not be ruled out.",
                )
                try:
                    resolved = os.path.realpath(source, strict=True)
                except FileNotFoundError:
                    if (
                        kind != "bind"
                        or missing_bind is None
                        or not missing_bind(row, item)
                    ):
                        raise
                    # The stale-cleanup caller proved genuine absence through
                    # an unchanged, non-symlink parent. Still check overlap.
                    resolved = source
                _require(
                    not any(
                        workspace_uninstall._overlap(resolved, path) for path in targets
                    ),
                    "Another container mounts the original workspace storage or a covering path.",
                )


def _registered(box, path, ident):
    registration, data = workspace_uninstall.read_discard_registration(
        box.state_root(), ident
    )
    guard = runtime_safeguard.load(box, path)
    _require(
        guard is not None and guard.volume == registration.home_volume,
        "This disposal mode requires a valid matching guarded runtime record.",
    )
    if entrypoint_migration.RECEIPT in data:
        _require(
            entrypoint_migration.replacement_matches(data, {"Id": guard.container}),
            "The guarded container does not match the existing conversion receipt.",
        )
    return registration, data, guard


@dataclass(frozen=True)
class Resources:
    scope: tuple
    container: tuple | None
    volume: tuple | None
    other_resources: tuple


def _review_resources(
    box,
    registration,
    data,
    engine,
    container_id,
    *,
    reviewed=None,
    stale=False,
    missing_bind=None,
):
    """Review canonical objects using the bound local engine, without mutations."""
    scope = engine.scope()
    containers, volumes = engine.catalog("container"), engine.catalog("volume")
    rows = engine.inspect("container", containers)
    selected = _old_container(data, container_id, rows)
    if selected is not None:
        container_id = selected["Id"]
    elif container_id is None and reviewed is not None and reviewed.container:
        container_id = reviewed.container[0]
    name = registration.home_volume
    # Native LookupVolume falls back to unique prefixes even without --force.
    # Never leave a pre-existing prefix neighbor as a possible rm target.
    _require(
        not any(
            candidate != name and candidate.startswith(name) for candidate in volumes
        ),
        "Another volume matches the original name as a prefix.",
    )
    volume_rows = engine.inspect("volume", volumes)
    volume = next((row for row in volume_rows if row["Name"] == name), None)
    if stale:
        _require(
            not any(
                row["Name"] != name
                and (row.get("Labels") or {}).get("io.oc-box.workspace") == data["id"]
                for row in volume_rows
            ),
            "The workspace has additional labeled volumes; inspect it separately.",
        )
    if container_id is not None:
        engine.exists("container", container_id, present=selected is not None)
    engine.exists(
        "container", registration.container_name, present=selected is not None
    )
    engine.exists("volume", name, present=volume is not None)
    _require(
        stale or selected is not None or volume is not None or reviewed is not None,
        "Neither original resource could bind this Podman store. An empty store cannot establish disposal.",
    )
    if reviewed is not None:
        _require(scope == reviewed.scope, "The original reviewed Podman store changed.")
    writable = _writable(scope, selected)
    original_layer = (
        reviewed.container[4] if reviewed is not None and reviewed.container else None
    )
    if writable is None and original_layer is not None:
        _require(
            _absent(original_layer[0]),
            "Container removal did not verify absence of the old writable layer.",
        )
    home = _volume(
        data,
        scope,
        volume,
        selected,
        reviewed.volume if reviewed is not None else None,
        stale=stale,
    )
    home_directory = str(Path(scope[2][0]) / name)
    targets = (home_directory,) + (
        ((writable or original_layer)[0],) if (writable or original_layer) else ()
    )
    metadata_only = stale and selected is None and home is None
    namespace_targets = []
    if not metadata_only:
        mounts = _mounts()
        if writable is not None:
            _exclusive_storage(mounts, writable[0], writable[1])
            namespace_targets.append((writable[0], writable[1]))
        if home is not None:
            _exclusive_storage(mounts, home_directory, home[3])
            namespace_targets.append((home_directory, home[3]))
    engine.check_namespace(namespace_targets)
    if not metadata_only:
        _other_consumers(
            data,
            container_id,
            rows,
            volume_rows,
            selected,
            targets,
            scope,
            missing_bind=missing_bind,
        )
    # Protect the project (including its marker), registry/evidence, and the
    # native host store. Keep them out of even the parent's engine trees, so
    # auxiliary container metadata removal cannot intersect those paths.
    host_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    for protected in (registration.project, box.state_root(), host_data / "opencode"):
        real = os.path.realpath(protected)
        _require(
            not any(workspace_uninstall._overlap(real, root) for root, _ in scope[:2]),
            "The project, registration or host history overlaps the Podman store.",
        )
    if not metadata_only:
        _require(
            container_id not in engine.mounted(rows),
            "The selected container is still mounted in the local Podman store.",
        )
    _require(
        engine.catalog("container") == containers
        and engine.catalog("volume") == volumes
        and engine.scope() == scope,
        "Podman resources changed during the disposal review.",
    )
    subject = None
    if selected is not None:
        state = selected["State"]
        subject = (
            selected["Id"],
            state["Status"],
            state.get("StartedAt"),
            state.get("FinishedAt"),
            writable,
        )
    return Resources(
        scope,
        subject,
        home,
        (
            tuple(item for item in containers if item != container_id),
            tuple(item for item in volumes if item != name),
        ),
    )


def _plan(box, path, ident, engine, *, observe=None, reviewed=None):
    if observe is None:
        observe = lambda: workspace_control.disposal_observation(path)
    registration, data, guard = _registered(box, path, ident)
    control = observe()
    _require(
        not workspace_service.controller_running(box, data),
        "A workspace controller is still present. Exit it before disposal.",
    )
    _require(
        workspace_identity.same_project(data, Path(registration.project)),
        "The original project directory identity could not be verified.",
    )
    resources = _review_resources(
        box, registration, data, engine, guard.container, reviewed=reviewed
    )
    _require(
        _registered(box, path, ident) == (registration, data, guard)
        and observe() == control,
        "The registration or stop intent changed during review.",
    )
    return Plan(
        registration,
        guard,
        control,
        resources.scope,
        resources.container,
        resources.volume,
        resources.other_resources,
    )


@contextlib.contextmanager
def _workspace_lock(path, registration):
    """Take the same lifecycle flock, without following a substituted lock."""
    with workspace_podman._path_fd(str(path.parent), os.geteuid(), directory=True) as (
        parent,
        info,
    ):
        stamp = workspace_podman._stamp(parent, info, True)
        _require(
            stamp == registration.directory_stamp,
            "The workspace registration directory changed.",
        )
        flags = os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(
                "lock", flags | os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
            )
        except FileExistsError:
            pinned = os.open("lock", flags | os.O_PATH, dir_fd=parent)
            try:
                workspace_control._file_info(pinned, private=False)
                fd = os.open(f"/proc/self/fd/{pinned}", os.O_RDWR | os.O_CLOEXEC)
            finally:
                os.close(pinned)
        try:

            def recheck():
                with workspace_podman._path_fd(
                    str(path.parent), os.geteuid(), directory=True
                ) as (now, current):
                    _require(
                        workspace_podman._stamp(now, current, True) == stamp,
                        "The locked registration directory changed.",
                    )
                    workspace_control._same_file(now, "lock", fd, private=False)
                    _require(
                        workspace_uninstall._mount_id(fd) == stamp[2],
                        "The workspace lifecycle lock crosses a mount boundary.",
                    )

            recheck()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused(
                    "The workspace has an active oc-box process. Exit it before disposal."
                ) from None
            recheck()
            yield recheck
        finally:
            os.close(fd)


def _review(plan):
    registration = plan.registration
    return "\n".join(
        (
            f"Discard guarded workspace: {registration.workspace_id}",
            f"Project directory and marker retained: {json.dumps(registration.project)}",
            f"Remove stopped container: {plan.container[0] if plan.container else 'already absent'}",
            f"Remove private home: {plan.volume[0] if plan.volume else 'already absent'}",
            "The private home includes conversations that never synced. No final sync will run.",
            "The project, host conversations, registration and original fence will be retained.",
            "No OpenCode startup, credential refresh or fence reset will occur.",
            "Do not change these resources with Podman or filesystem commands until disposal finishes.",
            f"To proceed, type DISCARD {registration.workspace_id}",
        )
    )


def _require_host():
    _require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not os.path.lexists("/.dockerenv")
        and not os.path.lexists("/run/.containerenv"),
        "Run guarded disposal on the host as your regular user.",
    )


def discard(box, ident):
    """Host only, called by the explicit guarded-discard CLI path."""
    attempted = False
    try:
        _require_host()
        _require(
            type(ident) is str and re.fullmatch(r"[a-f0-9]{32}", ident),
            "Guarded disposal requires the full workspace ID.",
        )
        path = box.state_root() / "workspaces" / ident / "workspace.json"
        registration, _, _ = _registered(box, path, ident)
        with (
            _workspace_lock(path, registration) as check_lock,
            workspace_control.disposal_admission(path) as observe,
            Engine(box) as engine,
        ):

            def check(*, reviewed=None):
                check_lock()
                plan = _plan(
                    box, path, ident, engine, observe=observe, reviewed=reviewed
                )
                check_lock()
                return plan

            reviewed = check()
            if not uninstall_tty.confirm_review(_review(reviewed), f"DISCARD {ident}"):
                raise Refused(
                    "Guarded disposal was not confirmed on the controlling terminal."
                )
            _require(
                check(reviewed=reviewed) == reviewed,
                "The confirmed disposal plan changed; no resource was removed.",
            )
            expected = reviewed
            for kind, target in (
                ("container", reviewed.container[0] if reviewed.container else None),
                ("volume", reviewed.volume[0] if reviewed.volume else None),
            ):
                if target is None:
                    continue
                _require(
                    check(reviewed=reviewed) == expected,
                    "The reviewed disposal scope changed; no further removal was attempted.",
                )
                attempted = True
                engine.remove(kind, target)
                expected = replace(expected, **{kind: None})
                _require(
                    check(reviewed=reviewed) == expected,
                    "Resource absence was not verified. No further removal was attempted.",
                )
        print(
            "Guarded workspace discarded. Project files, host archives, registration and the original fence were retained."
        )
        return 0
    except KeyboardInterrupt:
        if attempted:
            print(
                "Guarded disposal interrupted after a removal request. The result may be partial; "
                "inspect what remains before taking further action.",
                file=sys.stderr,
            )
        raise
    except Refused as exc:
        detail = (
            " The result may be partial; inspect what remains before taking further action."
            if attempted
            else ""
        )
        raise box.BoxError(str(exc) + detail) from None
    except (
        OSError,
        ValueError,
        RecursionError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        box.BoxError,
    ):
        raise box.BoxError(
            "The guarded disposal scope or result could not be verified. "
            "No further removal was attempted; inspect the selected local workspace."
        ) from None
