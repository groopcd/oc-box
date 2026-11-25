"""Focused failures for the guarded, one-shot host native writer."""

import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from oc_box import history_overlap_archive as archive
from oc_box import history_overlap_writer as writer
from oc_box.runtime_safeguard import HostBinary


VERSION = "2.4.1"
TARGET = "ses_writer_synthetic"
SECRET = "synthetic-transcript-not-for-diagnostics"


def deadline():
    return time.monotonic() + 5


class WireTests(unittest.TestCase):
    def response(self, *, status=b"200 OK", headers=b"", body=b"{}", length=None):
        length = len(body) if length is None else length
        return (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: application/json\r\nConnection: close\r\n"
            b"Content-Length: "
            + str(length).encode()
            + b"\r\n"
            + headers
            + b"\r\n"
            + body
        )

    def decode(self, wire, **options):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(wire)
            right.shutdown(socket.SHUT_WR)
            return writer._Exchange(
                left, 1234, "synthetic-password", deadline()
            ).receive(close=True, **options)

    def test_strict_http_response(self):
        self.assertEqual(self.decode(self.response())[::2], (200, {}))
        invalid = (
            self.response(
                status=b"302 Found", headers=b"Location: http://127.0.0.1:99/\r\n"
            ),
            self.response(headers=b"Content-Length: 2\r\n"),
            self.response(headers=b"Transfer-Encoding: chunked\r\n"),
            self.response(headers=b"Set-Cookie: token=unexpected\r\n"),
            self.response(body=b'{"data":1,"data":2}'),
            self.response(body=b'{"data":NaN}'),
            self.response(length=12),
            self.response(length=1),
            self.response() + b"x",
            self.response(headers=b"X-Long: " + b"a" * writer._MAX_HEADER + b"\r\n"),
        )
        for wire in invalid:
            with (
                self.subTest(wire_prefix=wire[:40]),
                self.assertRaises(writer.WriterRefusal),
            ):
                self.decode(wire)

    def test_export_bound_is_separate_and_still_requires_exact_close_framing(self):
        body = json.dumps({"data": "x" * (64 * 1024)}).encode()
        wire = self.response(body=body)
        with self.assertRaises(writer.WriterRefusal):
            self.decode(wire)
        self.assertEqual(
            self.decode(wire, max_body=archive.MAX_ARCHIVE_BYTES)[2],
            json.loads(body),
        )
        for invalid in (
            self.response(length=archive.MAX_ARCHIVE_BYTES + 1),
            self.response(length=1000000000),
            self.response(body=b'{"data":true', length=20),
            self.response(
                status=b"302 Found", headers=b"Location: http://127.0.0.1:99/\r\n"
            ),
            wire + b"unexpected",
        ):
            with (
                self.subTest(wire_prefix=invalid[:45]),
                self.assertRaises(writer.WriterRefusal),
            ):
                self.decode(invalid, max_body=archive.MAX_ARCHIVE_BYTES)


