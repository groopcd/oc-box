"""Linux process-hygiene checks; no native OpenCode, Podman or host services."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import smoke_history_recovery_v2 as fixture


class SubreaperTests(unittest.TestCase):
    def libc(self, previous, *, fail_enable=False, fail_restore=False):
        state = SimpleNamespace(value=previous, writes=[])

        def prctl(option, value, *_):
            if option == fixture.PR_GET_CHILD_SUBREAPER:
                value._obj.value = state.value
                return 0
            self.assertEqual(option, fixture.PR_SET_CHILD_SUBREAPER)
            state.writes.append(value)
            state.value = value
            return (
                -1
                if (
                    fail_enable
                    and len(state.writes) == 1
                    or fail_restore
                    and len(state.writes) == 2
                )
                else 0
            )

        return state, SimpleNamespace(prctl=Mock(side_effect=prctl))

    def test_previous_flag_restored_after_success_and_exceptions(self):
        for previous in (0, 1):
            for failure in (None, RuntimeError("synthetic"), KeyboardInterrupt()):
                with self.subTest(previous=previous, failure=type(failure).__name__):
                    state, libc = self.libc(previous)
                    with patch.object(fixture.ctypes, "CDLL", return_value=libc):
                        if failure is None:
                            with fixture.subreaper():
                                self.assertEqual(state.value, 1)
                        else:
                            with self.assertRaises(type(failure)) as caught:
                                with fixture.subreaper():
                                    self.assertEqual(state.value, 1)
                                    raise failure
                            self.assertIs(caught.exception, failure)
                    self.assertEqual(state.value, previous)
                    self.assertEqual(state.writes, [1, previous])

    def test_enable_failure_still_restores_known_prior_flag(self):
        state, libc = self.libc(0, fail_enable=True)
        with (
            patch.object(fixture.ctypes, "CDLL", return_value=libc),
            self.assertRaisesRegex(fixture.FixtureFailure, "enable fixture subreaper"),
        ):
            with fixture.subreaper():
                self.fail("Failed subreaper setup admitted work.")
        self.assertEqual(state.value, 0)
        self.assertEqual(state.writes, [1, 0])

    def test_main_restores_umask_even_after_exercise_or_flag_cleanup_failure(self):
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure):
                state, libc = self.libc(0, fail_restore=cleanup_failure)
                previous = os.umask(0o027)
                try:
                    with (
                        tempfile.TemporaryDirectory(
                            prefix="native-recovery-state-"
                        ) as root,
                        patch.object(fixture.ctypes, "CDLL", return_value=libc),
                        patch.object(
                            fixture,
                            "exercise",
                            side_effect=RuntimeError("synthetic-private-sentinel"),
                        ),
                        self.assertRaises(fixture.FixtureFailure) as raised,
                    ):
                        fixture.main("synthetic-opencode", temp_dir=root)
                    self.assertNotIn(
                        "synthetic-private-sentinel", str(raised.exception)
                    )
                    self.assertEqual(os.umask(0o027), 0o027)
                    self.assertEqual(state.writes, [1, 0])
                finally:
                    os.umask(previous)

    def test_non_linux_refuses_before_changing_process_state(self):
        with (
            patch.object(fixture.sys, "platform", "unsupported"),
            patch.object(fixture.ctypes, "CDLL") as library,
            self.assertRaisesRegex(fixture.FixtureFailure, "requires Linux"),
        ):
            with fixture.subreaper():
                self.fail("Non-Linux fixture entered.")
        library.assert_not_called()


@unittest.skipUnless(
    sys.platform == "linux"
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal")
    and hasattr(os, "P_PIDFD"),
    "Requires Linux Python pidfd bindings.",
)
class BoundShellTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-recovery-pidfd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        script, ready = self.root / "held.py", self.root / "ready"
        script.write_text(
            "import pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1]).touch()\n"
            "time.sleep(20)\n"
        )
        self.argv = [sys.executable, "-I", "-S", str(script), str(ready)]
        self.child = subprocess.Popen(
            self.argv,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while not ready.exists():
            self.assertIsNone(self.child.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.pidfile = self.root / "pid"
        self.pidfile.write_text(str(self.child.pid))
        # This test process is the actual parent of the controlled Python child.
        self.producer = SimpleNamespace(pid=os.getpid(), poll=lambda: None)

    def stop(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=5)

    def bind(self, **changes):
        arguments = {
            "pidfile": self.pidfile,
            "argv": self.argv,
            "project": self.root,
            "producer": self.producer,
        }
        return fixture.bound_shell(**{**arguments, **changes})

    def test_live_owned_child_is_bound_stopped_reaped_and_descriptor_closed(self):
        with self.bind() as descriptor:
            fixture.stop_bound_shell(descriptor)
            with self.assertRaises(ChildProcessError):
                os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_wrong_command_cwd_or_ancestry_never_opens_or_signals_pidfd(self):
        for changes in (
            {"argv": [*self.argv, "unexpected"]},
            {"project": self.root.parent},
            {"producer": SimpleNamespace(pid=self.child.pid, poll=lambda: None)},
        ):
            with self.subTest(changes=tuple(changes)):
                with (
                    patch.object(fixture.os, "pidfd_open") as open_fd,
                    patch.object(fixture.signal, "pidfd_send_signal") as send,
                    self.assertRaises(fixture.FixtureFailure),
                ):
                    with self.bind(**changes):
                        self.fail("Unowned shell was bound.")
                open_fd.assert_not_called()
                send.assert_not_called()
                self.assertIsNone(self.child.poll())

    def test_identity_change_during_binding_closes_fd_without_signalling(self):
        descriptors = []
        real_open = os.pidfd_open

        def open_fd(*args):
            descriptor = real_open(*args)
            descriptors.append(descriptor)
            return descriptor

        with (
            patch.object(
                fixture,
                "shell_identity",
                side_effect=[
                    ((os.getpid(), 1), 2),
                    ((os.getpid(), 3), 2),
                ],
            ),
            patch.object(fixture.os, "pidfd_open", side_effect=open_fd),
            patch.object(fixture.signal, "pidfd_send_signal") as send,
            self.assertRaisesRegex(fixture.FixtureFailure, "identity changed"),
        ):
            with self.bind():
                self.fail("Reused PID was accepted.")
        send.assert_not_called()
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_closed_descriptor_refuses_without_numeric_pid_signal_fallback(self):
        descriptor = os.pidfd_open(self.child.pid)
        os.close(descriptor)
        with (
            patch.object(
                fixture.os, "kill", side_effect=AssertionError("bare PID signal")
            ),
            patch.object(fixture.os, "waitid") as wait,
            self.assertRaisesRegex(fixture.FixtureFailure, "bound synthetic shell"),
        ):
            fixture.stop_bound_shell(descriptor)
        wait.assert_not_called()
        self.assertIsNone(self.child.poll())

    def test_already_reaped_bound_process_never_signals_a_numeric_replacement(self):
        with self.bind() as descriptor:
            self.child.kill()
            self.child.wait(timeout=5)
            with patch.object(
                fixture.os, "kill", side_effect=AssertionError("bare PID signal")
            ):
                fixture.stop_bound_shell(descriptor)

    def test_invalid_or_disappeared_pid_refuses_without_signalling(self):
        for value in ("0", "1", "-1", "unknown", "9" * 11, str(2**31 - 1)):
            with self.subTest(value=value):
                self.pidfile.write_text(value)
                with (
                    patch.object(fixture.signal, "pidfd_send_signal") as send,
                    self.assertRaises(fixture.FixtureFailure),
                ):
                    with self.bind():
                        self.fail("Invalid or absent process was accepted.")
                send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
