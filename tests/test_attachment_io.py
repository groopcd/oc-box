import base64
import copy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from oc_box import attachment_io


SESSION = "ses_attachment_io_fixture"
DIRECTORY = "/project/attachment-fixture"
MESSAGE = "msg_attachment_io_fixture"
PASSWORD = "synthetic-private-api-password"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MALICIOUS = (
    b"synthetic-native-error /synthetic/private/image.png token=not-a-secret\x1b[31m"
)
DEFAULT = object()


def payload(operation="send", *, resume=False):
    value = {
        "operation": operation,
        "session": SESSION,
        "directory": DIRECTORY,
        "version": "2.2.6",
    }
    if operation == "send":
        value["prompt"] = {
            "id": MESSAGE,
            "text": "Inspect this synthetic screenshot.",
            "files": [
                {
                    "uri": "data:image/png;base64," + base64.b64encode(PNG).decode(),
                    "name": "host-image.png",
                    "source": {"type": "inline"},
                }
            ],
            "delivery": "queue",
            "resume": resume,
        }
    return value


def session_info():
    return {
        "data": {
            "id": SESSION,
            "location": {"directory": DIRECTORY},
            "time": {"created": 1},
        }
    }


def acknowledgement():
    # Native admission may normalize the image. Identity and the native user/
    # queue envelope matter; byte-for-byte reflection of the upload does not.
    return {
        "data": {
            "id": MESSAGE,
            "sessionID": SESSION,
            "type": "user",
            "delivery": "queue",
            "payload": {
                "files": [
                    {
                        "uri": "data:image/webp;base64,bm9ybWFsaXplZA==",
                        "name": "normalized.webp",
                        "source": {"type": "inline"},
                    }
                ]
            },
        }
    }


