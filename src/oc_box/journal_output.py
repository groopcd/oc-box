"""Optional host-only systemd journal mirror of validated activity events."""

import json
import os
import selectors
import subprocess
import sys
import time

if __package__:
    from . import activity_log
else:
    import activity_log


_IDENTIFIER = "oc-box"
_FORMAT = "oc-box-activity-v1"
_PRIORITIES = {"debug": 7, "info": 6, "warn": 4, "error": 3}
_MAX_DATAGRAM_BYTES = 4096
_SEND_TIMEOUT = 0.75
# journald can capture the sending process's command line. Keep it static; the
# packet goes through the child's private stdin pipe, never argv or environment.
_SEND_SCRIPT = f"""\
import socket
import sys

packet = sys.stdin.buffer.read({_MAX_DATAGRAM_BYTES + 1})
if not packet or len(packet) > {_MAX_DATAGRAM_BYTES}:
    raise SystemExit(1)
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        sender.setblocking(False)
        sent = sender.sendto(packet, "/run/systemd/journal/socket")
except OSError:
    raise SystemExit(1) from None
raise SystemExit(0 if sent == len(packet) else 1)
"""
_OUTPUT_FIELDS = (
    "_UID",
    "SYSLOG_IDENTIFIER",
    "OC_BOX_FORMAT",
    "PRIORITY",
    "OC_BOX_WORKSPACE",
    "MESSAGE",
    "OC_BOX_RECORD",
)
# journalctl includes addressing metadata even with --output-fields. Newer
# versions include sequence numbers too. None of these fields are used or shown.
_ROW_FIELDS = frozenset(
    {
        *_OUTPUT_FIELDS,
        "__CURSOR",
        "__REALTIME_TIMESTAMP",
        "__MONOTONIC_TIMESTAMP",
        "_BOOT_ID",
        "__SEQNUM",
        "__SEQNUM_ID",
    }
)
_MAX_ROW_BYTES = 4096
_TIMEOUT = 3.0
_REAP_TIMEOUT = 0.25
_ERROR_MESSAGES = {
    "host-only": "Read the oc-box journald mirror on the host.",
    "missing-tool": "journalctl is not available.",
    "unavailable": "Could not read the oc-box journald mirror.",
    "timeout": "The oc-box journald query timed out.",
    "output-limit": "The oc-box journald query exceeded its output bound.",
}


class JournalError(Exception):
    """A read-side failure with a fixed, display-safe reason and message."""

    def __init__(self, reason):
        self.reason = reason
        message = _ERROR_MESSAGES[reason]
        if reason != "host-only":
            message += " Omit --journald to read the private activity log."
        super().__init__(message)


def _encoded(record):
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True)


def _reap(process):
    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=_REAP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _send_packet(payload):
    if len(payload) > _MAX_DATAGRAM_BYTES or not os.path.isabs(sys.executable):
        return False
    deadline = time.monotonic() + _SEND_TIMEOUT
    process = subprocess.Popen(
        ["python3", "-I", "-S", "-B", "-c", _SEND_SCRIPT],
        executable=sys.executable,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"LC_ALL": "C"},
        cwd="/",
        close_fds=True,
        bufsize=0,
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        process.communicate(payload, timeout=remaining)
        return process.returncode == 0
    finally:
        process.stdin.close()
        _reap(process)


def emit(record) -> bool:
    """Send an explicitly selected host event via a bounded helper; never retry."""
    try:
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            return False
        record = activity_log._sanitize(record)
        if record is None:
            return False
        encoded = _encoded(record)
        if len(encoded) + 1 > activity_log._MAX_RECORD_BYTES:
            return False
        fields = {
            "PRIORITY": _PRIORITIES[activity_log.event_level(record)],
            "SYSLOG_IDENTIFIER": _IDENTIFIER,
            "OC_BOX_FORMAT": _FORMAT,
            "MESSAGE": activity_log.format_event(record),
            "OC_BOX_RECORD": encoded,
        }
        if "workspace" in record:
            fields["OC_BOX_WORKSPACE"] = record["workspace"]
        # All values are fixed, sanitized ASCII or compact JSON; none can contain
        # literal newlines, so the native protocol's KEY=value form is sufficient.
        payload = "".join(f"{key}={value}\n" for key, value in fields.items()).encode(
            "ascii"
        )
        return _send_packet(payload)
    except Exception:  # noqa: BLE001 - mirroring must not affect the private append.
        return False


