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
