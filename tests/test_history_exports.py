"""Focused pipe/loopback checks; no native OpenCode, Podman or user state."""

import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from oc_box import history_reader as reader, history_recovery, oc_box as box
from oc_box import (
    lifecycle_output,
    opencode_v2_io,
    workspace_control,
    workspace_service,
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.calls.append(self.path)
        route = urlparse(self.path)
        pieces = route.path.split("/")
        if self.headers.get("Authorization") != self.server.authorization:
            self.send_error(403)
            return
        if route.path in self.server.redirects:
            status, target = self.server.redirects[route.path]
            self.send_response(status)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route.path == "/api/session":
            value = {
                "data": [s["info"] for s in self.server.sessions.values()],
                "cursor": {},
            }
        elif route.path.endswith("/message"):
            messages = (
                self.server.projected_prefix
                + self.server.sessions[pieces[3]]["messages"]
            )
            offset = 100 if parse_qs(route.query).get("cursor") else 0
            value = {
                "data": messages[offset : offset + 100],
                "cursor": {"next": "next" if len(messages) > offset + 100 else None},
            }
        elif route.path.endswith("/export"):
            value = {"data": self.server.sessions[pieces[4]]}
        else:
            self.send_error(404)
            return
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HistoryExportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(
            target=lambda: cls.server.serve_forever(poll_interval=0.01), daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="history-exports-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "state")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data.update(
            opencode_version="2.3.2", home=str(self.root / "home"), container="a" * 64
        )
        self.remaining = 30
        self.connection = {
            "url": "http://127.0.0.1:" + str(self.server.server_port),
            "password": "synthetic-worker-password",
            "workspace": self.data["id"],
        }
        self.server.authorization = (
            "Basic " + base64.b64encode(b"opencode:synthetic-worker-password").decode()
        )
        self.server.calls, self.server.sessions, self.server.redirects = [], {}, {}
        self.server.projected_prefix = []
        self.lease = reader.Reader(box, self.data, remaining=lambda: self.remaining)
        self.lease.connection = self.connection
        self.lease.child = SimpleNamespace(poll=lambda: None)
        self.lease._container_command = self.local_command
        for variable, value in (
            (reader.CURRENT, self.lease),
            (workspace_service.CONNECTION, self.connection),
        ):
            token = variable.set(value)
            self.addCleanup(variable.reset, token)

    def test_quiet_bridge_refuses_redirects_from_inventory_projection_export_and_activity(
        self,
    ):
        ident = "ses_quiet"
        unsettled = {
            "id": "msg_superseded",
            "type": "assistant",
            "time": {"created": 1},
        }
        source = (box.PACKAGE / "opencode_v2_io.py").read_text()
        endpoint = {key: self.connection[key] for key in ("url", "password")}
        projection = f"/api/session/{ident}/message"
        export = f"/api/experimental/session/{ident}/export"
        for path, action, code in (
            ("/api/session", "sessions", 301),
            (projection, "export", 302),
            (export, "export", 303),
            (export, "export", 304),
            (export, "export", 307),
            ("/api/session/active", "export", 308),
        ):
            for target in (
                "/untrusted",
                f"http://localhost:{self.server.server_port}/untrusted",
            ):
                with self.subTest(path=path, code=code, target=target):
                    self.server.calls.clear()
                    self.server.redirects = {path: (code, target)}
                    self.server.sessions[ident] = self.export(ident)
                    # The superseded turn is absent from the export, so the
                    # final activity check also uses the no-redirect opener.
                    self.server.projected_prefix = (
                        [unsettled] if path == "/api/session/active" else []
                    )
                    result = self.quiet_client(source, endpoint, action, ident)
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse(result.stdout)
                    self.assertEqual(
                        opencode_v2_io.fixed_message(result.stderr),
                        opencode_v2_io.ENDPOINT,
                    )
                    self.assertEqual(urlparse(self.server.calls[-1]).path, path)
                    self.assertNotIn("/untrusted", self.server.calls)

    def quiet_client(self, source, endpoint, action, ident):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                source,
                "quiet-history-" + action,
                "/must-not-start-native",
            ],
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            input=json.dumps(
                {
                    "directory": str(self.project),
                    "endpoint": endpoint,
                    "session": ident,
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_bridge_emits_only_fixed_pending_reason_for_unfinished_tail(self):
        ident = "ses_quiet"
        self.server.sessions[ident] = self.export(ident)
        self.server.sessions[ident]["messages"].append(
            {
                "id": "msg_synthetic_unfinished",
                "type": "assistant",
                "time": {"created": 1},
                "text": "synthetic-message-must-not-be-exposed",
            }
        )
        source = (box.PACKAGE / "opencode_v2_io.py").read_text()
        endpoint = {key: self.connection[key] for key in ("url", "password")}
        result = self.quiet_client(source, endpoint, "export", ident)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)
        self.assertEqual(
            opencode_v2_io.fixed_message(result.stderr), opencode_v2_io.UNSETTLED
        )
        self.assertEqual(opencode_v2_io.pending_reason(result.stderr), "assistant-tail")
        self.assertNotIn(ident, result.stderr)
        self.assertNotIn("synthetic-message-must-not-be-exposed", result.stderr)
        self.assertFalse(any("/export" in call for call in self.server.calls))

    def test_quiet_redirect_retains_verified_checkpoint_without_replaying(self):
        sessions = {
            ident: self.export(ident)
            for ident in ("ses_verified", "ses_refused", "ses_not_reached")
        }
        self.server.sessions = sessions
        self.server.redirects = {
            "/api/experimental/session/ses_refused/export": (302, "/untrusted")
        }
        self.data["opencode_version"] = "2.4.1"
        box.atomic_json(self.path, self.data)
        endpoint = {key: self.connection[key] for key in ("url", "password")}
        source = (box.PACKAGE / "opencode_v2_io.py").read_text()

        def read_command(_data, action):
            return ["quiet-history-" + action]

        def run(command, **kwargs):
            if command[0].startswith("quiet-history-"):
                return self.quiet_client(
                    source,
                    endpoint,
                    command[0].removeprefix("quiet-history-"),
                    json.loads(kwargs["input"]).get("session"),
                )
            self.assertIn("import", command)
            imports.append(command)
            return subprocess.CompletedProcess(command, 0)

        imports = []
        binding = box._QuietSyncBinding(self.data["container"], (), {})
        binding.start_verified = True
        for variable, value in (
            (reader.CURRENT, None),
            (box._QUIET_SYNC_BINDING, binding),
        ):
            token = variable.set(value)
            self.addCleanup(variable.reset, token)
        with (
            patch.object(
                workspace_service,
                "quiet_history_source",
                return_value=SimpleNamespace(read_command=read_command),
            ),
            patch.object(box, "exec_command") as ordinary,
            patch.object(box, "private_host_env", return_value={}),
            patch.object(box, "run", side_effect=run),
            patch.object(box, "verify_host_archive") as host_verify,
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(box.BoxError, "Invalid private OpenCode") as refused,
        ):
            box.sync_sessions(self.path, self.data)
        self.assertTrue(refused.exception.history_contract_refusal)
        ordinary.assert_not_called()
        host_verify.assert_called_once()
        self.assertEqual(len(imports), 1)
        self.assertEqual(set(box.session_state(self.path)["synced"]), {"ses_verified"})
        self.assertEqual(len(list((self.path.parent / "history").glob("*.json"))), 1)
        self.assertFalse(
            any(
                "untrusted" in item or "not_reached" in item
                for item in self.server.calls
            )
        )
        self.assertNotIn("Session history synced", output.getvalue())

    def local_command(self, arguments):
        # Replace only the container transport; keep Reader's command allowlist
        # and isolated remote command wrapper plus the real bridge worker.
        return box.ContainerCommand(
            [sys.executable, *arguments[1:]],
            {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "C.UTF-8"},
        )

    def export(self, ident, count=1):
        return {
            "info": {
                "id": ident,
                "title": ident,
                "location": {"directory": str(self.project)},
                "time": {"created": 1, "updated": 1},
            },
            "messages": [
                {
                    "id": f"msg_{n:04}",
                    "type": "user",
                    "text": "é\u2028 synthetic",
                    "time": {"created": n},
                }
                for n in range(count)
            ],
        }

    def test_one_helper_keeps_checked_pagination_pending_neighbors_and_serial_checkpoints(
        self,
    ):
        self.server.sessions = {
            "ses_first": self.export("ses_first", 101),
            "ses_pending": self.export("ses_pending"),
            "ses_last": self.export("ses_last"),
        }
        self.server.sessions["ses_pending"]["messages"].append(
            {
                "id": "msg_pending",
                "type": "assistant",
                "time": {"created": 2},
            }
        )
        popen = subprocess.Popen
        with (
            patch.object(reader.subprocess, "Popen", wraps=popen) as processes,
            patch.object(box, "private_host_env", return_value={}),
            patch.object(box, "verify_host_archive") as verify,
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, '{"data":[],"cursor":{}}'
                ),
            ) as destination,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            for attempt in range(3):
                if attempt == 2:
                    self.server.sessions["ses_last"]["info"]["title"] = (
                        "Changed without timestamp change"
                    )
                with self.assertRaisesRegex(
                    box.BoxError, "1 session\\(s\\) not settled"
                ):
                    box.sync_sessions(self.path, self.data)
                self.assertIsNone(reader.EXPORTS.get())
                state = box.session_state(self.path)
                self.assertEqual(set(state["synced"]), {"ses_first", "ses_last"})
                self.assertEqual(verify.call_count, (2, 2, 3)[attempt])
            self.assertEqual(processes.call_count, 3)
            self.assertEqual(
                sum("import" in c.args[0] for c in destination.call_args_list), 3
            )
            self.assertTrue(
                all(
                    self.connection["password"] not in repr(c.args[0])
                    for c in processes.call_args_list
                )
            )
        self.assertIn("unchanged=2, updated=0, pending=1", output.getvalue())
        self.assertNotIn(
            "Session history synced to your normal host", output.getvalue()
        )
        self.assertEqual(len(self.server.calls), 3 * 7)

    def test_stop_admission_is_rechecked_before_next_request_without_replay(self):
        self.server.sessions = {"ses_one": self.export("ses_one")}
        stop, refusal = [False], history_recovery.RecoveryError("stop-requested")

        @contextlib.contextmanager
        def admitted():
            if stop[0]:
                raise refusal
            yield

        with patch.object(history_recovery, "admit", side_effect=admitted):
            with self.assertRaises(history_recovery.RecoveryError) as raised:
                with reader.export_pass(box, self.data):
                    box.export_container_session(self.data, "ses_one")
                    stop[0] = True
                    box.export_container_session(self.data, "ses_one")
        self.assertIs(raised.exception, refusal)
        self.assertEqual(len(self.server.calls), 2)
        self.assertIsNone(reader.EXPORTS.get())

    def test_deadline_and_workspace_changes_refuse_without_new_requests(self):
        self.server.sessions = {"ses_one": self.export("ses_one")}
        for change in ("deadline", "workspace"):
            with self.subTest(change=change):
                self.remaining = 30
                with self.assertRaises(box.BoxError):
                    with reader.export_pass(box, self.data):
                        box.export_container_session(self.data, "ses_one")
                        before = len(self.server.calls)
                        if change == "deadline":
                            self.remaining = 0
                            box.export_container_session(self.data, "ses_one")
                        else:
                            box.export_container_session(
                                {**self.data, "id": "foreign"}, "ses_one"
                            )
                self.assertEqual(len(self.server.calls), before)
                self.assertIsNone(reader.EXPORTS.get())

    def test_malformed_or_extra_replies_abort_without_restart_or_private_error_text(
        self,
    ):
        cases = (
            (b'{"id":true,"size":2}\n[]', False),
            (b'{"id":1,"error":"private-sentinel"}\n', False),
            (b'{"id":1,"size":2}\n[]extra', False),
            (b'{"id":1,"size":2}\n[]', True),
        )
        for response, after_eof in cases:
            with self.subTest(response=response, after_eof=after_eof):
                script = (
                    "import sys,json\nsys.stdin.readline()\n"
                    "print(json.dumps({'protocol':'oc-box.history/1'}),flush=True)\n"
                    "sys.stdin.readline()\nsys.stdout.buffer.write("
                    + repr(response)
                    + ")\n"
                    "sys.stdout.buffer.flush()\nsys.stdin.read()\n"
                )
                if after_eof:
                    script += (
                        "sys.stdout.buffer.write(b'extra'); sys.stdout.buffer.flush()\n"
                    )
                command = box.ContainerCommand(
                    [sys.executable, "-I", "-S", "-c", script],
                    {"PATH": os.environ["PATH"]},
                )
                popen = subprocess.Popen
                with (
                    patch.object(self.lease, "command", return_value=command),
                    patch.object(reader.subprocess, "Popen", wraps=popen) as processes,
                    contextlib.redirect_stderr(io.StringIO()) as errors,
                    self.assertRaises(box.BoxError) as raised,
                ):
                    with reader.export_pass(box, self.data):
                        box.list_container_sessions(self.data)
                self.assertEqual(processes.call_count, 1)
                self.assertNotIn(
                    "private-sentinel", str(raised.exception) + errors.getvalue()
                )
                self.assertIsNone(reader.EXPORTS.get())

    def test_cleanup_interruption_reaps_and_preserves_nonzero_interruption(self):
        interruption = SystemExit(0)
        stream = reader._Exports(box, self.lease, self.data)
        stream.child = SimpleNamespace(
            pid=123456,
            stdin=Mock(),
            stdout=Mock(),
            poll=Mock(return_value=None),
            wait=Mock(return_value=0),
        )
        stream.child.stdin.close.side_effect = interruption
        child = stream.child
        with patch.object(reader.select, "select", return_value=([], [], [])):
            with self.assertRaises(SystemExit) as raised:
                stream.close()
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.code, 1)
        child.wait.assert_called_once_with(timeout=10)
        child.stdout.close.assert_called_once()
        self.assertIsNone(stream.child)

    def test_concise_double_failure_retains_original_and_marks_unverified_cleanup(self):
        originals = (
            lambda: history_recovery.RecoveryError("deadline"),
            lambda: workspace_control.ControlError("unsafe-control"),
            lambda: box.HistoryPending("synthetic-original"),
            lambda: RuntimeError("synthetic-original"),
            lambda: SystemExit(0),
        )
        for original in originals:
            for error in (
                box.BoxError("synthetic-cleanup"),
                KeyboardInterrupt(),
                SystemExit(0),
            ):
                primary = original()
                winner = (
                    error
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else primary
                )
                with (
                    self.subTest(primary=type(primary), cleanup=type(error)),
                    patch.object(reader, "_Exports") as stream_type,
                    lifecycle_output.invocation("sync", self.path.parent),
                    contextlib.redirect_stderr(io.StringIO()) as errors,
                    self.assertRaises(type(winner)) as raised,
                ):
                    stream_type.return_value.close.side_effect = error
                    with reader.export_pass(box, self.data):
                        raise primary
                self.assertIs(raised.exception, winner)
                self.assertTrue(winner.history_source_cleanup_failed)
                if isinstance(winner, SystemExit):
                    self.assertEqual(winner.code, 1)
                self.assertEqual(errors.getvalue(), "")
                stream_type.return_value.close.assert_called_once_with()
                self.assertIsNone(reader.EXPORTS.get())

    def test_non_reader_pass_does_not_create_helper(self):
        token = reader.CURRENT.set(None)
        try:
            with patch.object(reader.subprocess, "Popen") as process:
                with reader.export_pass(box, self.data):
                    self.assertIsNone(reader.EXPORTS.get())
            process.assert_not_called()
        finally:
            reader.CURRENT.reset(token)
