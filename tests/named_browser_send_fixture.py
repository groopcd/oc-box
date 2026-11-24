"""Single-turn, no-tool provider and passive Send observations for the TLS smoke."""

from contextlib import contextmanager
import json
import re
import socket
from socketserver import BaseRequestHandler, TCPServer
from threading import Thread, local
from time import monotonic, sleep
from unittest.mock import patch

from oc_box import web_studio_conversation, web_studio_send


SEND_TEXT = "Synthetic Studio browser Send: café <literal>"
REPLY_TEXT = "Synthetic browser Send received."
MODEL = "gpt-studio-browser-fixture"
_KEY = "synthetic-not-a-credential"
_ROUTE = "/v1/chat/completions"
_BODY_LIMIT = 128 * 1024
_HEAD_LIMIT = 16 * 1024
_REFUSAL = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class FixtureRefusal(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise FixtureRefusal(message)


def _read_request(socket_, authority, end):
    def recv(size):
        wait = end - monotonic()
        require(wait > 0, "Synthetic provider request deadline expired.")
        socket_.settimeout(wait)
        data = socket_.recv(size)
        require(data, "Synthetic provider request ended before its declared length.")
        return data

    pending = bytearray()
    while (boundary := pending.find(b"\r\n\r\n")) < 0:
        require(len(pending) < _HEAD_LIMIT, "Synthetic provider headers exceed limit.")
        pending.extend(recv(min(4096, _HEAD_LIMIT - len(pending))))
    start, *lines = bytes(pending[:boundary]).split(b"\r\n")
    require(
        start == f"POST {_ROUTE} HTTP/1.1".encode("ascii") and len(lines) <= 48,
        "Synthetic provider received an unexpected method or route.",
    )
    fields = {}
    for line in lines:
        key, colon, value = line.partition(b":")
        key = key.lower()
        require(
            colon and re.fullmatch(rb"[a-z0-9-]{1,64}", key) and key not in fields,
            "Synthetic provider received ambiguous headers.",
        )
        fields[key] = value.strip(b" ")
    length = fields.get(b"content-length", b"")
    require(
        fields.get(b"host") == authority.encode("ascii")
        and fields.get(b"authorization") == ("Bearer " + _KEY).encode("ascii")
        and fields.get(b"content-type") == b"application/json"
        and not {b"transfer-encoding", b"content-encoding", b"upgrade", b"expect"}
        & fields.keys()
        and re.fullmatch(rb"[1-9][0-9]{0,5}", length)
        and 0 < (size := int(length)) <= _BODY_LIMIT,
        "Synthetic provider request framing or scope changed.",
    )
    body = pending[boundary + 4 :]
    require(len(body) <= size, "Synthetic provider received surplus bytes.")
    while len(body) < size:
        body.extend(recv(min(4096, size - len(body))))
    return web_studio_conversation._json(bytes(body))


def _user_text(message):
    text = message.get("content")
    if type(text) is str:
        return text
    require(
        type(text) is list
        and all(
            type(part) is dict
            and part.get("type") == "text"
            and type(part.get("text")) is str
            for part in text
        ),
        "Synthetic provider received non-text user input.",
    )
    return "".join(part["text"] for part in text)


def _validate_prompt(value, seed_text):
    require(
        type(value) is dict
        and value.get("model") == MODEL
        and value.get("stream") is True,
        "Synthetic provider received an unexpected model or stream profile.",
    )
    # Native 2.4.1 may advertise built-in schemas even for a no-tools model
    # with a tool-deny policy. Accept only the bounded declarations; this
    # provider cannot issue a tool call and refuses any incoming tool result.
    tools = value.get("tools", [])
    require(
        type(tools) is list
        and len(tools) <= 64
        and all(
            type(item) is dict and item.get("type") == "function" for item in tools
        ),
        "Synthetic provider received an unexpected tool declaration.",
    )
    require(
        value.get("tool_choice") in (None, "none", "auto"),
        "Synthetic provider received a specific tool choice.",
    )
    messages = value.get("messages")
    require(
        type(messages) is list
        and 1 <= len(messages) <= 16
        and all(
            type(msg) is dict and msg.get("role") in ("system", "user")
            for msg in messages
        )
        and messages[-1].get("role") == "user",
        "Synthetic provider received an unexpected turn shape.",
    )
    texts = [_user_text(msg) for msg in messages if msg["role"] == "user"]
    require(
        texts == [seed_text, SEND_TEXT],
        "Synthetic provider did not receive exactly the selected fixture's two user messages.",
    )


def _reply_wire():
    chunks = []
    for delta, finish in (
        ({"role": "assistant", "content": REPLY_TEXT}, None),
        ({}, "stop"),
    ):
        chunks.append(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-studio-browser-fixture",
                    "object": "chat.completion.chunk",
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
                separators=(",", ":"),
            )
            + "\n\n"
        )
    payload = ("".join(chunks) + "data: [DONE]\n\n").encode("ascii")
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        + payload
    )


