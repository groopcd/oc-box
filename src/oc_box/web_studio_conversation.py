"""Read-only, plain-text Studio projection of one private native conversation.

No HTTP handler. A trusted caller must hold the verified workspace owner/client
lease and independently authorize the browser and exact selected session.
"""

import base64
from http.client import HTTPConnection, HTTPException
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import select
import signal
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlencode

NATIVE_VERSION = "2.4.1"
PAGE_SIZE = 24
MAX_PAGES = 3
MAX_NATIVE_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_TITLE_BYTES = 256
MAX_TEXT_BYTES = 4096
MAX_PARTS = 64
WORKER_TIMEOUT = 4
HOST_TIMEOUT = 6

_BASE_MESSAGE = {"id", "type", "time", "metadata"}
_MESSAGE_FIELDS = {
    "user": {"text", "files", "agents", "skills"},
    "assistant": {
        "agent",
        "model",
        "content",
        "snapshot",
        "finish",
        "rawFinish",
        "native",
        "cost",
        "tokens",
        "error",
        "retry",
    },
    "agent-switched": {"agent", "previous"},
    "model-switched": {"model", "previous"},
    "system": {"text", "description"},
    "synthetic": {"text", "description"},
    "skill": {"skill", "name", "text"},
    "shell": {"shellID", "command", "status", "exit", "output"},
    "compaction": {
        "status",
        "reason",
        "summary",
        "model",
        "native",
        "error",
        "cost",
        "tokens",
    },
    "idle": {"outcome", "error"},
}
_ASSISTANT_FIELDS = {
    "text": {"type", "text", "native"},
    "reasoning": {"type", "text", "native", "time"},
    "tool": {"type", "id", "name", "executed", "native", "state", "time"},
}
_USER_ATTACHMENTS = {
    "files": {"data", "mime", "source", "name", "description", "mention"},
    "agents": {"name", "mention"},
    "skills": {"id", "name", "text", "mention"},
}
_TOOL_STATE_FIELDS = {
    "streaming": {"status", "input"},
    "running": {"status", "input", "metadata"},
    "completed": {"status", "input", "metadata", "content"},
    "error": {"status", "input", "error", "metadata", "content"},
}


class ConversationError(ValueError):
    """Fixed refusal; never include transcript, endpoint, path or credentials."""

    def __init__(self):
        super().__init__("The Studio conversation could not be verified.")


def _refuse():
    raise ConversationError() from None


def _session_id(value):
    return isinstance(value, str) and re.fullmatch(r"ses[A-Za-z0-9_-]{1,125}", value)


def _message_id(value):
    return isinstance(value, str) and re.fullmatch(r"msg_[A-Za-z0-9_-]{1,124}", value)


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


def _endpoint(value):
    match = (
        re.fullmatch(r"(http://127\.0\.0\.1:([1-9][0-9]{0,4}))/?", value)
        if isinstance(value, str)
        else None
    )
    if match is None or int(match[2]) > 65535:
        _refuse()
    return match[1], int(match[2])


def _timestamp(value):
    return type(value) in (int, float) and 0 <= value <= 2**53 - 1


def _json(payload):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            _refuse()
        return result

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            _refuse()
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _: _refuse(),
        parse_float=finite,
    )


def _data(reply):
    if not isinstance(reply, dict) or set(reply) != {"data"}:
        _refuse()
    return reply["data"]


