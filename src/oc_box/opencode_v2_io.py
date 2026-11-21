"""Private standard-CLI V2 service lease for credential refresh and migration.

Used inside a workspace (and offline fixtures). Secret input/output stays in
memory/pipes; a fresh --stdio server never elects or modifies a shared service.
"""

import base64
import contextlib
from http.client import HTTPConnection
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def require_control_pipe():
    """Refuse credential-bearing readiness on a terminal or a disk file."""
    descriptor = sys.stdout.fileno()
    mode = os.fstat(descriptor).st_mode
    if stat.S_ISFIFO(mode):
        return descriptor
    if stat.S_ISSOCK(mode):
        # Some exec transports supply a local stream socket instead of a pipe.
        with socket.socket(fileno=os.dup(descriptor)) as channel:
            if channel.family == socket.AF_UNIX and channel.type == socket.SOCK_STREAM:
                channel.getpeername()
                return descriptor
    raise ValueError("Private OpenCode control output requires a local pipe or socket.")


def write_control_record(descriptor, record):
    """Send a bounded readiness frame to the already-checked control channel."""
    frame = (json.dumps(record) + "\n").encode("utf-8")
    if len(frame) > 4096:
        raise ValueError("Invalid private OpenCode control record.")
    while frame:
        size = os.write(descriptor, frame)
        if size <= 0:
            raise OSError("Private OpenCode control channel closed.")
        frame = frame[size:]


# Constant, secret-free explanations that may be shown on the host. Only these
# exact strings are ever surfaced; every other failure stays generic because
# native errors can contain credential-bearing request/response data.
ENDPOINT = "Invalid private OpenCode 2.x endpoint."
NOT_READY = "Private OpenCode 2.x server did not become ready."
READINESS = "Invalid private OpenCode 2.x readiness response."
INVENTORY = "Invalid OpenCode 2.x session inventory."
INVENTORY_ENTRY = "Invalid OpenCode 2.x session inventory entry."
INVENTORY_INCONSISTENT = "Inconsistent OpenCode 2.x session inventory."
INVENTORY_CURSOR = "Invalid OpenCode 2.x session inventory cursor."
HISTORY = "Invalid OpenCode 2.x projected history."
HISTORY_MESSAGE = "Invalid OpenCode 2.x projected message."
HISTORY_INCONSISTENT = "Inconsistent OpenCode 2.x projected history."
HISTORY_CURSOR = "Invalid OpenCode 2.x projected history cursor."
EXPORT = "Invalid OpenCode 2.x history export."
ACTIVITY = "Invalid OpenCode 2.x activity response."
UNSETTLED = "OpenCode 2.x history is unsettled; retry sync after completion."
CHANGED = "OpenCode 2.x history changed or omitted messages; retry sync."
WORKER = "OpenCode 2.x history worker failed."
HISTORY_WORKER_PROTOCOL = "oc-box.history/1"
HISTORY_WORKER_CONTROL = 65536
HISTORY_WORKER_PAYLOAD = 1073741824
FIXED_MESSAGES = frozenset(
    (
        ENDPOINT,
        NOT_READY,
        READINESS,
        INVENTORY,
        INVENTORY_ENTRY,
        INVENTORY_INCONSISTENT,
        INVENTORY_CURSOR,
        HISTORY,
        HISTORY_MESSAGE,
        HISTORY_INCONSISTENT,
        HISTORY_CURSOR,
        EXPORT,
        ACTIVITY,
        UNSETTLED,
        CHANGED,
        WORKER,
    )
)
# Refusals that only mean "not settled right now". The sync loop continues with
# other sessions after these; every other fixed message still aborts the sync.
PENDING_MESSAGES = frozenset((UNSETTLED, CHANGED))
INTERRUPTED = "ocBoxInterrupted"
MARKER = "oc-box-fixed: "


class FixedError(ValueError):
    """Carries only one of FIXED_MESSAGES; safe to show on the host."""

    def __init__(self, message):
        assert message in FIXED_MESSAGES
        super().__init__(message)


def fixed_message(stderr):
    """Return the whitelisted explanation from a failed bridge, else None."""
    for line in reversed((stderr or "").splitlines()):
        if line.startswith(MARKER) and line[len(MARKER) :] in FIXED_MESSAGES:
            return line[len(MARKER) :]
    return None


