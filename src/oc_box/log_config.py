"""Host activity journal policy and its explicit dashboard settings writer."""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

_CONFIG_LIMIT = 64 * 1024
_CONFIG_NAME = "config.json"
# The updater owns the other writable keys in this same file.
_CONFIG_LOCK = ".update-settings.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_MIN_FILE_SIZE = 1024
_MAX_FILE_SIZE = 4 * 1024 * 1024
_MAX_USE = 16 * 1024 * 1024
_MAX_FILES = 16
_MAX_KEEP_FREE = 2**63 - 1
_MAX_AGE = 365 * 24 * 60 * 60
_SIZE_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}
_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SUFFIX = re.compile(r"([0-9]{1,19})([A-Za-z])")
_KEYS = frozenset(
    {
        "directory",
        "journald",
        "SystemMaxFileSize",
        "SystemMaxFiles",
        "SystemMaxUse",
        "SystemKeepFree",
        "MaxFileSec",
        "MaxRetentionSec",
    }
)
_INVALID_CONFIG = "Activity log configuration is invalid or unsafe."
_OVERRIDES: ContextVar[tuple[Path | None, bool | None]] = ContextVar(
    "oc_box_log_overrides", default=(None, None)
)


class LogConfigError(ValueError):
    """Fixed diagnostic with no config values, paths or underlying errors."""


class _ConfigChanged(Exception):
    pass


class _CommitUnconfirmed(Exception):
    pass


@dataclass(frozen=True)
class LogPolicy:
    directory: Path | None
    max_file_size: int
    max_files: int  # Includes the active file.
    max_use: int
    keep_free: int
    max_file_sec: int
    max_retention_sec: int
    configured: bool = False
    journald: bool = False


@dataclass(frozen=True)
class _ConfigSnapshot:
    directory: Path
    parent: tuple | None
    entry: tuple | None
    digest: bytes | None


@dataclass(frozen=True)
class HostLogSettings:
    """Saved host settings, independent of the current invocation's overrides."""

    directory: str | None
    effective_directory: Path | None
    journald: bool
    journald_configured: bool
    _snapshot: _ConfigSnapshot = field(repr=False)


def _config_directory():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if base.anchor != "/" or ".." in base.parts:
        raise ValueError
    return base / "oc-box"


def _check_config_directory(fd, *, leaf=False):
    info = os.fstat(fd)
    uid = os.getuid()
    if leaf:
        if info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError
    elif info.st_uid not in (0, uid) or (
        info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
    ):
        # A sticky root-owned /tmp-like ancestor is safe when each child
        # must itself belong to the user or root.
        raise ValueError