def _snapshot(fetch, origin, directory, route, ident):
    info = fetch("/api/info")
    if (
        not isinstance(info, dict)
        or info.get("version") != NATIVE_VERSION
        or info.get("urls") != [origin]
        or type(pid := info.get("pid")) is not int
        or not 0 < pid <= 2**53 - 1
    ):
        _refuse()
    location = fetch("/api/location")
    if (
        not isinstance(location, dict)
        or location.get("directory") != directory
        or not isinstance(project := location.get("project"), dict)
        or not isinstance(project_id := project.get("id"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id) is None
    ):
        _refuse()
    project_dir = _directory(project.get("directory"))
    canonical = _directory(project.get("canonical"))
    path = PurePosixPath(directory)
    if not path.is_relative_to(project_dir):
        _refuse()
    relative = path.relative_to(project_dir)
    subpath = None if relative == PurePosixPath(".") else str(relative)
    session = _data(fetch(route))
    if (
        not isinstance(session, dict)
        or session.get("id") != ident
        or session.get("location") != {"directory": directory}
        or session.get("projectID") != project_id
        or session.get("subpath") != subpath
        or "revert" in session
        or "fork" in session
        or not isinstance(times := session.get("time"), dict)
        or not all(_timestamp(times.get(key)) for key in ("created", "updated"))
        or (
            "parentID" in session
            and (not _session_id(session["parentID"]) or session["parentID"] == ident)
        )
    ):
        _refuse()
    return (pid, project_dir, canonical, project_id, session)


def _cursor(value):
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value) is None
    ):
        _refuse()
    return value


def _page(reply, limit):
    if (
        not isinstance(reply, dict)
        or set(reply) != {"data", "cursor"}
        or not isinstance(items := reply["data"], list)
        or len(items) > limit
        or not isinstance(cursors := reply["cursor"], dict)
        or set(cursors) != {"next", "previous"}
    ):
        _refuse()
    previous, following = _cursor(cursors["previous"]), _cursor(cursors["next"])
    if (not items and (previous is not None or following is not None)) or (
        following is not None and following == previous
    ):
        _refuse()
    return items, following


def _require_unmoved(fetch, messages_route):
    # Check the whole history via native's type filter, not just the visible
    # window. Even moved-away-and-back sessions can contain foreign messages.
    items, _ = _page(
        fetch(messages_route + "?limit=1&order=desc&type=location-switched"), 1
    )
    if items:
        _refuse()