class NativeRequests:
    def __init__(self, *, sessions=None, info=DEFAULT, reply=DEFAULT):
        self.sessions = sessions if sessions is not None else [session_info()]
        self.info = {"version": "2.2.6"} if info is DEFAULT else info
        self.reply = acknowledgement() if reply is DEFAULT else reply
        self.calls = []
        self.session_reads = 0

    def __call__(self, method, path, value=None):
        self.calls.append((method, path, copy.deepcopy(value)))
        if (method, path) == ("GET", "/api/info"):
            return self.info
        if (method, path) == ("GET", "/api/session/" + SESSION):
            index = min(self.session_reads, len(self.sessions) - 1)
            self.session_reads += 1
            return self.sessions[index]
        if (method, path) == ("POST", "/api/session/" + SESSION + "/prompt"):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        raise AssertionError("Unexpected synthetic API request")

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class AttachmentDeliveryTests(unittest.TestCase):
    def refuse(self, value, category="invalid-request", *, native=None):
        native = native if native is not None else NativeRequests()
        with self.assertRaises(attachment_io.Refused) as raised:
            attachment_io.deliver(native, value)
        self.assertEqual(str(raised.exception), category)
        self.assertEqual(native.posts, [])
        return native

    def test_check_validates_native_version_and_exact_target_without_mutation(self):
        native = NativeRequests()
        self.assertEqual(attachment_io.deliver(native, payload("check")), {"ok": True})
        self.assertEqual(
            native.calls,
            [
                ("GET", "/api/info", None),
                ("GET", "/api/session/" + SESSION, None),
            ],
        )

    def test_invalid_operation_identity_directory_version_and_extra_keys_refuse(self):
        cases = [
            ("not-object", []),
            ("null", None),
            ("extra", {**payload("check"), "hostPath": "/synthetic/host/image.png"}),
        ]
        for key, values in (
            ("operation", (None, "", "POST", "delete", [], True)),
            (
                "session",
                (
                    None,
                    "",
                    "ses",
                    "../ses_escape",
                    "ses/a",
                    "ses_é",
                    1,
                    "ses" + "x" * 126,
                ),
            ),
            (
                "directory",
                (None, "", "relative", "/project/\0escape", 1, "/" + "é" * 2048),
            ),
            ("version", (None, "", "2.2.5", "2.2.6\n", [], {}, 226)),
        ):
            for index, value in enumerate(values):
                cases.append((f"{key}-{index}", {**payload("check"), key: value}))
        for key in ("operation", "session", "directory", "version"):
            value = payload("check")
            del value[key]
            cases.append(("missing-" + key, value))
        for name, value in cases:
            with self.subTest(case=name):
                native = self.refuse(value)
                self.assertEqual(native.calls, [])

    def test_all_supported_versions_require_exact_native_report(self):
        for version in ("2.2.6", "2.2.7", "2.3.0"):
            with self.subTest(version=version):
                value = {**payload("check"), "version": version}
                native = NativeRequests(info={"version": version})
                self.assertEqual(attachment_io.deliver(native, value), {"ok": True})
        for info in (
            None,
            [],
            {},
            {"data": []},
            {"data": {}},
            {"data": {"version": "2.2.6"}},
            {"data": {"version": "2.2.7"}},
            {"data": {"version": "v2.2.6"}},
            {"version": "2.2.7"},
            {"version": "v2.2.6"},
        ):
            with self.subTest(info=info):
                native = self.refuse(
                    payload(), "native-version", native=NativeRequests(info=info)
                )
                self.assertEqual(native.calls, [("GET", "/api/info", None)])

    def test_missing_wrong_or_unwrapped_session_is_unavailable_without_post(self):
        for response in (
            None,
            [],
            {},
            {"data": []},
            {"data": {}},
            {"data": {"id": "ses_other"}},
            session_info()["data"],
        ):
            with self.subTest(response=response):
                self.refuse(
                    payload(),
                    "session-unavailable",
                    native=NativeRequests(sessions=[response]),
                )

    def test_directory_is_exact_not_prefix_basename_or_normalized_match(self):
        for location in (
            None,
            {},
            {"directory": DIRECTORY + "/child"},
            {"directory": DIRECTORY + "/"},
            {"directory": "/elsewhere"},
            {"directory": "/project/./attachment-fixture"},
        ):
            with self.subTest(location=location):
                response = session_info()
                response["data"]["location"] = location
                self.refuse(
                    payload(),
                    "session-scope",
                    native=NativeRequests(sessions=[response]),
                )

    def test_observed_staged_reverts_and_archives_refuse_check_and_send(self):
        for operation in ("check", "send"):
            for state in ("revert", "archived", "missing-time"):
                with self.subTest(operation=operation, state=state):
                    response = session_info()
                    if state == "revert":
                        response["data"]["revert"] = {"messageID": "msg_staged"}
                        category = "session-revert"
                    else:
                        if state == "archived":
                            response["data"]["time"]["archived"] = 1
                        else:
                            del response["data"]["time"]
                        category = "session-archived"
                    native = self.refuse(
                        payload(operation),
                        category,
                        native=NativeRequests(sessions=[response]),
                    )
                    self.assertEqual(native.session_reads, 1)

    def test_falsy_but_present_revert_or_archive_is_not_mistaken_for_absence(self):
        for key in ("revert", "archived"):
            for marker in ({}, False, 0, ""):
                with self.subTest(key=key, marker=marker):
                    response = session_info()
                    if key == "revert":
                        response["data"]["revert"] = marker
                    else:
                        response["data"]["time"]["archived"] = marker
                    self.refuse(
                        payload(),
                        "session-" + key,
                        native=NativeRequests(sessions=[response]),
                    )

    def test_just_before_send_recheck_refuses_changed_scope_revert_or_archive(self):
        for state, category in (
            ("location", "session-scope"),
            ("revert", "session-revert"),
            ("archived", "session-archived"),
            ("identity", "session-unavailable"),
        ):
            with self.subTest(state=state):
                changed = session_info()
                if state == "location":
                    changed["data"]["location"]["directory"] = DIRECTORY + "/other"
                elif state == "revert":
                    changed["data"]["revert"] = {"messageID": "msg_staged"}
                elif state == "identity":
                    changed["data"]["id"] = "ses_replaced"
                else:
                    changed["data"]["time"]["archived"] = 1
                native = self.refuse(
                    payload(),
                    category,
                    native=NativeRequests(sessions=[session_info(), changed]),
                )
                self.assertEqual(
                    [(method, path) for method, path, _ in native.calls],
                    [
                        ("GET", "/api/info"),
                        ("GET", "/api/session/" + SESSION),
                        ("GET", "/api/session/" + SESSION),
                    ],
                )
                self.assertTrue(all(body is None for _, _, body in native.calls))

    def test_check_cannot_smuggle_prompt_and_send_requires_exact_prompt_shape(self):
        value = payload("check")
        value["prompt"] = payload()["prompt"]
        self.refuse(value)
        for prompt in (None, [], {}, {"text": "incomplete"}):
            with self.subTest(prompt=prompt):
                self.refuse({**payload(), "prompt": prompt})
        for key in ("id", "text", "files", "delivery", "resume"):
            with self.subTest(missing=key):
                value = payload()
                del value["prompt"][key]
                self.refuse(value)
        value = payload()
        value["prompt"]["agent"] = "build"
        self.refuse(value)

    def test_prompt_id_text_delivery_and_resume_are_strict_and_bounded(self):
        for key, values in (
            ("id", (None, "", "msg_", "msg/escape", "msg_é", 1, "msg_" + "x" * 126)),
            ("text", (None, [], 123, "x" * 16385, "é" * 8193)),
            ("delivery", (None, "immediate", "queue\n", True)),
            ("resume", (None, 0, 1, "true", [], {})),
        ):
            for index, bad in enumerate(values):
                with self.subTest(key=key, case=index):
                    value = payload()
                    value["prompt"][key] = bad
                    self.refuse(value)

    def test_file_shape_has_no_remote_url_file_uri_path_or_non_inline_source(self):
        bad_files = [
            None,
            {},
            [],
            [None],
            [payload()["prompt"]["files"][0]] * 2,
        ]
        original = payload()["prompt"]["files"][0]
        for key, values in (
            (
                "uri",
                (
                    None,
                    1,
                    "file:///synthetic/host/image.png",
                    "https://example.invalid/image.png",
                    "/synthetic/host/image.png",
                    "data:text/plain;base64,eA==",
                    "data:image/svg+xml;base64,eA==",
                    "data:image/png,eA==",
                    "data:image/png;base64,",
                    "data:image/png;base64,%%%",
                    "data:image/png;base64,eA==\n",
                    "data:image/png;base64,é",
                ),
            ),
            (
                "name",
                (
                    None,
                    1,
                    "../host-image.png",
                    "/host-image.png",
                    "host-image.svg",
                    "host-image.jpg",
                    "host-image.png\n",
                ),
            ),
            (
                "source",
                (
                    None,
                    {},
                    {"type": "file"},
                    {"type": "inline", "path": "/synthetic/host/image.png"},
                ),
            ),
        ):
            for bad in values:
                bad_files.append([{**original, key: bad}])
        for key in ("uri", "name", "source"):
            bad = dict(original)
            del bad[key]
            bad_files.append([bad])
        bad_files.append([{**original, "path": "/synthetic/host/image.png"}])
        for index, files in enumerate(bad_files):
            with self.subTest(case=index):
                value = payload()
                value["prompt"]["files"] = files
                self.refuse(value)

    def test_image_byte_limit_accepts_exact_boundary_and_refuses_one_byte_over(self):
        for size in (8 * 1024 * 1024, 8 * 1024 * 1024 + 1):
            with self.subTest(size=size):
                value = payload()
                value["prompt"]["files"][0]["uri"] = (
                    "data:image/png;base64," + base64.b64encode(b"x" * size).decode()
                )
                if size > 8 * 1024 * 1024:
                    self.refuse(value)
                else:
                    native = NativeRequests()
                    self.assertEqual(
                        attachment_io.deliver(native, value),
                        {"ok": True, "message": MESSAGE},
                    )
                    self.assertEqual(len(native.posts), 1)

    def test_queue_and_explicit_resume_forward_once_after_immediate_target_recheck(
        self,
    ):
        for resume in (False, True):
            for extension in ("png", "jpeg", "gif", "webp"):
                with self.subTest(resume=resume, extension=extension):
                    value = payload(resume=resume)
                    value["prompt"]["text"] = "é" * 8192
                    image = value["prompt"]["files"][0]
                    image["name"] = "host-image." + extension
                    image["uri"] = image["uri"].replace(
                        "image/png", "image/" + extension
                    )
                    native = NativeRequests()
                    self.assertEqual(
                        attachment_io.deliver(native, value),
                        {"ok": True, "message": MESSAGE},
                    )
                    self.assertEqual(
                        native.calls,
                        [
                            ("GET", "/api/info", None),
                            ("GET", "/api/session/" + SESSION, None),
                            ("GET", "/api/session/" + SESSION, None),
                            (
                                "POST",
                                "/api/session/" + SESSION + "/prompt",
                                value["prompt"],
                            ),
                        ],
                    )
                    self.assertIs(native.posts[0][2]["resume"], resume)

    def test_wrong_acknowledgements_are_unverified_after_exactly_one_post(self):
        replies = [None, [], {}, {"data": None}, acknowledgement()["data"]]
        for key, values in (
            ("id", (None, "msg_other")),
            ("sessionID", (None, "ses_other")),
            ("type", (None, "assistant")),
            ("delivery", (None, "immediate")),
            (
                "payload",
                (None, {}, {"files": None}, {"files": []}, {"files": [{}, {}]}),
            ),
        ):
            for bad in values:
                reply = acknowledgement()
                reply["data"][key] = bad
                replies.append(reply)
        replies.extend(
            (
                TimeoutError("synthetic timeout after admission"),
                OSError(MALICIOUS.decode()),
                attachment_io.Refused("session-unavailable"),
            )
        )
        for index, reply in enumerate(replies):
            with self.subTest(case=index):
                native = NativeRequests(reply=reply)
                with self.assertRaises(attachment_io.Refused) as raised:
                    attachment_io.deliver(native, payload())
                self.assertEqual(str(raised.exception), "delivery-unverified")
                self.assertEqual(len(native.posts), 1)
                self.assertEqual(len(native.calls), 4)