def _deadline_timeout(deadline, maximum=60):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise FixedError(NOT_READY)
    return min(maximum, remaining)


def _deadline_http(parsed, method, path, data, headers, deadline):
    """Public HTTPConnection socket access keeps every I/O within one budget."""
    connection = HTTPConnection(
        parsed.hostname, parsed.port, timeout=_deadline_timeout(deadline)
    )
    try:
        connection.connect()
        stream = connection.sock
        stream.settimeout(_deadline_timeout(deadline))
        connection.request(method, path, body=data, headers=headers)
        stream.settimeout(_deadline_timeout(deadline))
        with connection.getresponse() as response:
            if response.status >= 400:
                raise HTTPError(
                    path, response.status, "Private request failed", {}, None
                )
            chunks = []
            while not response.isclosed():
                stream.settimeout(_deadline_timeout(deadline))
                chunk = response.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            _deadline_timeout(deadline)
            body = b"".join(chunks)
            value = json.loads(body) if body else None
            _deadline_timeout(deadline)
            return value
    finally:
        connection.close()


def endpoint_request(endpoint):
    """Use a private loopback endpoint without persisting its credentials."""
    address, password = endpoint["url"], endpoint["password"]
    parsed = urlparse(address)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not isinstance(password, str)
        or not password
    ):
        raise FixedError(ENDPOINT)
    auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
    opener = build_opener(ProxyHandler({}))

    def request(method, path, data=None, *, deadline=None):
        headers = {"Authorization": auth, "Content-Type": "application/json"}
        encoded = json.dumps(data).encode() if data is not None else None
        if deadline is not None:
            return _deadline_http(parsed, method, path, encoded, headers, deadline)
        req = Request(
            address.rstrip("/") + path,
            method=method,
            data=encoded,
            headers=headers,
        )
        with opener.open(req, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else None

    request.endpoint = {"url": address, "password": password}
    return request


def session_inventory(request, directory):
    """Read every page, including children and archived sessions, or fail closed."""
    query = (
        "/api/session?directory=" + quote(directory, safe="") + "&order=asc&limit=100"
    )
    values, identities, cursors = [], set(), set()
    cursor = None
    while True:
        page = request(
            "GET", query + ("&cursor=" + quote(cursor, safe="") if cursor else "")
        )
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("cursor"), dict)
        ):
            raise FixedError(INVENTORY)
        for item in page["data"]:
            if not isinstance(item, dict):
                raise FixedError(INVENTORY_ENTRY)
            ident = item.get("id")
            if (
                not isinstance(ident, str)
                or ident in identities
                or not isinstance(item.get("location"), dict)
                or item.get("location", {}).get("directory") != directory
            ):
                raise FixedError(INVENTORY_INCONSISTENT)
            identities.add(ident)
            values.append(item)
        cursor = page["cursor"].get("next")
        if cursor is None:
            return values
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise FixedError(INVENTORY_CURSOR)
        cursors.add(cursor)


def web_session_info(info):
    """Validate metadata for explicit browsing, not startup/history eligibility."""

    def ident(value):
        return (
            isinstance(value, str)
            and len(value) <= 128
            and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None
        )

    def timestamp(value):
        return type(value) in (int, float) and 0 <= value <= 2**53 - 1

    if (
        not isinstance(info, dict)
        or not ident(info.get("id"))
        or not isinstance(info.get("location"), dict)
        or not isinstance(info["location"].get("directory"), str)
        or not info["location"]["directory"].startswith("/")
        or not isinstance(info.get("time"), dict)
        or not timestamp(info["time"].get("updated"))
        or (
            info["time"].get("archived") is not None
            and not timestamp(info["time"]["archived"])
        )
        or (info.get("parentID") is not None and not ident(info["parentID"]))
    ):
        raise FixedError(INVENTORY_ENTRY)
    return info


def service_session_catalog(request):
    """Complete THIS private service's metadata; no directory/root/archive filter.

    Separate from session_inventory: this is only an explicit web chooser's
    discovery input, never a broader history-sync or automatic-resume scope.
    """
    query = "/api/session?order=asc&limit=100"
    values, identities, cursors = [], set(), set()
    cursor = None
    while True:
        page = request(
            "GET", query + ("&cursor=" + quote(cursor, safe="") if cursor else "")
        )
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("cursor"), dict)
        ):
            raise FixedError(INVENTORY)
        for info in page["data"]:
            web_session_info(info)
            if info["id"] in identities:
                raise FixedError(INVENTORY_INCONSISTENT)
            identities.add(info["id"])
            values.append(info)
        cursor = page["cursor"].get("next")
        if cursor is None:
            return values
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise FixedError(INVENTORY_CURSOR)
        cursors.add(cursor)


