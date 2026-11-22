"""Explicit native V2 credential exports and fresh-store-only host restore.

Never run an auth command against the normal host profile. Standalone still
starts a native server, including its scheduler, and is only used here with
new config, data, state, cache, runtime and working directories on tmpfs.
"""

import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import history_reader, private_memory
    from .backup_crypto import BackupError
else:
    import history_reader
    import private_memory
    from backup_crypto import BackupError


MAX_EXPORT = 2 * 1024 * 1024
MAX_CREDENTIALS = 512
MAX_DATABASE = 16 * 1024 * 1024
_MAX_SCHEMA_OBJECTS = 256
_DATABASE = "opencode.db"
_LEGACY_NEIGHBORS = frozenset(("auth.json", "mcp-auth.json"))
_SHM = Path("/dev/shm")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_ID = re.compile(r"cred_[A-Za-z0-9_-]{1,128}")
_V2_VERSION = re.compile(
    rb"(?:opencode )?v?(2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\n"
)
_TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_VALUE_FIELDS = {
    "key": frozenset(("type", "key")),
    "oauth": frozenset(("type", "methodID", "access", "refresh", "expires")),
    "external": frozenset(("type", "methodID")),
}
_MESSAGES = {
    "invalid-export": "The native credential export is invalid or exceeds its limits.",
    "unsafe-source": "Supply a private owned native export file or a non-terminal binary stream.",
    "approval": "Native credential restore requires separate explicit approval.",
    "platform": "Native credential restore requires a supported Linux host.",
    "destination": "Native credential restore requires a fresh host OpenCode data directory with no existing store.",
    "native-version": "Native credential restore could not verify the stable OpenCode 2.x executable or its database path.",
    "native-operation": "Isolated native credential restore could not be verified; no destination database was created.",
    "system-config": "Managed native OpenCode configuration prevents isolated credential restore.",
    "memory-stage": "Verified Linux tmpfs staging is unavailable; no native credential store was created.",
}


