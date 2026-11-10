"""Native V2 persistent-session shell client, run only inside the workspace.

Standard-library HTTP/WebSocket transport. Service credentials arrive in the
client environment only and are cleared before shell creation (which uses the
server's environment). Ctrl-] detaches without removing the persistent terminal.

A normal WebSocket 1000 close after attachment/replay ends the attachment, not
necessarily the shell. A known shell exit retains its status. Bare EOF requires
completed replay, a known exit and a complete WebSocket message boundary.
Received terminal ends/errors take priority over a same-cycle local detach;
without a known terminal end, an explicit local detach remains successful.
"""

import base64
import hashlib
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, ProxyHandler

MAX_FRAME = 16 * 1024 * 1024


class WebSocket:
    def __init__(self, address, path, authorization):
        self.socket = socket.create_connection(
            (address.hostname, address.port), timeout=10
        )
        self.buffer = bytearray()
        self.fragment = bytearray()
        self.fragment_type = None
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = f"GET {path} HTTP/1.1\r\nHost: {address.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: {authorization}\r\n\r\n"
            self.socket.sendall(request.encode())
            while b"\r\n\r\n" not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise ValueError("Incomplete terminal WebSocket handshake.")
                self.buffer.extend(chunk)
                if len(self.buffer) > 65536:
                    raise ValueError("Invalid terminal WebSocket handshake.")
            headers, extra = bytes(self.buffer).split(b"\r\n\r\n", 1)
            expected = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
                    usedforsecurity=False,
                ).digest()
            ).decode()
            values = {
                line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
                for line in headers.split(b"\r\n")[1:]
                if b":" in line
            }
            status = headers.split(b"\r\n", 1)[0].split(b" ")
            if (
                len(status) < 2
                or status[1] != b"101"
                or values.get(b"sec-websocket-accept") != expected.encode()
                or values.get(b"upgrade", b"").lower() != b"websocket"
            ):
                raise ValueError("Persistent terminal connection was refused.")
            self.buffer = bytearray(extra)
            self.socket.settimeout(None)
        except BaseException:
            self.socket.close()
            raise

    def send(self, payload, opcode=2):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        header += (
            bytes([0x80 | size])
            if size < 126
            else bytes([0x80 | 126]) + struct.pack("!H", size)
            if size < 65536
            else bytes([0x80 | 127]) + struct.pack("!Q", size)
        )
        self.socket.sendall(
            header
            + mask
            + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        )

    def frames(self):
        frames = []
        while len(self.buffer) >= 2:
            first, second = self.buffer[:2]
            length = second & 127
            header = 2 + (2 if length == 126 else 8 if length == 127 else 0)
            if len(self.buffer) < header:
                break
            if length >= 126:
                length = struct.unpack(
                    "!H" if length == 126 else "!Q", self.buffer[2:header]
                )[0]
            if length > MAX_FRAME or first & 0x70 or second & 0x80:
                raise ValueError("Invalid terminal WebSocket frame.")
            if len(self.buffer) < header + length:
                break
            payload = bytes(self.buffer[header : header + length])
            del self.buffer[: header + length]
            opcode = first & 15
            if opcode >= 8:
                if not first & 0x80 or length > 125:
                    raise ValueError("Invalid terminal WebSocket control frame.")
                if opcode == 9:
                    self.send(payload, 10)
                elif opcode == 8:
                    frames.append((8, payload))
                elif opcode != 10:
                    raise ValueError("Unsupported terminal WebSocket control frame.")
                continue
            if opcode in (1, 2):
                if self.fragment_type is not None:
                    raise ValueError("Invalid terminal WebSocket fragmentation.")
                self.fragment_type = opcode
            elif opcode != 0 or self.fragment_type is None:
                raise ValueError("Invalid terminal WebSocket data frame.")
            self.fragment.extend(payload)
            if len(self.fragment) > MAX_FRAME:
                raise ValueError("Oversized terminal WebSocket message.")
            if first & 0x80:
                frames.append((self.fragment_type, bytes(self.fragment)))
                self.fragment.clear()
                self.fragment_type = None
        return frames

    def close(self):
        try:
            self.send(struct.pack("!H", 1000), 8)
        except OSError:
            pass
        self.socket.close()


def output(content):
    view = memoryview(content)
    while view:
        written = os.write(sys.stdout.fileno(), view)
        if not written:
            raise OSError("Terminal output closed.")
        view = view[written:]


def terminal_size():
    try:
        value = os.get_terminal_size(sys.stdout.fileno())
        return min(65535, value.columns or 80), min(65535, value.lines or 24)
    except OSError:
        return 80, 24


def choose_terminal(request, session, cwd):
    terminals = request("GET", "/api/experimental/session/" + session + "/terminal")[
        "data"
    ]
    running = [
        terminal for terminal in terminals if terminal.get("status") == "running"
    ]
    if running:
        current = request(
            "GET", "/api/experimental/session/" + session + "/terminal/read"
        )["data"]
        return next(
            (
                terminal
                for terminal in running
                if current and terminal["id"] == current["ptyID"]
            ),
            running[-1],
        )
    cols, rows = terminal_size()
    return request(
        "POST",
        "/api/experimental/session/" + session + "/terminal",
        {
            "args": [],
            "cwd": cwd,
            "title": "Terminal",
            "env": {},
            "size": {"cols": cols, "rows": rows},
        },
    )["data"]