class EnvironmentTests(unittest.TestCase):
    def test_private_temp_is_a_managed_home_sibling_with_unchanged_selectors(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data_home = root / "data"
            for index, database in enumerate(
                (None, "oc-box-v2.db", str(data_home / "opencode" / "explicit.db"))
            ):
                with self.subTest(database=database):
                    scratch = root / f"scratch-{index}"
                    scratch.mkdir(mode=0o700)
                    env = writer.writer_environment(
                        scratch,
                        data_home,
                        VERSION,
                        database=database,
                        disable_channel_db="1",
                    )
                    self.assertEqual(env["XDG_DATA_HOME"], str(data_home))
                    self.assertEqual(env.get("OPENCODE_DB"), database)
                    self.assertEqual(env["OPENCODE_DISABLE_CHANNEL_DB"], "1")
                    self.assertEqual(env["OPENCODE_FLAGS"], "-machines")
                    self.assertEqual(Path(env["TMPDIR"]), scratch / "tmp")
                    self.assertEqual(Path(env["HOME"]), scratch / "home")
                    self.assertEqual(env["OPENCODE_TEST_HOME"], env["HOME"])
                    self.assertCountEqual(
                        (path.name for path in scratch.iterdir()),
                        ("home", "config", "state", "cache", "runtime", "tmp"),
                    )
                    for path in scratch.iterdir():
                        self.assertTrue(path.is_dir())
                        self.assertEqual(path.stat().st_mode & 0o077, 0)
                    self.assertEqual(
                        writer._checked_environment(env, VERSION, deadline()), env
                    )


class OperationTests(unittest.TestCase):
    def enter(self, manager):
        return self.stack.enter_context(manager)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.enter(tempfile.TemporaryDirectory()))
        self.cwd = self.root / "controller"
        self.cwd.mkdir(mode=0o700)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir(mode=0o700)
        self.env = writer.writer_environment(self.scratch, self.root / "data", VERSION)
        self.host = HostBinary(10, (), writer._STOCK[VERSION])
        self.child_factory = self.enter(patch.object(writer, "_Child", autospec=True))
        self.child = self.child_factory.return_value
        self.child.port.return_value = 1234
        self.child.retire.return_value = True
        self.ready = self.enter(patch.object(writer, "_ready"))
        raw_socket = self.enter(patch.object(writer.socket, "socket"))
        self.stream = raw_socket.return_value.__enter__.return_value
        self.stream.getsockname.return_value = ("127.0.0.1", 4567)
        self.exchange = self.enter(
            patch.object(writer, "_Exchange", autospec=True)
        ).return_value
        self.exchange.get.return_value = (
            401,
            {b"www-authenticate": b'Basic realm="Secure Area"'},
            {"_tag": "UnauthorizedError", "message": "Authentication required"},
        )
        self.exchange.receive.return_value = (
            200,
            {},
            {"data": {"id": TARGET, "location": {"directory": str(self.cwd)}}},
        )
        self.archive = {
            "info": {"id": TARGET, "location": {"directory": "/synthetic/original"}},
            "messages": [{"id": "msg_once", "type": "user", "text": SECRET}],
        }

    def create(self, *, target=TARGET, gate=contextlib.nullcontext, raw=None):
        with archive.sealed_candidate(
            [json.dumps(self.archive).encode() if raw is None else raw]
        ) as candidate:
            return writer.create_once(
                self.host,
                VERSION,
                self.env,
                self.cwd,
                candidate,
                target,
                gate=gate,
                deadline=deadline(),
            )

    def readback(self, *, target=TARGET, gate=contextlib.nullcontext):
        return writer.readback_once(
            self.host,
            VERSION,
            self.env,
            self.cwd,
            target,
            gate=gate,
            deadline=deadline(),
        )

    def test_single_admitted_dispatch_and_verified_retirement(self):
        events = []

        @contextlib.contextmanager
        def gate():
            events.append("enter")
            yield
            events.append("exit")

        self.child.start.side_effect = lambda *args: events.append("spawn")
        self.child.check.side_effect = lambda *args: events.append("identity")
        self.exchange.send.side_effect = lambda *args: events.append("write")
        self.child.retire.side_effect = lambda: events.append("retire") or True
        with patch.dict(
            os.environ, {"HTTP_PROXY": SECRET, "OPENCODE_PASSWORD": SECRET}
        ):
            self.assertEqual(self.create(gate=gate), TARGET)
        self.assertEqual(
            events,
            [
                "enter",
                "spawn",
                "exit",
                "identity",
                "enter",
                "identity",
                "write",
                "exit",
                "write",
                "identity",
                "retire",
            ],
        )
        self.assertEqual(self.exchange.header.call_count, 1)
        method, route = self.exchange.header.call_args.args
        self.assertEqual((method, route), ("POST", "/api/experimental/session/import"))
        body = self.exchange.send.call_args.args[0]
        self.assertEqual(
            len(body), self.exchange.header.call_args.kwargs["content_length"]
        )
        self.assertEqual(
            json.loads(body), {**self.archive, "location": {"directory": str(self.cwd)}}
        )
        child_env = self.child.start.call_args.args[1]
        self.assertEqual(
            {k: v for k, v in child_env.items() if k != "OPENCODE_PASSWORD"}, self.env
        )
        self.assertNotEqual(child_env["OPENCODE_PASSWORD"], SECRET)
        self.ready.assert_called_once_with(
            self.exchange, self.child, VERSION, self.env["TMPDIR"]
        )
        self.stream.connect.assert_called_once_with(("127.0.0.1", 1234))

    def test_gate_revoked_after_readiness_prevents_post(self):
        calls = 0

        @contextlib.contextmanager
        def gate():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError(SECRET)
            yield

        with self.assertRaises(writer.WriterRefusal) as failure:
            self.create(gate=gate)
        self.assertEqual(failure.exception.category, "admission")
        self.assertFalse(failure.exception.history_create_attempted)
        self.assertNotIn(SECRET, str(failure.exception))
        self.exchange.send.assert_not_called()
        self.child.retire.assert_called_once_with()

    def test_wrong_candidate_or_environment_never_starts_native(self):
        with self.assertRaises(writer.WriterRefusal) as wrong:
            self.create(target="ses_wrong")
        self.assertEqual(wrong.exception.category, "candidate")
        with self.assertRaises(writer.WriterRefusal):
            self.create(raw=b'{"info":{},"info":{},"messages":[]}')
        self.archive["info"]["parentID"] = "ses_foreign"
        with self.assertRaises(writer.WriterRefusal):
            self.create()
        self.env["HTTPS_PROXY"] = SECRET
        with self.assertRaises(writer.WriterRefusal) as env:
            self.create()
        self.assertEqual(env.exception.category, "binding")
        self.child.start.assert_not_called()

    def test_broad_or_unbound_temp_refuses_before_native_start(self):
        for tmpdir in (
            self.scratch,
            self.scratch / "home",
            self.root / "other" / "tmp",
        ):
            with (
                self.subTest(tmpdir=tmpdir),
                patch.dict(self.env, {"TMPDIR": str(tmpdir)}),
            ):
                with self.assertRaises(writer.WriterRefusal) as failure:
                    self.create()
                self.assertEqual(failure.exception.category, "binding")
        self.child.start.assert_not_called()

    def test_private_parent_and_managed_children_must_be_real_and_private(self):
        tmp = self.scratch / "tmp"
        tmp.rmdir()
        tmp.symlink_to(self.scratch / "home", target_is_directory=True)
        with self.assertRaises(writer.WriterRefusal) as linked:
            self.create()
        self.assertEqual(linked.exception.category, "binding")
        tmp.unlink()
        tmp.mkdir(mode=0o700)

        config = self.scratch / "config"
        config.chmod(0o750)
        with self.assertRaises(writer.WriterRefusal) as public:
            self.create()
        self.assertEqual(public.exception.category, "binding")
        config.chmod(0o700)

        alias = self.root / "alias"
        alias.symlink_to(self.scratch, target_is_directory=True)
        # A completely consistent lexical layout does not authorize even
        # a symlink to the very same private scratch directory.
        self.env = writer._environment(alias, self.env["XDG_DATA_HOME"], VERSION)
        with self.assertRaises(writer.WriterRefusal) as parent:
            self.create()
        self.assertEqual(parent.exception.category, "binding")
        self.child.start.assert_not_called()

    def test_no_secret_post_when_listener_auth_or_identity_not_proven(self):
        self.exchange.get.return_value = (200, {}, {})
        with self.assertRaises(writer.WriterRefusal) as failure:
            self.create()
        self.assertEqual(failure.exception.category, "identity")
        self.ready.assert_not_called()
        self.exchange.get.assert_called_once_with("/api/info", authenticated=False)
        self.exchange.send.assert_not_called()

    def test_exact_native_conflict_is_never_an_acknowledgement(self):
        self.exchange.receive.return_value = (
            409,
            {},
            {
                "_tag": "ConflictError",
                "message": "Session already exists: " + TARGET,
                "resource": TARGET,
            },
        )
        with self.assertRaises(writer.WriterRefusal) as failure:
            self.create()
        self.assertEqual(failure.exception.category, "conflict")
        self.assertTrue(failure.exception.history_create_attempted)
        self.assertFalse(failure.exception.history_write_uncertain)
        self.assertFalse(failure.exception.history_destination_cleanup_failed)
        self.exchange.header.assert_called_once()

    def test_wrong_target_redirect_unbound_conflict_and_timeout_are_uncertain(self):
        replies = (
            (
                200,
                {},
                {
                    "data": {
                        "id": "ses_other_one",
                        "location": {"directory": str(self.cwd)},
                    }
                },
            ),
            (302, {}, {}),
            (409, {}, {"_tag": "ConflictError", "resource": "ses_other"}),
            (500, {}, {}),
            TimeoutError(SECRET),
        )
        for reply in replies:
            with self.subTest(reply_type=type(reply)):
                self.exchange.reset_mock()
                if isinstance(reply, Exception):
                    self.exchange.receive.side_effect = reply
                else:
                    self.exchange.receive.return_value = reply
                with self.assertRaises(writer.WriterRefusal) as failure:
                    self.create()
                self.assertEqual(failure.exception.category, "unacknowledged")
                self.assertTrue(failure.exception.history_write_uncertain)
                self.assertNotIn(SECRET, str(failure.exception))
                self.exchange.header.assert_called_once()

    def test_acknowledgement_cannot_waive_retirement_and_interrupt_keeps_status(self):
        self.child.retire.return_value = False
        with self.assertRaises(writer.WriterRefusal) as retirement:
            self.create()
        self.assertEqual(retirement.exception.category, "cleanup")
        self.assertTrue(retirement.exception.history_write_uncertain)
        self.assertTrue(retirement.exception.history_destination_cleanup_failed)
        self.exchange.receive.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt) as interrupted:
            self.create()
        self.assertTrue(interrupted.exception.history_create_attempted)
        self.assertTrue(interrupted.exception.history_write_uncertain)
        self.assertTrue(interrupted.exception.history_destination_cleanup_failed)

    def test_sigint_during_retirement_does_not_skip_reaping_evidence(self):
        def retire():
            signal.raise_signal(signal.SIGINT)
            return True

        self.child.retire.side_effect = retire
        with self.assertRaises(KeyboardInterrupt) as interrupted:
            self.create()
        self.assertTrue(interrupted.exception.history_write_uncertain)
        self.assertFalse(interrupted.exception.history_destination_cleanup_failed)

    def test_readback_uses_a_fresh_child_and_one_admitted_exact_get(self):
        self.assertEqual(self.create(), TARGET)
        self.child.retire.assert_called_once_with()
        old_env = self.child.start.call_args.args[1]
        self.exchange.reset_mock()

        fresh = Mock()
        self.child_factory.return_value = fresh
        fresh.port.return_value = 1234
        fresh.retire.return_value = True
        self.exchange.receive.return_value = (200, {}, {"data": self.archive})
        events = []

        @contextlib.contextmanager
        def gate():
            events.append("enter")
            yield
            events.append("exit")

        fresh.start.side_effect = lambda *args: events.append("spawn")
        self.exchange.send.side_effect = lambda *args: events.append("dispatch")
        fresh.retire.side_effect = lambda: events.append("retire") or True
        self.assertEqual(self.readback(gate=gate), self.archive)
        self.assertEqual(
            events, ["enter", "spawn", "exit", "enter", "dispatch", "exit", "retire"]
        )
        self.assertEqual(self.child_factory.call_count, 2)
        self.child.retire.assert_called_once_with()
        fresh.retire.assert_called_once_with()
        new_env = fresh.start.call_args.args[1]
        self.assertNotEqual(new_env["OPENCODE_PASSWORD"], old_env["OPENCODE_PASSWORD"])
        self.assertEqual(
            {k: v for k, v in new_env.items() if k != "OPENCODE_PASSWORD"}, self.env
        )
        self.assertEqual(fresh.start.call_args.args[2], str(self.cwd))
        self.exchange.header.assert_called_once_with(
            "GET", "/api/experimental/session/" + TARGET + "/export", close=True
        )
        self.exchange.send.assert_called_once_with(self.exchange.header.return_value)
        self.exchange.receive.assert_called_once_with(
            close=True, max_body=archive.MAX_ARCHIVE_BYTES
        )
        self.ready.assert_called_with(self.exchange, fresh, VERSION, self.env["TMPDIR"])

    def test_readback_refuses_route_injection_and_revoked_dispatch(self):
        for target in ("ses", "ses_/../../api/session", "ses_a?secret=1", "ses_a\r\n"):
            with (
                self.subTest(target=target),
                self.assertRaises(writer.WriterRefusal) as failure,
            ):
                self.readback(target=target)
            self.assertEqual(failure.exception.category, "binding")
        self.child.start.assert_not_called()
        self.exchange.send.assert_not_called()

        calls = 0

        @contextlib.contextmanager
        def gate():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError(SECRET)
            yield

        with self.assertRaises(writer.WriterRefusal) as failure:
            self.readback(gate=gate)
        self.assertEqual(failure.exception.category, "admission")
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertFalse(failure.exception.history_destination_cleanup_failed)
        self.child.start.assert_called_once()
        self.exchange.header.assert_not_called()

    def test_readback_proves_exact_id_and_never_waives_cleanup(self):
        exact = {"data": self.archive}
        wrong = {**self.archive, "info": {"id": "ses_other"}}
        malformed = {**self.archive, "messages": {}}
        for status, export in (
            (302, exact),
            (404, exact),
            (200, {"data": wrong}),
            (200, {"data": malformed}),
            (200, {**exact, "extra": True}),
        ):
            with self.subTest(status=status):
                self.exchange.receive.return_value = (status, {}, export)
                with self.assertRaises(writer.WriterRefusal) as failure:
                    self.readback()
                self.assertEqual(failure.exception.category, "readback")
                self.assertFalse(failure.exception.history_destination_cleanup_failed)
                self.assertFalse(failure.exception.history_create_attempted)

        self.exchange.receive.return_value = (200, {}, exact)
        self.child.retire.side_effect = OSError(SECRET)
        with self.assertRaises(writer.WriterRefusal) as cleanup:
            self.readback()
        self.assertEqual(cleanup.exception.category, "cleanup")
        self.assertTrue(cleanup.exception.history_destination_cleanup_failed)
        self.assertFalse(cleanup.exception.history_write_uncertain)
        self.assertNotIn(SECRET, str(cleanup.exception))

        self.exchange.receive.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt) as interrupted:
            self.readback()
        self.assertTrue(interrupted.exception.history_destination_cleanup_failed)
        self.assertFalse(interrupted.exception.history_create_attempted)


class ChildRetirementTests(unittest.TestCase):
    def start(self, code):
        child = writer._Child()
        child.process = subprocess.Popen(
            [sys.executable, "-IBS", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            start_new_session=True,
        )
        child.pidfd = os.pidfd_open(child.process.pid)
        os.set_blocking(child.process.stdout.fileno(), False)
        self.addCleanup(
            lambda: child.retire() if child.process.returncode is None else None
        )
        return child

    def test_requires_direct_wait_and_clean_eof(self):
        for code, expected, status in (
            ("import sys; sys.stdin.read()", True, 0),
            ("import sys; sys.stdin.read(); print('extra')", False, 0),
            ("import sys; sys.stdin.read(); sys.exit(3)", False, 3),
            ("import time; time.sleep(120)", False, -signal.SIGKILL),
        ):
            with self.subTest(expected=expected, status=status):
                child = self.start(code)
                with patch.object(writer, "_EOF_GRACE", 0.3):
                    self.assertIs(child.retire(), expected)
                self.assertEqual(child.process.returncode, status)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(child.process.pid, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
