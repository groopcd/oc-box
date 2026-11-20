"""Read-only host activity journal policy and invocation-local overrides."""

import argparse
import json
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_CONFIG_LIMIT = 64 * 1024
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


def _read_config():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if base.anchor != "/" or ".." in base.parts:
        raise ValueError
    directory = base / "oc-box"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open("/", flags)
    try:
        for component in directory.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                return None
            os.close(parent)
            parent = child
        try:
            fd = os.open(
                "config.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o022
                or before.st_nlink != 1
                or before.st_size > _CONFIG_LIMIT
            ):
                raise ValueError
            raw = bytearray()
            while len(raw) <= _CONFIG_LIMIT:
                chunk = os.read(fd, _CONFIG_LIMIT + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            if (
                len(raw) > _CONFIG_LIMIT
                or len(raw) != before.st_size
                or after.st_nlink != 1
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise ValueError
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


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


def _load(default_file_size, default_max_files):
    # Internal callers can keep synthetic test bounds below the public minimum.
    _quantity(default_file_size, 1, _MAX_FILE_SIZE)
    _quantity(default_max_files, 1, _MAX_FILES)
    raw = _read_config()
    config = (
        {}
        if raw is None
        else json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    )
    if not isinstance(config, dict):
        raise TypeError
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
    override_directory, override_journald = _OVERRIDES.get()
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
        return _load(default_file_size, default_max_files)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    # Don't attach a JSON decoder error or OSError that may retain private data.
    raise LogConfigError(_INVALID_CONFIG) from None
