"""Metadata-only browser history of an already-leased private workspace.

This is independent of TUI membership, focus, and startup selection. The
caller owns browser authentication and holds a verified owner/client lease
for the entire call. Only the registered in-container project directory is
queried: no browser path, descendants, moved locations, or host inventory.
"""

import base64
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

PAGE_SIZE = 50
MAX_ROWS = 1000
# Native can return a next cursor on a full last page, followed by an empty page.
MAX_PAGES = 21
MAX_PAGE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_TITLE_BYTES = 1024
MAX_CURSOR_BYTES = 16384
WORKER_TIMEOUT = 4
HOST_TIMEOUT = 6


class CatalogError(ValueError):
    """Fixed refusal; no path, native body, cursor, or credential in the error."""

    def __init__(self):
        super().__init__("Workspace history could not be verified.")


def _refuse():
    raise CatalogError() from None


def _directory(value):
    if (
        not isinstance(value, str)
        or not 1 < len(value) <= 4096
        or not value.startswith("/")
        or value.startswith("//")
        or str(path := PurePosixPath(value)) != value
        or ".." in path.parts
        or len(value.encode("utf-8")) > 4096
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    ):
        _refuse()
    return value


def _port(url):
    if not isinstance(url, str) or len(url) > 23:
        _refuse()
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", url)
    if match is None or int(match[1]) > 65535:
        _refuse()
    return int(match[1])


def _id(value):
    return (
        isinstance(value, str)
        and len(value) <= 128
        and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None
    )


def _timestamp(value):
    return type(value) in (int, float) and 0 <= value <= 2**53 - 1


def _display_fields(row, hidden=()):
    if not isinstance(row, dict) or not _id(row.get("id")):
        _refuse()
    ident, times = row["id"], row.get("time")
    title = row.get("title", ident)
    if (
        not isinstance(title, str)
        or not isinstance(times, dict)
        or not all(_timestamp(times.get(key)) for key in ("created", "updated"))
        or ("archived" in times and not _timestamp(times["archived"]))
        or any(value in title or value in ident for value in hidden)
    ):
        _refuse()
    # Titles are untrusted text (potentially prompt-derived), not HTML, and
    # clipping is not a promise to redact secrets users put in their own titles.
    title.encode("utf-8")  # refuse malformed escaped surrogate characters
    title = "".join(char if char.isprintable() else " " for char in title)
    clipped = title.encode("utf-8")[:MAX_TITLE_BYTES].decode("utf-8", "ignore").strip()
    return {
        "id": ident,
        "title": clipped or ident,
        "time": {
            key: times[key]
            for key in ("created", "updated", "archived")
            if key in times
        },
    }


def _json(payload):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            _refuse()
        return value

    def invalid_constant(_):
        _refuse()

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=invalid_constant,
    )


def _cursor(value):
    if value is not None and (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_CURSOR_BYTES
        or len(value.encode("utf-8")) > MAX_CURSOR_BYTES
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    ):
        _refuse()
    return value


def _scoped_row(raw, directory, *, expected=None, hidden=()):
    row = _display_fields(raw, hidden)
    location, project, ident = raw.get("location"), raw.get("projectID"), row["id"]
    if (
        not isinstance(location, dict)
        or location.get("directory") != directory
        or not isinstance(project, str)
        or not 0 < len(project) <= 256
        or (expected is not None and ident != expected)
        or (
            "parentID" in raw and (not _id(raw["parentID"]) or raw["parentID"] == ident)
        )
    ):
        _refuse()
    # A child can refer to a moved parent; never disclose that ID.
    return {**row, "isChild": "parentID" in raw}


def _inventory(fetch, directory, *, hidden=()):
    """Read a whole cursor walk or refuse. Native offers no snapshot transaction."""
    query = [
        ("directory", _directory(directory)),
        ("order", "desc"),
        ("limit", PAGE_SIZE),
    ]
    rows, seen, cursors = [], set(), set()
    cursor = None
    remaining = MAX_TOTAL_BYTES
    for _ in range(MAX_PAGES):
        if remaining <= 0:
            _refuse()
        fields = query if cursor is None else [*query, ("cursor", cursor)]
        path = "/api/session?" + urlencode(fields, quote_via=quote)
        budget = min(remaining, MAX_PAGE_BYTES)
        payload = fetch(path, budget)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= budget:
            _refuse()
        remaining -= len(payload)
        page = _json(payload)
        if (
            not isinstance(page, dict)
            or not isinstance(items := page.get("data"), list)
            or len(items) > PAGE_SIZE
            or len(rows) + len(items) > MAX_ROWS
            or not isinstance(pagination := page.get("cursor"), dict)
            or "next" not in pagination
        ):
            _refuse()
        for raw in items:
            row = _scoped_row(raw, directory, hidden=hidden)
            ident = row["id"]
            if ident in seen:
                _refuse()
            seen.add(ident)
            rows.append(row)
        _cursor(pagination.get("previous"))
        cursor = _cursor(pagination["next"])
        if cursor is None:
            return rows
        if cursor in cursors:
            _refuse()
        cursors.add(cursor)
    _refuse()