def verified_web_sessions(request, members):
    """Read-only membership intersection, with native GET identity verification."""
    if (
        not isinstance(members, list)
        or len(members) > 4096
        or any(
            not isinstance(ident, str)
            or len(ident) > 128
            or re.fullmatch(r"ses[A-Za-z0-9_-]+", ident) is None
            for ident in members
        )
        or len(set(members)) != len(members)
    ):
        raise FixedError(INVENTORY_ENTRY)
    catalog = {info["id"] for info in service_session_catalog(request)}
    verified = []
    for ident in members:
        if ident not in catalog:
            continue
        try:
            reply = request("GET", "/api/session/" + quote(ident, safe=""))
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            continue
        if not isinstance(reply, dict):
            raise FixedError(INVENTORY_ENTRY)
        info = web_session_info(reply.get("data"))
        if info["id"] != ident:
            raise FixedError(INVENTORY_INCONSISTENT)
        verified.append(info)
    return verified


def checked_export(request, ident):
    """Native export filters unsettled messages; never accept that as full history.

    A running shell/compaction, or an unfinished assistant with nothing settled
    after it, is unsettled history and is refused. An unfinished assistant that
    the session has already moved past (a settled message follows it and the
    session is idle) will never settle: native keeps it out of its export, so the
    archive mirrors that export and the omitted count is reported separately.
    """
    route = "/api/session/" + quote(ident, safe="") + "/message?limit=100"
    projected, states, identities, cursors = [], [], set(), set()
    cursor = None
    while True:
        page = request(
            "GET",
            route + ("&cursor=" + quote(cursor, safe="") if cursor else "&order=asc"),
        )
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("cursor"), dict)
        ):
            raise FixedError(HISTORY)
        for message in page["data"]:
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise FixedError(HISTORY_MESSAGE)
            if message["id"] in identities:
                raise FixedError(HISTORY_INCONSISTENT)
            if message.get("type") in ("shell", "compaction") and (
                message.get("status") == "running"
            ):
                raise FixedError(UNSETTLED)
            incomplete = (
                message.get("type") == "assistant"
                and message.get("time", {}).get("completed") is None
            )
            identities.add(message["id"])
            projected.append(message)
            states.append(incomplete)
        cursor = page["cursor"].get("next")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise FixedError(HISTORY_CURSOR)
        if not page["data"] or cursor is None:
            break
        if cursor in cursors:
            raise FixedError(HISTORY_CURSOR)
        cursors.add(cursor)
    settled = [index for index, incomplete in enumerate(states) if not incomplete]
    omitted = {index for index, incomplete in enumerate(states) if incomplete}
    if omitted and (not settled or max(omitted) > settled[-1]):
        raise FixedError(UNSETTLED)
    response = request(
        "GET", "/api/experimental/session/" + quote(ident, safe="") + "/export"
    )
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise FixedError(EXPORT)
    exported = response["data"]
    expected = [
        message for index, message in enumerate(projected) if index not in omitted
    ]
    if exported.get("messages") != expected:
        raise FixedError(CHANGED)
    if omitted:
        # Read activity last: a turn that started during the export is live.
        activity = request("GET", "/api/session/active")
        if not isinstance(activity, dict) or not isinstance(activity.get("data"), dict):
            raise FixedError(ACTIVITY)
        if ident in activity["data"]:
            raise FixedError(UNSETTLED)
        exported = {**exported, INTERRUPTED: {"assistant": len(omitted)}}
    return exported


