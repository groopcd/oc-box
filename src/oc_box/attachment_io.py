"""Providerless container-side image admission through the existing private API.

The host sends image bytes only on stdin. Stdout contains a fixed result and,
on verified admission, a native message ID. No host paths or native errors are
printed. This helper never discovers/starts a service or follows redirects.
"""

import base64
from http.client import HTTPConnection
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

MAX_BODY = 12 * 1024 * 1024
MAX_REPLY = 12 * 1024 * 1024
BUDGET = 30
# Only direct admission depends on demonstrated scheduler behavior. A matching
# OpenAPI declaration is not evidence that a new runtime honors resume:false.
DIRECT_RELEASES = frozenset(("2.2.6", "2.2.7", "2.3.0", "2.3.2", "2.4.0", "2.4.1"))
CATEGORIES = frozenset(
    (
        "invalid-request",
        "session-unavailable",
        "session-scope",
        "session-revert",
        "session-archived",
        "private-auth",
        "native-version",
        "delivery-unverified",
    )
)


class Refused(ValueError):
    pass


def canonical_v2(version):
    return (
        isinstance(version, str)
        and len(version) <= 64
        and re.fullmatch(r"2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)
        is not None
    )


def target(request, session, directory):
    response = request("GET", "/api/session/" + session)
    info = response.get("data") if isinstance(response, dict) else None
    if not isinstance(info, dict) or info.get("id") != session:
        raise Refused("session-unavailable")
    if (
        not isinstance(info.get("location"), dict)
        or info["location"].get("directory") != directory
    ):
        raise Refused("session-scope")
    if info.get("revert") is not None:
        raise Refused("session-revert")
    if (
        not isinstance(info.get("time"), dict)
        or info["time"].get("archived") is not None
    ):
        raise Refused("session-archived")


def deliver(request, value):
    """Validate a fixed operation; only `send` can admit one native input."""
    if not isinstance(value, dict) or set(value) - {
        "operation",
        "session",
        "directory",
        "version",
        "prompt",
    }:
        raise Refused("invalid-request")
    session, directory, version = (
        value.get(k) for k in ("session", "directory", "version")
    )
    operation = value.get("operation")
    if (
        not isinstance(session, str)
        or not re.fullmatch(r"ses[A-Za-z0-9_-]{1,125}", session)
        or not isinstance(directory, str)
        or not directory.startswith("/")
        or "\0" in directory
        or len(directory.encode()) > 4096
        or not canonical_v2(version)
        or operation not in ("check", "send", "broker-check")
        or (operation != "broker-check" and version not in DIRECT_RELEASES)
        or (operation != "send" and "prompt" in value)
    ):
        raise Refused("invalid-request")
    info = request("GET", "/api/info")
    # Unlike session routes, server.info returns bare ServerInfo on the
    # supported native releases, not a {"data": ...} envelope.
    if not isinstance(info, dict) or info.get("version") != version:
        raise Refused("native-version")
    target(request, session, directory)
    if operation != "send":
        return {"ok": True}
    prompt = value.get("prompt")
    if not isinstance(prompt, dict) or set(prompt) != {
        "id",
        "text",
        "files",
        "delivery",
        "resume",
    }:
        raise Refused("invalid-request")
    ident = prompt.get("id")
    files = prompt.get("files")
    if (
        not isinstance(ident, str)
        or not re.fullmatch(r"msg_[A-Za-z0-9_-]{1,125}", ident)
        or not isinstance(prompt.get("text"), str)
        or len(prompt["text"].encode()) > 16384
        or prompt["delivery"] != "queue"
        or type(prompt["resume"]) is not bool
        or not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
        or set(files[0]) != {"uri", "name", "source"}
        or files[0]["source"] != {"type": "inline"}
        or not isinstance(files[0]["uri"], str)
        or not re.fullmatch(r"host-image\.(png|jpeg|gif|webp)", str(files[0]["name"]))
    ):
        raise Refused("invalid-request")
    uri = files[0]["uri"]
    prefix, separator, encoded = uri.partition(";base64,")
    if not separator or prefix not in {
        "data:image/png",
        "data:image/jpeg",
        "data:image/gif",
        "data:image/webp",
    }:
        raise Refused("invalid-request")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        raise Refused("invalid-request") from None
    if not 0 < len(content) <= 8 * 1024 * 1024:
        raise Refused("invalid-request")
    # Native prompt admission can commit an existing staged revert. Recheck
    # immediately before mutation; the API has no compare-and-swap precondition.
    target(request, session, directory)
    try:
        reply = request("POST", "/api/session/" + session + "/prompt", prompt)
        admitted = reply.get("data") if isinstance(reply, dict) else None
        if (
            not isinstance(admitted, dict)
            or admitted.get("id") != ident
            or admitted.get("sessionID") != session
            or admitted.get("type") != "user"
            or admitted.get("delivery") != "queue"
            or not isinstance(admitted.get("payload"), dict)
            or not isinstance(admitted["payload"].get("files"), list)
            or len(admitted["payload"]["files"]) != 1
        ):
            raise Refused("delivery-unverified")
    except Exception:
        # A timeout/malformed acknowledgement can follow successful admission.
        # Never retry automatically or claim that nothing reached the session.
        raise Refused("delivery-unverified") from None
    return {"ok": True, "message": ident}


def connection(address, password):
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
        or not password
    ):
        raise Refused("invalid-request")
    auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
    deadline = time.monotonic() + BUDGET

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def request(method, path, value=None):
        stream = HTTPConnection(parsed.hostname, parsed.port, timeout=remaining())
        try:
            stream.connect()
            sock = stream.sock
            sock.settimeout(remaining())
            body = json.dumps(value).encode() if value is not None else None
            stream.request(
                method,
                path,
                body=body,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
            sock.settimeout(remaining())
            with stream.getresponse() as response:
                if response.status in (401, 403):
                    raise Refused("private-auth")
                if response.status != 200:
                    raise Refused("session-unavailable")
                chunks, size = [], 0
                # HTTP/1.0 peers may close the socket as soon as the declared
                # body is consumed. Do not touch that closed socket on another
                # iteration; HTTPResponse still owns framing/completeness.
                while not response.isclosed():
                    sock.settimeout(remaining())
                    chunk = response.read1(min(65536, MAX_REPLY + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_REPLY:
                        raise Refused("session-unavailable")
                remaining()
                return json.loads(b"".join(chunks))
        finally:
            stream.close()

    return request


def main():
    sending = False
    try:
        raw = sys.stdin.buffer.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise Refused("invalid-request")
        value = json.loads(raw)
        sending = isinstance(value, dict) and value.get("operation") == "send"
        request = connection(sys.argv[1], os.environ.pop("OPENCODE_PASSWORD"))
        result = deliver(request, value)
    except Exception as error:
        category = (
            str(error)
            if isinstance(error, Refused) and str(error) in CATEGORIES
            else "delivery-unverified"
            if sending
            else "session-unavailable"
        )
        print(json.dumps({"ok": False, "category": category}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
