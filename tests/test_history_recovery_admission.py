"""Local subprocess ownership around the history admission gate."""

import contextlib
import io
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import history_recovery as recovery
from oc_box import oc_box


_PYTHON = [sys.executable, "-I", "-S", "-B", "-c"]
_HELD = [*_PYTHON, "import time; time.sleep(20)"]


def fail_on_exit(error):
    @contextlib.contextmanager
    def gate():
        yield
        raise error

    return gate


class AdmittedProcessTests(unittest.TestCase):
    def test_wrapped_wait_uncertainty_is_reported_and_vetoes_reader_retry(self):
        timeout = subprocess.TimeoutExpired("synthetic", 1)
        error = oc_box.BoxError("History source read failed.")
        error.__cause__ = timeout
        error.history_source_read_failed = True
        for release in ("2.3.0", "2.4.1"):
            with self.subTest(release=release):
                data = {"opencode_version": release}
                timeout.history_local_cleanup_failed = False
                self.assertTrue(recovery.exit_retry_eligible(oc_box, data, error))
                timeout.history_local_cleanup_failed = True
                self.assertFalse(recovery.exit_retry_eligible(oc_box, data, error))
                self.assertEqual(
                    oc_box._history_failure_reason(error), "cleanup-unverified"
                )

    def record_subprocesses(self, on_spawn=lambda _child: None):
        popen = subprocess.Popen
        self.children = []

        def cleanup(child):
            if child.returncode is None:
                child.kill()
                child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            self.children.append(child)
            self.addCleanup(cleanup, child)
            on_spawn(child)
            return child

        return patch.object(recovery.subprocess, "Popen", side_effect=spawn)

    def assert_reaped(self, child):
        self.assertEqual(child.returncode, -signal.SIGKILL)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        for stream in (child.stdin, child.stdout, child.stderr):
            self.assertTrue(stream is None or stream.closed)

    def run_held(self, gate, **options):
        return recovery.run_admitted(
            _HELD,
            gate,
            cwd="/",
            env={},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )

    def test_gate_exit_error_or_interruption_reaps_the_local_process(self):
        for original in (RuntimeError("gate exit"), KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(error=type(original).__name__):
                stderr = io.StringIO()
                with (
                    self.record_subprocesses() as popen,
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(type(original)) as raised,
                ):
                    self.run_held(fail_on_exit(original))
                self.assertIs(raised.exception, original)
                self.assertFalse(
                    getattr(original, "history_local_cleanup_failed", False)
                )
                self.assertEqual(stderr.getvalue(), "")
                popen.assert_called_once()
                self.assert_reaped(self.children[0])

    def test_sigint_between_child_creation_and_handle_assignment_is_deferred(self):
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        events = []

        def interrupt(_child):
            signal.raise_signal(signal.SIGINT)
            events.append("constructor returned")

        @contextlib.contextmanager
        def gate():
            try:
                yield
            except KeyboardInterrupt:
                events.append("gate saw interrupt")
                raise

        with (
            self.record_subprocesses(interrupt),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.run_held(gate)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertEqual(events, ["constructor returned", "gate saw interrupt"])
        self.assertFalse(
            getattr(raised.exception, "history_local_cleanup_failed", False)
        )
        self.assert_reaped(self.children[0])

    def test_second_sigint_during_cleanup_does_not_replace_original(self):
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        original = RuntimeError("gate exit")
        killpg = os.killpg

        def interrupt_cleanup(pid, sig):
            signal.raise_signal(signal.SIGINT)
            killpg(pid, sig)

        with (
            self.record_subprocesses(),
            patch.object(recovery.os, "killpg", side_effect=interrupt_cleanup),
            self.assertRaises(RuntimeError) as raised,
        ):
            self.run_held(fail_on_exit(original))
        self.assertIs(raised.exception, original)
        self.assertFalse(getattr(original, "history_local_cleanup_failed", False))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assert_reaped(self.children[0])

    def test_normal_gate_keeps_process_arguments_and_communicates_after_release(self):
        command = [
            *_PYTHON,
            "import os, sys; assert os.getpid() == os.getsid(0);"
            "sys.stdout.buffer.write(sys.stdin.buffer.read());"
            "print(os.environ['SYNTHETIC'], file=sys.stderr); sys.exit(7)",
        ]
        events = []

        @contextlib.contextmanager
        def gate():
            events.append("enter")
            yield
            events.append("release")

        def track(child):
            events.append("spawn")
            communicate = child.communicate

            def after_release(**kwargs):
                self.assertEqual(events, ["enter", "spawn", "release"])
                self.assertEqual(kwargs, {"input": b"synthetic", "timeout": 3})
                return communicate(**kwargs)

            child.communicate = after_release

        options = {
            "env": {"SYNTHETIC": "bounded"},
            "cwd": "/",
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "start_new_session": False,
        }
        with self.record_subprocesses(track) as popen:
            result = recovery.run_admitted(
                command, gate, input=b"synthetic", timeout=3, **options
            )
        popen.assert_called_once_with(
            command, **{**options, "stdin": subprocess.PIPE, "start_new_session": True}
        )
        self.assertIs(result.args, command)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (7, b"synthetic", b"bounded\n"),
        )

    def test_communication_timeout_reaps_and_preserves_timeout(self):
        with (
            self.record_subprocesses(),
            self.assertRaises(subprocess.TimeoutExpired) as raised,
        ):
            self.run_held(contextlib.nullcontext, timeout=0.02)
        self.assertEqual(raised.exception.timeout, 0.02)
        self.assertFalse(
            getattr(raised.exception, "history_local_cleanup_failed", False)
        )
        self.assert_reaped(self.children[0])

    def test_gate_entry_refusal_and_normal_spawn_error_have_no_owned_child(self):
        original = RuntimeError("gate entry")

        @contextlib.contextmanager
        def gate():
            raise original
            yield

        with (
            patch.object(recovery.subprocess, "Popen") as popen,
            self.assertRaises(RuntimeError) as raised,
        ):
            self.run_held(gate)
        self.assertIs(raised.exception, original)
        self.assertFalse(getattr(original, "history_local_cleanup_failed", False))
        popen.assert_not_called()

        missing = FileNotFoundError("synthetic executable not found")
        with (
            patch.object(recovery.subprocess, "Popen", side_effect=missing),
            self.assertRaises(FileNotFoundError) as raised,
        ):
            self.run_held(contextlib.nullcontext)
        self.assertIs(raised.exception, missing)
        self.assertFalse(getattr(missing, "history_local_cleanup_failed", False))

    def test_interrupt_in_constructor_without_handle_reports_uncertainty(self):
        original = KeyboardInterrupt()
        with (
            patch.object(recovery.subprocess, "Popen", side_effect=original),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.run_held(contextlib.nullcontext)
        self.assertIs(raised.exception, original)
        self.assertTrue(original.history_local_cleanup_failed)
        self.assertIn("Local command cleanup could not be verified", stderr.getvalue())

    def test_unverified_wait_does_not_signal_or_claim_remote_cleanup(self):
        original = RuntimeError("gate exit")
        child = SimpleNamespace(
            pid=1234, returncode=0, stdin=Mock(), stdout=Mock(), stderr=Mock()
        )
        with (
            patch.object(recovery.subprocess, "Popen", return_value=child),
            patch.object(
                recovery.os,
                "waitpid",
                side_effect=ChildProcessError("private diagnostic"),
            ),
            patch.object(recovery.os, "killpg") as kill,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(RuntimeError) as raised,
        ):
            self.run_held(fail_on_exit(original))
        self.assertIs(raised.exception, original)
        self.assertTrue(original.history_local_cleanup_failed)
        self.assertFalse(hasattr(original, "history_source_cleanup_failed"))
        self.assertFalse(hasattr(original, "history_destination_cleanup_failed"))
        self.assertIn("Local command cleanup could not be verified", stderr.getvalue())
        self.assertNotIn("private diagnostic", stderr.getvalue())
        kill.assert_not_called()
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close.assert_called_once_with()

    def test_expired_local_reap_budget_preserves_the_original_interrupt(self):
        original = KeyboardInterrupt()
        child = SimpleNamespace(
            pid=1234, returncode=None, stdin=None, stdout=None, stderr=None
        )
        with (
            patch.object(recovery.subprocess, "Popen", return_value=child),
            patch.object(recovery.os, "waitpid", return_value=(0, 0)),
            patch.object(recovery.os, "killpg") as kill,
            patch.object(recovery.time, "monotonic", side_effect=[10, 15]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.run_held(fail_on_exit(original))
        self.assertIs(raised.exception, original)
        self.assertTrue(original.history_local_cleanup_failed)
        self.assertIn("Local command cleanup could not be verified", stderr.getvalue())
        self.assertIsNone(child.returncode)
        kill.assert_called_once_with(child.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