@contextlib.contextmanager
def server(binary, *, exit_info=None, startup_deadline=None):
    if startup_deadline is not None:
        _deadline_timeout(startup_deadline)
    password = secrets.token_urlsafe(32)
    env = {**os.environ, "OPENCODE_PASSWORD": password}
    descriptor = os.memfd_create("oc-box-v2-errors", os.MFD_CLOEXEC)
    with os.fdopen(descriptor, "w+b") as errors:
        child = subprocess.Popen(
            [binary, "serve", "--stdio", "--port", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=env,
            start_new_session=True,
        )
        assert child.stdout is not None
        try:
            deadline = time.monotonic() + 45
            if startup_deadline is not None:
                deadline = min(deadline, startup_deadline)
            output = bytearray()
            while b"\n" not in output:
                if child.poll() is not None or time.monotonic() > deadline:
                    raise FixedError(NOT_READY)
                wait = (
                    0.1
                    if startup_deadline is None
                    else min(0.1, _deadline_timeout(deadline))
                )
                if select.select([child.stdout], [], [], wait)[0]:
                    output.extend(os.read(child.stdout.fileno(), 4096))
                if len(output) > 65536:
                    raise FixedError(READINESS)
            address = json.loads(output.split(b"\n", 1)[0])["url"]
            request = endpoint_request({"url": address, "password": password})
            request.alive = lambda: child.poll() is None

            # Migration is asynchronous in upstream. Do not mistake an empty
            # session list for successful completion and lose the resume ID.
            deadline = time.monotonic() + 120
            if startup_deadline is not None:
                deadline = min(deadline, startup_deadline)
            while True:
                if startup_deadline is None:
                    status = request("GET", "/api/experimental/migration/v1")
                else:
                    _deadline_timeout(deadline)
                    status = request(
                        "GET", "/api/experimental/migration/v1", deadline=deadline
                    )
                    _deadline_timeout(deadline)
                if not isinstance(status, dict):
                    raise ValueError("Invalid OpenCode migration response.")
                status = status.get("data", status)
                if status.get("status") == "completed":
                    break
                if status.get("status") == "error" or time.monotonic() > deadline:
                    raise ValueError(
                        "OpenCode's V1-to-V2 session migration failed or timed out; the original V1 database is retained."
                    )
                time.sleep(
                    0.1
                    if startup_deadline is None
                    else min(0.1, _deadline_timeout(deadline))
                )
            yield request
        finally:
            forced = False
            if child.stdin:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forced = True
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
            if exit_info is not None:
                exit_info.update(code=child.returncode, forced=forced)


def seed_database():
    root = Path(os.environ["XDG_DATA_HOME"]) / "opencode"
    source, target = root / "opencode.db", root / "oc-box-v2.db"
    if not source.is_file() or target.exists():
        return
    staged = root / (".oc-box-v2-" + secrets.token_hex(8) + ".db")
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with (
            contextlib.closing(
                sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            ) as old,
            contextlib.closing(sqlite3.connect(staged)) as new,
        ):
            old.backup(new)
        os.link(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def refresh_credentials(binary, entries):
    if not isinstance(entries, list):
        raise ValueError("Invalid OpenCode credential export.")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("value"), dict)
            or not isinstance(entry.get("label"), str)
        ):
            raise ValueError("Invalid OpenCode credential entry.")
    with server(binary) as request:
        current = {
            entry["id"]: entry for entry in request("GET", "/api/credential")["data"]
        }
        for entry in entries:
            old = current.get(entry["id"])
            comparable = ("integrationID", "label", "value")
            if old and any(old.get(key) != entry.get(key) for key in comparable):
                request("DELETE", "/api/credential/" + quote(entry["id"], safe=""))
                old = None
            if old is None:
                request(
                    "POST",
                    "/api/credential",
                    {
                        key: entry[key]
                        for key in ("id", "integrationID", "label", "value")
                        if entry.get(key) is not None
                    }
                    | {"activate": bool(entry.get("active"))},
                )
        # Apply host active selections after all replacements, so the first
        # inactive entry in a new integration cannot accidentally win.
        for entry in entries:
            if entry.get("active"):
                request(
                    "POST",
                    "/api/credential/" + quote(entry["id"], safe="") + "/activate",
                )


HISTORY_SOURCE_SUPPORTED = frozenset(("2.3.0",))
HISTORY_SOURCE_MAX_PAYLOAD = 131072
HISTORY_SOURCE_MARKER = "oc-box-history-source: "
HISTORY_SOURCE_FALLBACKS = frozenset(
    (
        "database-missing",
        "schedule-present",
        "jobs-present",
        "queued-work",
        "execution-present",
    )
)
HISTORY_SOURCE_ERRORS = frozenset(
    (
        "payload",
        "database-unsafe",
        "database-invalid",
        "schedule-schema",
        "system-config",
        "job-schema",
        "execution-schema",
        "unsupported-version",
        "activity",
        "startup-timeout",
        "lease-timeout",
        "lease-ended",
        "control",
        "cleanup-failed",
    )
)