class Provider(TCPServer):
    """Fixed local provider: one accepted request, no tools or outbound work."""

    request_queue_size = 2

    def __init__(self, ident):
        # Guard here as well as the smoke CLI, before binding any socket.
        from smoke_named_pake_browser import loopback_namespace

        loopback_namespace()
        self.seed_text = "SYNTHETIC_" + ident
        self.requests = self.completed = 0
        self.rejection = None
        super().__init__(("127.0.0.1", 0), ProviderRequest)

    @property
    def authority(self):
        return f"127.0.0.1:{self.server_address[1]}"

    def config(self, base):
        return base | {
            "model": "fixture/" + MODEL,
            "warming": False,
            "lsp": False,
            "providers": {
                "fixture": {
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {
                        "baseURL": f"http://{self.authority}/v1",
                        "apiKey": _KEY,
                    },
                    "models": {
                        MODEL: {
                            "name": "Synthetic Studio browser Send",
                            "limit": {"context": 32000, "output": 2000},
                            "capabilities": {
                                "tools": False,
                                "input": ["text"],
                                "output": ["text"],
                            },
                        }
                    },
                }
            },
            "policies": base["policies"]
            + [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "fixture", "effect": "allow"},
                {"action": "tool.use", "resource": "*", "effect": "deny"},
            ],
        }

    def handle_error(self, *_):
        self.rejection = "Synthetic provider handler failed."

    @contextmanager
    def serving(self):
        thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield self
        finally:
            self.shutdown()
            self.server_close()
            thread.join(timeout=3)
            require(not thread.is_alive(), "Synthetic provider did not reap.")
        require(
            self.requests == self.completed == 1 and self.rejection is None,
            "The synthetic provider did not see exactly one completed request.",
        )


class ProviderRequest(BaseRequestHandler):
    def handle(self):
        provider = self.server
        provider.requests += 1
        try:
            require(
                provider.requests == 1, "Synthetic provider received a second request."
            )
            _validate_prompt(
                _read_request(self.request, provider.authority, monotonic() + 3),
                provider.seed_text,
            )
            self.request.settimeout(1)
            self.request.sendall(_reply_wire())
            provider.completed += 1
        except FixtureRefusal as error:
            provider.rejection = str(error)  # Only fixed require() messages.
        except Exception:
            provider.rejection = "Synthetic provider I/O or JSON failed."
        if provider.rejection is not None:
            try:
                self.request.settimeout(1)
                self.request.sendall(_REFUSAL)
            except OSError:
                pass