@contextmanager
def _open_config_directory(path, *, writer=False, create=False):
    parent = os.open("/", _DIR_FLAGS)
    try:
        if writer:
            _check_config_directory(parent)
        for index, component in enumerate(path.parts[1:], 2):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                else:
                    os.fsync(parent)
                child = os.open(component, _DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
            if writer:
                _check_config_directory(parent, leaf=index == len(path.parts))
        yield parent
    finally:
        os.close(parent)


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (
        *_identity(info),
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _stat_entry(parent, name):
    return os.stat(name, dir_fd=parent, follow_symlinks=False)


def _check_config_file(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_nlink != 1
        or not 0 <= info.st_size <= _CONFIG_LIMIT
    ):
        raise ValueError


def _read_config_file(parent, *, writer=False):
    if parent is None:
        return None, None
    try:
        entry = _stat_entry(parent, _CONFIG_NAME)
    except FileNotFoundError:
        return None, None
    _check_config_file(entry)
    if writer and (entry.st_mode & 0o7111 or not entry.st_mode & stat.S_IWUSR):
        raise ValueError
    fd = os.open(_CONFIG_NAME, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
    try:
        stamp = _stamp(entry)
        if _stamp(os.fstat(fd)) != stamp:
            raise ValueError
        raw = bytearray()
        while len(raw) <= _CONFIG_LIMIT:
            chunk = os.read(fd, _CONFIG_LIMIT + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if (
            len(raw) > _CONFIG_LIMIT
            or len(raw) != entry.st_size
            or _stamp(os.fstat(fd)) != stamp
            or _stamp(_stat_entry(parent, _CONFIG_NAME)) != stamp
        ):
            raise ValueError
        return bytes(raw), stamp
    finally:
        os.close(fd)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _directory(value):
    if not isinstance(value, str) or not value.isprintable():
        raise ValueError
    path = Path(value).expanduser()
    if (
        path.anchor != "/"
        or path == Path("/")
        or ".." in path.parts
        or not str(path).isprintable()
    ):
        raise ValueError
    # This is lexical only; journal access must separately validate each component.
    return path


def directory_argument(value: str) -> Path:
    """Parse a lexical directory without opening or resolving it."""
    try:
        return _directory(value)
    except (OSError, ValueError, RuntimeError):
        pass
    raise argparse.ArgumentTypeError(
        "must be an absolute non-root directory without '..' or non-printing characters"
    )


@contextmanager
def overrides(*, directory: Path | str | None = None, journald: bool | None = None):
    """Override the host policy in this context; None retains an enclosing value."""
    try:
        if directory is not None:
            directory = _directory(
                str(directory) if isinstance(directory, Path) else directory
            )
        if journald is not None and type(journald) is not bool:
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise LogConfigError(_INVALID_CONFIG) from None

    outer_directory, outer_journald = _OVERRIDES.get()
    token = _OVERRIDES.set(
        (
            outer_directory if directory is None else directory,
            outer_journald if journald is None else journald,
        )
    )
    try:
        yield
    finally:
        _OVERRIDES.reset(token)


def add_launch_arguments(parser: argparse.ArgumentParser) -> None:
    """Register launcher log policy and expanded diagnostic flags."""
    parser.add_argument(
        "--log-dir",
        type=directory_argument,
        metavar="DIRECTORY",
        help="use this private activity log directory for this invocation",
    )
    mirror = parser.add_mutually_exclusive_group()
    mirror.add_argument(
        "--journald",
        dest="journald",
        action="store_true",
        help="also mirror activity events to the local system journal on the host",
    )
    mirror.add_argument(
        "--no-journald",
        dest="journald",
        action="store_false",
        help="disable host system journal mirroring for this invocation",
    )
    parser.set_defaults(journald=None)
    parser.add_argument(
        "--lifecycle-diagnostics",
        action="store_true",
        help="show expanded launcher lifecycle diagnostics",
    )


def _quantity(value, minimum, maximum, units=None):
    if units is not None and isinstance(value, str):
        match = _SUFFIX.fullmatch(value)
        if match and match[2] in units:
            value = int(match[1]) * units[match[2]]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError
    return value


@dataclass(frozen=True)
class _JsonFloat:
    # Keep unrelated numbers exactly, including values that a binary float
    # would round, overflow, or underflow during a read-modify-write.
    literal: str


def _parse_config(raw):
    config = (
        {}
        if raw is None
        else json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_JsonFloat,
        )
    )
    if not isinstance(config, dict):
        raise TypeError
    return config


def _policy(
    config,
    default_file_size=256 * 1024,
    default_max_files=4,
    overrides=(None, None),
):
    # Internal callers can keep synthetic test bounds below the public minimum.
    _quantity(default_file_size, 1, _MAX_FILE_SIZE)
    _quantity(default_max_files, 1, _MAX_FILES)
    logs = config.get("logs", {})
    if not isinstance(logs, dict) or logs.keys() - _KEYS:
        raise ValueError

    def number(key, default, minimum, maximum, units=None):
        if key not in logs:
            return default
        return _quantity(logs[key], minimum, maximum, units)

    directory = _directory(logs["directory"]) if "directory" in logs else None
    journald = logs.get("journald", False)
    if type(journald) is not bool:
        raise ValueError
    override_directory, override_journald = overrides
    max_file_size = number(
        "SystemMaxFileSize",
        default_file_size,
        _MIN_FILE_SIZE,
        _MAX_FILE_SIZE,
        _SIZE_UNITS,
    )
    max_files = number("SystemMaxFiles", default_max_files, 1, _MAX_FILES)
    return LogPolicy(
        directory=directory if override_directory is None else override_directory,
        max_file_size=max_file_size,
        max_files=max_files,
        max_use=number(
            "SystemMaxUse",
            min(max_file_size * max_files, _MAX_USE),
            _MIN_FILE_SIZE,
            _MAX_USE,
            _SIZE_UNITS,
        ),
        keep_free=number("SystemKeepFree", 0, 0, _MAX_KEEP_FREE, _SIZE_UNITS),
        max_file_sec=number("MaxFileSec", 0, 0, _MAX_AGE, _TIME_UNITS),
        max_retention_sec=number("MaxRetentionSec", 0, 0, _MAX_AGE, _TIME_UNITS),
        configured=bool(logs)
        or override_directory is not None
        or override_journald is not None,
        journald=journald if override_journald is None else override_journald,
    )


def load(default_file_size=256 * 1024, default_max_files=4) -> LogPolicy:
    """Load host policy without touching the journal; refuse unsafe configuration."""
    try:
        with _open_config_directory(_config_directory()) as parent:
            raw, _ = _read_config_file(parent)
        return _policy(
            _parse_config(raw), default_file_size, default_max_files, _OVERRIDES.get()
        )
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    # Don't attach a JSON decoder error or OSError that may retain private data.
    raise LogConfigError(_INVALID_CONFIG) from None


def _snapshot(path, parent, raw, entry):
    return _ConfigSnapshot(
        path,
        None if parent is None else _identity(os.fstat(parent)),
        entry,
        None if raw is None else hashlib.sha256(raw).digest(),
    )


def _host_settings(config, snapshot):
    policy = _policy(config)
    logs = config.get("logs", {})
    return HostLogSettings(
        logs.get("directory"),
        policy.directory,
        policy.journald,
        "journald" in logs,
        snapshot,
    )


def load_host_settings() -> HostLogSettings:
    """Take a bounded, read-only snapshot for an explicitly saved Settings draft."""
    try:
        path = _config_directory()
        with _open_config_directory(path) as parent:
            raw, entry = _read_config_file(parent)
            return _host_settings(
                _parse_config(raw), _snapshot(path, parent, raw, entry)
            )
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    raise LogConfigError(_INVALID_CONFIG) from None


def _encode_config(config):
    def encode(value):
        if isinstance(value, _JsonFloat):
            return value.literal
        if isinstance(value, dict):
            return (
                "{"
                + ",".join(
                    json.dumps(key) + ":" + encode(item) for key, item in value.items()
                )
                + "}"
            )
        if isinstance(value, list):
            return "[" + ",".join(map(encode, value)) + "]"
        return json.dumps(value, allow_nan=False)

    raw = (encode(config) + "\n").encode("ascii")
    if len(raw) > _CONFIG_LIMIT:
        raise ValueError
    return raw


@contextmanager
def _config_write_lock(parent):
    try:
        fd = os.open(
            _CONFIG_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        _check_lock_file(_stat_entry(parent, _CONFIG_LOCK))
        fd = os.open(_CONFIG_LOCK, os.O_RDWR | _FILE_FLAGS, dir_fd=parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_write_lock(parent, fd)
        yield fd
    finally:
        os.close(fd)


def _check_lock_file(info):
    _check_config_file(info)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError


def _check_write_lock(parent, fd):
    info = os.fstat(fd)
    _check_lock_file(info)
    if _stamp(info) != _stamp(_stat_entry(parent, _CONFIG_LOCK)):
        raise ValueError


def _publish_config(parent, name, *, missing):
    if not missing:
        os.replace(name, _CONFIG_NAME, src_dir_fd=parent, dst_dir_fd=parent)
        return
    # First save must not overwrite an entry that appeared even after the
    # final check. Linux renameat2 avoids a crash window with two hard links.
    import ctypes

    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    no_replace = 1  # Linux RENAME_NOREPLACE
    if rename(parent, os.fsencode(name), parent, os.fsencode(_CONFIG_NAME), no_replace):
        if ctypes.get_errno() == errno.EEXIST:
            raise _ConfigChanged
        raise OSError


def _verify_parent(path, parent):
    with _open_config_directory(path, writer=True) as current:
        if current is None or _identity(os.fstat(current)) != _identity(
            os.fstat(parent)
        ):
            raise _ConfigChanged


def _commit_config(parent, path, lock, expected, raw):
    name = ".host-logging." + secrets.token_hex(16) + ".tmp"
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=parent
    )
    published = False
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        _verify_parent(path, parent)
        _check_write_lock(parent, lock)
        staged = os.fstat(fd)
        _check_config_file(staged)
        if _stamp(staged) != _stamp(_stat_entry(parent, name)):
            raise _ConfigChanged
        old, old_entry = _read_config_file(parent, writer=True)
        if _snapshot(path, parent, old, old_entry) != expected:
            raise _ConfigChanged
        # The protected ancestry and shared lock exclude other UID and
        # cooperating writers, not same-UID editors ignoring this lock.
        _publish_config(parent, name, missing=old_entry is None)
        published = True
        try:
            os.fsync(parent)
            _verify_parent(path, parent)
            actual, entry = _read_config_file(parent, writer=True)
            if actual != raw or entry is None or entry[:2] != _identity(staged):
                raise _CommitUnconfirmed
            return _snapshot(path, parent, actual, entry)
        except (OSError, ValueError, _ConfigChanged):
            raise _CommitUnconfirmed from None
    finally:
        if not published:
            try:
                if _identity(_stat_entry(parent, name)) == _identity(os.fstat(fd)):
                    os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        os.close(fd)


def _save_host_settings(settings, directory, journald):
    path = _config_directory()
    expected = settings._snapshot
    if path != expected.directory:
        raise _ConfigChanged
    with _open_config_directory(path, writer=True) as parent:
        raw, entry = _read_config_file(parent, writer=True)
        if _snapshot(path, parent, raw, entry) != expected:
            raise _ConfigChanged
    config = _parse_config(raw)
    saved = _host_settings(config, expected)
    logs = dict(config.get("logs", {}))
    if directory is None:
        logs.pop("directory", None)
    else:
        logs["directory"] = directory
    if journald != saved.journald:
        logs["journald"] = journald
    if logs == config.get("logs", {}):
        return saved, False
    config["logs"] = logs
    candidate = _encode_config(config)
    verified = _parse_config(candidate)
    _policy(verified)
    if verified != config:
        raise ValueError
    with (
        _open_config_directory(path, writer=True, create=True) as parent,
        _config_write_lock(parent) as lock,
    ):
        current, current_entry = _read_config_file(parent, writer=True)
        latest = _snapshot(path, parent, current, current_entry)
        if expected.parent is None:
            # We may have created the initially absent config directories.
            if latest.entry is not None:
                raise _ConfigChanged
        elif latest != expected:
            raise _ConfigChanged
        committed = _commit_config(parent, path, lock, latest, candidate)
    return _host_settings(verified, committed), True


def save_host_settings(
    settings: HostLogSettings, *, directory: str | None, journald: bool
) -> tuple[HostLogSettings, bool]:
    """Explicitly save just the two editable keys against the displayed snapshot."""
    try:
        if directory is not None:
            if not isinstance(directory, str) or len(directory) > _CONFIG_LIMIT:
                raise ValueError
            _directory(directory)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    else:
        if type(journald) is not bool:
            raise LogConfigError("System journal mirroring must be on or off.")
        try:
            return _save_host_settings(settings, directory, journald)
        except _ConfigChanged:
            message = "Host config changed. Reopen the dashboard before saving."
        except _CommitUnconfirmed:
            message = "Host logging save could not be verified. Reopen the dashboard to check."
        except (AttributeError, OSError, ValueError, TypeError, RuntimeError):
            message = "Could not safely save host logging settings."
        raise LogConfigError(message) from None
    raise LogConfigError(
        "Enter a valid absolute or home-relative log directory."
    ) from None