class HistorySourceError(ValueError):
    """Fixed diagnostics for the optional, non-recovery source lease."""

    def __init__(self, category):
        self.category = category if category in HISTORY_SOURCE_ERRORS else "payload"
        super().__init__(self.category)


def history_source_database(guards, database, *, deadline=None):
    """Read execution metadata only; never initialize, migrate or edit a store.

    This is stricter than the post-stop reader: *all* durable jobs, queued work
    and claims exclude this optimization. They remain the original per-request
    path's responsibility, not permission to recover or resume a job here.
    """
    try:
        guards["_check_system_config"]()
        binding = guards["_check_database"](database)
    except guards["Refusal"] as error:
        if error.category in HISTORY_SOURCE_FALLBACKS:
            return error.category
        raise HistorySourceError(error.category) from None
    try:

        def verify(info):
            fields = ("st_dev", "st_ino", "st_uid", "st_mode", "st_nlink")
            if (
                binding is None
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o022
                or any(getattr(info, key) != getattr(binding, key) for key in fields)
            ):
                raise HistorySourceError("database-unsafe")

        verify(database.lstat())
        with contextlib.ExitStack() as stack:
            descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            stack.callback(os.close, descriptor)

            def bound():
                verify(os.fstat(descriptor))
                verify(database.lstat())

            bound()
            connection = stack.enter_context(
                contextlib.closing(
                    sqlite3.connect(
                        # Keep the canonical pathname so committed WAL rows remain
                        # visible; /proc/fd and immutable URIs are not substitutes.
                        database.as_uri() + "?mode=ro",
                        uri=True,
                        timeout=1
                        if deadline is None
                        else _deadline_timeout(deadline, 1),
                    )
                )
            )
            bound()
            if deadline is not None:
                connection.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 1000
                )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")

            def table(name, required, primary):
                if connection.execute(
                    "SELECT type FROM sqlite_master WHERE name = ?", (name,)
                ).fetchall() != [("table",)]:
                    raise HistorySourceError(
                        "execution-schema"
                        if name.startswith("session")
                        else "job-schema"
                    )
                columns = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info('" + name + "')")
                }
                if (
                    any(
                        key not in columns or columns[key][2].upper() != kind
                        for key, kind in required.items()
                    )
                    or not columns[primary][5]
                ):
                    raise HistorySourceError(
                        "execution-schema"
                        if name.startswith("session")
                        else "job-schema"
                    )

            table(
                "job",
                {
                    "notification_id": "TEXT",
                    "id": "TEXT",
                    "session_id": "TEXT",
                    "kind": "TEXT",
                    "status": "TEXT",
                    "recovery": "TEXT",
                },
                "notification_id",
            )
            table("kv", {"key": "TEXT", "value": "TEXT"}, "key")
            table(
                "session_inbox",
                {"id": "TEXT", "session_id": "TEXT", "delivery": "TEXT"},
                "id",
            )
            table(
                "session_v2",
                {
                    "id": "TEXT",
                    "time_suspended": "INTEGER",
                    "time_compacting": "INTEGER",
                    "time_busy": "INTEGER",
                    "time_idle": "INTEGER",
                },
                "id",
            )
            timestamps = ("time_suspended", "time_compacting", "time_busy", "time_idle")
            if connection.execute(
                "SELECT 1 FROM session_v2 WHERE "
                + " OR ".join(
                    "(" + name + " IS NOT NULL AND typeof(" + name + ") != 'integer')"
                    for name in timestamps
                )
                + " LIMIT 1"
            ).fetchone():
                raise HistorySourceError("execution-schema")
            # Include legacy background records: migration must not turn a
            # superficially empty job table into resumed native work.
            fallback = None
            if connection.execute('SELECT 1 FROM "job" LIMIT 1').fetchone() or (
                connection.execute(
                    "SELECT 1 FROM kv WHERE key LIKE 'job.background/%' LIMIT 1"
                ).fetchone()
            ):
                fallback = "jobs-present"
            elif connection.execute("SELECT 1 FROM session_inbox LIMIT 1").fetchone():
                fallback = "queued-work"
            elif connection.execute(
                "SELECT 1 FROM session_v2 WHERE time_suspended IS NOT NULL "
                "OR time_compacting IS NOT NULL "
                "OR (time_busy IS NOT NULL AND "
                "(time_idle IS NULL OR time_busy > time_idle)) LIMIT 1"
            ).fetchone():
                fallback = "execution-present"
            # Recheck schedules in the same read snapshot as jobs/claims.
            if connection.execute("SELECT 1 FROM schedule LIMIT 1").fetchone():
                fallback = "schedule-present"
            bound()
        verify(database.lstat())
        if deadline is not None:
            _deadline_timeout(deadline)
    except OSError:
        raise HistorySourceError("database-unsafe") from None
    except sqlite3.Error:
        if deadline is not None:
            _deadline_timeout(deadline)
        raise HistorySourceError("database-invalid") from None
    return fallback


