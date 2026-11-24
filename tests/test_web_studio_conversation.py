import base64
from email.message import Message
import json
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from oc_box import oc_box as box, web_studio_conversation as studio

ORIGIN = "http://127.0.0.1:49152"
DIRECTORY = "/workspace/repository/project"
ID = "ses_studio"
ROUTE = "/api/session/" + ID
MESSAGES = ROUTE + "/message"
PASSWORD = "synthetic_workspace_" + "p" * 24
TOKEN = base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
PRIVATE = "SYNTHETIC_OMITTED_DATA"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def user(ident="msg_user", **fields):
    return {
        "id": ident,
        "type": "user",
        "text": "Question",
        "time": {"created": 1},
        **fields,
    }


def assistant(parts, ident="msg_assistant"):
    return {
        "id": ident,
        "type": "assistant",
        "content": parts,
        "time": {"created": 2},
        "agent": "build",
        "model": {"id": "test", "providerID": "synthetic-provider"},
    }


def page(rows=(), next_cursor=None, previous=None):
    return {"data": list(rows), "cursor": {"next": next_cursor, "previous": previous}}


class Fixture:
    def __init__(self):
        self.info = {"version": "2.4.1", "urls": [ORIGIN], "pid": 42}
        self.location = {
            "directory": DIRECTORY,
            "project": {
                "id": "verified_project",
                "directory": "/workspace/repository",
                "canonical": "/workspace/repository",
            },
        }
        self.session = {
            "id": ID,
            "projectID": "verified_project",
            "subpath": "project",
            "location": {"directory": DIRECTORY},
            "title": "Synthetic Studio",
            "time": {"created": 1, "updated": 2},
            "permissions": [PRIVATE],
        }
        self.pages = {None: page([user()])}
        self.items = {}
        self.moved = page()
        self.calls = []
        self.after = {}

    def request(self, target, limit):
        second = target in self.calls
        self.calls.append(target)
        path, query = urlsplit(target).path, parse_qs(urlsplit(target).query)
        if target == "/api/info":
            result = self.info
        elif target == "/api/location":
            result = self.location
        elif target == ROUTE:
            result = {"data": self.session}
        elif path == MESSAGES and query.get("type") == ["location-switched"]:
            result = self.moved
        elif path == MESSAGES:
            cursor = query.get("cursor", [None])[0]
            result = self.pages[cursor]
        else:
            ident = path.removeprefix(MESSAGES + "/")
            if ident in self.items:
                result = self.items[ident]
            else:
                result = {
                    "data": next(
                        row
                        for page in self.pages.values()
                        for row in page["data"]
                        if row["id"] == ident
                    )
                }
        result = self.after.get(target, result) if second else result
        return result if isinstance(result, bytes) else encode(result)

    def read(self):
        return studio._conversation(
            self.request, ORIGIN, DIRECTORY, ID, (PASSWORD, TOKEN)
        )


