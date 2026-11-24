"""OCOR transport tests with a pinned synthetic pipe peer, not cryptography."""

import errno
import hashlib
import os
from pathlib import Path
import signal
import sys
import threading
import unittest
from contextlib import contextmanager
from time import monotonic
from unittest.mock import patch

from oc_box import private_memory
from oc_box import web_pake_worker as pipe
from oc_box import web_registration_worker as worker

from test_web_pake_worker import _memfd


_PEER = r"""
import errno, os, resource, signal, stat, sys, time

mode = sys.argv[1]
assert not open("/proc/self/environ", "rb").read()
assert os.getcwd() == "/"
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
assert stat.S_ISFIFO(os.fstat(0).st_mode) and stat.S_ISFIFO(os.fstat(1).st_mode)
assert os.fstat(2).st_rdev == os.stat("/dev/null").st_rdev
for entry in os.listdir("/proc/self/fd"):
    if int(entry) > 2:
        try:
            os.fstat(int(entry))
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("unexpected inherited descriptor")
os.write(2, b"discardable diagnostic")

frame = sys.stdin.buffer.read(231)  # Requires EOF on any valid request.
profile = bytes.fromhex("f71544a382ec1b3d1280e8a13ba9413d705e8dd62b68724cecd6e008f74f122c")
command = frame[5:6]
header = b"OCOR\x01" + command + profile
assert frame[:38] == header
if command == b"\x01":
    assert frame[38:] == b""
    payload = b"S" * 128
elif command == b"\x02":
    assert frame[38:] == b"S" * 128 + b"I" * 32 + b"B" * 32
    payload = b"R" * 64
else:
    assert command == b"\x03" and frame[38:] == b"U" * 192
    payload = b"U" * 192
reply = header + b"\x00" + payload
replies = {
    "wrong_magic": b"OCOP" + reply[4:],
    "wrong_version": reply[:4] + b"\x02" + reply[5:],
    "wrong_operation": reply[:5] + b"\x00" + reply[6:],
    "wrong_profile": reply[:37] + bytes([reply[37] ^ 1]) + reply[38:],
    "bad_status": header + b"\xff",
    "refused": header + b"\x01",
    "refused_with_payload": header + b"\x01" + payload,
    "short_header": reply[:37],
    "short_payload": reply[:-1],
    "long_payload": reply + b"x",
    "empty": b"",
    "changed_record": reply[:-1] + b"x",
}
if mode == "fragment":
    os.write(1, reply[:7])
    time.sleep(.015)
    os.write(1, reply[7:40])
    time.sleep(.015)
    os.write(1, reply[40:])
elif mode != "wait":
    os.write(1, replies.get(mode, reply))
if mode == "nonzero":
    sys.exit(7)
if mode == "signalled":
    os.kill(os.getpid(), signal.SIGTERM)
if mode == "late_junk":
    time.sleep(.025)
    os.write(1, b"x")
if mode == "no_exit":
    os.close(1)
if mode in ("wait", "no_exit", "no_eof"):
    time.sleep(10)  # The host must interrupt and reap this fixture.
    sys.exit(9)
"""

_INPUT = dict(
    setup=b"S" * 128, credential_identifier=b"I" * 32, registration_request=b"B" * 32
)


