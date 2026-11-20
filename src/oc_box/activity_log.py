"""Private, bounded activity metadata; never a transcript or error log.

Only fixed event categories, workspace IDs and bounded numeric measurements are
accepted. Diagnostic failures are deliberately silent and cannot turn a successful
lifecycle operation into a failure. ``root`` is the *state* root; the journal
lives in its private ``log/`` directory by default. No reader creates files.
"""

import fcntl
import io
import json
import os
import re
import stat
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .log_config import load
else:
    from log_config import load

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
# Safety bounds are independent of the current write/retention policy.
_HARD_FILE_BYTES = 4 * 1024 * 1024
_HARD_TOTAL_BYTES = 16 * 1024 * 1024
_HARD_FILE_COUNT = 16
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


def _policy():
    return load(default_file_size=_MAX_FILE_BYTES, default_max_files=_BACKUP_COUNT + 1)


def log_path(root=None) -> Path:
    """Return the selected active path without opening or creating any log files."""
    override = _policy().directory
    return (override if override is not None else _root(root) / "log") / _FILE


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
def _log_directory(root, *, create, policy=None):
    override = (_policy() if policy is None else policy).directory
    path = override if override is not None else _root(root)
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
        if override is None:
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


def _check_file(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Unsafe activity file")


def _open_file(directory, name, *, create=False, writable=False, exclusive=False):
    flags = (
        (os.O_RDWR if writable else os.O_RDONLY)
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
        | os.O_NONBLOCK
    )
    if create:
        flags |= os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        _check_file(os.fstat(fd))
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


def _filename(index):
    return f"{_FILE}.{index}" if index else _FILE


def _same_file(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _read_file(fd, info):
    """Read exactly the admitted bytes, with no extra read past the budget."""
    data = io.BytesIO()
    remaining = info.st_size
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            raise ValueError("Incomplete activity journal")
        data.write(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    _check_file(after)
    if (
        after.st_size != info.st_size
        or after.st_mtime_ns != info.st_mtime_ns
        or after.st_ctime_ns != info.st_ctime_ns
    ):
        raise ValueError("Changed activity journal")
    data.seek(0)
    return data


def _parse_record(line):
    if len(line.rstrip(b"\r\n")) > _MAX_RECORD_BYTES:
        return None
    try:
        return _sanitize(json.loads(line))
    except (ValueError, UnicodeError, RecursionError):
        return None


def _inspect_files(directory, policy, now):
    """Check all fixed names before changing any of them."""
    files = {}
    remaining = _HARD_TOTAL_BYTES
    prefix = b""
    aged = False
    for index in range(_HARD_FILE_COUNT):
        try:
            fd = _open_file(directory, _filename(index))
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            size = info.st_size
            if size > min(_HARD_FILE_BYTES, remaining):
                raise ValueError("Oversized activity journal")
            remaining -= size
            files[index] = info
            oversized = size > policy.max_file_size
            check_age = index == 0 and policy.max_file_sec and size
            data = _read_file(fd, info) if oversized or check_age else None
            if index == 0 and size:
                # Isolate a partial final record from the next append.
                if data is None:
                    last = os.pread(fd, 1, size - 1)
                else:
                    data.seek(-1, os.SEEK_END)
                    last = data.read(1)
                    data.seek(0)
                if not last:
                    raise ValueError("Incomplete activity journal")
                if last != b"\n":
                    prefix = b"\n"
            if data is not None:
                first = None
                for line in data:
                    record = _parse_record(line)
                    if record is None:
                        if oversized:
                            raise ValueError("Oversized activity journal")
                    elif first is None:
                        first = record["timestamp"]
                        if not oversized:
                            break
                if check_age and first is not None:
                    opened = datetime.fromisoformat(first[:-1] + "+00:00").timestamp()
                    aged = now - opened >= policy.max_file_sec
        finally:
            os.close(fd)
    return files, prefix, aged


def _check_name(directory, index, info):
    current = os.stat(_filename(index), dir_fd=directory, follow_symlinks=False)
    _check_file(current)
    if not _same_file(current, info):
        raise ValueError("Changed activity journal")


def _discard_archive(directory, files, index):
    if not 1 <= index < _HARD_FILE_COUNT:
        raise ValueError("Invalid activity archive")
    _check_name(directory, index, files[index])
    os.unlink(_filename(index), dir_fd=directory)
    del files[index]


def _move_file(directory, files, source, target):
    _check_name(directory, source, files[source])
    try:
        os.stat(_filename(target), dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Unexpected activity archive")
    os.rename(
        _filename(source), _filename(target), src_dir_fd=directory, dst_dir_fd=directory
    )
    files[target] = files.pop(source)


def _available_bytes(directory, required):
    info = os.fstatvfs(directory)
    block_size = info.f_frsize or info.f_bsize
    if block_size <= 0 or (info.f_blocks > 0 and required > info.f_blocks * block_size):
        raise ValueError("Activity free-space reservation exceeds capacity")
    return max(0, info.f_bavail) * block_size


def _prepare_append(directory, files, policy, now, rotate, final_size, append_size):
    """Make room and report whether to drop the outgoing active after a sync."""
    archives = sorted(index for index in files if index)
    keep = [0, *archives] if rotate else archives.copy()
    total = final_size + sum(files[index].st_size for index in keep)
    while keep:
        # All retention removes only the oldest part of the history. Even if
        # mtimes are out of order, an interior deletion would break the follow
        # reader's ordered snapshot overlap and could replay older events.
        expired = (
            policy.max_retention_sec
            and now - files[keep[-1]].st_mtime >= policy.max_retention_sec
        )
        if not (expired or len(keep) >= policy.max_files or total > policy.max_use):
            break
        total -= files[keep.pop()].st_size

    required = policy.keep_free + append_size
    available = _available_bytes(directory, required) if policy.keep_free else None
    for index in reversed(archives):
        if index not in keep:
            _discard_archive(directory, files, index)
    # Close any pre-existing gaps without changing the order.
    for target, source in enumerate(sorted(index for index in files if index), 1):
        if source != target:
            _move_file(directory, files, source, target)

    if available is not None:
        if len(archives) != len(files) - int(0 in files):
            available = _available_bytes(directory, required)
        # An unlink might not free logical file bytes (e.g. open descriptors,
        # sparse files). Measure again; only already archived files qualify.
        while available < required:
            oldest = max(files, default=0)
            if not oldest:
                raise ValueError("Insufficient activity journal space")
            _discard_archive(directory, files, oldest)
            available = _available_bytes(directory, required)

    if rotate:
        for index in sorted(files, reverse=True):
            _move_file(directory, files, index, index + 1)
    # Do not discard the outgoing active file until its replacement has
    # synced. By that point it has a checked archive name, never the active name.
    # If it must be discarded, all older archives were already pruned above.
    # Even on sync failure only one file (at most 4 MiB) plus one record remains,
    # safely below the absolute aggregate read limit.
    return rotate and 0 not in keep


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
    A shared lock serializes append/rotation; default retention is four 256-KiB files.
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
        policy = _policy()
        if len(payload) > min(_MAX_RECORD_BYTES, policy.max_file_size, policy.max_use):
            return False
        with (
            _log_directory(root, create=True, policy=policy) as directory,
            _locked(directory, write=True),
        ):
            now = time.time()
            files, prefix, aged = _inspect_files(directory, policy, now)
            size = files[0].st_size if 0 in files else 0
            final_size = size + len(prefix) + len(payload)
            rotate = bool(size) and (
                aged or final_size > min(policy.max_file_size, policy.max_use)
            )
            if rotate:
                prefix = b""
                final_size = len(payload)
            pending = prefix + payload
            discard_outgoing = _prepare_append(
                directory, files, policy, now, rotate, final_size, len(pending)
            )

            active = files.get(0)
            fd = _open_file(
                directory,
                _FILE,
                create=active is None,
                exclusive=active is None,
                writable=True,
            )
            try:
                if active is not None and not _same_file(os.fstat(fd), active):
                    raise ValueError("Changed activity journal")
                if os.lseek(fd, 0, os.SEEK_END) != final_size - len(pending):
                    raise ValueError("Changed activity journal")
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise OSError("Activity write failed")
                    pending = pending[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            if discard_outgoing:
                _discard_archive(directory, files, 1)
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
    partial read. The current write policy does not truncate earlier valid files:
    reads are capped at 16 files, 4 MiB/file, 16 MiB total and 10,000 records.
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
        policy = _policy()
        with (
            _log_directory(root, create=False, policy=policy) as directory,
            _locked(directory, write=False),
        ):
            missing_newer = False
            remaining = _HARD_TOTAL_BYTES
            for index in range(_HARD_FILE_COUNT):
                try:
                    fd = _open_file(directory, _filename(index))
                except FileNotFoundError:
                    missing_newer = True
                    continue
                except (OSError, ValueError):
                    if strict_snapshot:
                        return []
                    continue
                try:
                    if strict_snapshot and missing_newer:
                        return []
                    info = os.fstat(fd)
                    if info.st_size > min(_HARD_FILE_BYTES, remaining):
                        raise ValueError("Oversized activity journal")
                    remaining -= info.st_size
                    file_records = deque(maxlen=records.maxlen - len(records))
                    oversized = info.st_size > policy.max_file_size
                    for line in _read_file(fd, info):
                        record = _parse_record(line)
                        if record is None and oversized:
                            raise ValueError("Oversized activity journal")
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
                            file_records.append(record)
                    records.extendleft(reversed(file_records))
                except (OSError, ValueError):
                    if strict_snapshot:
                        return []
                finally:
                    os.close(fd)
    except Exception:  # noqa: BLE001 - malformed/unavailable diagnostics fail closed.
        return []
    return list(records)