class ConversationTests(unittest.TestCase):
    def assert_refused(self, callback):
        with self.assertRaisesRegex(
            studio.ConversationError,
            "^The Studio conversation could not be verified\\.$",
        ) as raised:
            callback()
        self.assertTrue(raised.exception.__suppress_context__)

    def test_exact_session_gets_only_bounded_plain_user_and_assistant_rows(self):
        fixture = Fixture()
        fixture.session.update(
            {"title": "<session>\nTitle\u009b", "parentID": "ses_parent"}
        )
        fixture.pages = {
            None: page(
                [
                    {
                        "id": "msg_idle",
                        "type": "idle",
                        "time": {"created": 5},
                        "outcome": "succeeded",
                    },
                    assistant(
                        [
                            {"type": "reasoning", "text": PRIVATE},
                            {
                                "type": "tool",
                                "id": "call",
                                "name": PRIVATE,
                                "time": {"created": 2},
                                "state": {
                                    "status": "completed",
                                    "input": {"hidden": PRIVATE},
                                    "content": [
                                        {
                                            "type": "file",
                                            "uri": PRIVATE,
                                            "mime": "image/png",
                                        }
                                    ],
                                },
                            },
                            {
                                "type": "text",
                                "text": "<script>literal()</script>\r\nLine\x1b\u202e",
                            },
                            {
                                "type": "text",
                                "text": "Next",
                                "native": {"hidden": PRIVATE},
                            },
                        ]
                    ),
                ],
                "second",
                "back",
            ),
            "second": page(
                [
                    {
                        "id": "msg_shell",
                        "type": "shell",
                        "time": {"created": 1},
                        "command": PRIVATE,
                    },
                    user(files=[{"data": PRIVATE}], metadata={"hidden": PASSWORD}),
                ]
            ),
        }
        self.assertEqual(
            fixture.read(),
            {
                "sessionID": ID,
                "title": "<session> Title",
                "hasEarlier": False,
                "messages": [
                    {
                        "id": "msg_user",
                        "role": "user",
                        "text": "Question",
                        "truncated": False,
                    },
                    {
                        "id": "msg_assistant",
                        "role": "assistant",
                        "truncated": False,
                        "text": "<script>literal()</script>\nLine\n\nNext",
                    },
                ],
            },
        )
        self.assertEqual(
            [url for url in fixture.calls if url.startswith(MESSAGES + "?")],
            [
                MESSAGES + "?limit=1&order=desc&type=location-switched",
                MESSAGES + f"?limit={studio.PAGE_SIZE}&order=desc",
                MESSAGES + f"?limit={studio.PAGE_SIZE}&cursor=second",
                MESSAGES + "?limit=1&order=desc&type=location-switched",
            ],
        )
        self.assertEqual(
            [url for url in fixture.calls if url.startswith(MESSAGES + "/")],
            [
                MESSAGES + "/msg_assistant",
                MESSAGES + "/msg_user",
            ],
        )

    def test_reject_out_of_scope_before_reading_any_transcript_page(self):
        for field, altered in (
            ("info", {"urls": [ORIGIN + "/", "http://127.0.0.1:49153"]}),
            ("info", {"version": "2.4.2"}),
            ("info", {"pid": True}),
            ("location", {"directory": DIRECTORY + "/nested"}),
            ("session", {"location": {"directory": DIRECTORY + "-sibling"}}),
            ("session", {"projectID": "different_project"}),
            ("session", {"subpath": "different_subpath"}),
            ("session", {"id": "ses_different"}),
            ("session", {"parentID": ID}),
            ("session", {"fork": {"sessionID": "ses_elsewhere"}}),
            ("session", {"revert": {"messageID": "msg_example"}}),
        ):
            with self.subTest(field=field, altered=altered):
                fixture = Fixture()
                getattr(fixture, field).update(altered)
                self.assert_refused(fixture.read)
                self.assertFalse(any(url.startswith(MESSAGES) for url in fixture.calls))

    def test_moved_returned_wrong_id_or_changed_snapshot_withholds_result(self):
        cases = {
            "location event outside window": lambda f: setattr(
                f, "moved", page([{"type": "location-switched"}])
            ),
            "location event inside window": lambda f: f.pages.update(
                {
                    None: page(
                        [user(), {"id": "msg_switched", "type": "location-switched"}]
                    )
                }
            ),
            "move during read": lambda f: f.after.update(
                {
                    MESSAGES + "?limit=1&order=desc&type=location-switched": page(
                        [{"type": "location-switched"}]
                    ),
                }
            ),
            "changed service": lambda f: f.after.update(
                {
                    "/api/info": {**f.info, "pid": 84},
                }
            ),
            "changed project root": lambda f: f.after.update(
                {
                    "/api/location": {
                        **f.location,
                        "project": {**f.location["project"], "canonical": "/elsewhere"},
                    },
                }
            ),
            "changed session": lambda f: f.after.update(
                {
                    ROUTE: {"data": {**f.session, "title": "A later title"}},
                }
            ),
            "same id but different message": lambda f: f.items.update(
                {
                    "msg_user": {"data": user(text="Not the listed message")},
                }
            ),
            "different message ID": lambda f: f.items.update(
                {
                    "msg_user": {"data": user("msg_other")},
                }
            ),
        }
        for name, setup in cases.items():
            with self.subTest(name=name):
                fixture = Fixture()
                setup(fixture)
                self.assert_refused(fixture.read)

    def test_unsupported_content_late_in_walk_fails_closed(self):
        tool_with_unknown_output = {
            "type": "tool",
            "id": "call",
            "name": "tool",
            "state": {"status": "completed", "content": [{"type": "sound"}]},
        }
        for label, bad in (
            ("new message kind", user("msg_second", type="future")),
            ("foreign message owner", user("msg_second", sessionID="ses_foreign")),
            ("unknown assistant part", assistant([{"type": "image", "text": PRIVATE}])),
            ("unknown tool output part", assistant([tool_with_unknown_output])),
            (
                "tool payload mislabeled text",
                assistant([{"type": "text", "output": PRIVATE}]),
            ),
            ("malformed message ID", user("msg/a")),
            ("duplicate message ID", user()),
            ("invalid timestamp", user("msg_second", time={"created": True})),
            ("invalid user part list", user("msg_second", files=[PRIVATE])),
            (
                "unknown user attachment shape",
                user("msg_second", files=[{"type": "future"}]),
            ),
            (
                "too many parts",
                assistant([{"type": "text", "text": "x"}] * (studio.MAX_PARTS + 1)),
            ),
            ("bad title/role promotion", user("msg_second", role="system")),
        ):
            with self.subTest(label=label):
                fixture = Fixture()
                fixture.pages = {None: page([user()], "two"), "two": page([bad])}
                self.assert_refused(fixture.read)

    def test_cursor_order_exhaustion_and_partial_window(self):
        fixture = Fixture()
        fixture.pages = {
            None: page([user("msg_003")], "second"),
            "second": page([user("msg_002")], "third"),
            "third": page([user("msg_001")], "fourth"),
        }
        value = fixture.read()
        self.assertTrue(value["hasEarlier"])
        self.assertEqual(
            [row["id"] for row in value["messages"]], ["msg_001", "msg_002", "msg_003"]
        )
        self.assertFalse(any("fourth" in url for url in fixture.calls))
        for label, last in (
            ("invalid cursor", page([user("msg_next")], "x&directory=/foreign")),
            ("repeated cursor", page([user("msg_next")], "second")),
            ("empty continuation", page([], "third")),
            ("wrong page size", page([user()] * (studio.PAGE_SIZE + 1))),
            ("malformed page", {"data": [], "cursor": {}}),
        ):
            with self.subTest(label=label):
                fixture = Fixture()
                fixture.pages = {None: page([user()], "second"), "second": last}
                self.assert_refused(fixture.read)

    def test_unicode_bounds_and_credential_reconstruction_guard(self):
        fixture = Fixture()
        fixture.session["title"] = "a" + "🦉" * studio.MAX_TITLE_BYTES
        fixture.pages[None] = page([user(text="é" + "🦉" * studio.MAX_TEXT_BYTES)])
        value = fixture.read()
        self.assertEqual(
            len(value["title"].encode()), 1 + 4 * ((studio.MAX_TITLE_BYTES - 1) // 4)
        )
        self.assertEqual(
            len(value["messages"][0]["text"].encode()),
            2 + 4 * ((studio.MAX_TEXT_BYTES - 2) // 4),
        )
        self.assertTrue(value["messages"][0]["truncated"])
        for text in (PASSWORD, TOKEN, PASSWORD[:5] + "\x1b" + PASSWORD[5:]):
            fixture = Fixture()
            fixture.pages[None] = page([user(text=text)])
            self.assert_refused(fixture.read)

    def test_strict_json_and_native_byte_limits(self):
        for mutated in (
            b'{"version":"2.4.1","version":"2.4.1"}',
            b'{"bad":1e999}',
            b'{"bad":NaN}',
        ):
            with self.subTest(mutated=mutated):
                fixture = Fixture()
                fixture.info = mutated
                self.assert_refused(fixture.read)
        with patch.object(studio, "MAX_REPLY_BYTES", 64):
            self.assert_refused(Fixture().read)
        with patch.object(studio, "MAX_NATIVE_BYTES", 512):
            self.assert_refused(Fixture().read)

    def test_worker_result_cannot_smuggle_fields_reassign_role_or_rebind_selection(
        self,
    ):
        value = Fixture().read()
        self.assertEqual(
            studio._display_value(encode(value), ID, (PASSWORD, TOKEN)), value
        )
        for edited in (
            {**value, "credentials": PRIVATE},
            {**value, "sessionID": "ses_other"},
            {**value, "title": PASSWORD},
            {**value, "messages": [{**value["messages"][0], "role": "system"}]},
            {**value, "messages": [{**value["messages"][0], "text": "\x00hide"}]},
            {**value, "messages": value["messages"] * 2},
        ):
            with self.subTest(edited=edited):
                self.assert_refused(
                    lambda: studio._display_value(encode(edited), ID, (PASSWORD, TOKEN))
                )

    def test_spawning_requires_exact_workspace_version_origin_id_and_deadline(self):
        data = {
            "id": "a" * 32,
            "opencode_version": "2.4.1",
            "container_project": DIRECTORY,
            "project": "/different-host-project",
        }
        endpoint = {
            "workspace": data["id"],
            "version": "2.4.1",
            "url": ORIGIN,
            "password": PASSWORD,
        }
        cases = (
            ({}, {"workspace": "b" * 32}, ID, None),
            ({"opencode_version": "2.4.1-rc.1"}, {}, ID, None),
            ({}, {"version": "2.4.2"}, ID, None),
            ({}, {"url": "https://127.0.0.1:49152"}, ID, None),
            ({}, {"url": "http://user:pass@127.0.0.1:49152"}, ID, None),
            ({}, {"url": "http://127.0.0.1:49152?x=1"}, ID, None),
            ({}, {"url": "http://localhost:49152"}, ID, None),
            ({}, {"url": 123}, ID, None),
            ({}, {"password": "short"}, ID, None),
            ({"container_project": "/a/../foreign"}, {}, ID, None),
            ({}, {}, "ses_example/../another", None),
            ({}, {}, "ses_" + PASSWORD, None),
            ({}, {}, ID, time.monotonic() - 1),
        )
        with patch.object(box.workspace_service, "client_command") as spawn:
            for record, connection, ident, deadline in cases:
                with self.subTest(record=record, connection=connection, ident=ident):
                    self.assert_refused(
                        lambda: studio.conversation(
                            box,
                            {**data, **record},
                            {**endpoint, **connection},
                            ident,
                            deadline=deadline,
                        )
                    )
            spawn.assert_not_called()

    def test_bridge_requires_bounded_output_clean_eof_and_success(self):
        commands = (
            "import sys; sys.stdout.write('partial'); sys.exit(1)",
            "print('x' * 100)",
            "import sys,time; print('partial', flush=True); time.sleep(10)",
        )
        with patch.object(studio, "MAX_OUTPUT_BYTES", 10):
            for code in commands:
                with self.subTest(code=code):
                    command = box.ContainerCommand(
                        [sys.executable, "-I", "-S", "-c", code],
                        box.maintenance_host_env(),
                    )
                    self.assert_refused(
                        lambda: studio._read_bridge(command, time.monotonic() + 0.3)
                    )

    def test_native_wire_rejects_redirects_encodings_and_ambiguous_length(self):
        baseline = (("Content-Type", "application/json"), ("Content-Length", "2"))
        with patch.object(studio, "HTTPConnection") as connector:
            for label, status, headers, body in (
                ("valid", 200, baseline, b"{}"),
                (
                    "redirect",
                    302,
                    baseline + (("Location", "https://other.invalid"),),
                    b"{}",
                ),
                ("encoding", 200, baseline + (("Content-Encoding", "gzip"),), b"{}"),
                (
                    "transfer",
                    200,
                    baseline + (("Transfer-Encoding", "chunked"),),
                    b"{}",
                ),
                ("dup length", 200, baseline + (("Content-Length", "2"),), b"{}"),
                ("short body", 200, baseline, b"{"),
                ("over budget", 200, (baseline[0], ("Content-Length", "3")), b"{ }"),
            ):
                with self.subTest(label=label):
                    connection = connector.return_value
                    connection.reset_mock()
                    response = MagicMock(status=status, headers=Message())
                    response.read.return_value = body
                    for name, value in headers:
                        response.headers[name] = value
                    connection.getresponse.return_value.__enter__.return_value = (
                        response
                    )
                    read = lambda: studio._request(49152, "Basic " + TOKEN, ROUTE, 2)
                    if label == "valid":
                        self.assertEqual(read(), b"{}")
                    else:
                        self.assert_refused(read)
                    connection.request.assert_called_once_with(
                        "GET",
                        ROUTE,
                        headers={
                            "Authorization": "Basic " + TOKEN,
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                            "Connection": "close",
                        },
                    )
                    connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