@dataclass
class HTTPReply:
    body: bytes
    status: int = 200
    headers: dict = field(default_factory=dict)
    delay: float = 0
    chunks: tuple = ()
    interval: float = 0


class LoopbackNative:
    """One bounded fixture thread, private loopback socket, no native service."""

    def __init__(self, responder=None):
        self.responder = responder
        self.requests = []
        self.errors = []
        self.release = threading.Event()
        self.session_reads = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                self.connection.settimeout(1)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= attachment_io.MAX_BODY:
                    owner.errors.append("request-bound")
                    return
                body = self.rfile.read(length)
                record = {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
                owner.requests.append(record)
                try:
                    reply = owner.responder(record) if owner.responder else None
                    if reply is None:
                        reply = owner.default_reply(record)
                    if reply.delay and owner.release.wait(reply.delay):
                        return
                    self.send_response(reply.status)
                    self.send_header(
                        "Content-Length",
                        str(
                            sum(map(len, reply.chunks))
                            if reply.chunks
                            else len(reply.body)
                        ),
                    )
                    for key, value in reply.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    for chunk in reply.chunks or (reply.body,):
                        if reply.interval and owner.release.wait(reply.interval):
                            return
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except Exception:
                    # Preserve a fixed fixture failure, never raw native errors.
                    owner.errors.append("handler-failure")

        class Server(HTTPServer):
            def handle_error(self, *_):
                owner.errors.append("server-failure")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

    def default_reply(self, record):
        if (record["method"], record["path"]) == ("GET", "/api/info"):
            value = {"version": "2.2.6"}
        elif record["method"] == "GET" and record["path"] == "/api/session/" + SESSION:
            self.session_reads += 1
            value = session_info()
        elif record["method"] == "POST" and record["path"] == (
            "/api/session/" + SESSION + "/prompt"
        ):
            value = acknowledgement()
        else:
            self.errors.append("unexpected-request")
            return HTTPReply(b"", status=404)
        return HTTPReply(json.dumps(value).encode())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("Synthetic loopback fixture did not stop")
        if self.errors:
            raise AssertionError("Synthetic loopback fixture failed: " + self.errors[0])


class AttachmentHelperProcessTests(unittest.TestCase):
    def run_helper(self, url, value, *, limits=None, raw=None):
        # Use the normal package import, including for bounded limit overrides.
        # No source rewriting or duplicate runtime module loads are needed.
        if limits:
            program = "from oc_box import attachment_io as helper\n"
            for name, limit in limits.items():
                if name not in ("MAX_BODY", "MAX_REPLY", "BUDGET"):
                    raise AssertionError("Unexpected fixture limit")
                program += f"helper.{name} = {limit!r}\n"
            program += "raise SystemExit(helper.main())\n"
            command = [sys.executable, "-B", "-c", program, url]
        else:
            command = [sys.executable, "-B", "-m", "oc_box.attachment_io", url]
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(attachment_io.__file__).resolve().parents[1]),
            "OPENCODE_PASSWORD": PASSWORD,
        }
        self.last_command, self.last_environment = command, environment
        data = json.dumps(value).encode() if raw is None else raw
        with tempfile.TemporaryDirectory(prefix="attachment-io-fixture-") as directory:
            with subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                cwd=directory,
            ) as child:
                self.child = child
                try:
                    stdout, stderr = child.communicate(data, timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    self.fail("Synthetic attachment helper exceeded its outer deadline")
                result = subprocess.CompletedProcess(
                    command, child.returncode, stdout, stderr
                )
            self.assertEqual(list(Path(directory).iterdir()), [])
        return result

    def assert_result(self, result, category=None):
        expected = (
            {"ok": False, "category": category}
            if category
            else {"ok": True, "message": MESSAGE}
        )
        self.assertEqual(result.returncode, 1 if category else 0)
        self.assertEqual(result.stdout, (json.dumps(expected) + "\n").encode())
        self.assertEqual(result.stderr, b"")
        return json.loads(result.stdout)

    def test_real_helper_authenticates_and_image_bytes_travel_only_over_stdin(self):
        observed = {}

        def responder(record):
            if not observed:
                root = Path("/proc") / str(self.child.pid)
                observed["argv"] = (root / "cmdline").read_bytes()
                observed["env"] = (root / "environ").read_bytes()
            return None

        value = payload(resume=True)
        with LoopbackNative(responder) as server:
            result = self.run_helper(server.url, value)
        self.assert_result(result)
        self.assertEqual(len(server.requests), 4)
        expected_auth = (
            "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
        )
        self.assertTrue(
            all(
                request["authorization"] == expected_auth for request in server.requests
            )
        )
        self.assertTrue(all(not request["body"] for request in server.requests[:-1]))
        self.assertEqual(json.loads(server.requests[-1]["body"]), value["prompt"])
        encoded = base64.b64encode(PNG)
        for location in (
            observed["argv"],
            observed["env"],
            json.dumps(self.last_command).encode(),
            json.dumps(self.last_environment).encode(),
            result.stdout,
            result.stderr,
        ):
            self.assertNotIn(encoded, location)
            self.assertNotIn(b"data:image/", location)
            self.assertNotIn(b"/synthetic/host/image.png", location)
        self.assertNotIn(PASSWORD.encode(), observed["argv"])
        self.assertEqual(self.last_environment["OPENCODE_PASSWORD"], PASSWORD)

    def test_recheck_on_real_transport_sends_no_prompt_bytes_after_scope_or_revert_change(
        self,
    ):
        for state in ("location", "revert"):
            with self.subTest(state=state):
                count = 0

                def responder(record):
                    nonlocal count
                    if record["path"] == "/api/session/" + SESSION:
                        count += 1
                        if count == 2:
                            changed = session_info()
                            if state == "location":
                                changed["data"]["location"]["directory"] += "/changed"
                            else:
                                changed["data"]["revert"] = {"messageID": "msg_staged"}
                            return HTTPReply(json.dumps(changed).encode())
                    return None

                with LoopbackNative(responder) as server:
                    result = self.run_helper(server.url, payload())
                self.assert_result(
                    result, "session-scope" if state == "location" else "session-revert"
                )
                self.assertEqual(count, 2)
                self.assertEqual(len(server.requests), 3)
                self.assertTrue(all(r["method"] == "GET" for r in server.requests))
                self.assertTrue(all(r["body"] == b"" for r in server.requests))

    def test_http_no_body_malformed_and_wrong_admission_reply_never_retry_post(self):
        wrong = acknowledgement()
        wrong["data"]["sessionID"] = "ses_other"
        for name, reply in (
            ("http-error", HTTPReply(MALICIOUS, status=500)),
            ("no-content", HTTPReply(b"", status=204)),
            ("empty-success", HTTPReply(b"")),
            ("malformed", HTTPReply(MALICIOUS)),
            ("wrong-identity", HTTPReply(json.dumps(wrong).encode())),
        ):
            with self.subTest(case=name):

                def responder(record):
                    return reply if record["method"] == "POST" else None

                with LoopbackNative(responder) as server:
                    result = self.run_helper(server.url, payload())
                self.assert_result(result, "delivery-unverified")
                self.assertEqual(
                    [r["method"] for r in server.requests],
                    ["GET", "GET", "GET", "POST"],
                )
                self.assertNotIn(MALICIOUS, result.stdout + result.stderr)

    def test_connection_refusal_has_fixed_output_and_no_traceback(self):
        # Reserve a loopback port without listening, avoiding races with a
        # different test binding a port released before the child connects.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            url = "http://127.0.0.1:" + str(reservation.getsockname()[1])
            for operation, category in (
                ("check", "session-unavailable"),
                ("send", "delivery-unverified"),
            ):
                with self.subTest(operation=operation):
                    result = self.run_helper(url, payload(operation))
                    self.assert_result(result, category)

    def test_request_body_bound_is_inclusive_and_oversize_never_contacts_service(self):
        limit = 512
        raw = json.dumps(payload("check")).encode()
        self.assertLess(len(raw), limit)
        with LoopbackNative() as server:
            result = self.run_helper(
                server.url,
                None,
                raw=raw.ljust(limit, b" "),
                limits={"MAX_BODY": limit},
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'{"ok": true}\n')
        self.assertEqual(result.stderr, b"")
        self.assertEqual(len(server.requests), 2)
        with LoopbackNative() as server:
            result = self.run_helper(
                server.url,
                None,
                raw=raw.ljust(limit + 1, b" "),
                limits={"MAX_BODY": limit},
            )
        self.assert_result(result, "invalid-request")
        self.assertEqual(server.requests, [])

    def test_reply_bound_is_inclusive_and_oversize_post_reply_is_unverified(self):
        limit = 512
        for extra in (0, 1):
            with self.subTest(extra=extra):

                def responder(record):
                    if record["method"] == "POST":
                        body = json.dumps(acknowledgement()).encode()
                        return HTTPReply(body.ljust(limit + extra, b" "))
                    return None

                with LoopbackNative(responder) as server:
                    result = self.run_helper(
                        server.url, payload(), limits={"MAX_REPLY": limit}
                    )
                self.assert_result(result, "delivery-unverified" if extra else None)
                self.assertEqual(len(server.requests), 4)
                self.assertEqual(sum(r["method"] == "POST" for r in server.requests), 1)
        with LoopbackNative(
            lambda _: HTTPReply(
                json.dumps({"version": "2.2.6"}).encode().ljust(limit + 1)
            )
        ) as server:
            result = self.run_helper(
                server.url, payload("check"), limits={"MAX_REPLY": limit}
            )
        self.assert_result(result, "session-unavailable")
        self.assertEqual(len(server.requests), 1)

    def test_post_response_timeout_is_unverified_and_does_not_repeat_admission(self):
        def responder(record):
            if record["method"] == "POST":
                return HTTPReply(json.dumps(acknowledgement()).encode(), delay=1)
            return None

        with LoopbackNative(responder) as server:
            result = self.run_helper(server.url, payload(), limits={"BUDGET": 0.15})
        self.assert_result(result, "delivery-unverified")
        self.assertEqual(
            [r["method"] for r in server.requests], ["GET", "GET", "GET", "POST"]
        )

    def test_continuously_dribbled_reply_cannot_extend_total_request_budget(self):
        reply = HTTPReply(
            b"",
            chunks=(b" ",) * 50,
            interval=0.03,
        )
        with LoopbackNative(lambda _: reply) as server:
            result = self.run_helper(
                server.url, payload("check"), limits={"BUDGET": 0.15}
            )
        self.assert_result(result, "session-unavailable")
        self.assertEqual(len(server.requests), 1)

    def test_redirect_is_refused_without_following_location(self):
        with LoopbackNative(
            lambda _: HTTPReply(
                MALICIOUS,
                status=302,
                headers={"Location": "http://example.invalid/synthetic-target"},
            )
        ) as server:
            result = self.run_helper(server.url, payload("check"))
        self.assert_result(result, "session-unavailable")
        self.assertEqual(len(server.requests), 1)

    def test_connection_factory_rejects_non_private_or_credential_bearing_urls(self):
        for address, password in (
            ("https://127.0.0.1:8080", PASSWORD),
            ("http://localhost:8080", PASSWORD),
            ("http://0.0.0.0:8080", PASSWORD),
            ("http://example.invalid:8080", PASSWORD),
            ("http://user:password@127.0.0.1:8080", PASSWORD),
            ("http://127.0.0.1", PASSWORD),
            ("http://127.0.0.1:8080/api", PASSWORD),
            ("http://127.0.0.1:8080/?token=synthetic", PASSWORD),
            ("http://127.0.0.1:8080/#fragment", PASSWORD),
            ("http://127.0.0.1:8080", ""),
        ):
            with (
                self.subTest(address=address),
                patch.object(attachment_io, "HTTPConnection") as connection,
            ):
                with self.assertRaises(attachment_io.Refused) as raised:
                    attachment_io.connection(address, password)
                self.assertEqual(str(raised.exception), "invalid-request")
                connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