def _plain(value, limit, hidden, *, multiline=False):
    if not isinstance(value, str) or any(secret in value for secret in hidden):
        _refuse()
    value.encode("utf-8")  # Do not silently drop malformed escaped surrogates.
    if multiline:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(
        char
        if (multiline and char in "\t\n")
        or (unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
        else ("" if multiline else " ")
        for char in value
    )
    # Removing a control character must not accidentally reconstruct a secret.
    if any(secret in value for secret in hidden):
        _refuse()
    encoded = value.encode("utf-8")
    return encoded[:limit].decode("utf-8", "ignore"), len(encoded) > limit


def _check_tool(part):
    state = part.get("state")
    if (
        not isinstance(part.get("id"), str)
        or not isinstance(part.get("name"), str)
        or not isinstance(state, dict)
        or not isinstance(status := state.get("status"), str)
        or status not in _TOOL_STATE_FIELDS
        or not state.keys() <= _TOOL_STATE_FIELDS[status]
    ):
        _refuse()
    if "content" not in state:
        if status == "completed":
            _refuse()
        return
    items = state["content"]
    if not isinstance(items, list) or not 0 < len(items) <= MAX_PARTS:
        _refuse()
    for item in items:
        if not isinstance(item, dict):
            _refuse()
        if item.get("type") == "text":
            if set(item) != {"type", "text"} or not isinstance(item["text"], str):
                _refuse()
        elif item.get("type") == "file":
            if (
                not item.keys() <= {"type", "uri", "mime", "name"}
                or not isinstance(item.get("uri"), str)
                or not isinstance(item.get("mime"), str)
            ):
                _refuse()
        else:
            _refuse()


def _project_message(raw, seen, hidden):
    if (
        not isinstance(raw, dict)
        or not _message_id(ident := raw.get("id"))
        or ident in seen
        or not isinstance(kind := raw.get("type"), str)
        or kind not in _MESSAGE_FIELDS
        or not raw.keys() <= _BASE_MESSAGE | _MESSAGE_FIELDS[kind]
        or not isinstance(times := raw.get("time"), dict)
        or not _timestamp(times.get("created"))
    ):
        _refuse()
    seen.add(ident)
    if kind == "user":
        for key, fields in _USER_ATTACHMENTS.items():
            if key in raw and (
                not isinstance(items := raw[key], list)
                or len(items) > MAX_PARTS
                or not all(
                    isinstance(item, dict) and item.keys() <= fields for item in items
                )
            ):
                _refuse()
        text = raw.get("text")
    elif kind == "assistant":
        if not isinstance(parts := raw.get("content"), list) or len(parts) > MAX_PARTS:
            _refuse()
        text_parts = []
        for part in parts:
            if (
                not isinstance(part, dict)
                or not isinstance(part_kind := part.get("type"), str)
                or part_kind not in _ASSISTANT_FIELDS
                or not part.keys() <= _ASSISTANT_FIELDS[part_kind]
            ):
                _refuse()
            if part_kind == "tool":
                _check_tool(part)
            elif not isinstance(part_text := part.get("text"), str):
                _refuse()
            elif part_kind == "text":
                text_parts.append(part_text)
        text = "\n\n".join(text_parts)
    else:
        return None
    text, clipped = _plain(text, MAX_TEXT_BYTES, hidden, multiline=True)
    if any(secret in ident for secret in hidden):
        _refuse()
    if not text.strip():
        return None
    return {"id": ident, "role": kind, "text": text, "truncated": clipped}


def _conversation(request, origin, directory, ident, hidden):
    if not _session_id(ident) or any(secret in ident for secret in hidden):
        _refuse()
    remaining = MAX_NATIVE_BYTES

    def fetch(path):
        nonlocal remaining
        limit = min(MAX_REPLY_BYTES, remaining)
        if limit <= 0:
            _refuse()
        payload = request(path, limit)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
            _refuse()
        remaining -= len(payload)
        return _json(payload)

    route = "/api/session/" + ident
    messages = route + "/message"
    before = _snapshot(fetch, origin, directory, route, ident)
    _require_unmoved(fetch, messages)
    title, _ = _plain(before[-1].get("title", ident), MAX_TITLE_BYTES, hidden)
    rows, seen, cursors = [], set(), set()
    cursor = None
    for _ in range(MAX_PAGES):
        query = {"limit": PAGE_SIZE}
        query.update({"order": "desc"} if cursor is None else {"cursor": cursor})
        items, cursor = _page(fetch(messages + "?" + urlencode(query)), PAGE_SIZE)
        for raw in items:
            row = _project_message(raw, seen, hidden)
            if row is not None:
                # List rows do not echo the session ID. Cross-check each row
                # that will be disclosed via the native session+message route.
                if _data(fetch(messages + "/" + row["id"])) != raw:
                    _refuse()
                rows.append(row)
        if cursor is None:
            break
        if cursor in cursors:
            _refuse()
        cursors.add(cursor)
    _require_unmoved(fetch, messages)
    if _snapshot(fetch, origin, directory, route, ident) != before:
        _refuse()
    return {
        "sessionID": ident,
        "title": title.strip() or ident,
        "messages": rows[::-1],  # Native's descending walk, presented oldest first.
        "hasEarlier": cursor is not None,
    }


def _request(port, authorization, path, budget):
    # Direct HTTPConnection cannot follow a redirect or use ambient proxies.
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
            if (
                response.status != 200
                or response.headers.get_content_type() != "application/json"
                or len(response.headers.get_all("Content-Type", ())) != 1
                or "Transfer-Encoding" in response.headers
                or response.headers.get_all("Content-Encoding", [])
                not in ([], ["identity"])
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


def _worker(url, directory, ident):
    # Bound total execution, including a peer trickling HTTP headers or body.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.setitimer(signal.ITIMER_REAL, WORKER_TIMEOUT)
    origin, port = _endpoint(url)
    if os.getcwd() != _directory(directory):
        _refuse()
    password = os.environ.pop("OPENCODE_PASSWORD", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password) is None:
        _refuse()
    token = base64.b64encode(("opencode:" + password).encode("ascii")).decode("ascii")
    value = _conversation(
        lambda path, limit: _request(port, "Basic " + token, path, limit),
        origin,
        directory,
        ident,
        (password, token),
    )
    output = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if not 0 < len(output) <= MAX_OUTPUT_BYTES:
        _refuse()
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        _refuse()
    return remaining


def _read_bridge(command, deadline):
    _remaining(deadline)
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
            while select.select([stream], [], [], _remaining(deadline))[0]:
                chunk = os.read(
                    stream.fileno(), min(65536, MAX_OUTPUT_BYTES + 1 - len(payload))
                )
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_OUTPUT_BYTES:
                    _refuse()
            else:
                _refuse()
        if not payload or process.wait(timeout=_remaining(deadline)) != 0:
            _refuse()
        return bytes(payload)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def _display_value(payload, ident, hidden):
    # Treat worker stdout as input, not a way to forward arbitrary native JSON.
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_OUTPUT_BYTES:
        _refuse()
    value = _json(payload)
    if (
        not isinstance(value, dict)
        or set(value) != {"sessionID", "title", "hasEarlier", "messages"}
        or value["sessionID"] != ident
        or any(secret in ident for secret in hidden)
        or type(value["hasEarlier"]) is not bool
        or not isinstance(messages := value["messages"], list)
        or len(messages) > PAGE_SIZE * MAX_PAGES
    ):
        _refuse()
    title, clipped = _plain(value["title"], MAX_TITLE_BYTES, hidden)
    if clipped or title != value["title"] or not title or title != title.strip():
        _refuse()
    seen = set()
    for row in messages:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "role", "text", "truncated"}
            or not _message_id(message_id := row["id"])
            or message_id in seen
            or any(secret in message_id for secret in hidden)
            or row["role"] not in ("user", "assistant")
            or type(row["truncated"]) is not bool
        ):
            _refuse()
        text, clipped = _plain(row["text"], MAX_TEXT_BYTES, hidden, multiline=True)
        if clipped or text != row["text"] or not text.strip():
            _refuse()
        seen.add(message_id)
    return value


