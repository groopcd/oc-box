"""Private standard-CLI V2 service lease for credential refresh and migration.

Used inside a workspace (and offline fixtures). Secret input/output stays in
memory/pipes; a fresh --stdio server never elects or modifies a shared service.
"""

import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import select
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, ProxyHandler

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

    def request(method, path, data=None):
        req = Request(
            address.rstrip("/") + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": auth, "Content-Type": "application/json"},
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
def server(binary):
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
            output = bytearray()
            while b"\n" not in output:
                if child.poll() is not None or time.monotonic() > deadline:
                    raise FixedError(NOT_READY)
                if select.select([child.stdout], [], [], 0.1)[0]:
                    output.extend(os.read(child.stdout.fileno(), 4096))
                if len(output) > 65536:
                    raise FixedError(READINESS)
            address = json.loads(output.split(b"\n", 1)[0])["url"]
            request = endpoint_request({"url": address, "password": password})
            request.alive = lambda: child.poll() is None

            # Migration is asynchronous in upstream. Do not mistake an empty
            # session list for successful completion and lose the resume ID.
            deadline = time.monotonic() + 120
            while True:
                status = request("GET", "/api/experimental/migration/v1")
                if not isinstance(status, dict):
                    raise ValueError("Invalid OpenCode migration response.")
                status = status.get("data", status)
                if status.get("status") == "completed":
                    break
                if status.get("status") == "error" or time.monotonic() > deadline:
                    raise ValueError(
                        "OpenCode's V1-to-V2 session migration failed or timed out; the original V1 database is retained."
                    )
                time.sleep(0.1)
            yield request
        finally:
            if child.stdin:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()


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
    elif action == "lease":
        with server(binary) as request:
            # Control credentials are returned only through the owner's pipe,
            # kept in memory, and never written to the workspace or service.json.
            print(json.dumps(request.endpoint), flush=True)
            while request.alive():
                if select.select([sys.stdin], [], [], 0.2)[0] and not os.read(
                    sys.stdin.fileno(), 4096
                ):
                    break
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
