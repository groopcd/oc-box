"""Private canned provider for synthetic native V2 host-sharing fixtures.

This is not a model service or an acceptance runner. Arming is host-memory-only:
put the token returned by ``plan`` in a native user prompt, let native OpenCode
execute the advertised tool, then poll ``take``. Payloads never leave memory via
logs, files, or a control endpoint. Start with fresh synthetic native state.
"""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import threading


MAX_BODY = 32 * 1024 * 1024
MAX_ARGUMENTS = 1024 * 1024
MAX_RETAINED = 64 * 1024 * 1024
MAX_REQUESTS = 200
MAX_CASE_REQUESTS = 4
MAX_PLANS = 200
MAX_MESSAGES = 2048
TOKEN = re.compile(r"(?<![A-Za-z0-9_])SHARING_CASE_[0-9a-f]{32}(?![A-Za-z0-9_])")
COMPLETE = "SYNTHETIC_SHARING_COMPLETE"
ERROR = b'{"error":{"message":"Synthetic sharing provider refused request.","type":"fixture_error"}}'
ERROR_CATEGORIES = (
    "protocol",
    "route",
    "framing",
    "body-size",
    "json",
    "request-shape",
    "messages",
    "token",
    "request-budget",
    "case-budget",
    "initial-history",
    "tool-unavailable",
    "result-phase",
    "result-shape",
    "call-order",
    "result-size",
    "response-write",
    "transport",
    "parser",
)
PLAN_PHASES = ("planned", "issued", "finishing", "finished", "taken")


class _Refusal(Exception):
    """Fixed HTTP status and allowlisted category; no offending values."""

    def __init__(self, status=400, category="protocol"):
        super().__init__()
        self.status = status
        self.category = category if category in ERROR_CATEGORIES else "protocol"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _text(part) for part in content if isinstance(part, (str, dict))
        )
    if isinstance(content, dict):
        value = content.get("text")
        return value if isinstance(value, str) else _text(content.get("content"))
    return ""


