"""Real loopback HTTP guards only; no native, Podman, or host acceptance.

Every payload and tool output is synthetic and remains in process memory.
"""

import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools/sharing_fixture_provider.py"
spec = importlib.util.spec_from_file_location("sharing_fixture_provider", SCRIPT)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)

IMAGE = "data:image/png;base64,c3ludGhldGljLWltYWdl"
PRIVATE = "SYNTHETIC_PRIVATE_NEVER_DIAGNOSE_THIS"


class SharingFixtureProviderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = provider.Provider()
        self.addCleanup(self.fixture.close)

    def http(self, method="POST", path="/v1/chat/completions", body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.fixture.port, timeout=3
        )
        self.addCleanup(connection.close)
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        status, kind = response.status, response.getheader("Content-Type")
        connection.close()
        return status, kind, payload

    def body(self, token, action="execute", history=None):
        return {
            "model": "share-model",
            "stream": True,
            "messages": [
                *(history or []),
                {"role": "user", "content": "Run this synthetic case: " + token},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": action,
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }

    def raw_http(self, request, *, finish_body=True, timeout=3):
        connection = socket.create_connection(
            ("127.0.0.1", self.fixture.port), timeout=timeout
        )
        self.addCleanup(connection.close)
        connection.sendall(request)
        if finish_body:
            connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
        connection.close()
        head, payload = b"".join(chunks).split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), payload

    def diagnostics(self, *, requests, errors, categories=None, phases=None):
        result = self.fixture.diagnostics()
        self.assertEqual(
            set(result), {"requests", "errors", "error_categories", "plan_phases"}
        )
        self.assertEqual(result["requests"], requests)
        self.assertEqual(result["errors"], errors)
        self.assertEqual(result["requests"], self.fixture.requests)
        self.assertEqual(result["errors"], self.fixture.errors)
        self.assertEqual(sum(result["error_categories"].values()), errors)
        self.assertLessEqual(
            set(result["error_categories"]), set(provider.ERROR_CATEGORIES)
        )
        self.assertEqual(set(result["plan_phases"]), set(provider.PLAN_PHASES))
        for counts in (result["error_categories"], result["plan_phases"]):
            self.assertTrue(
                all(type(count) is int and count >= 0 for count in counts.values())
            )
        if categories is not None:
            self.assertEqual(result["error_categories"], categories)
        if phases is not None:
            expected = dict.fromkeys(provider.PLAN_PHASES, 0)
            expected.update(phases)
            self.assertEqual(result["plan_phases"], expected)
        return result

    def events(self, body):
        status, kind, payload = self.http(body=body)
        self.assertEqual(status, 200)
        self.assertEqual(kind, "text/event-stream")
        events = [
            line[6:] for line in payload.splitlines() if line.startswith(b"data: ")
        ]
        self.assertEqual(events[-1], b"[DONE]")
        return [json.loads(event) for event in events[:-1]]

    def issued(self, token, action="execute", history=None):
        body = self.body(token, action, history)
        events = self.events(body)
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")
        call = events[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], action)
        body["messages"].append(
            {"role": "assistant", "content": None, "tool_calls": [call]}
        )
        return body, call

    def completed(self, body):
        events = self.events(body)
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], provider.COMPLETE)
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def take(self, token):
        # The HTTP reader can see the final bytes a scheduling slice before
        # the worker records that the finished SSE was flushed successfully.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.fixture.take(token)
            if result is not None:
                return result
            time.sleep(0.005)
        self.fail("provider-result-timeout")

    def refused(
        self, body=None, *, status=400, path="/v1/chat/completions", headers=None
    ):
        code, kind, payload = self.http(body=body, path=path, headers=headers)
        self.assertEqual(code, status)
        self.assertEqual(kind, "application/json")
        self.assertEqual(payload, provider.ERROR)

    def test_native_226_config_and_private_host_memory_control(self):
        config = self.fixture.config()
        self.assertEqual(config["model"], "fixture/share-model")
        self.assertEqual(config["default_agent"], "build")
        for field in ("snapshots", "formatter", "lsp"):
            self.assertIs(config[field], False)
        self.assertEqual(config["plugins"], [])
        self.assertEqual(config["update"], "disable")
        self.assertNotIn("mcp", config)
        self.assertEqual(
            config["policies"],
            [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "fixture", "effect": "allow"},
            ],
        )
        native = config["providers"]["fixture"]
        self.assertEqual(native["package"], "aisdk:@ai-sdk/openai-compatible")
        self.assertEqual(
            native["settings"],
            {
                "baseURL": f"http://host.containers.internal:{self.fixture.port}/v1",
                "apiKey": "synthetic-not-a-credential",
            },
        )
        self.assertEqual(
            native["models"]["share-model"]["modalities"],
            {
                "input": ["text", "image"],
                "output": ["text"],
            },
        )
        self.assertEqual(self.fixture.server.server_address[0], "127.0.0.1")
        self.assertTrue(self.fixture.server.daemon_threads)
        self.assertTrue(self.fixture.thread.daemon)
        # The native smoke's older aliases are deliberately not mixed in.
        self.assertNotIn("provider", config)
        self.assertNotIn("npm", native)
        self.assertNotIn("options", native)
        self.refused({"action": "execute", "arguments": {}}, path="/plan", status=404)
        self.refused({}, path="/network", status=404)

    def test_optional_unrelated_mcp_is_copied_and_has_no_environment(self):
        command = ["python3", "-I", "synthetic_readonly_mcp.py"]
        config = self.fixture.config(command)
        command.append("do-not-forward-later-mutation")
        self.assertEqual(
            config["mcp"],
            {
                "servers": {
                    "unrelated": {
                        "type": "local",
                        "command": ["python3", "-I", "synthetic_readonly_mcp.py"],
                    }
                }
            },
        )
        for invalid in ([], "python3", ["python3", ""], ["python3", "bad\0value"]):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "^provider-mcp-command$"),
            ):
                self.fixture.config(invalid)

    def test_serial_plans_and_all_real_native_tool_names(self):
        history = []
        tokens = set()
        for action, arguments in (
            (
                "execute",
                {"code": "return await tools.synthetic.read_file({path:'text.txt'})"},
            ),
            ("shell", {"command": "printf SYNTHETIC", "workdir": "/synthetic"}),
            ("read", {"path": "/synthetic/text.txt"}),
        ):
            with self.subTest(action=action):
                token = self.fixture.plan(action, arguments)
                self.assertRegex(token, r"^SHARING_CASE_[0-9a-f]{32}$")
                self.assertNotIn(token, tokens)
                tokens.add(token)
                self.assertIsNone(self.fixture.take(token))
                body, call = self.issued(token, action, history)
                self.assertEqual(json.loads(call["function"]["arguments"]), arguments)
                result = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "SYNTHETIC_" + action,
                }
                body["messages"].append(result)
                self.completed(body)
                captured = self.take(token)
                self.assertEqual(
                    captured,
                    {
                        "text": "SYNTHETIC_" + action,
                        "images": [],
                        "tool_results": [result],
                    },
                )
                self.assertIsNone(self.fixture.take(token))
                history = body["messages"]
        self.assertEqual(self.fixture.requests, 6)
        self.assertEqual(self.fixture.errors, 0)

    def test_separate_native_session_histories_do_not_share_plan_state(self):
        first = self.fixture.plan("execute", {"code": "return 'FIRST'"})
        second = self.fixture.plan("execute", {"code": "return 'SECOND'"})
        for token, text in ((first, "FIRST"), (second, "SECOND")):
            body, call = self.issued(token)
            body["messages"].append(
                {"role": "tool", "tool_call_id": call["id"], "content": text}
            )
            self.completed(body)
            self.assertEqual(self.take(token)["text"], text)
        self.assertEqual(self.fixture.errors, 0)

    def test_tool_content_lists_and_native_followup_image_user_message(self):
        token = self.fixture.plan(
            "execute",
            {"code": "return await tools.synthetic.read_image({path:'pixel.png'})"},
        )
        body, call = self.issued(token)
        content = [{"type": "text", "text": "SYNTHETIC_IMAGE_RESULT 中🙂"}]
        result = {"role": "tool", "tool_call_id": call["id"], "content": content}
        body["messages"].extend(
            [
                result,
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": IMAGE}}],
                },
            ]
        )
        self.completed(body)
        captured = self.take(token)
        self.assertEqual(captured["text"], "SYNTHETIC_IMAGE_RESULT 中🙂")
        self.assertEqual(captured["images"], [IMAGE])
        self.assertEqual(captured["tool_results"], [result])
        content[0]["text"] = "MUTATED_LATER"
        self.assertEqual(
            captured["tool_results"][0]["content"][0]["text"],
            "SYNTHETIC_IMAGE_RESULT 中🙂",
        )

    def test_dict_content_and_images_stay_structured_not_parsed_from_text(self):
        token = self.fixture.plan("read", {"path": "/synthetic/pixel.png"})
        body, call = self.issued(token, "read")
        content = {
            "text": "SYNTHETIC "
            + json.dumps({"url": "data:image/png;base64,NOT_AN_IMAGE_BLOCK"})
        }
        body["messages"].extend(
            [
                {"role": "tool", "tool_call_id": call["id"], "content": content},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": IMAGE}},
                        {"type": "image_url", "image_url": {"url": IMAGE}},
                    ],
                },
            ]
        )
        self.completed(body)
        captured = self.take(token)
        self.assertEqual(captured["text"], content["text"])
        self.assertEqual(captured["images"], [IMAGE])
        self.assertEqual(captured["tool_results"][0]["content"], content)

    def test_unknown_malformed_and_non_user_plan_tokens_are_refused(self):
        known = self.fixture.plan("execute", {})
        unknown = "SHARING_CASE_" + "0" * 32
        while unknown == known:
            unknown = "SHARING_CASE_" + "1" * 32
        bodies = [
            self.body(unknown),
            self.body("SHARING_CASE_not_hex"),
            self.body("no token"),
            self.body(known + " " + unknown),
            {**self.body(known), "messages": [{"role": "system", "content": known}]},
            {**self.body(known), "messages": [{"role": "tool", "content": known}]},
            # A later unknown user token must not fall back to an earlier plan.
            {
                **self.body(known),
                "messages": [
                    {"role": "user", "content": known},
                    {"role": "user", "content": unknown},
                ],
            },
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.refused(body)
        self.assertIsNone(self.fixture.take(known))
        with self.assertRaisesRegex(ValueError, "^provider-token$"):
            self.fixture.take(unknown)
        self.assertEqual(self.fixture.errors, len(bodies))
        self.diagnostics(
            requests=len(bodies),
            errors=len(bodies),
            categories={"token": len(bodies)},
            phases={"planned": 1},
        )

    def test_out_of_order_wrong_call_and_reuse_are_refused(self):
        token = self.fixture.plan("execute", {"code": "return 'SYNTHETIC'"})
        body, call = self.issued(token)
        self.refused(self.body(token))  # No native tool result yet.
        wrong = {
            "role": "tool",
            "tool_call_id": "call_not_ours",
            "content": "SYNTHETIC",
        }
        body["messages"].append(wrong)
        self.refused(body)
        wrong["tool_call_id"] = call["id"]
        self.completed(body)
        self.assertEqual(self.take(token)["text"], "SYNTHETIC")
        self.refused(body, status=429)  # Fifth attempt exceeds the per-case cap.
        self.assertIsNone(self.fixture.take(token))

    def test_completed_case_cannot_issue_again_within_case_request_budget(self):
        token = self.fixture.plan("execute", {})
        body, call = self.issued(token)
        body["messages"].append(
            {"role": "tool", "tool_call_id": call["id"], "content": "DONE"}
        )
        self.completed(body)
        self.take(token)
        self.refused(self.body(token))
        self.diagnostics(
            requests=3,
            errors=1,
            categories={"result-phase": 1},
            phases={"taken": 1},
        )

    def test_tool_result_without_native_assistant_call_is_refused(self):
        token = self.fixture.plan("execute", {})
        body, call = self.issued(token)
        body["messages"].pop()
        body["messages"].append(
            {"role": "tool", "tool_call_id": call["id"], "content": "DONE"}
        )
        self.refused(body)
        self.assertIsNone(self.fixture.take(token))

    def test_native_tool_result_before_assistant_call_is_refused(self):
        token = self.fixture.plan("execute", {})
        body, call = self.issued(token)
        body["messages"].insert(
            1, {"role": "tool", "tool_call_id": call["id"], "content": "DONE"}
        )
        self.refused(body)
        self.assertIsNone(self.fixture.take(token))

    def test_original_user_content_blocks_and_nested_tool_result_are_preserved(self):
        token = self.fixture.plan("execute", {})
        body = self.body(token)
        body["messages"][0]["content"] = [{"type": "text", "text": token}]
        events = self.events(body)
        call = events[0]["choices"][0]["delta"]["tool_calls"][0]
        content = {"content": [{"type": "text", "text": "SYNTHETIC_NESTED"}]}
        body["messages"].extend(
            [
                {"role": "assistant", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": content},
            ]
        )
        self.completed(body)
        result = self.take(token)
        self.assertEqual(result["text"], "SYNTHETIC_NESTED")
        self.assertEqual(result["tool_results"][0]["content"], content)

    def test_plan_validation_is_fixed_and_copies_arguments(self):
        for action, arguments in (
            ("write", {}),
            ("execute", []),
            ("read", {"bad": object()}),
        ):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "^provider-plan$"),
            ):
                self.fixture.plan(action, arguments)
        arguments = {"code": "SYNTHETIC_ORIGINAL"}
        token = self.fixture.plan("execute", arguments)
        arguments["code"] = "MUTATED_AFTER_PLAN"
        _, call = self.issued(token)
        self.assertEqual(
            json.loads(call["function"]["arguments"]), {"code": "SYNTHETIC_ORIGINAL"}
        )

    def test_request_body_bounds_and_malformed_contracts(self):
        for raw in (b"{}", b"not json", b"[]", b'{"model":"private-unexpected"}'):
            with self.subTest(raw=raw):
                self.refused(raw)
        with patch.object(provider, "MAX_BODY", 64):
            self.refused(b"x" * 65, status=413)
        token = self.fixture.plan("execute", {})
        for change in (
            {"model": "other"},
            {"stream": False},
            {"messages": {}},
            {"tools": []},
        ):
            with self.subTest(change=change):
                body = self.body(token)
                body.update(change)
                self.refused(body)
        self.assertIsNone(self.fixture.take(token))

    def test_request_category_matrix_uses_only_fixed_labels(self):
        cases = (
            ("wrong-model", {"model": PRIVATE}, [], "request-shape"),
            ("not-streaming", {"stream": False}, [], "request-shape"),
            ("missing-messages", {"messages": None}, [], "messages"),
            ("empty-messages", {"messages": []}, [], "messages"),
            ("non-object-message", {"messages": [PRIVATE]}, [], "messages"),
            (
                "too-many-messages",
                {
                    "messages": [{"role": "user", "content": PRIVATE}]
                    * (provider.MAX_MESSAGES + 1)
                },
                [],
                "messages",
            ),
            (
                "malformed-token",
                {"messages": [{"role": "user", "content": "SHARING_CASE_" + PRIVATE}]},
                [],
                "token",
            ),
            ("missing-tools", {"tools": None}, [], "tool-unavailable"),
            ("empty-tools", {"tools": []}, [], "tool-unavailable"),
            (
                "unhashable-tool-name",
                {"tools": [{"type": "function", "function": {"name": [PRIVATE]}}]},
                [],
                "tool-unavailable",
            ),
            (
                "premature-result",
                {},
                [{"role": "tool", "tool_call_id": PRIVATE, "content": PRIVATE}],
                "initial-history",
            ),
            (
                "premature-call",
                {},
                [{"role": "assistant", "tool_calls": [{"id": PRIVATE}]}],
                "initial-history",
            ),
            (
                "malformed-initial-call",
                {},
                [{"role": "assistant", "tool_calls": {"private": PRIVATE}}],
                "initial-history",
            ),
        )
        expected = {}
        for index, (name, change, tail, category) in enumerate(cases, 1):
            with self.subTest(case=name):
                token = self.fixture.plan("execute", {"code": PRIVATE})
                body = self.body(token)
                body.update(change)
                if isinstance(body["messages"], list):
                    body["messages"].extend(tail)
                self.refused(body, headers={"Authorization": "Bearer " + PRIVATE})
                expected[category] = expected.get(category, 0) + 1
                self.diagnostics(
                    requests=index,
                    errors=index,
                    categories=expected,
                    phases={"planned": index},
                )
                self.assertIsNone(self.fixture.take(token))

    def test_real_http_framing_body_json_and_route_category_matrix(self):
        base = b"POST /v1/chat/completions HTTP/1.1\r\nHost: synthetic\r\n"
        cases = (
            ("missing-length", b"", b"", 400, "framing"),
            (
                "duplicate-length",
                b"Content-Length: 2\r\nContent-Length: 2\r\n",
                b"{}",
                400,
                "framing",
            ),
            (
                "nondecimal-length",
                b"Content-Length: " + PRIVATE.encode() + b"\r\n",
                b"",
                400,
                "framing",
            ),
            ("negative-length", b"Content-Length: -1\r\n", b"", 400, "framing"),
            (
                "transfer-encoding",
                b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
                b"{}",
                400,
                "framing",
            ),
            (
                "content-encoding",
                b"Content-Length: 2\r\nContent-Encoding: gzip\r\n",
                b"{}",
                400,
                "framing",
            ),
            ("empty-body", b"Content-Length: 0\r\n", b"", 413, "body-size"),
            (
                "oversized-body",
                f"Content-Length: {provider.MAX_BODY + 1}\r\n".encode(),
                b"",
                413,
                "body-size",
            ),
            ("truncated-body", b"Content-Length: 3\r\n", b"{", 400, "framing"),
            ("invalid-json", b"Content-Length: 1\r\n", b"{", 400, "json"),
            ("invalid-utf8", b"Content-Length: 1\r\n", b"\xff", 400, "json"),
            ("nonobject-json", b"Content-Length: 2\r\n", b"[]", 400, "request-shape"),
        )
        expected = {}
        for index, (name, headers, body, status, category) in enumerate(cases, 1):
            with self.subTest(case=name):
                code, payload = self.raw_http(base + headers + b"\r\n" + body)
                self.assertEqual((code, payload), (status, provider.ERROR))
                expected[category] = expected.get(category, 0) + 1
                self.diagnostics(requests=index, errors=index, categories=expected)
        code, _, payload = self.http("GET", "/" + PRIVATE)
        self.assertEqual((code, payload), (404, provider.ERROR))
        expected["route"] = 1
        self.diagnostics(
            requests=len(cases), errors=len(cases) + 1, categories=expected
        )

    def test_parser_and_unknown_method_failures_never_reflect_request(self):
        cases = (
            (
                "malformed-request-line",
                b"GET /"
                + PRIVATE.encode()
                + b" EXTRA HTTP/1.1\r\nHost: synthetic\r\n\r\n",
                400,
                "parser",
            ),
            (
                "overlong-private-header",
                # Exactly one overlong line, without unread trailing bytes.
                b"GET /health HTTP/1.1\r\nX-Private: "
                + PRIVATE.encode().ljust(65524, b"x")
                + b"\r\n",
                431,
                "parser",
            ),
            (
                "unknown-method",
                b"PATCH /" + PRIVATE.encode() + b" HTTP/1.1\r\nHost: synthetic\r\n\r\n",
                501,
                "route",
            ),
        )
        expected = {}
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            for index, (name, request, status, category) in enumerate(cases, 1):
                with self.subTest(case=name):
                    code, payload = self.raw_http(request)
                    self.assertEqual((code, payload), (status, provider.ERROR))
                    expected[category] = expected.get(category, 0) + 1
                    self.diagnostics(requests=0, errors=index, categories=expected)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_result_category_matrix_preserves_strict_native_call_order(self):
        cases = (
            ("missing-result", "result-phase"),
            ("multiple-results", "result-phase"),
            ("wrong-result-id", "result-shape"),
            ("null-content", "result-shape"),
            ("missing-call", "call-order"),
            ("malformed-call-list", "call-order"),
            ("wrong-call-id", "call-order"),
            ("wrong-function", "call-order"),
            ("malformed-arguments", "call-order"),
            ("wrong-argument-type", "call-order"),
            ("changed-arguments", "call-order"),
            ("result-before-call", "call-order"),
        )
        expected = {}
        for index, (name, category) in enumerate(cases, 1):
            with self.subTest(case=name):
                token = self.fixture.plan("execute", {"code": PRIVATE})
                body, call = self.issued(token)
                result = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": PRIVATE,
                }
                body["messages"].append(result)
                if name == "missing-result":
                    body["messages"].pop()
                elif name == "multiple-results":
                    body["messages"].append(dict(result))
                elif name == "wrong-result-id":
                    result["tool_call_id"] = PRIVATE
                elif name == "null-content":
                    result["content"] = None
                elif name == "missing-call":
                    body["messages"].pop(1)
                elif name == "malformed-call-list":
                    body["messages"][1]["tool_calls"] = PRIVATE
                elif name == "wrong-call-id":
                    call["id"] = PRIVATE
                elif name == "wrong-function":
                    call["function"]["name"] = PRIVATE
                elif name == "malformed-arguments":
                    call["function"]["arguments"] = PRIVATE
                elif name == "wrong-argument-type":
                    call["function"]["arguments"] = {"private": PRIVATE}
                elif name == "changed-arguments":
                    call["function"]["arguments"] = json.dumps(
                        {"code": PRIVATE + "_CHANGED"}
                    )
                elif name == "result-before-call":
                    body["messages"][1:] = [result, body["messages"][1]]
                self.refused(body)
                expected[category] = expected.get(category, 0) + 1
                self.diagnostics(
                    requests=index * 2,
                    errors=index,
                    categories=expected,
                    phases={"issued": index},
                )
                self.assertIsNone(self.fixture.take(token))

    def test_diagnostics_are_fresh_count_only_snapshots_and_preserve_image_results(
        self,
    ):
        self.diagnostics(requests=0, errors=0, categories={}, phases={})
        root = "/synthetic/" + PRIVATE + "/project"
        token = self.fixture.plan("read", {"path": root})
        snapshot = self.diagnostics(
            requests=0, errors=0, categories={}, phases={"planned": 1}
        )
        snapshot["requests"] = 999
        snapshot["error_categories"]["protocol"] = 999
        snapshot["plan_phases"]["planned"] = 999
        self.diagnostics(requests=0, errors=0, categories={}, phases={"planned": 1})
        body, call = self.issued(token, "read")
        self.diagnostics(requests=1, errors=0, categories={}, phases={"issued": 1})
        content = {
            "content": [{"type": "text", "text": PRIVATE}],
            "private": {"root": root, "provider_payload": PRIVATE},
        }
        result = {"role": "tool", "tool_call_id": call["id"], "content": content}
        body["messages"].extend(
            [
                result,
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": IMAGE}}],
                },
            ]
        )
        self.completed(body)
        deadline = time.monotonic() + 2
        while (
            self.fixture.diagnostics()["plan_phases"]["finished"] != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        self.diagnostics(requests=2, errors=0, categories={}, phases={"finished": 1})
        captured = self.take(token)
        self.assertEqual(
            captured, {"text": PRIVATE, "images": [IMAGE], "tool_results": [result]}
        )
        self.refused(
            {"model": PRIVATE, "messages": {"root": root, "token": token}},
            headers={"Authorization": "Bearer " + PRIVATE, "X-Private": root},
        )
        self.refused({"provider_payload": PRIVATE}, path=root, status=404)
        diagnostics = self.diagnostics(
            requests=4,
            errors=2,
            categories={"request-shape": 1, "route": 1},
            phases={"taken": 1},
        )
        for field, forbidden in (
            ("sentinel", PRIVATE),
            ("root", root),
            ("token", token),
            ("call-id", call["id"]),
            ("image", IMAGE),
            ("authorization-header", "Authorization"),
            ("private-header", "X-Private"),
            ("payload", "provider_payload"),
            ("model", "share-model"),
            ("arguments", "arguments"),
            ("result", "tool_results"),
            ("token-prefix", "SHARING_CASE_"),
        ):
            with self.subTest(field=field):
                self.assertNotIn(forbidden, json.dumps(diagnostics))
        self.fixture.close()
        closed = self.diagnostics(
            requests=4,
            errors=2,
            categories={"request-shape": 1, "route": 1},
            phases={"taken": 1},
        )
        self.assertEqual(self.fixture._plans, {})
        self.assertEqual(self.fixture._retained, 0)
        self.assertNotIn(PRIVATE, json.dumps(closed))
        closed["plan_phases"]["taken"] = 999
        closed["error_categories"]["route"] = 999
        self.fixture.close()
        self.diagnostics(
            requests=4,
            errors=2,
            categories={"request-shape": 1, "route": 1},
            phases={"taken": 1},
        )

    def test_close_then_diagnostics_preserves_issued_counts_not_private_plans(self):
        self.fixture.plan("read", {"path": "/synthetic/" + PRIVATE})
        token = self.fixture.plan("execute", {"code": PRIVATE})
        self.issued(token)
        self.fixture.close()
        closed = self.diagnostics(
            requests=1,
            errors=0,
            categories={},
            phases={"planned": 1, "issued": 1},
        )
        self.assertEqual(self.fixture._plans, {})
        self.assertEqual(self.fixture._retained, 0)
        self.assertNotIn(PRIVATE, json.dumps(closed))
        self.assertNotIn(token, json.dumps(closed))
        closed["plan_phases"]["issued"] = 999
        closed["error_categories"]["protocol"] = 999
        self.fixture.close()
        self.diagnostics(
            requests=1,
            errors=0,
            categories={},
            phases={"planned": 1, "issued": 1},
        )

    def test_failed_sse_write_never_publishes_result_or_counts_failure_twice(self):
        token = self.fixture.plan("execute", {"code": PRIVATE})
        body, call = self.issued(token)
        body["messages"].append(
            {"role": "tool", "tool_call_id": call["id"], "content": PRIVATE}
        )
        handler = self.fixture.server.RequestHandlerClass
        output, errors = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            patch.object(handler, "end_headers", side_effect=OSError(PRIVATE)),
            self.assertRaises((http.client.RemoteDisconnected, ConnectionError)),
        ):
            self.http(body=body)
        self.assertLess(time.monotonic() - started, 4.5)
        self.diagnostics(
            requests=2,
            errors=1,
            categories={"response-write": 1},
            phases={"finishing": 1},
        )
        self.assertIsNone(self.fixture.take(token))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn(PRIVATE, json.dumps(self.fixture.diagnostics()))
        self.assertGreater(self.fixture._retained, 0)
        self.fixture.close()
        closed = self.diagnostics(
            requests=2,
            errors=1,
            categories={"response-write": 1},
            phases={"finishing": 1},
        )
        self.assertEqual(self.fixture._plans, {})
        self.assertEqual(self.fixture._retained, 0)
        self.assertNotIn(PRIVATE, json.dumps(closed))
        self.assertNotIn(token, json.dumps(closed))
        closed["plan_phases"]["finishing"] = 999
        self.fixture.close()
        self.diagnostics(
            requests=2,
            errors=1,
            categories={"response-write": 1},
            phases={"finishing": 1},
        )

    def test_failed_refusal_write_keeps_original_category_and_single_error(self):
        handler = self.fixture.server.RequestHandlerClass
        cases = (
            ("POST", "/v1/chat/completions", {"model": PRIVATE}, "request-shape"),
            ("GET", "/" + PRIVATE, None, "route"),
        )
        expected = {}
        with patch.object(handler, "end_headers", side_effect=OSError(PRIVATE)):
            for index, (method, path, body, category) in enumerate(cases, 1):
                with (
                    self.subTest(method=method),
                    self.assertRaises(
                        (http.client.RemoteDisconnected, ConnectionError)
                    ),
                ):
                    self.http(method, path, body)
                expected[category] = expected.get(category, 0) + 1
                self.diagnostics(requests=1, errors=index, categories=expected)

    def test_incomplete_request_body_timeout_is_one_bounded_transport_error(self):
        started = time.monotonic()
        status, payload = self.raw_http(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: synthetic\r\n"
            b"Content-Length: 3\r\n\r\n{",
            finish_body=False,
            timeout=5,
        )
        self.assertEqual((status, payload), (400, provider.ERROR))
        self.assertLess(time.monotonic() - started, 4.5)
        self.diagnostics(requests=1, errors=1, categories={"transport": 1})

    def test_protocol_fallback_and_untrusted_category_never_retain_exception_details(
        self,
    ):
        cases = (
            ("unexpected-protocol", RuntimeError(PRIVATE), 400, "protocol"),
            (
                "untrusted-category",
                provider._Refusal(category=PRIVATE),
                400,
                "protocol",
            ),
            ("fixed-status", provider._Refusal(413, "result-size"), 413, "result-size"),
        )
        expected = {}
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            for index, (name, failure, status, category) in enumerate(cases, 1):
                with self.subTest(case=name):
                    with patch.object(self.fixture, "_reply", side_effect=failure):
                        self.refused({"provider_payload": PRIVATE}, status=status)
                    expected[category] = expected.get(category, 0) + 1
                    result = self.diagnostics(
                        requests=index, errors=index, categories=expected
                    )
                    self.assertNotIn(PRIVATE, json.dumps(result))
                    if isinstance(failure, provider._Refusal):
                        self.assertEqual(failure.args, ())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_request_case_plan_and_retained_result_bounds(self):
        token = self.fixture.plan("execute", {})
        with patch.object(provider, "MAX_CASE_REQUESTS", 1):
            body, call = self.issued(token)
            body["messages"].append(
                {"role": "tool", "tool_call_id": call["id"], "content": "DONE"}
            )
            self.refused(body, status=429)
        with patch.object(provider, "MAX_REQUESTS", self.fixture.requests):
            self.refused(body, status=429)
        with patch.object(provider, "MAX_PLANS", 1):
            with self.assertRaisesRegex(RuntimeError, "^provider-plan-bound$"):
                self.fixture.plan("read", {})
        with patch.object(provider, "MAX_RETAINED", 1):
            self.refused(body, status=413)
        self.diagnostics(
            requests=4,
            errors=3,
            categories={"case-budget": 1, "request-budget": 1, "result-size": 1},
            phases={"issued": 1},
        )

    def test_large_utf8_tool_text_and_image_are_not_truncated(self):
        token = self.fixture.plan("execute", {})
        body, call = self.issued(token)
        # More than the old smoke's 2 MiB limit, including whole UTF-8 points.
        text = "中🙂" * (512 * 1024)
        image = "data:image/png;base64," + "c3lu" * (2 * 1024 * 1024)
        body["messages"].extend(
            [
                {"role": "tool", "tool_call_id": call["id"], "content": text},
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": image}}],
                },
            ]
        )
        self.completed(body)
        result = self.take(token)
        self.assertEqual(result["text"], text)
        self.assertEqual(result["images"], [image])
        self.assertEqual(self.fixture.errors, 0)

    def test_catalog_health_do_not_increment_inference_or_contact_other_services(self):
        for path in (
            "/health",
            "/healthz",
            "/v1/models",
            "/v1/models/",
            "/api/v1/models",
            "/api/v1/models/",
            "/api/v1/health",
            "/api/v1/version",
        ):
            with self.subTest(path=path):
                status, kind, body = self.http("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(kind, "application/json")
                self.assertEqual(json.loads(body), {"data": [], "models": []})
        self.assertEqual(self.fixture.requests, 0)
        self.assertEqual(self.fixture.errors, 0)

    def test_refusals_do_not_echo_or_log_payload_headers_paths_or_exceptions(self):
        private = "SYNTHETIC_PRIVATE_NEVER_LOG_THIS"
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.refused(
                {"model": private, "messages": private},
                headers={"Authorization": "Bearer " + private},
            )
            self.refused(b"{" + private.encode(), path="/" + private, status=404)
            status, _, body = self.http("GET", "/" + private)
            self.assertEqual(status, 404)
            self.assertEqual(body, provider.ERROR)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn(private, repr(vars(self.fixture)))
        self.assertEqual(self.fixture.errors, 3)

    def test_close_is_bounded_with_unfinished_client_and_is_idempotent(self):
        connection = socket.create_connection(
            ("127.0.0.1", self.fixture.port), timeout=2
        )
        self.addCleanup(connection.close)
        connection.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: synthetic\r\n"
            b"Content-Length: 100000\r\n\r\n{"
        )
        deadline = time.monotonic() + 2
        while self.fixture.requests == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.fixture.requests, 1)
        started = time.monotonic()
        self.fixture.close()
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertFalse(self.fixture.thread.is_alive())
        self.fixture.close()
        self.assertEqual(self.fixture._plans, {})
        self.assertEqual(self.fixture._retained, 0)
        with self.assertRaisesRegex(RuntimeError, "^provider-closed$"):
            self.fixture.plan("read", {})

    def test_context_manager_stops_listener_and_daemon_thread(self):
        with provider.Provider() as fixture:
            thread, port = fixture.thread, fixture.port
        self.assertFalse(thread.is_alive())
        with self.assertRaises(OSError):
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            connection.close()


if __name__ == "__main__":
    unittest.main()