@unittest.skipUnless(sys.platform == "linux", "Linux-only OCOR transport")
class RegistrationWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (
            hasattr(os, "memfd_create")
            and hasattr(os, "pidfd_open")
            and hasattr(signal, "pidfd_send_signal")
            and os.execve in os.supports_fd
        ):
            raise unittest.SkipTest("Linux descriptor exec/pidfds unavailable")
        cls._argv0 = os.readlink("/proc/self/exe")
        cls._executable = Path("/proc/self/exe").read_bytes()
        cls._digest = hashlib.sha256(cls._executable).digest()
        cls._fd = _memfd(cls._executable)
        cls.addClassCleanup(os.close, cls._fd)
        private_memory.seal(cls._fd)

    def _backend(self, *, fd=None, timeout=0.6):
        result = worker.RegistrationWorker(
            self._fd if fd is None else fd,
            expected_sha256=self._digest,
            io_timeout=timeout,
        )
        self.addCleanup(result.close)
        return result

    @contextmanager
    def _peer(self, mode, spawned=None):
        children = []
        original = pipe._spawn

        def capture(fd):
            child = original(fd)
            children.append(child)
            if spawned is not None:
                spawned.set()
            return child

        with (
            patch.object(
                pipe,
                "_VERIFIER_ARGV",
                (self._argv0, "-I", "-S", "-B", "-c", _PEER, mode),
            ),
            patch.object(pipe, "_spawn", side_effect=capture),
        ):
            yield children

    def _reaped(self, children, count):
        self.assertEqual(len(children), count)
        for child in children:
            self.assertIs(type(child.returncode), int)
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def _refused(self):
        return self.assertRaisesRegex(
            worker.RegistrationError, r"^Web registration worker refused\.$"
        )

    def test_exact_operations_use_private_pipes_and_reap_before_returning(self):
        backend = self._backend()
        with open("/dev/null", "rb") as unrelated:
            os.set_inheritable(unrelated.fileno(), True)
            with (
                patch.dict(
                    os.environ, {"OC_BOX_TEST_AMBIENT": "synthetic not for child"}
                ),
                self._peer("fragment") as children,
            ):
                self.assertEqual(backend.generate_setup(), b"S" * 128)
                self._reaped(children, 1)
                self.assertEqual(backend.respond(**_INPUT), b"R" * 64)
                self._reaped(children, 2)
                self.assertEqual(backend.validate_upload(b"U" * 192), b"U" * 192)
                self._reaped(children, 3)
        self.assertTrue(backend.close())

    def test_pins_exact_digest_and_borrowed_descriptor_is_independent(self):
        with patch.object(pipe, "_spawn") as spawn:
            with self._refused():
                worker.RegistrationWorker(self._fd, expected_sha256=b"?" * 32)
            unsealed = _memfd(self._executable)
            try:
                with self._refused():
                    worker.RegistrationWorker(unsealed, expected_sha256=self._digest)
                private_memory.seal(unsealed)
                backend = self._backend(fd=unsealed)
            finally:
                os.close(unsealed)
            spawn.assert_not_called()
        with self._peer("success") as children:
            self.assertEqual(backend.generate_setup(), b"S" * 128)
            self._reaped(children, 1)

    def test_invalid_widths_deadlines_and_foreign_reaping_refuse_before_spawn(self):
        backend = self._backend()
        with patch.object(pipe, "_spawn") as spawn:
            for field in _INPUT:
                for bad in (b"", b"x" * 4096, bytearray(_INPUT[field])):
                    with self.subTest(field=field, bad_type=type(bad)), self._refused():
                        backend.respond(**{**_INPUT, field: bad})
            for upload in (b"U" * 191, b"U" * 193, bytearray(b"U" * 192)):
                with self._refused():
                    backend.validate_upload(upload)
            for deadline in (False, monotonic() - 0.1, float("nan"), float("inf")):
                with self._refused():
                    backend.generate_setup(deadline=deadline)
            with (
                patch.object(signal, "getsignal", return_value=signal.SIG_IGN),
                self._refused(),
            ):
                backend.generate_setup()
            spawn.assert_not_called()

    def test_every_reply_requires_exact_header_status_body_eof_and_clean_exit(self):
        backend = self._backend(timeout=0.3)
        for mode in (
            "wrong_magic",
            "wrong_version",
            "wrong_operation",
            "wrong_profile",
            "bad_status",
            "refused",
            "refused_with_payload",
            "short_header",
            "short_payload",
            "long_payload",
            "empty",
            "late_junk",
            "nonzero",
            "signalled",
            "no_eof",
            "no_exit",
        ):
            with self.subTest(mode=mode), self._peer(mode) as children:
                with self._refused():
                    backend.generate_setup()
                self._reaped(children, 1)
        with self._peer("changed_record") as children:
            with self._refused():
                backend.validate_upload(b"U" * 192)
            self._reaped(children, 1)

    def test_caller_deadline_cancels_the_same_child_without_retry(self):
        backend = self._backend(timeout=3)
        with self._peer("wait") as children:
            with self._refused():
                backend.generate_setup(deadline=monotonic() + 0.15)
            self._reaped(children, 1)
            self.assertEqual(children[0].returncode, -signal.SIGKILL)

    def test_close_cancels_the_only_active_slot_and_cannot_restart(self):
        backend = self._backend(timeout=3)
        spawned = threading.Event()
        outcomes = []

        def invoke():
            try:
                outcomes.append(backend.generate_setup())
            except worker.RegistrationError as error:
                outcomes.append(type(error))

        with self._peer("wait", spawned) as children:
            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            try:
                self.assertTrue(spawned.wait(timeout=2))
                with self._refused():
                    backend.respond(**_INPUT)
            finally:
                self.assertTrue(backend.close())
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, [worker.RegistrationError])
            with self._refused():
                backend.generate_setup()
            self._reaped(children, 1)

    def test_unverified_wait_fences_and_close_rechecks_only_the_original_child(self):
        backend = self._backend()
        with self._peer("success") as children:
            with patch.object(
                os, "waitpid", side_effect=ChildProcessError(errno.ECHILD, "do not log")
            ):
                with self.assertRaisesRegex(
                    worker.RegistrationCleanupError,
                    r"^Web registration worker cleanup could not be verified\.$",
                ):
                    backend.generate_setup()
                with self.assertRaises(worker.RegistrationCleanupError):
                    backend.respond(**_INPUT)
                self.assertFalse(backend.close())
                self.assertEqual(len(children), 1)
            self.assertTrue(backend.close())
            self._reaped(children, 1)
            with self.assertRaises(worker.RegistrationCleanupError):
                backend.generate_setup()

    def test_spawn_exception_is_only_fixed_refusal(self):
        backend = self._backend()
        with patch.object(
            pipe, "_spawn", side_effect=OSError("test raw diagnostic")
        ) as spawn:
            with self._refused() as caught:
                backend.generate_setup()
            self.assertTrue(caught.exception.__suppress_context__)
            spawn.assert_called_once()
        self.assertTrue(backend.close())