def history_source_quiet(request, *, deadline=None):
    """No foreground drain or job of any state may belong to this lease."""
    for route, empty in (
        ("/api/session/active", {}),
        ("/api/experimental/job", []),
    ):
        response = (
            request("GET", route)
            if deadline is None
            else request("GET", route, deadline=deadline)
        )
        if not isinstance(response, dict) or response.get("data") != empty:
            raise HistorySourceError("activity")


def _history_source_payload(deadline):
    payload = bytearray()
    while b"\n" not in payload:
        if not select.select(
            [sys.stdin], [], [], min(0.1, _deadline_timeout(deadline))
        )[0]:
            continue
        chunk = os.read(0, min(4096, HISTORY_SOURCE_MAX_PAYLOAD + 1 - len(payload)))
        if not chunk:
            raise HistorySourceError("payload")
        payload.extend(chunk)
        if len(payload) > HISTORY_SOURCE_MAX_PAYLOAD:
            raise HistorySourceError("payload")
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise HistorySourceError("payload")
    return bytes(payload)


def history_source_lease(binary):
    """Container-only optional quiet source; never a recovery Reader or owner.

    The caller holds its normal workspace lifecycle lock. There is no shared
    service discovery, controller socket, config refresh or execution request.
    Only the real source DATA directory survives in otherwise empty discovery
    roots. This function runs in a disposable isolated Python process, not the
    host interpreter: its environment/cwd changes are intentionally child-local.
    """
    startup_deadline = time.monotonic() + 45
    # No asynchronous alarm is armed: native cleanup always receives its own
    # finite wait/kill allowance, including a failure just before this deadline.
    payload = _history_source_payload(startup_deadline)
    if len(payload) > HISTORY_SOURCE_MAX_PAYLOAD or not payload.endswith(b"\n"):
        raise HistorySourceError("payload")
    settings = json.loads(payload)
    if (
        not isinstance(settings, dict)
        or set(settings) != {"guards", "version", "home"}
        or settings["version"] not in HISTORY_SOURCE_SUPPORTED
        or not isinstance(settings["guards"], str)
        or not isinstance(settings["home"], str)
        or not settings["home"].startswith("/")
        or os.path.normpath(settings["home"]) != settings["home"]
        or "\0" in settings["home"]
    ):
        raise HistorySourceError("payload")
    descriptor = require_control_pipe()
    guards = {"__name__": "oc_box_source_guards"}
    exec(settings["guards"], guards)
    database = Path(settings["home"]) / ".local/share/opencode/oc-box-v2.db"
    fallback = history_source_database(guards, database, deadline=startup_deadline)
    if fallback:
        write_control_record(descriptor, {"fallback": fallback})
        return
    # Clear create-time secrets before *any* native command. Reuse only the
    # stdlib guard/environment helpers, not Reader's verified-stop lifetime.
    with guards["_remote_environment"](settings["home"]) as (root, env):
        os.chdir(root)
        os.environ.clear()
        os.environ.update(env)
        version = subprocess.run(
            [binary, "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=_deadline_timeout(startup_deadline, 10),
        )
        try:
            if version.returncode:
                raise HistorySourceError("unsupported-version")
            guards["_check_version"](version.stdout, settings["version"])
        except guards["Refusal"]:
            raise HistorySourceError("unsupported-version") from None
        exit_info = {}
        _deadline_timeout(startup_deadline)
        with server(
            binary, exit_info=exit_info, startup_deadline=startup_deadline
        ) as request:
            history_source_quiet(request, deadline=startup_deadline)
            # Check once more before publishing; startup must not introduce
            # durable work. A post-start refusal is fatal, never a fallback.
            if history_source_database(guards, database, deadline=startup_deadline):
                raise HistorySourceError("activity")
            _deadline_timeout(startup_deadline)
            write_control_record(descriptor, {"endpoint": request.endpoint})
            lease_deadline = time.monotonic() + 600
            while request.alive():
                if time.monotonic() >= lease_deadline:
                    raise HistorySourceError("lease-timeout")
                if select.select(
                    [sys.stdin],
                    [],
                    [],
                    min(0.1, max(0, lease_deadline - time.monotonic())),
                )[0]:
                    if os.read(0, 1):
                        raise HistorySourceError("control")
                    break
            else:
                raise HistorySourceError("lease-ended")
            final_deadline = time.monotonic() + 5
            history_source_quiet(request, deadline=final_deadline)
            if history_source_database(guards, database, deadline=final_deadline):
                raise HistorySourceError("activity")
        if exit_info != {"code": 0, "forced": False}:
            raise HistorySourceError("cleanup-failed")


def history_worker():
    """One already-guarded reader pass; this protocol never starts a service."""
    os.environ.clear()
    os.environ.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    pending, eof = bytearray(), object()
    deadline = time.monotonic() + 45

    def control():
        while b"\n" not in pending:
            if len(pending) >= HISTORY_WORKER_CONTROL:
                raise FixedError(WORKER)
            budget = _deadline_timeout(deadline, 180)
            if not select.select([incoming], [], [], budget)[0]:
                raise FixedError(NOT_READY)
            try:
                capacity = HISTORY_WORKER_CONTROL - len(pending)
                chunk = os.read(incoming, min(4096, capacity))
            except BlockingIOError:
                continue
            if not chunk:
                if pending:
                    raise FixedError(WORKER)
                return eof
            pending.extend(chunk)
        end = pending.index(b"\n")
        line = bytes(pending[:end])
        del pending[: end + 1]
        return json.loads(line.decode("utf-8"))

    def reply(header, body=b""):
        for content in ((json.dumps(header) + "\n").encode("utf-8"), body):
            view = memoryview(content)
            while view:
                budget = _deadline_timeout(deadline, 180)
                if not select.select([], [outgoing], [], budget)[1]:
                    raise FixedError(NOT_READY)
                try:
                    count = os.write(outgoing, view[:65536])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise FixedError(WORKER)
                view = view[count:]

    try:
        incoming, outgoing = sys.stdin.fileno(), sys.stdout.fileno()
        os.set_blocking(incoming, False)
        os.set_blocking(outgoing, False)
        settings = control()
        if settings is eof:
            return
        if not isinstance(settings, dict):
            raise FixedError(WORKER)
        if set(settings) != {"endpoint", "directory", "timeout"}:
            raise FixedError(WORKER)
        directory, timeout = settings["directory"], settings["timeout"]
        if (
            not isinstance(directory, str)
            or not directory.startswith("/")
            or len(directory) > 4096
            or any(char in directory for char in "\0\r\n")
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 180
        ):
            raise FixedError(WORKER)
        deadline = time.monotonic() + timeout
        endpoint = settings["endpoint"]
        if not isinstance(endpoint, dict) or set(endpoint) != {"url", "password"}:
            raise FixedError(ENDPOINT)
        request = endpoint_request(endpoint)
        del settings, endpoint, request.endpoint
        reply({"protocol": HISTORY_WORKER_PROTOCOL})

        def bounded_request(method, path):
            return request(method, path, deadline=deadline)

        sequence = 1
        while True:
            try:
                message = control()
                if message is eof:
                    return
                if (
                    not isinstance(message, dict)
                    or type(message.get("id")) is not int
                    or message["id"] != sequence
                ):
                    raise FixedError(WORKER)
                action, session = message.get("action"), message.get("session")
                keys = {"id", "action"}
                if action == "export":
                    keys.add("session")
                if set(message) != keys or action not in ("sessions", "export"):
                    raise FixedError(WORKER)
                if action == "export" and (
                    not isinstance(session, str)
                    or len(session) > 256
                    or re.fullmatch(r"ses[A-Za-z0-9_-]+", session) is None
                ):
                    raise FixedError(WORKER)
                value = (
                    session_inventory(bounded_request, directory)
                    if action == "sessions"
                    else checked_export(bounded_request, session)
                )
                body = json.dumps(value).encode("utf-8")
                if len(body) > HISTORY_WORKER_PAYLOAD:
                    raise FixedError(WORKER)
            except FixedError as error:
                reason = str(error)
                reply({"id": sequence, "error": reason})
                if reason not in PENDING_MESSAGES:
                    raise
            except Exception:
                reply({"id": sequence, "error": WORKER})
                raise FixedError(WORKER) from None
            else:
                reply({"id": sequence, "size": len(body)}, body)
                del value, body
            sequence += 1
    except FixedError:
        raise
    except Exception:
        raise FixedError(WORKER) from None


def main(argv):
    action, binary = argv
    if action == "seed":
        seed_database()
    elif action == "auth":
        refresh_credentials(binary, json.load(sys.stdin))
    elif action == "ready":
        with server(binary):
            pass
    elif action in ("sessions", "export"):
        settings = json.load(sys.stdin)
        endpoint = settings.get("endpoint")
        with (
            contextlib.nullcontext(endpoint_request(endpoint))
            if endpoint is not None
            else server(binary)
        ) as request:
            value = (
                session_inventory(request, settings["directory"])
                if action == "sessions"
                else checked_export(request, settings["session"])
            )
            print(json.dumps(value), flush=True)
    elif action == "web-sessions":
        settings = json.load(sys.stdin)
        # Mandatory existing endpoint: never discover/start a service here.
        value = verified_web_sessions(
            endpoint_request(settings["endpoint"]), settings["members"]
        )
        print(json.dumps(value), flush=True)
    elif action == "lease":
        descriptor = require_control_pipe()
        with server(binary) as request:
            # Control credentials are returned only through the owner's pipe,
            # kept in memory, and never written to the workspace or service.json.
            write_control_record(descriptor, request.endpoint)
            while request.alive():
                if select.select([sys.stdin], [], [], 0.2)[0] and not os.read(
                    sys.stdin.fileno(), 4096
                ):
                    break
    elif action == "history-source":
        history_source_lease(binary)
    elif action == "history-worker":
        history_worker()
    elif action == "plugins":
        with server(binary) as request:
            query = "?directory=" + quote(os.getcwd(), safe="")
            request("GET", "/api/model/default" + query)
            plugins = request("GET", "/api/plugin" + query)["data"]
            if not any(
                plugin.get("id") == "oc-box.build-permissions"
                and plugin.get("state", {}).get("status") == "active"
                for plugin in plugins
            ):
                raise ValueError(
                    "The oc-box Build permissions plugin did not activate."
                )
            for plugin in plugins:
                if plugin.get("state", {}).get("status") == "failed":
                    source = plugin.get("source", {})
                    name = (
                        source.get("target")
                        or source.get("path")
                        or plugin.get("id")
                        or "unknown"
                    )
                    print(
                        "oc-box: OpenCode 2.x plugin unavailable: "
                        + str(name)
                        + "; port this plugin to the V2 API or install its V2-compatible release."
                    )
                    error = str(plugin.get("state", {}).get("error", ""))
                    if "oc-box V1 adapter: unsupported hooks " in error:
                        import re

                        match = re.search(
                            r"oc-box V1 adapter: unsupported hooks ([A-Za-z0-9., _-]+)",
                            error,
                        )
                        if match:
                            print(
                                "oc-box: OpenCode 2.x plugin unavailable: unsupported legacy hooks: "
                                + match[1][:300]
                            )
    else:
        raise ValueError("Unknown private OpenCode operation.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except HistorySourceError as error:
        print(HISTORY_SOURCE_MARKER + error.category, file=sys.stderr)
        sys.exit(2)
    except FixedError as error:
        # One of FIXED_MESSAGES only; the host re-checks it against the whitelist.
        print(MARKER + str(error), file=sys.stderr)
        sys.exit(2)
    except Exception:
        # Native errors can contain credential-bearing request/response data.
        print(
            "Private OpenCode 2.x operation failed; no private tool output was logged.",
            file=sys.stderr,
        )
        sys.exit(1)
