"""Private, bounded activity metadata; never a transcript or error log.

Only fixed event categories, workspace IDs and bounded numeric measurements are
accepted. Diagnostic failures are deliberately silent and cannot turn a successful
lifecycle operation into a failure. ``root`` is the *state* root; the journal
lives in its private ``log/`` directory. No reader creates files.
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
        "lifecycle.stage",
        "lifecycle.notice",
        "history.wait",
        "history.pass",
        "history.recovery",
        "history.result",
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
LEVELS = ("debug", "info", "warn", "error")
STAGES = frozenset(
    {
        "backend",
        "version",
        "workspace",
        "container",
        "credentials",
        "configuration",
        "bootstrap",
        "dependencies",
        "service",
        "session",
        "client",
        "history-wait",
        "history-sync",
        "history-recovery",
        "standalone",
        "private-endpoint",
        "post-stop-reader",
        "foreground-query",
        "background-query",
        "poll-delay",
        "shutdown",
    }
)
REASONS = frozenset(
    {
        "timeout",
        "transport",
        "pending",
        "verification",
        "unsupported-version",
        "cleanup-unverified",
        "cancelled",
        "refused",
        "startup-failed",
        "holder-active",
        "schema",
        "io",
        "interrupted",
        "not-needed",
        "sync-failed",
        "scope-unavailable",
        "client-failed",
        "source-unavailable",
        "stop-requested",
    }
)
METRICS = frozenset(
    {
        "sessions",
        "exported",
        "unchanged",
        "updated",
        "pending",
        "interrupted",
        "foreground",
        "jobs_running",
        "jobs_completed",
        "jobs_error",
        "jobs_cancelled",
        "shell",
        "subagent",
        "image",
        "complete_pairs",
        "foreground_queries",
        "background_queries",
        "foreground_total_ms",
        "foreground_max_ms",
        "background_total_ms",
        "background_max_ms",
        "remaining_ms",
        "last_complete_age_ms",
    }
)
MAX_METRIC_VALUE = 2**31 - 1

_FILE = "activity.jsonl"
_LOCK = "activity.lock"
_MAX_FILE_BYTES = 256 * 1024
_BACKUP_COUNT = 3
_MAX_RECORD_BYTES = 1024
_MAX_READ_EVENTS = 10000
_LOCK_TIMEOUT = 0.25
_DISPLAY_FIELDS = (
    "command",
    "workspace",
    "stage",
    "outcome",
    "reason",
    "exit_code",
    "duration_ms",
)
_FIELDS = frozenset({"timestamp", "event", "level", "metrics", *_DISPLAY_FIELDS})
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


def _private_directory(fd, *, state_root=False):
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (
            bool(stat.S_IMODE(info.st_mode) & 0o022)
            if state_root
            else stat.S_IMODE(info.st_mode) != 0o700
        )
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
        # Older registrations can have an owned 0755 state root. Preserve it;
        # the journal itself remains in a strictly private child directory.
        _private_directory(fd, state_root=True)
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


def _workspace_id(value):
    return isinstance(value, str) and _WORKSPACE.fullmatch(value) is not None


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
    for name, choices in (
        ("command", COMMANDS),
        ("outcome", OUTCOMES),
        ("level", LEVELS),
        ("stage", STAGES),
        ("reason", REASONS),
    ):
        if name in record and not _choice(record[name], choices):
            return None
    if "workspace" in record and not _workspace_id(record["workspace"]):
        return None
    if "exit_code" in record and not _integer(record["exit_code"], -255, 255):
        return None
    if "duration_ms" in record and not _integer(record["duration_ms"], 0, 2**53 - 1):
        return None
    cleaned = dict(record)
    if "metrics" in record:
        metrics = record["metrics"]
        if type(metrics) is not dict or len(metrics) > len(METRICS):
            return None
        metrics = metrics.copy()
        if any(
            type(name) is not str
            or name not in METRICS
            or not _integer(value, 0, MAX_METRIC_VALUE)
            for name, value in metrics.items()
        ):
            return None
        cleaned["metrics"] = metrics
    return cleaned


def event_level(record) -> str:
    """Return stored severity or the fixed default for an unlevelled record."""
    level = record.get("level")
    if _choice(level, LEVELS):
        return level
    event = record.get("event")
    if event == "command.failed" or record.get("outcome") in (
        "failure",
        "refused",
        "unavailable",
    ):
        return "error"
    if event in ("lifecycle.stage", "history.wait", "history.pass"):
        return "debug"
    if event == "lifecycle.notice":
        return "warn"
    return "info"


def format_event(record) -> str:
    """Render fixed journal metadata without the timestamp or severity prefix."""
    record = _sanitize(record)
    if record is None:
        return ""
    parts = [record["event"]]
    parts.extend(f"{name}={record[name]}" for name in _DISPLAY_FIELDS if name in record)
    parts.extend(
        f"{name}={value}" for name, value in sorted(record.get("metrics", {}).items())
    )
    return " ".join(parts)


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
    level=None,
    stage=None,
    reason=None,
    metrics=None,
    root=None,
) -> bool:
    """Append allowlisted metadata; return False silently on invalid/unsafe I/O.

    Files are 0600, the log directory is 0700, and symlinks, hardlinks,
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
            ("level", level),
            ("stage", stage),
            ("reason", reason),
            ("metrics", metrics),
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
        if (
            os.environ.get("OC_BOX_JOURNALD") == "1"
            and os.environ.get("OC_BOX_CONTAINER") != "1"
        ):
            try:
                if __package__:
                    from . import journal_output
                else:
                    import journal_output
                journal_output.emit(record)
            except Exception:  # noqa: BLE001, S110 - the optional sink is silent.
                # The optional sink cannot change the private append result.
                pass
        return True
    except Exception:  # noqa: BLE001 - optional diagnostics must never abort callers.
        # Diagnostics must not change lifecycle outcomes, including unavailable
        # filesystems/locks and unsupported platforms. Never log the exception.
        return False