class SendProof:
    """Observe the actual StudioSend call, bytes, acknowledged ID and exit."""

    def __init__(self, provider, ident):
        self.provider, self.ident = provider, ident
        self.actions = self.posts = 0
        self.ack_id = None
        self.queued = False

    @contextmanager
    def observe(self, identity, browser):
        execute = web_studio_send.StudioSend.execute
        sendall = socket.socket.sendall
        acknowledged = web_studio_send._acknowledged
        scoped = local()
        head = f"POST /api/session/{self.ident}/prompt HTTP/1.1".encode("ascii")
        expected = json.dumps(
            {"text": SEND_TEXT, "delivery": "queue", "resume": True},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        def action(operation, box, *, native_password, text):
            self.actions += 1
            scope = operation._scope
            require(
                self.actions == 1
                and text == SEND_TEXT
                and scope.browser is browser
                and scope.workspace_id == identity.workspace_id
                and scope.session_id == self.ident
                and scope.owner_generation == identity.generation
                and scope.endpoint == identity.endpoint
                and scope.container_id == identity.container_id,
                "The browser built an unexpected Studio Send operation.",
            )
            scoped.operation = operation
            try:
                result = execute(
                    operation, box, native_password=native_password, text=text
                )
                self.queued = (
                    result is web_studio_send.SendResult.QUEUED
                    and operation.cleanup_confirmed
                )
                return result
            finally:
                del scoped.operation

        def actual_wire(peer, data, *args, **kwargs):
            operation = getattr(scoped, "operation", None)
            if (
                operation is not None
                and peer is operation._upstream
                and data.startswith(b"POST ")
            ):
                self.posts += 1
                header, separator, body = data.partition(b"\r\n\r\n")
                require(
                    self.posts == 1
                    and separator
                    and header.split(b"\r\n", 1)[0] == head
                    and body == expected,
                    "Native Send did not write the exact production resume:true body.",
                )
            return sendall(peer, data, *args, **kwargs)

        def inbox_ack(value, ident, text):
            result = acknowledged(value, ident, text)
            if getattr(scoped, "operation", None) is not None:
                require(
                    self.ack_id is None and ident == self.ident and text == SEND_TEXT,
                    "The native Send acknowledgment was not for this action.",
                )
                self.ack_id = value["data"]["id"]
            return result

        with (
            patch.object(web_studio_send.StudioSend, "execute", action),
            patch.object(socket.socket, "sendall", actual_wire),
            patch.object(web_studio_send, "_acknowledged", inbox_ack),
        ):
            yield

    def verify_history(self, native, *, wait=True):
        require(
            self.actions == self.posts == 1 and self.queued and self.ack_id is not None,
            "The exact native queue acknowledgment and relay drain were not observed.",
        )
        route = "/api/session/" + self.ident
        end = monotonic() + (10 if wait else 4)
        while monotonic() < end:
            require(self.provider.rejection is None, self.provider.rejection)
            value = native(
                "GET",
                "/api/experimental/session/" + self.ident + "/export",
                deadline=end,
                max_body=65536,
            )["data"]
            messages = value["messages"]
            if any(
                row["type"] == "idle" and row.get("outcome") == "succeeded"
                for row in messages
            ):
                break
            require(wait, "Reopened native history did not contain the completed turn.")
            sleep(0.05)
        else:
            raise FixtureRefusal(
                "Native fixture did not persist a completed synthetic turn."
            )

        users = [row for row in messages if row["type"] == "user"]
        assistants = [row for row in messages if row["type"] == "assistant"]
        require(
            value["info"]["id"] == self.ident
            and len(users) == 2
            and users[0]["text"] == "SYNTHETIC_" + self.ident
            and users[1]["text"] == SEND_TEXT
            and users[1]["id"] == self.ack_id
            and len(assistants) == 1
            and assistants[0]["model"]["id"] == MODEL
            and assistants[0]["model"]["providerID"] == "fixture"
            and all(part["type"] == "text" for part in assistants[0]["content"])
            and "".join(part["text"] for part in assistants[0]["content"]) == REPLY_TEXT
            and not any(row["type"] == "shell" for row in messages),
            "The native export did not retain the exact acknowledged, tool-free turn.",
        )
        require(
            native("GET", route + "/inbox", deadline=end) == {"data": []}
            and native("GET", "/api/session/active", deadline=end) == {"data": {}}
            and native("GET", "/api/credential", deadline=end) == {"data": []}
            and self.provider.requests == self.provider.completed == 1,
            "The native turn did not finish in scoped, provider-only fixture state.",
        )