class NativeAuthError(BackupError):
    """Fixed diagnostic; no paths, credential IDs or tool output."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(_MESSAGES[reason])


@dataclass(frozen=True)
class NativeAuthRestore:
    version: str
    count: int


def _fail(reason):
    raise NativeAuthError(reason) from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail("invalid-export")
        result[key] = value
    return result


def _identifier(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _decoded(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_EXPORT:
        _fail("invalid-export")
    try:
        rows = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: _fail("invalid-export"),
        )
    except (ValueError, RecursionError):
        _fail("invalid-export")
    if not isinstance(rows, list) or len(rows) > MAX_CREDENTIALS:
        _fail("invalid-export")
    ids = set()
    selected = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.keys() != {"id", "integrationID", "label", "active", "value"}
            or not isinstance(row["id"], str)
            or _ID.fullmatch(row["id"]) is None
            or row["id"] in ids
            or not _identifier(row["integrationID"])
            or not isinstance(row["label"], str)
            or len(row["label"]) > 4096
            or type(row["active"]) is not bool
            or not isinstance(row["value"], dict)
        ):
            _fail("invalid-export")
        ids.add(row["id"])
        name = row["integrationID"]
        selected[name] = selected.get(name, 0) + row["active"]
        value = row["value"]
        kind = value.get("type")
        required = _VALUE_FIELDS.get(kind) if isinstance(kind, str) else None
        if (
            required is None
            or not required <= value.keys() <= required | {"metadata"}
            or ("metadata" in value and not isinstance(value["metadata"], dict))
            or (kind == "key" and not isinstance(value["key"], str))
            or (kind != "key" and not _identifier(value["methodID"]))
            or (
                kind == "oauth"
                and (
                    not isinstance(value["access"], str)
                    or not isinstance(value["refresh"], str)
                    or type(value["expires"]) is not int
                    or not 0 <= value["expires"] <= 2**53 - 1
                )
            )
        ):
            _fail("invalid-export")
    if any(count != 1 for count in selected.values()):
        _fail("invalid-export")
    # Native lists the selected row last for each integration. Keep that
    # ordering on import; canonicalize JSON without changing the row order.
    try:
        canonical = json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    except (ValueError, RecursionError):
        _fail("invalid-export")
    if len(canonical) > MAX_EXPORT:
        _fail("invalid-export")
    return rows, canonical


def validate_native_export(payload):
    """Return a bounded, validated native export in canonical UTF-8 JSON."""
    return _decoded(payload)[1]


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_export_stream(stream):
    if stream.isatty():
        _fail("unsafe-source")
    result = bytearray()
    while len(result) <= MAX_EXPORT:
        chunk = stream.read(min(65536, MAX_EXPORT + 1 - len(result)))
        if not isinstance(chunk, bytes):
            _fail("unsafe-source")
        if not chunk:
            return bytes(result)
        result.extend(chunk)
    _fail("invalid-export")


def read_native_export(path=None, *, stream=None):
    """Read one explicit private local file or binary stream; never auto-export."""
    if (path is None) == (stream is None):
        _fail("unsafe-source")
    try:
        if stream is not None:
            return validate_native_export(_read_export_stream(stream))
        path = Path(path)
        with os.fdopen(os.open(path, _READ_FLAGS), "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                _fail("unsafe-source")
            if info.st_size > MAX_EXPORT:
                _fail("invalid-export")
            result = _read_export_stream(source)
            if _signature(info) != _signature(os.fstat(source.fileno())) or _signature(
                info
            ) != _signature(path.lstat()):
                _fail("unsafe-source")
        return validate_native_export(result)
    except (OSError, ValueError, TypeError, AttributeError):
        _fail("unsafe-source")


def _target(data_home):
    try:
        path = Path(data_home)
    except (ValueError, TypeError):
        _fail("destination")
    if path.anchor != "/" or path == Path("/") or ".." in path.parts:
        _fail("destination")
    return path / "opencode"


def _directory(path):
    fd = os.open("/", _DIR_FLAGS)
    uid = os.getuid()
    try:
        for name in path.parts[1:]:
            parent = os.fstat(fd)
            if parent.st_uid not in (0, uid) or (
                parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX
            ):
                _fail("destination")
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != uid or info.st_mode & 0o022:
            _fail("destination")
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _neighbors(fd):
    """Only inspect metadata. Legacy credential contents never enter V2 staging."""
    result = {}
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name not in _LEGACY_NEIGHBORS:
                _fail("destination")
            info = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                _fail("destination")
            result[entry.name] = _signature(info)
    return result


def check_native_destination(data_home):
    """Metadata-only fresh destination check. Repeat after approval and staging."""
    try:
        fd = _directory(_target(data_home))
        if fd is not None:
            try:
                _neighbors(fd)
            finally:
                os.close(fd)
    except (OSError, ValueError):
        _fail("destination")


def _mount_id(fd):
    with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
        for line in stream.read(4096).splitlines():
            if line.startswith(b"mnt_id:\t"):
                return int(line[8:])
    _fail("memory-stage")


def _tmpfs_mount(fd):
    ident = str(_mount_id(fd)).encode("ascii")
    with open("/proc/self/mountinfo", "rb") as stream:
        for line in stream:
            fields, _, filesystem = line.partition(b" - ")
            parts = fields.split()
            if (
                len(parts) >= 5
                and parts[0] == ident
                and parts[4] == b"/dev/shm"
                and filesystem.split()[:1] == [b"tmpfs"]
            ):
                return int(ident)
    _fail("memory-stage")


@contextmanager
def _memory_stage():
    with tempfile.TemporaryDirectory(dir=_SHM, prefix="oc-box-native-auth-") as name:
        stage = Path(name)
        base = os.open(_SHM, _DIR_FLAGS)
        try:
            info = os.fstat(base)
            if info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
                _fail("memory-stage")
            mount = _tmpfs_mount(base)
            child = os.open(stage.name, _DIR_FLAGS, dir_fd=base)
            try:
                info = os.fstat(child)
                if (
                    info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or _mount_id(child) != mount
                ):
                    _fail("memory-stage")
            finally:
                os.close(child)
        finally:
            os.close(base)
        yield stage


def _environment(stage):
    for name in (
        "home",
        "config",
        "data",
        "state",
        "cache",
        "runtime",
        "system",
        "cwd",
        "tmp",
    ):
        (stage / name).mkdir(mode=0o700)
    return history_reader.fence_environment(
        {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(stage / "home"),
            "TMPDIR": str(stage / "tmp"),
            "XDG_RUNTIME_DIR": str(stage / "runtime"),
            **{
                f"XDG_{name.upper()}_HOME": str(stage / name)
                for name in ("config", "data", "state", "cache")
            },
        },
    ) | {
        "XDG_CONFIG_DIRS": str(stage / "system"),
        "XDG_DATA_DIRS": str(stage / "system"),
        "OPENCODE_DISABLE_FFF": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }


def _system_config():
    try:
        history_reader._check_system_config()
    except history_reader.Refusal:
        _fail("system-config")


# No preexec_fn: the dashboard can be multithreaded. Bounds apply to the child
# and its private server. Only a fixed helper and public executable/args use argv.
_EXEC = (
    "import os,resource,sys;"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
    "n=int(sys.argv[1]);old=resource.getrlimit(resource.RLIMIT_FSIZE);"
    "n=min(n,old[1]) if old[1]>=0 else n;"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(n,n));"
    "ifexec=sys.argv[2:];os.execv(ifexec[0],ifexec)"
)


class _Native:
    def __init__(self, binary, stage, environment):
        try:
            source = Path(binary)
            if not source.is_absolute():
                _fail("native-version")
            self.binary = source.resolve(strict=True)
            info = self.binary.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in (0, os.getuid())
                or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
                or not os.access(self.binary, os.X_OK)
            ):
                _fail("native-version")
            self.identity = _signature(info)
        except (OSError, RuntimeError, ValueError, TypeError):
            _fail("native-version")
        self.cwd = stage / "cwd"
        self.env = environment

    def _check(self):
        try:
            if _signature(self.binary.lstat()) != self.identity:
                _fail("native-version")
        except OSError:
            _fail("native-version")

    def call(self, *args, payload=None, limit=MAX_EXPORT, reason="native-operation"):
        _system_config()
        self._check()
        try:
            with (
                os.fdopen(
                    private_memory.create("oc-box-native-auth-stdin"), "w+b"
                ) as source,
                os.fdopen(
                    private_memory.create("oc-box-native-auth-stdout"), "w+b"
                ) as output,
            ):
                if payload is not None:
                    source.write(payload)
                    source.seek(0)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        _EXEC,
                        str(MAX_DATABASE),
                        str(self.binary),
                        *args,
                    ],
                    cwd=self.cwd,
                    env=self.env,
                    stdin=source,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    umask=0o077,
                )
                try:
                    code = process.wait(timeout=15)
                except BaseException:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait()
                    raise
                self._check()
                # In the audited native contract the standalone CLI reaps its
                # server. Do not use output or the DB while its group lives.
                deadline = time.monotonic() + 1
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        _fail(reason)
                    time.sleep(0.02)
                if code != 0 or os.fstat(output.fileno()).st_size > limit:
                    _fail(reason)
                output.seek(0)
                return output.read(limit + 1)
        except (OSError, subprocess.SubprocessError, ValueError):
            _fail(reason)


def _sidecars(db):
    return any(
        os.path.lexists(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal")
    )


def _fresh_database(db, stage, credentials=0, *, expected_schema=None, read=False):
    """Read only a quiescent scratch database; reject any saved work."""
    if _sidecars(db) or db.parent.resolve(strict=True) != db.parent:
        _fail("native-operation")
    with os.fdopen(os.open(db, _READ_FLAGS), "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or not 0 < info.st_size <= MAX_DATABASE
        ):
            _fail("native-operation")
        # Unlike history recovery, this is a newly created tmpfs DB with no
        # sidecars and no surviving native process. Immutable read-only mode
        # does not create its own SQLite sidecars in that private directory.
        with closing(
            sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        ) as database:
            deadline = time.monotonic() + 1
            database.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
            database.execute("PRAGMA trusted_schema=OFF")
            schema = tuple(
                database.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "ORDER BY type, name LIMIT ?",
                    (_MAX_SCHEMA_OBJECTS + 1,),
                )
            )
            if (
                not schema
                or len(schema) > _MAX_SCHEMA_OBJECTS
                or (expected_schema is not None and schema != expected_schema)
            ):
                _fail("native-operation")
            seen_credentials = 0
            for kind, name, _, definition in schema:
                # The verified native scratch store only needs ordinary tables
                # and indexes, not triggers, views or virtual tables.
                if kind == "index":
                    continue
                if (
                    kind != "table"
                    or not isinstance(name, str)
                    or not _TABLE_NAME.fullmatch(name)
                    or not isinstance(definition, str)
                    or not definition.upper().startswith("CREATE TABLE ")
                ):
                    _fail("native-operation")
                if name in ("migration", "__drizzle_migrations", "sqlite_sequence"):
                    continue
                if name == "credential":
                    seen_credentials = len(
                        database.execute(
                            'SELECT 1 FROM "credential" LIMIT ?', (credentials + 1,)
                        ).fetchall()
                    )
                elif name == "project":
                    # The native importer registers its empty config directory.
                    # Allow only that inert scratch record, not project commands.
                    projects = database.execute(
                        'SELECT worktree, commands, sandboxes FROM "project" LIMIT 2'
                    ).fetchall()
                    if projects not in (
                        [],
                        [(str(stage / "config/opencode"), None, "[]")],
                    ):
                        _fail("native-operation")
                elif database.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone():
                    # Includes schedules, automations, jobs, KV and history,
                    # even when future native versions add new table names.
                    _fail("native-operation")
            if seen_credentials != credentials:
                _fail("native-operation")
        result = source.read(MAX_DATABASE + 1) if read else None
        if (
            (
                result is not None
                and (
                    len(result) != info.st_size
                    or not result.startswith(b"SQLite format 3\0")
                )
            )
            or _signature(os.fstat(source.fileno())) != _signature(info)
            or _signature(db.lstat()) != _signature(info)
            or db.parent.resolve(strict=True) != db.parent
            or _sidecars(db)
        ):
            _fail("native-operation")
        return schema, result


def _stage_database(stage, binary, rows, canonical, version):
    native = _Native(binary, stage, _environment(stage))
    reported = native.call("--version", limit=64, reason="native-version")
    match = _V2_VERSION.fullmatch(reported)
    detected = match[1].decode("ascii") if match else None
    if detected is None or (version is not None and version != detected):
        _fail("native-version")
    db = stage / "data" / "opencode" / _DATABASE
    if native.call("debug", "paths", "db", limit=4096) != os.fsencode(db) + b"\n":
        _fail("native-version")
    try:
        if db.parent.resolve() != db.parent or os.path.lexists(db) or _sidecars(db):
            _fail("native-operation")
        if _decoded(native.call("auth", "export", "--standalone"))[0]:
            _fail("native-operation")
        schema, _ = _fresh_database(db, stage)
        native.call("auth", "import", "--standalone", payload=canonical, limit=1024)
        # A later standalone command can start a scheduler again. Recheck
        # emptiness and the fresh schema before asking native to re-export.
        _fresh_database(db, stage, len(rows), expected_schema=schema)
        verified = _decoded(native.call("auth", "export", "--standalone"))[0]
        key = lambda row: row["id"]
        if validate_native_export(
            json.dumps(sorted(rows, key=key), ensure_ascii=False).encode()
        ) != validate_native_export(
            json.dumps(sorted(verified, key=key), ensure_ascii=False).encode()
        ):
            _fail("native-operation")
        _, contents = _fresh_database(
            db, stage, len(rows), expected_schema=schema, read=True
        )
        return detected, contents
    except NativeAuthError as error:
        if error.reason == "invalid-export":
            _fail("native-operation")
        raise
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        _fail("native-operation")


def prepare_native_export(payload, *, binary, data_home, confirmed=False, version=None):
    """Return (safe result, verified private DB bytes or None), without publishing.

    The caller must bind consent to the payload and the destination, keep native
    OpenCode stopped, and publish via the same reviewed destination plan. The
    returned DB bytes are plaintext secrets; tmpfs and memory may be swapped.
    """
    if confirmed is not True:
        _fail("approval")
    if sys.platform != "linux" or os.getuid() != os.geteuid():
        _fail("platform")
    rows, canonical = _decoded(payload)
    path = _target(data_home)
    check_native_destination(path.parent)
    if not rows:
        return NativeAuthRestore("", 0), None
    _system_config()
    try:
        with _memory_stage() as stage:
            detected, database = _stage_database(
                stage, binary, rows, canonical, version
            )
    except OSError:
        _fail("memory-stage")
    return NativeAuthRestore(detected, len(rows)), database