def _request(port, authorization, path, budget):
    """Inside the container, talk directly to the one authenticated loopback peer."""
    connection = HTTPConnection("127.0.0.1", port, timeout=WORKER_TIMEOUT)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Authorization": authorization,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            sizes = response.headers.get_all("Content-Length", ())
            encodings = response.headers.get_all("Content-Encoding", [])
            # The observed native JSON responses are length-framed. Do not
            # accept redirects, decompression, or possibly incomplete EOF bodies.
            if (
                response.status != 200
                or response.headers.get_content_type() != "application/json"
                or len(response.headers.get_all("Content-Type", ())) != 1
                or "Transfer-Encoding" in response.headers
                or encodings not in ([], ["identity"])
                or len(sizes) != 1
                or re.fullmatch(r"[1-9][0-9]{0,6}", sizes[0]) is None
                or not 0 < (size := int(sizes[0])) <= budget
            ):
                _refuse()
            payload = response.read(size)
            if len(payload) != size:
                _refuse()
            return payload
    finally:
        connection.close()


def _selected_row(fetch, directory, ident, *, hidden=()):
    if not _id(ident):
        _refuse()
    payload = _json(fetch("/api/session/" + ident, MAX_PAGE_BYTES))
    return _scoped_row(
        payload.get("data") if isinstance(payload, dict) else None,
        directory,
        expected=ident,
        hidden=hidden,
    )


def _worker(url, directory, selected=None):
    # Hard whole-operation budget, including trickled HTTP headers and pipe I/O.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.setitimer(signal.ITIMER_REAL, WORKER_TIMEOUT)
    port = _port(url)
    if os.getcwd() != _directory(directory):
        _refuse()
    password = os.environ.pop("OPENCODE_PASSWORD", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password) is None:
        _refuse()
    token = base64.b64encode(("opencode:" + password).encode("ascii")).decode("ascii")
    authorization = "Basic " + token

    def fetch(path, budget):
        return _request(port, authorization, path, budget)

    hidden = (password, token)
    rows = (
        _inventory(fetch, directory, hidden=hidden)
        if selected is None
        else [_selected_row(fetch, directory, selected, hidden=hidden)]
    )
    output = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(output) > MAX_OUTPUT_BYTES:
        _refuse()
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


def _read_bridge(command):
    """Cap output while reading, then require clean EOF and process success."""
    deadline = time.monotonic() + HOST_TIMEOUT
    process = subprocess.Popen(
        command,
        env=command.environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        payload = bytearray()
        with process.stdout as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
                    _refuse()
                chunk = os.read(
                    stream.fileno(), min(65536, MAX_OUTPUT_BYTES + 1 - len(payload))
                )
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_OUTPUT_BYTES:
                    _refuse()
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            _refuse()
        return bytes(payload)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def _display_catalog(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_OUTPUT_BYTES:
        _refuse()
    rows = _json(payload)
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        _refuse()
    seen = set()
    for row in rows:
        expected = _display_fields(row)
        if (
            type(row.get("isChild")) is not bool
            or row != {**expected, "isChild": row["isChild"]}
            or row["id"] in seen
        ):
            _refuse()
        seen.add(row["id"])
    return rows


def _project_rows(box, data, connection, selected=None):
    """Return ordered id/title/time/isChild rows through an existing private lease.

    A limit or partial result raises CatalogError, not an empty catalog.
    An empty list means a successfully exhausted native inventory.
    """
    try:
        if (
            not isinstance(data, dict)
            or not isinstance(connection, dict)
            or not isinstance(ident := data.get("id"), str)
            or not ident
            or connection.get("workspace") != ident
        ):
            _refuse()
        box.workspace_service.validate_endpoint(connection)
        url = connection["url"]
        _port(url)
        directory = _directory(box.container_project(data))
        command = box.workspace_service.client_command(
            box,
            data,
            connection,
            [
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                Path(__file__).read_text(encoding="utf-8"),
                url,
                directory,
                *([selected] if selected is not None else []),
            ],
        )
        return _display_catalog(_read_bridge(command))
    except CatalogError:
        raise
    except (
        box.BoxError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        _refuse()


def catalog(box, data, connection):
    return _project_rows(box, data, connection)


def selection(box, data, connection, ident):
    """Recheck one result in the exact project directory before navigation."""
    if not _id(ident):
        _refuse()
    rows = _project_rows(box, data, connection, ident)
    if len(rows) != 1 or rows[0]["id"] != ident:
        _refuse()
    return rows[0]


if __name__ == "__main__":
    try:
        _worker(*sys.argv[1:])
    except Exception:  # noqa: BLE001 - subprocess failure crosses the private pipe only as exit status.
        # Native errors and Python exceptions can refer to private data. Only
        # success writes stdout; the host observes a nonzero exit otherwise.
        sys.exit(1)