def read_events(
    root=None, *, limit=1000, level=None, workspace=None, strict_snapshot=False
) -> list[dict]:
    """Return the latest valid records oldest-first, without creating anything.

    Match at least ``level`` and exactly ``workspace`` before applying ``limit``.
    Malformed/oversized records are ignored. Normally unsafe files are skipped;
    ``strict_snapshot`` instead returns an empty list on any unsafe, unreadable,
    oversized or missing file, except for an absent oldest prefix of rotations.
    This lets a follower retain its previous snapshot rather than replay after a
    partial read. Reads remain bounded by four files and 10,000 returned records.
    Invalid filters and read failures return an empty list.
    """
    if (
        type(limit) is not int
        or limit <= 0
        or (level is not None and not _choice(level, LEVELS))
        or (workspace is not None and not _workspace_id(workspace))
    ):
        return []
    minimum = LEVELS.index(level) if level is not None else None
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
            found = False
            for name in names:
                try:
                    fd = _open_file(directory, name)
                except FileNotFoundError:
                    if strict_snapshot and found:
                        return []
                    continue
                except (OSError, ValueError):
                    if strict_snapshot:
                        return []
                    continue
                found = True
                try:
                    size = os.fstat(fd).st_size
                    if size > _MAX_FILE_BYTES:
                        if strict_snapshot:
                            return []
                        continue
                    data = bytearray()
                    while len(data) <= _MAX_FILE_BYTES:
                        chunk = os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > _MAX_FILE_BYTES or (
                        strict_snapshot and len(data) != size
                    ):
                        if strict_snapshot:
                            return []
                        continue
                    for line in data.splitlines():
                        if len(line) > _MAX_RECORD_BYTES:
                            continue
                        try:
                            record = _sanitize(json.loads(line))
                        except (ValueError, UnicodeError, RecursionError):
                            continue
                        if (
                            record is not None
                            and (
                                workspace is None
                                or record.get("workspace") == workspace
                            )
                            and (
                                minimum is None
                                or LEVELS.index(event_level(record)) >= minimum
                            )
                        ):
                            records.append(record)
                finally:
                    os.close(fd)
    except Exception:  # noqa: BLE001 - malformed/unavailable diagnostics fail closed.
        return []
    return list(records)