def conversation(box, data, connection, ident, *, deadline=None):
    """Read the selected session under an already-held, verified private lease.

    Return only sessionID/title/messages/hasEarlier, or raise ConversationError.
    Never give this callable a browser-supplied workspace, connection or path.
    The caller must enforce lease/admission validity through response delivery.
    """
    try:
        end = time.monotonic() + HOST_TIMEOUT
        if deadline is not None:
            _remaining(deadline)
            end = min(end, deadline)
        if (
            not isinstance(data, dict)
            or not isinstance(connection, dict)
            or not isinstance(workspace := data.get("id"), str)
            or not workspace
            or connection.get("workspace") != workspace
            or data.get("opencode_version") != NATIVE_VERSION
            or connection.get("version") != NATIVE_VERSION
            or not _session_id(ident)
        ):
            _refuse()
        url = connection.get("url")
        _endpoint(url)
        box.workspace_service.validate_endpoint(connection)
        password = connection["password"]
        token = base64.b64encode(("opencode:" + password).encode("ascii")).decode(
            "ascii"
        )
        hidden = (password, token)
        if any(secret in ident for secret in hidden):
            _refuse()
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
                ident,
            ],
        )
        value = _display_value(_read_bridge(command, end), ident, hidden)
        _remaining(end)
        return value
    except ConversationError:
        raise
    except (
        box.BoxError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        _refuse()


if __name__ == "__main__":
    try:
        _worker(*sys.argv[1:])
    except (
        OSError,
        ValueError,
        TypeError,
        HTTPException,
        RecursionError,
        OverflowError,
    ):
        # No raw stdout/stderr failure or partial native body crosses the pipe.
        sys.exit(1)
