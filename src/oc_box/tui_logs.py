"""Read-only, bounded in-memory log views for the management TUI.

External container logs can contain secrets. Control/ANSI removal and common
credential-pattern redaction are best-effort display hygiene, NOT a guarantee
that arbitrary secrets are removable. Output is never copied to a disk log.
Podman is queried with the maintenance environment and a verified immutable ID;
no container is started, entered, or otherwise changed by these readers.
"""

import json
import os
import re
import selectors
import subprocess
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone

if __package__:
    from . import activity_log, journal_output
else:
    import activity_log
    import journal_output


_MAX_ENTRIES = 10000
_MAX_SOURCES = 8
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_MESSAGE_CHARS = 4096
_MAX_RAW_CHARS = _MAX_MESSAGE_CHARS + 80
_PODMAN_TIMEOUT = 10
_LABEL = "io.oc-box.workspace"
_WORKSPACE = re.compile(r"[0-9a-f]{32}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2}))[ \t]+"
)
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
_PODMAN_SGR = re.compile(r"(?:(?:\x1b\[|\x9b)[0-9;:]*m)+")
_AUTH = re.compile(
    r"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|(?:bearer|basic)\s+[^\s,;]+|[^\s,;]+)"
)
_KEY_VALUE = re.compile(
    r"(?i)\b([a-z0-9_.-]{0,80}(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd|token|secret|credential)[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|\"[^\"]*\"|'[^']*'|[^\s,;&\]}]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/-]+=*")
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"sk-[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]+|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_URL_AUTH = re.compile(r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@")

JOURNAL_SOURCE = "journald"
JOURNAL_LIMIT = 1000
LOG_COLUMNS = (
    ("timestamp", "Time", 26),
    ("source", "Source", 13),
    ("level", "Severity", 9),
    ("message", "Message", 68),
)
_LEVEL_RANK = {level: rank for rank, level in enumerate(activity_log.LEVELS)}
_LEVEL_RANK["warning"] = _LEVEL_RANK["warn"]


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    source: str
    level: str
    message: str
    raw: str | None = None

    def raw_line(self):
        if self.raw is not None:
            return self.raw
        # Local diagnostics and synthetic samples may have no source record.
        return " ".join(field for field in (self.timestamp, self.message) if field)


PODMAN_UNAVAILABLE = LogEntry("", "podman", "warning", "Podman logs unavailable.")


class _PodmanEntries(list[LogEntry]):
    """Keep immutable container provenance with a verified reader snapshot."""

    def __init__(self, entries, container_id):
        super().__init__(entries)
        self.container_id = container_id