def _images(value):
    """Only structured image URLs, never URLs embedded in a text result."""
    found, pending = [], [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            for key in ("url", "image_url"):
                url = item.get(key)
                if isinstance(url, str) and url.startswith("data:image/"):
                    if url not in found:
                        found.append(url)
            pending.extend(reversed(list(item.values())))
    return found


class Provider:
    """Loopback-only HTTP fixture with bounded, single-use in-memory plans.

    ``requests`` counts POST attempts (not health/catalog reads); ``errors``
    counts failed HTTP operations once, including parser and GET refusals.
    ``diagnostics()`` returns independent fixed-label count dictionaries only.
    ``take(token)`` returns None while pending or after consumption, otherwise
    {'text': str, 'images': list[str], 'tool_results': list[dict]}. Tool results
    are the original tool message dictionaries, including list/dict content.
    """

    def __init__(self):
        self.requests = self.errors = 0
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._plans = {}
        self._closed_plan_phases = None
        self._error_categories = {}
        self._retained = 0
        self._closed = False
        self._connections = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self._error_recorded = False
                super().setup()

            def log_message(self, *_):
                pass

            def log_error(self, *_):
                # The base handler consumes request-line timeouts itself.
                self._record_error("transport")

            def version_string(self):
                return "SyntheticSharingFixture"

            def _record_error(self, category):
                if not self._error_recorded:
                    self._error_recorded = True
                    outer._record_error(category)

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    self._record_error("transport")
                except Exception:
                    self._record_error("parser")

            def finish(self):
                try:
                    super().finish()
                except Exception:
                    self._record_error("transport")
                finally:
                    with outer._lock:
                        outer._connections.discard(self.connection)

            def send_error(self, code, message=None, explain=None, *, category=None):
                # BaseHTTPRequestHandler otherwise echoes malformed request
                # lines in HTML and can log headers/path/parser exceptions.
                if self._error_recorded:
                    return
                if category is None:
                    category = "route" if code == 501 else "parser"
                self._record_error(category)
                try:
                    self._send(code, ERROR, "application/json")
                except Exception:
                    pass  # A failed refusal write is still one refusal.

            def _send(self, status, content, kind):
                self.close_connection = True
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(content)
                    self.wfile.flush()
                except Exception:
                    self._record_error("response-write")
                    raise

            def do_GET(self):
                paths = (
                    "/health",
                    "/healthz",
                    "/v1/models",
                    "/v1/models/",
                    "/api/v1/models",
                    "/api/v1/models/",
                    "/api/v1/health",
                    "/api/v1/version",
                )
                if self.path not in paths:
                    self.send_error(404, category="route")
                    return
                # LEMONADE_BASE_URL may point here so optional discovery never
                # contacts a real host service. There are deliberately no models.
                self._send(200, b'{"data":[],"models":[]}', "application/json")

            def do_POST(self):
                try:
                    with outer._lock:
                        outer.requests += 1
                        if outer._closed or outer.requests > MAX_REQUESTS:
                            raise _Refusal(429, "request-budget")
                    if self.path != "/v1/chat/completions":
                        raise _Refusal(404, "route")
                    lengths = self.headers.get_all("Content-Length", [])
                    if (
                        len(lengths) != 1
                        or not lengths[0].isascii()
                        or not lengths[0].isdecimal()
                        or self.headers.get("Transfer-Encoding") is not None
                        or self.headers.get("Content-Encoding", "identity")
                        != "identity"
                    ):
                        raise _Refusal(category="framing")
                    try:
                        size = int(lengths[0])
                    except ValueError:
                        raise _Refusal(category="framing") from None
                    if not 0 < size <= MAX_BODY:
                        raise _Refusal(413, "body-size")
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise _Refusal(category="framing")
                    try:
                        body = json.loads(raw)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        raise _Refusal(category="json") from None
                    del raw
                    with outer._lock:
                        token, delta, reason, result = outer._reply(body)
                    payload = []
                    for part, finish in ((delta, None), ({}, reason)):
                        event = {
                            "id": "chatcmpl-sharing-fixture",
                            "object": "chat.completion.chunk",
                            "model": "share-model",
                            "choices": [
                                {"index": 0, "delta": part, "finish_reason": finish}
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                        payload.append(b"data: " + _encoded(event) + b"\n\n")
                    payload.append(b"data: [DONE]\n\n")
                    self._send(200, b"".join(payload), "text/event-stream")
                    # Completion is visible only after the finished SSE writes.
                    if result is not None:
                        with outer._lock:
                            plan = outer._plans.get(token)
                            if plan is not None and not outer._closed:
                                plan["result"] = result
                                plan["phase"] = "finished"
                except Exception as error:
                    status = error.status if isinstance(error, _Refusal) else 400
                    if isinstance(error, _Refusal):
                        category = error.category
                    elif isinstance(error, OSError):
                        category = "transport"
                    else:
                        category = "protocol"
                    self.send_error(status, category=category)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(3)
                with outer._lock:
                    outer._connections.add(connection)
                return connection, address

            def handle_error(self, *_):
                # Socket/parser failures must not print native payloads.
                outer._record_error("transport")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _record_error(self, category):
        category = category if category in ERROR_CATEGORIES else "protocol"
        with self._lock:
            self.errors += 1
            self._error_categories[category] = (
                self._error_categories.get(category, 0) + 1
            )

    def diagnostics(self):
        """Fresh host-memory-only counts; no plan keys, payloads, or HTTP data.

        Error categories include only observed labels; plan phases include all
        fixed phases, including zeros. Closing retains only final phase counts
        while clearing plans; HTTP counters remain live through shutdown.
        """
        with self._lock:
            phases = (
                self._plan_phase_counts()
                if self._closed_plan_phases is None
                else dict(self._closed_plan_phases)
            )
            return {
                "requests": self.requests,
                "errors": self.errors,
                "error_categories": dict(self._error_categories),
                "plan_phases": phases,
            }

    def _plan_phase_counts(self):
        """Caller holds _lock; retain only fixed phase labels and integers."""
        phases = dict.fromkeys(PLAN_PHASES, 0)
        for plan in self._plans.values():
            phases[plan["phase"]] += 1
        return phases

    def plan(self, action, arguments):
        """Arm a single execute/shell/read call; return SHARING_CASE_<32 hex>."""
        try:
            if action not in ("execute", "shell", "read") or not isinstance(
                arguments, dict
            ):
                raise ValueError()
            encoded = _encoded(arguments)
            if len(encoded) > MAX_ARGUMENTS:
                raise ValueError()
            arguments = json.loads(encoded)
        except Exception:
            raise ValueError("provider-plan") from None
        with self._lock:
            if self._closed:
                raise RuntimeError("provider-closed")
            if len(self._plans) >= MAX_PLANS:
                raise RuntimeError("provider-plan-bound")
            token = "SHARING_CASE_" + secrets.token_hex(16)
            while token in self._plans:
                token = "SHARING_CASE_" + secrets.token_hex(16)
            self._plans[token] = {
                "action": action,
                "arguments": arguments,
                "phase": "planned",
                "requests": 0,
                "call": "call_sharing_" + token[13:],
                "bytes": 0,
            }
            return token

    def take(self, token):
        """Consume a finished result once. Unknown tokens are fixed refusals."""
        with self._lock:
            if not isinstance(token, str) or token not in self._plans:
                raise ValueError("provider-token")
            plan = self._plans[token]
            if plan["phase"] != "finished":
                return None
            result = plan.pop("result")
            self._retained -= plan["bytes"]
            plan["bytes"] = 0
            plan["phase"] = "taken"
            plan["arguments"] = {}
            return result

    def _reply(self, body):
        """Validate protocol order under the provider lock; no network control."""
        if (
            not isinstance(body, dict)
            or body.get("stream") is not True
            or body.get("model") != "share-model"
        ):
            raise _Refusal(category="request-shape")
        messages = body.get("messages")
        if (
            not isinstance(messages, list)
            or not 0 < len(messages) <= MAX_MESSAGES
            or not all(isinstance(item, dict) for item in messages)
        ):
            raise _Refusal(category="messages")
        token, anchor = None, None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "user":
                continue
            try:
                text = _text(message.get("content"))
            except RecursionError:
                raise _Refusal(category="messages") from None
            if "SHARING_CASE_" not in text:
                continue
            tokens = set(TOKEN.findall(text))
            if len(tokens) != 1:
                raise _Refusal(category="token")
            (token,) = tokens
            anchor = index
            break
        if token not in self._plans:
            raise _Refusal(category="token")
        plan = self._plans[token]
        plan["requests"] += 1
        if plan["requests"] > MAX_CASE_REQUESTS:
            raise _Refusal(429, "case-budget")
        tail = messages[anchor + 1 :]
        results = [message for message in tail if message.get("role") == "tool"]
        calls = []
        for index, message in enumerate(tail):
            if message.get("role") != "assistant":
                continue
            advertised = message.get("tool_calls", [])
            if not isinstance(advertised, list) or not all(
                isinstance(call, dict) for call in advertised
            ):
                category = (
                    "initial-history" if plan["phase"] == "planned" else "call-order"
                )
                raise _Refusal(category=category)
            calls.extend((index, call) for call in advertised)
        if plan["phase"] == "planned":
            tools = body.get("tools")
            if results or calls:
                raise _Refusal(category="initial-history")
            if not isinstance(tools, list):
                raise _Refusal(category="tool-unavailable")
            try:
                names = {
                    item["function"].get("name")
                    for item in tools
                    if isinstance(item, dict)
                    and item.get("type") == "function"
                    and isinstance(item.get("function"), dict)
                }
            except TypeError:
                raise _Refusal(category="tool-unavailable") from None
            if plan["action"] not in names:
                raise _Refusal(category="tool-unavailable")
            call = {
                "index": 0,
                "id": plan["call"],
                "type": "function",
                "function": {
                    "name": plan["action"],
                    "arguments": _encoded(plan["arguments"]).decode(),
                },
            }
            plan["phase"] = "issued"
            return token, {"tool_calls": [call]}, "tool_calls", None
        if plan["phase"] != "issued" or len(results) != 1:
            raise _Refusal(category="result-phase")
        result = results[0]
        if result.get("tool_call_id") != plan["call"] or not isinstance(
            result.get("content"), (str, list, dict)
        ):
            raise _Refusal(category="result-shape")
        if len(calls) != 1:
            raise _Refusal(category="call-order")
        call_index, call = calls[0]
        result_index = next(
            index for index, message in enumerate(tail) if message is result
        )
        if (
            call_index >= result_index
            or call.get("id") != plan["call"]
            or call.get("type") != "function"
            or not isinstance(call.get("function"), dict)
            or call["function"].get("name") != plan["action"]
        ):
            raise _Refusal(category="call-order")
        try:
            arguments = json.loads(call["function"].get("arguments", ""))
        except (TypeError, ValueError, RecursionError):
            raise _Refusal(category="call-order") from None
        if arguments != plan["arguments"]:
            raise _Refusal(category="call-order")
        try:
            captured = {
                "text": _text(result["content"]),
                "images": _images(tail),
                "tool_results": copy.deepcopy(results),
            }
            size = len(_encoded(captured))
        except (TypeError, ValueError, RecursionError):
            raise _Refusal(category="result-shape") from None
        if size > MAX_BODY or self._retained + size > MAX_RETAINED:
            raise _Refusal(413, "result-size")
        self._retained += size
        plan["bytes"] = size
        plan["phase"] = "finishing"
        return token, {"content": COMPLETE}, "stop", captured

    def config(self, unrelated_command=None):
        """Native 2.2.6 config; no credentials, plugins, or other providers."""
        if unrelated_command is not None and (
            not isinstance(unrelated_command, list)
            or not unrelated_command
            or not all(
                isinstance(part, str) and part and "\0" not in part
                for part in unrelated_command
            )
        ):
            raise ValueError("provider-mcp-command")
        configuration = {
            "$schema": "https://opencode.ai/config.json",
            "snapshots": False,
            "formatter": False,
            "lsp": False,
            "update": "disable",
            "default_agent": "build",
            "plugins": [],
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "fixture", "effect": "allow"},
            ],
            "providers": {
                "fixture": {
                    "package": "aisdk:@ai-sdk/openai-compatible",
                    "settings": {
                        "baseURL": f"http://host.containers.internal:{self.port}/v1",
                        "apiKey": "synthetic-not-a-credential",
                    },
                    "models": {
                        "share-model": {
                            "name": "Synthetic sharing fixture",
                            "limit": {"context": 32000, "output": 2000},
                            "modalities": {
                                "input": ["text", "image"],
                                "output": ["text"],
                            },
                        },
                    },
                },
            },
            "model": "fixture/share-model",
        }
        if unrelated_command is not None:
            configuration["mcp"] = {
                "servers": {
                    "unrelated": {
                        "type": "local",
                        "command": list(unrelated_command),
                    }
                }
            }
        return configuration

    def close(self):
        """Bound shutdown even when a client leaves an HTTP body unfinished."""
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                connections = list(self._connections)
                self._closed_plan_phases = self._plan_phase_counts()
                self._plans.clear()
                self._retained = 0
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            closer = threading.Thread(target=self.server.shutdown, daemon=True)
            closer.start()
            closer.join(2)
            self.server.server_close()
            self.thread.join(2)
            if closer.is_alive() or self.thread.is_alive():
                raise RuntimeError("provider-stop")
