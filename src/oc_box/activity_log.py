"""Private, bounded activity metadata; never a transcript or error log.

Only fixed event/command/outcome names, workspace IDs and numeric measurements
are accepted. Diagnostic failures are deliberately silent and cannot turn a
successful lifecycle operation into a failure. ``root`` is the *state* root;
the journal lives in its private ``log/`` directory. No reader creates files.
"""

import fcntl
import json
import os
import re
import stat
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

EVENTS = frozenset(
    {
        "command.started",
        "command.finished",
        "command.failed",
        "tui.started",
        "tui.finished",
        "session.archived",
        "session.unarchived",
        "session.deleted",
        "session.favorited",
    }
)
COMMANDS = frozenset(
    {
        "start",
        "stop",
        "sync",
        "reset",
        "repair",
        "terminal",
        "web",
        "upgrade",
        "secrets",
        "credentials",
        "backup",
        "backup-info",
        "restore",
        "purge",
        "list",
        "distros",
        "status",
        "doctor",
        "completion",
        "tui",
        "sessions",
    }
)
OUTCOMES = frozenset({"success", "failure", "cancelled", "refused", "unavailable"})

_FILE = "activity.jsonl"
_LOCK = "activity.lock"
_MAX_FILE_BYTES = 256 * 1024
_BACKUP_COUNT = 3
_MAX_RECORD_BYTES = 1024
_MAX_READ_EVENTS = 10000
_LOCK_TIMEOUT = 0.25
_FIELDS = frozenset(
    {
        "timestamp",
        "event",
        "command",
        "workspace",
        "outcome",
        "exit_code",
        "duration_ms",
    }
)
_WORKSPACE = re.compile(r"[0-9a-f]{32}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


def _root(root):
    if root is None:
        root = os.environ.get("OC_BOX_STATE_HOME")
        if root is None:
            root = (
                Path(
                    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                )
                / "oc-box"
            )
    # Do not resolve symlinks: every component is opened with O_NOFOLLOW below.
    path = Path(root).expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("Invalid activity directory")
    return path


def _private_directory(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Unsafe activity directory")


@contextmanager
def _log_directory(root, *, create):
    path = _root(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        # Ancestors may be public (/tmp, /home); state/log must be private.
        _private_directory(fd)
        if create:
            try:
                os.mkdir("log", 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        child = os.open("log", flags, dir_fd=fd)
        os.close(fd)
        fd = child
        _private_directory(fd)
        yield fd
    finally:
        os.close(fd)


def _open_file(directory, name, *, create=False, writable=False):
    flags = (
        (os.O_RDWR if writable else os.O_RDONLY)
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
        | os.O_NONBLOCK
    )
    if create:
        flags |= os.O_CREAT
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Unsafe activity file")
        return fd
    except Exception:
        os.close(fd)
        raise


@contextmanager
def _locked(directory, *, write):
    fd = _open_file(directory, _LOCK, create=write, writable=write)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Activity journal busy") from None
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


def _choice(value, choices):
    return isinstance(value, str) and value in choices


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _sanitize(record):
    if not isinstance(record, dict) or set(record) - _FIELDS:
        return None
    if not _choice(record.get("event"), EVENTS):
        return None
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
        return None
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        return None
    if "command" in record and not _choice(record["command"], COMMANDS):
        return None
    if "outcome" in record and not _choice(record["outcome"], OUTCOMES):
        return None
    workspace = record.get("workspace")
    if "workspace" in record and not (
        isinstance(workspace, str) and _WORKSPACE.fullmatch(workspace)
    ):
        return None
    if "exit_code" in record and not _integer(record["exit_code"], -255, 255):
        return None
    if "duration_ms" in record and not _integer(record["duration_ms"], 0, 2**53 - 1):
        return None
    return dict(record)


def _validate_files(directory):
    for name in (_FILE, *(f"{_FILE}.{index}" for index in range(1, _BACKUP_COUNT + 1))):
        try:
            fd = _open_file(directory, name)
        except FileNotFoundError:
            continue
        try:
            if os.fstat(fd).st_size > _MAX_FILE_BYTES:
                raise ValueError("Oversized activity journal")
        finally:
            os.close(fd)


def _rotate(directory):
    for index in range(_BACKUP_COUNT, 0, -1):
        source = _FILE if index == 1 else f"{_FILE}.{index - 1}"
        try:
            os.replace(
                source, f"{_FILE}.{index}", src_dir_fd=directory, dst_dir_fd=directory
            )
        except FileNotFoundError:
            pass


def record_event(
    event,
    *,
    command=None,
    workspace=None,
    outcome=None,
    exit_code=None,
    duration_ms=None,
    root=None,
) -> bool:
    """Append allowlisted metadata; return False silently on invalid/unsafe I/O.

    Files are 0600, state/log directories are 0700, and symlinks, hardlinks,
    foreign ownership and unsafe existing permissions are refused, not repaired.
    A shared lock serializes append/rotation. Retention is four 256-KiB files.
    No exception text or arbitrary caller field is ever written.
    """
    try:
        record = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "event": event,
        }
        for name, value in (
            ("command", command),
            ("workspace", workspace),
            ("outcome", outcome),
            ("exit_code", exit_code),
            ("duration_ms", duration_ms),
        ):
            if value is not None:
                record[name] = value
        record = _sanitize(record)
        if record is None:
            return False
        payload = (
            json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
        if len(payload) > _MAX_RECORD_BYTES or len(payload) > _MAX_FILE_BYTES:
            return False
        with (
            _log_directory(root, create=True) as directory,
            _locked(directory, write=True),
        ):
            _validate_files(directory)
            fd = _open_file(directory, _FILE, create=True, writable=True)
            try:
                size = os.fstat(fd).st_size
                # Recover from a partial final record without swallowing the next.
                prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
                if size + len(prefix) + len(payload) > _MAX_FILE_BYTES:
                    os.close(fd)
                    fd = None
                    _rotate(directory)
                    fd = _open_file(directory, _FILE, create=True, writable=True)
                    prefix = b""
                os.lseek(fd, 0, os.SEEK_END)
                pending = prefix + payload
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise OSError("Activity write failed")
                    pending = pending[written:]
                os.fsync(fd)
            finally:
                if fd is not None:
                    os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - optional diagnostics must never abort callers.
        # Diagnostics must not change lifecycle outcomes, including unavailable
        # filesystems/locks and unsupported platforms. Never log the exception.
        return False


def read_events(root=None, *, limit=1000) -> list[dict]:
    """Return the latest valid records oldest-first, without creating anything.

    Malformed/oversized records and unsafe files are ignored. The read is bounded
    by four journal files and at most 10,000 returned records. Failure is empty.
    """
    if type(limit) is not int or limit <= 0:
        return []
    records = deque(maxlen=min(limit, _MAX_READ_EVENTS))
    try:
        with (
            _log_directory(root, create=False) as directory,
            _locked(directory, write=False),
        ):
            names = [
                *(f"{_FILE}.{index}" for index in range(_BACKUP_COUNT, 0, -1)),
                _FILE,
            ]
            for name in names:
                try:
                    fd = _open_file(directory, name)
                except (OSError, ValueError):
                    continue
                try:
                    if os.fstat(fd).st_size > _MAX_FILE_BYTES:
                        continue
                    data = bytearray()
                    while len(data) <= _MAX_FILE_BYTES:
                        chunk = os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > _MAX_FILE_BYTES:
                        continue
                    for line in data.splitlines():
                        if len(line) > _MAX_RECORD_BYTES:
                            continue
                        try:
                            record = _sanitize(json.loads(line))
                        except (ValueError, UnicodeError, RecursionError):
                            continue
                        if record is not None:
                            records.append(record)
                finally:
                    os.close(fd)
    except Exception:  # noqa: BLE001 - malformed/unavailable diagnostics fail closed.
        return []
    return list(records)