def raw_copy_text(entries, max_bytes):
    """Assemble complete RAW matches within the byte cap, or no payload at all."""
    lines = []
    remaining = max_bytes
    for entry in entries:
        line = entry.raw_line()
        remaining -= bool(lines)  # LF between records, including empty records.
        if len(line) > remaining:
            return None
        remaining -= len(line.encode("utf-8"))
        if remaining < 0:
            return None
        lines.append(line)
    return "\n".join(lines)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value)
    value = "".join(
        " " if char == "\t" else char
        for char in value
        if char == "\t"
        or unicodedata.category(char) not in {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"}
    )
    value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
    value = _AUTH.sub(r"\1[REDACTED]", value)
    value = _KEY_VALUE.sub(r"\1[REDACTED]", value)
    value = _BEARER.sub(r"\1 [REDACTED]", value)
    value = _TOKEN.sub("[REDACTED]", value)
    return value if len(value) <= limit else value[:limit] + "…"


def _entry(entry, *, source=None):
    if not isinstance(entry, LogEntry):
        raise TypeError("Expected LogEntry")
    return LogEntry(
        _clean(entry.timestamp, 80),
        _clean(entry.source if source is None else source, 64),
        _clean(entry.level, 16),
        _clean(entry.message, _MAX_MESSAGE_CHARS),
        None if entry.raw is None else _clean(entry.raw, _MAX_RAW_CHARS),
    )


def _timestamp_key(timestamp):
    # Normalize offset/fraction representations, retaining nanoseconds rather
    # than lexicographically ordering equivalent RFC3339 strings differently.
    match = _TIME.fullmatch(timestamp + " ")
    if match:
        try:
            fraction = re.search(r"\.(\d{1,9})", timestamp)
            whole = re.sub(r"\.\d{1,9}", "", timestamp).replace("Z", "+00:00")
            value = datetime.fromisoformat(whole).astimezone(timezone.utc)
            return (1, value, int(fraction[1].ljust(9, "0")) if fraction else 0)
        except (ValueError, OverflowError):
            pass
    return (0, datetime.min.replace(tzinfo=timezone.utc), 0)


def sort_entries(entries, column, descending=False):
    """Sort the table only, with stable ties and missing metadata last."""
    known, unknown = [], []
    for entry in entries:
        if column == "timestamp":
            present, *value = _timestamp_key(entry.timestamp)
        elif column == "level":
            value = _LEVEL_RANK.get(entry.level.casefold())
            present = value is not None
        else:
            value = getattr(entry, column).casefold()
            present = bool(value)
        if present:
            known.append((value, entry))
        else:
            unknown.append(entry)
    known.sort(key=lambda item: item[0], reverse=descending)
    return [entry for _, entry in known] + unknown


def fifo_tail_drop(previous, current):
    """Find a bounded head trim followed by an exact retained tail and new rows."""
    # DataTable reindexes on each remove, so large jumps should rebuild.
    for drop in range(min(len(previous), 17)):
        kept = len(previous) - drop
        if len(current) > kept and previous[drop:] == current[:kept]:
            return drop
    return None


class LogBuffer:
    """Query-independent source tails, bounded by total ``max_entries``.

    Replacing one source leaves all other source tails intact, subject to the
    overall bound. Eviction uses arrival order, never the active search query.
    At most eight source buffers are retained. Results are timestamp ordered;
    equal timestamps preserve arrival order. No entries are persisted.
    """

    def __init__(self, max_entries=2000):
        if type(max_entries) is not int or not 1 <= max_entries <= _MAX_ENTRIES:
            raise ValueError("max_entries must be between 1 and 10000")
        self.max_entries = max_entries
        self._sources = OrderedDict()
        self._sequence = 0

    def _put(self, entry):
        source = entry.source
        if source not in self._sources:
            if len(self._sources) >= _MAX_SOURCES:
                self._sources.popitem(last=False)
            self._sources[source] = deque()
        self._sources.move_to_end(source)
        self._sequence += 1
        self._sources[source].append((self._sequence, entry))
        while sum(len(items) for items in self._sources.values()) > self.max_entries:
            oldest = min(
                (name for name, items in self._sources.items() if items),
                key=lambda name: self._sources[name][0][0],
            )
            self._sources[oldest].popleft()

    def replace(self, source, entries):
        """Replace a source's snapshot, keeping the newest bounded entries."""
        if not isinstance(source, str):
            raise TypeError("Expected source name")
        source = _clean(source, 64)
        # Retain only a bounded tail even when given a generator. Validate the
        # new snapshot before removing the old one.
        tail = deque(
            (_entry(item, source=source) for item in entries), maxlen=self.max_entries
        )
        self._sources.pop(source, None)
        for item in tail:
            self._put(item)

    def refresh(self, source, entries, *, preserve=()):
        """Replace only when the actual retained tail differs.

        Higher-priority sources in this refresh keep their allocation. Refill
        from the reader after eviction, but only to the capacity left after
        those sources, so two unchanged readers cannot evict each other.
        """
        source = _clean(source, 64)
        protected = {
            name for name in preserve if name != source and self._sources.get(name)
        }
        capacity = self.max_entries - sum(len(self._sources[n]) for n in protected)
        if len(protected) >= _MAX_SOURCES:
            capacity = 0
        tail = deque(entries, maxlen=capacity)
        current = tuple(entry for _, entry in self._sources.get(source, ()))
        # Already validated reader snapshots normally match retained display
        # rows exactly; only re-run hygiene when the bounded candidate changed.
        if len(tail) == len(current) and all(
            isinstance(item, LogEntry)
            and (item if item.source == source else replace(item, source=source)) == old
            for item, old in zip(tail, current)
        ):
            return False
        tail = tuple(_entry(item, source=source) for item in tail)
        if tail == current:
            return False
        self._sources.pop(source, None)
        if tail:
            while len(self._sources) >= _MAX_SOURCES:
                self._sources.pop(next(n for n in self._sources if n not in protected))
            excess = (
                sum(map(len, self._sources.values())) + len(tail) - self.max_entries
            )
            for _ in range(excess):
                oldest = min(
                    (
                        n
                        for n, items in self._sources.items()
                        if items and n not in protected
                    ),
                    key=lambda n: self._sources[n][0][0],
                )
                self._sources[oldest].popleft()
            for item in tail:
                self._put(item)
        return True

    def append(self, entry):
        """Append a displayed entry, regardless of current filters."""
        self._put(_entry(entry))

    def search(
        self,
        query="",
        source=None,
        *,
        level=None,
        source_order=False,
    ) -> list[LogEntry]:
        """Filter display fields; RAW preserves the original order within each source."""
        if (
            not isinstance(query, str)
            or (source is not None and not isinstance(source, str))
            or (level is not None and level not in activity_log.LEVELS)
        ):
            return []
        query = query.casefold()
        minimum = _LEVEL_RANK[level] if level is not None else -1
        # A combined RAW view has no original inter-source ordering. Keep
        # deterministic source groups even when one snapshot refreshes alone.
        sources = (
            sorted(self._sources.items()) if source_order else self._sources.items()
        )
        rows = [
            row
            for name, items in sources
            if source is None or name == source
            for row in items
        ]
        if not source_order:
            rows.sort(key=lambda row: (_timestamp_key(row[1].timestamp), row[0]))
        return [
            entry
            for _, entry in rows
            if _LEVEL_RANK.get(entry.level.casefold(), -1) >= minimum
            and (
                not query
                or any(
                    query in field.casefold()
                    for field in (
                        entry.timestamp,
                        entry.source,
                        entry.level,
                        entry.message,
                    )
                )
            )
        ]


def _activity_entries(records, source):
    entries = []
    for record in records:
        clean = activity_log._sanitize(record)
        if clean is not None:
            entries.append(
                LogEntry(
                    clean["timestamp"],
                    source,
                    activity_log.event_level(clean),
                    activity_log.format_event(clean),
                    json.dumps(clean, ensure_ascii=True, separators=(",", ":")),
                )
            )
    return entries


def activity_entries(root=None, limit=1000) -> list[LogEntry]:
    """Read the private activity snapshot, with only validated JSONL in RAW."""
    return _activity_entries(activity_log.read_events(root, limit=limit), "oc-box")


def activity_snapshot():
    path, records = activity_log.read_snapshot()
    return path, _activity_entries(records, "oc-box")


def activity_path():
    """Identify the effective private reader path without opening the log."""
    try:
        return activity_log.log_path()
    except (OSError, ValueError):
        return None


def journal_entries(limit=JOURNAL_LIMIT) -> list[LogEntry]:
    """Read only the same-UID mirror as a distinct, explicitly loaded source."""
    return _activity_entries(
        journal_output.read_events(limit=limit, level=None), JOURNAL_SOURCE
    )


class _LogUnavailable(Exception):
    pass


def _podman_output(arguments, env):
    """Capture at most one MiB through pipes, never temporary disk files."""
    output = bytearray()
    truncated = False
    deadline = time.monotonic() + _PODMAN_TIMEOUT
    with subprocess.Popen(
        ["podman", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _LogUnavailable() from None
                    if not selector.select(remaining):
                        raise _LogUnavailable() from None
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(65536, _MAX_OUTPUT_BYTES + 1 - len(output)),
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > _MAX_OUTPUT_BYTES:
                        truncated = True
                        del output[_MAX_OUTPUT_BYTES:]
                        process.kill()
                        break
            if truncated:
                process.wait(timeout=2)
            else:
                result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if result:
                    raise _LogUnavailable() from None
        except BaseException:
            process.kill()
            process.wait()
            raise
    return output.decode("utf-8", errors="replace"), truncated


def _unavailable():
    return [PODMAN_UNAVAILABLE]


def _podman_record(line):
    """Sanitize a complete producer record without guessing invisible boundaries."""
    line = line.removesuffix("\r")
    match = _TIME.match(line)
    if match is not None and not _timestamp_key(match[1])[0]:
        match = None
    prefix = line[: match.end()].replace("\t", " ") if match else ""
    body = line[match.end() :] if match else line

    def remove_style(match):
        # Isolated SGR decoration is safe to strip. A style change at a
        # non-space gap can hide either a split token or a credential separator.
        if (
            0 < match.start()
            and match.end() < len(body)
            and body[match.start() - 1] != " "
            and body[match.end()] != " "
        ):
            return "\0"
        return ""

    visible = _PODMAN_SGR.sub(remove_style, body)
    if any(
        char != " " and (char.isspace() or unicodedata.category(char).startswith("C"))
        for char in visible.strip(" \t")
    ):
        # Dropping or spacing arbitrary controls can each make recognizable
        # credentials disappear from a redaction regex while exposing values.
        # Keep the real record/time but withhold its ambiguous message.
        visible = "[REDACTED]"
    message = _clean(visible, _MAX_MESSAGE_CHARS)
    raw = prefix + _clean(visible, _MAX_RAW_CHARS)
    return _entry(LogEntry(match[1] if match else "", "podman", "", message, raw))


def podman_entries(box, data, tail=500) -> list[LogEntry]:
    """Read a bounded snapshot of owned container logs; never follow in Podman.

    Call again to follow while retaining UI search state in LogBuffer. Backend
    failures return a fixed diagnostic, never exception/stdout/stderr details.
    Container stderr is log content, subject to the same best-effort redaction.
    """
    if type(tail) is not int or tail <= 0:
        return []
    tail = min(tail, _MAX_ENTRIES)
    try:
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not _WORKSPACE.fullmatch(data["id"])
        ):
            return _unavailable()
        env = box.maintenance_host_env()
        info = box.inspect_owned(data, env=env)
        if info is None:
            return []
        ident = info.get("Id")
        labels = (info.get("Config") or {}).get("Labels") or {}
        if (
            not isinstance(ident, str)
            or not _CONTAINER.fullmatch(ident)
            or labels.get(_LABEL) != data["id"]
        ):
            return _unavailable()
        output, truncated = _podman_output(
            ["logs", "--timestamps", "--tail", str(tail), ident],
            env,
        )
        if truncated:
            # Do not publish a byte-limited fragment that could end midway
            # through a control sequence or a recognizable credential.
            output = output[: output.rfind("\n") + 1]
        # Only the producer's LF delimits records. str.splitlines() would turn
        # controlled separators inside a record into independent unredacted rows.
        lines = output.removesuffix("\n").split("\n") if output else []
        entries = [_podman_record(line) for line in lines[-tail:]]
        if truncated:
            entries.append(
                LogEntry(
                    "", "podman", "warning", "Podman log output truncated (byte limit)."
                )
            )
        return _PodmanEntries(entries, ident)
    except Exception:  # noqa: BLE001 - never expose backend exception text to the UI.
        return _unavailable()
