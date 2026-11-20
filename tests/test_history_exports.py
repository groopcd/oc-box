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
from oc_box import lifecycle_output, workspace_control, workspace_service


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
        if route.path == "/api/session":
            value = {
                "data": [s["info"] for s in self.server.sessions.values()],
                "cursor": {},
            }
        elif route.path.endswith("/message"):
            messages = self.server.sessions[pieces[3]]["messages"]
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
        self.server.calls, self.server.sessions = [], {}
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