def run(address, session, cwd):
    parsed = urlparse(address)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"ses[A-Za-z0-9_-]{1,200}", session)
    ):
        raise ValueError("Invalid private session terminal endpoint.")
    authorization = (
        "Basic "
        + base64.b64encode(
            ("opencode:" + os.environ.pop("OPENCODE_PASSWORD")).encode()
        ).decode()
    )
    opener = build_opener(ProxyHandler({}))

    def request(method, path, data=None):
        req = Request(
            address + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
            },
        )
        with opener.open(req, timeout=15) as response:
            return json.load(response)

    info = request("GET", "/api/info")
    if info.get("capabilities", {}).get("persistentPty") is False:
        raise ValueError(
            "This OpenCode service does not support session shell terminals."
        )
    terminal = choose_terminal(request, session, cwd)
    ident = quote(terminal["id"], safe="")
    snapshot = request(
        "GET", "/api/experimental/persistent-pty/" + ident + "/snapshot"
    )["data"]
    attachment = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    query = urlencode(
        {
            "cursor": snapshot["info"]["output"]["tail"],
            "role": "controller",
            "attachment_id": attachment,
            "takeover": "true",
            "input_protocol": "1",
        }
    )
    websocket = WebSocket(
        parsed,
        "/api/experimental/persistent-pty/" + ident + "/connect?" + query,
        authorization,
    )
    previous = None
    resized = True
    attached = False
    ready = False
    controlling = False
    code = None

    def resize(*_):
        nonlocal resized
        resized = True

    handler = None
    try:
        previous = (
            termios.tcgetattr(sys.stdin.fileno())
            if os.isatty(sys.stdin.fileno())
            else None
        )
        handler = signal.signal(signal.SIGWINCH, resize)
        if previous:
            tty.setraw(sys.stdin.fileno())
        output(base64.b64decode(snapshot["checkpoint"]))
        deadline = time.monotonic() + 30
        readable = []
        while True:
            if not ready and time.monotonic() >= deadline:
                raise ValueError("Persistent session terminal replay timed out.")
            # Parse the bounded batch from the preceding recv before handling
            # co-ready stdin. Do not drain more socket data or await a future
            # frame before honoring a local detach.
            for opcode, payload in websocket.frames():
                if opcode == 8:
                    if len(payload) < 2 or struct.unpack("!H", payload[:2])[0] != 1000:
                        raise ValueError(
                            "Persistent session terminal became unavailable."
                        )
                    if not ready:
                        raise ValueError(
                            "Persistent session terminal connection ended unexpectedly."
                        )
                    # Native 2.2.6 queues 1000 from attachment onEnd, not only
                    # after an exited event. Preserve this normal attachment end.
                    return code if code is not None else 0
                if opcode == 2:
                    output(payload)
                    continue
                event = json.loads(payload)
                if event.get("type") == "attached":
                    if event.get("inputProtocol") != 1:
                        raise ValueError(
                            "Unsupported persistent terminal input protocol."
                        )
                    attached = True
                    controlling = event.get("role") == "controller"
                elif event.get("type") == "controller_changed":
                    controlling = event.get("attachmentID") == attachment
                    if controlling:
                        resized = True
                elif event.get("type") == "replay_complete":
                    if not attached:
                        raise ValueError("Invalid persistent terminal replay sequence.")
                    ready = True
                elif event.get("type") == "resized":
                    output(base64.b64decode(event["checkpoint"]))
                elif event.get("type") == "exited":
                    code = event.get("exitCode")
                    if code is not None and (
                        type(code) is not int or not 0 <= code <= 255
                    ):
                        raise ValueError("Invalid persistent terminal exit status.")
            cols, rows = terminal_size()
            if sys.stdin in readable:
                content = os.read(sys.stdin.fileno(), 4096)
                prefix, detach, _ = content.partition(b"\x1d")
                if (not content or detach) and code is not None:
                    # A received shell exit must not be rewritten as detach 0.
                    return code
                if prefix:
                    websocket.send(struct.pack("!BHH", 1, cols, rows) + prefix)
                if not content or detach:
                    # Detach, preserving the shell and its state in OpenCode.
                    return 0
            if ready and controlling and resized:
                websocket.send(struct.pack("!BHH", 0, cols, rows))
                resized = False
            readers = [websocket.socket] + ([sys.stdin] if ready else [])
            readable, _, _ = select.select(readers, [], [], 0.2)
            if websocket.socket in readable:
                chunk = websocket.socket.recv(65536)
                if not chunk:
                    if (
                        ready
                        and code is not None
                        and not websocket.buffer
                        and websocket.fragment_type is None
                    ):
                        return code
                    raise ValueError(
                        "Persistent session terminal connection ended unexpectedly."
                    )
                websocket.buffer.extend(chunk)
    finally:
        try:
            websocket.close()
        finally:
            try:
                if handler is not None:
                    signal.signal(signal.SIGWINCH, handler)
            finally:
                if previous:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)


if __name__ == "__main__":
    try:
        sys.exit(run(*sys.argv[1:]))
    except (OSError, ValueError, KeyError):
        print(
            "oc-box: Native session terminal connection failed; private response data was not logged.",
            file=sys.stderr,
        )
        sys.exit(1)