def _unique_pairs(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError
    return fields


def _validated_row(line, *, uid, priority, workspace):
    try:
        row = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if (
            not isinstance(row, dict)
            or row.keys() - _ROW_FIELDS
            or any(not isinstance(value, str) for value in row.values())
            or row.get("_UID") != uid
            or row.get("SYSLOG_IDENTIFIER") != _IDENTIFIER
            or row.get("OC_BOX_FORMAT") != _FORMAT
            or (workspace is not None and row.get("OC_BOX_WORKSPACE") != workspace)
        ):
            return None
        encoded = row.get("OC_BOX_RECORD")
        if (
            encoded is None
            or not encoded.isascii()
            or len(encoded) + 1 > activity_log._MAX_RECORD_BYTES
        ):
            return None
        record = activity_log._sanitize(
            json.loads(encoded, object_pairs_hook=_unique_pairs)
        )
        if record is None or encoded != _encoded(record):
            return None
        expected_priority = _PRIORITIES[activity_log.event_level(record)]
        if (
            expected_priority > priority
            or row.get("PRIORITY") != str(expected_priority)
            or row.get("MESSAGE") != activity_log.format_event(record)
            or ("OC_BOX_WORKSPACE" in row) != ("workspace" in record)
            or row.get("OC_BOX_WORKSPACE") != record.get("workspace")
        ):
            return None
        return record
    except (ValueError, RecursionError):
        return None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JournalError("timeout")
    return remaining


def _snapshot(arguments, *, limit, uid, priority, workspace):
    """Stream a single bounded snapshot and publish nothing on command failure."""
    deadline = time.monotonic() + _TIMEOUT
    maximum = limit * _MAX_ROW_BYTES
    total = rows = 0
    pending = bytearray()
    records = []
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
        cwd="/",
        bufsize=0,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if not selector.select(_remaining(deadline)):
                    raise JournalError("timeout")
                chunk = os.read(
                    process.stdout.fileno(), min(65536, maximum + 1 - total)
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise JournalError("output-limit")
                pending.extend(chunk)
                start = 0
                while (end := pending.find(b"\n", start)) != -1:
                    rows += 1
                    if rows > limit or end + 1 - start > _MAX_ROW_BYTES:
                        raise JournalError("output-limit")
                    record = _validated_row(
                        pending[start:end],
                        uid=uid,
                        priority=priority,
                        workspace=workspace,
                    )
                    if record is not None:
                        records.append(record)
                    start = end + 1
                del pending[:start]
                if len(pending) >= _MAX_ROW_BYTES:
                    raise JournalError("output-limit")
        if process.wait(timeout=_remaining(deadline)):
            raise JournalError("unavailable")
        # JSON mode terminates each row with LF. An incomplete fragment is not
        # a record. journalctl already returns the selected tail oldest-first.
        return records
    finally:
        process.stdout.close()
        _reap(process)


def read_events(*, limit, level, workspace=None) -> list[dict]:
    """Read the selected local same-UID mirror, independent of write opt-in."""
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise JournalError("host-only")
    if (
        type(limit) is not int
        or limit <= 0
        or (level is not None and not activity_log._choice(level, activity_log.LEVELS))
        or (workspace is not None and not activity_log._workspace_id(workspace))
    ):
        return []
    limit = min(limit, activity_log._MAX_READ_EVENTS)
    priority = _PRIORITIES[level or "debug"]
    uid = str(os.getuid())
    arguments = [
        "journalctl",
        "--quiet",
        "--no-pager",
        "--output=json",
        f"--output-fields={','.join(_OUTPUT_FIELDS)}",
        f"--lines={limit}",
        f"--priority={priority}",
        "--",
        f"_UID={uid}",
        f"SYSLOG_IDENTIFIER={_IDENTIFIER}",
        f"OC_BOX_FORMAT={_FORMAT}",
    ]
    if workspace is not None:
        arguments.append(f"OC_BOX_WORKSPACE={workspace}")
    try:
        return _snapshot(
            arguments, limit=limit, uid=uid, priority=priority, workspace=workspace
        )
    except FileNotFoundError:
        raise JournalError("missing-tool") from None
    except subprocess.TimeoutExpired:
        raise JournalError("timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise JournalError("unavailable") from None
