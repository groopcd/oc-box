"""Synthetic protocol peers; no cryptography, credentials or live targets."""

import contextlib
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time
import unittest
from unittest.mock import patch

from oc_box import web_pake_worker as worker


_SEALS = (
    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
)
_LARGE = worker.WorkerSizes(4000, 3500, 2999, 3073, 4096)

# The disposable fixture is a Python program run by a sealed snapshot of the
# test interpreter's ELF. Only the private fixed argv constant is replaced;
# the real descriptor exec, process, Linux pipes and waits are exercised.
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
os.write(2, b"discarded test-only diagnostic")

def read_exact(n):
    response = bytearray()
    while len(response) < n:
        part = os.read(0, n - len(response))
        if not part:
            return bytes(response)
        response.extend(part)
    return bytes(response)

def hang():
    time.sleep(10)
    raise AssertionError("fixture failed to be cancelled")

start = b"OCOP\x01\x01"
finish = b"OCOP\x01\x02"
ok = b"\x00"
if mode == "close_input":
    os.close(0)
    hang()
if mode == "write_hang":
    hang()
if mode == "large":
    setup, record, ke1, ke2, ke3 = (4000, 3500, 2999, 3073, 4096)
else:
    setup, record, ke1, ke2, ke3 = (128, 192, 96, 320, 64)
present = b"\x00" + bytes(record) if mode == "fake" else b"\x01" + b"R" * record
expected = start + b"S" * setup + present + b"I"*32 + b"C"*32 + b"V"*32 + b"D"*32 + b"1"*ke1
assert read_exact(len(expected)) == expected
reply = start + ok + b"2" * ke2
if mode in ("half_start", "hang_start"):
    os.write(1, reply[:10])
    if mode == "hang_start":
        hang()
    sys.exit(0)
if mode == "start_bad_status":
    os.write(1, start + b"\x02")
    sys.exit(0)
if mode == "start_refuse":
    os.write(1, start + b"\x01")
    sys.exit(0)
if mode == "start_wrong_opcode":
    os.write(1, finish + ok)
    sys.exit(0)
if mode == "start_junk":
    os.write(1, reply + b"x")
    hang()
if mode == "fragment":
    os.write(1, reply[:3])
    time.sleep(.02)
    os.write(1, reply[3:87])
    time.sleep(.02)
    os.write(1, reply[87:])
else:
    os.write(1, reply)
if mode == "late_start_junk":
    time.sleep(.05)
    os.write(1, b"x")
if mode == "idle_hang":
    hang()
if mode == "discard_eof":
    assert not os.read(0, 1)
    sys.exit(0)
assert read_exact(len(finish) + ke3) == finish + b"3" * ke3
assert not os.read(0, 1)
if mode == "finish_empty":
    sys.exit(0)
if mode == "half_finish":
    os.write(1, finish[:3])
    sys.exit(0)
if mode == "finish_refuse":
    os.write(1, finish + b"\x01")
    sys.exit(0)
if mode == "finish_wrong_version":
    os.write(1, b"OCOP\x02\x02" + ok)
    sys.exit(0)
if mode == "finish_bad_status":
    os.write(1, finish + b"\xff")
    sys.exit(0)
if mode == "finish_junk":
    os.write(1, finish + ok + b"x")
    sys.exit(0)
os.write(1, finish + ok)
if mode == "late_finish_junk":
    time.sleep(.05)
    os.write(1, b"x")
if mode == "finish_no_exit":
    os.close(1)
    hang()
if mode == "finish_no_eof":
    hang()
if mode == "finish_nonzero":
    sys.exit(4)
if mode == "finish_signalled":
    os.kill(os.getpid(), signal.SIGTERM)
"""


def _request(sizes=worker.LAB_SIZES, **changes):
    values = {
        "setup": b"S" * sizes.setup,
        "registration": b"R" * sizes.registration,
        "credential_identifier": b"I" * 32,
        "client_identifier": b"C" * 32,
        "verifier_identifier": b"V" * 32,
        "context": b"D" * 32,
        "ke1": b"1" * sizes.ke1,
    }
    return {**values, **changes}


def _memfd(data):
    fd = os.memfd_create("pake-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o500)
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(data)
    except BaseException:
        os.close(fd)
        raise
    return fd


@unittest.skipUnless(sys.platform == "linux", "Linux-only verifier transport")
class PakeWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(
            (
                hasattr(os, "memfd_create"),
                hasattr(os, "pidfd_open"),
                hasattr(signal, "pidfd_send_signal"),
                os.execve in os.supports_fd,
            )
        ):
            raise unittest.SkipTest("Linux descriptor exec/pidfds unavailable")
        cls._executable = Path("/proc/self/exe").read_bytes()
        cls._digest = hashlib.sha256(cls._executable).digest()
        cls._fd = _memfd(cls._executable)
        cls.addClassCleanup(os.close, cls._fd)
        fcntl.fcntl(cls._fd, fcntl.F_ADD_SEALS, _SEALS)

    def _worker(self, *, sizes=worker.LAB_SIZES, io_timeout=0.6, **options):
        backend = worker.PakeWorker(
            self._fd,
            expected_sha256=self._digest,
            sizes=sizes,
            io_timeout=io_timeout,
            **options,
        )
        self.addCleanup(backend.close)
        return backend

    @contextlib.contextmanager
    def _peer(self, mode, *, spawned=None):
        children = []
        original = worker._spawn

        def capture(fd):
            child = original(fd)
            children.append(child)
            if spawned is not None:
                spawned.set()
            return child

        args = ("pake-test", "-I", "-S", "-B", "-c", _PEER, mode)
        with (
            patch.object(worker, "_VERIFIER_ARGV", args),
            patch.object(worker, "_spawn", capture),
        ):
            yield children

    def _assert_reaped(self, children):
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertIs(type(child.returncode), int)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)

    def _assert_refusal(self):
        return self.assertRaisesRegex(
            worker.PakeWorkerError, r"^Web PAKE worker refused\.$"
        )

    def test_success_uses_private_channels_exact_widths_and_only_one_child(self):
        with open("/dev/null", "rb") as unrelated:
            os.set_inheritable(unrelated.fileno(), True)
            with patch.dict(os.environ, {"OC_BOX_TEST_AMBIENT": "not-for-the-child"}):
                with self._peer("fragment") as children:
                    backend = self._worker()
                    response, attempt = backend.start(**_request())
                    self.assertEqual(response, b"2" * 320)
                    self.assertTrue(attempt.finish(ke3=b"3" * 64))
                    self.assertFalse(attempt.finish(b"3" * 64))
                    attempt.discard()
                    self.assertTrue(backend.close())
                    self._assert_reaped(children)

    def test_fake_path_and_separate_trusted_suite_sizes(self):
        for mode, sizes, changes in (
            ("fake", worker.LAB_SIZES, {"registration": None}),
            ("large", _LARGE, {}),
        ):
            with self.subTest(mode=mode), self._peer(mode) as children:
                backend = self._worker(sizes=sizes)
                response, attempt = backend.start(**_request(sizes, **changes))
                self.assertEqual(response, b"2" * sizes.ke2)
                self.assertTrue(attempt.finish(b"3" * sizes.ke3))
                self._assert_reaped(children)

    def test_invalid_executable_and_digest_refuse_without_spawn(self):
        unsealed = _memfd(self._executable)
        self.addCleanup(os.close, unsealed)
        script = _memfd(b"#!not-an-elf")
        self.addCleanup(os.close, script)
        fcntl.fcntl(script, fcntl.F_ADD_SEALS, _SEALS)
        with self._peer("fragment") as children:
            for fd, digest in (
                (unsealed, self._digest),
                (script, hashlib.sha256(b"#!not-an-elf").digest()),
                (self._fd, bytes(32)),
                (self._fd, b"not a digest"),
            ):
                with self.subTest(fd=fd), self._assert_refusal():
                    worker.PakeWorker(
                        fd, expected_sha256=digest, sizes=worker.LAB_SIZES
                    )
            self.assertFalse(children)

    def test_borrowed_executable_fd_may_close_after_construction(self):
        fd = os.dup(self._fd)
        try:
            backend = worker.PakeWorker(
                fd, expected_sha256=self._digest, sizes=worker.LAB_SIZES
            )
            self.addCleanup(backend.close)
        finally:
            os.close(fd)
        with self._peer("fragment") as children:
            _, attempt = backend.start(**_request())
            self.assertTrue(attempt.finish(b"3" * 64))
            self._assert_reaped(children)

    def test_invalid_input_refuses_before_spawn_and_malformed_finish_consumes(self):
        backend = self._worker()
        with self._peer("discard_eof") as children:
            for name, value in (
                ("context", bytes(31)),
                ("context", bytes(33)),
                ("context", bytearray(32)),
                ("credential_identifier", b"I"),
                ("client_identifier", None),
                ("verifier_identifier", b"V" * 65),
                ("registration", b""),
                ("setup", bytes(129)),
                ("ke1", bytes(4097)),
                ("deadline", time.monotonic() - 1),
                ("deadline", float("nan")),
            ):
                with self.subTest(name=name), self._assert_refusal():
                    backend.start(**_request(**{name: value}))
            self.assertFalse(children)
            _, attempt = backend.start(**_request())
            self.assertFalse(attempt.finish(bytearray(64)))
            self.assertFalse(attempt.finish(b"3" * 64))
            self._assert_reaped(children)
            self.assertEqual(children[0].returncode, 0)

    def test_partial_closed_malformed_or_stalled_start_refuses_and_reaps(self):
        for mode in (
            "half_start",
            "hang_start",
            "start_junk",
            "start_refuse",
            "start_wrong_opcode",
            "start_bad_status",
            "close_input",
            "write_hang",
        ):
            with self.subTest(mode=mode), self._peer(mode) as children:
                sizes = _LARGE if mode == "write_hang" else worker.LAB_SIZES
                backend = self._worker(sizes=sizes, io_timeout=0.3)
                before = time.monotonic()
                with self._assert_refusal():
                    backend.start(**_request(sizes))
                self.assertLess(time.monotonic() - before, 2)
                self._assert_reaped(children)
                self.assertTrue(backend.close())

    def test_late_start_output_before_finish_refuses(self):
        backend = self._worker()
        with self._peer("late_start_junk") as children:
            _, attempt = backend.start(**_request())
            poller = select.poll()
            poller.register(children[0].stdout.fileno(), select.POLLIN)
            self.assertTrue(poller.poll(1000))
            self.assertFalse(attempt.finish(b"3" * 64))
            self._assert_reaped(children)

    def test_finish_requires_exact_terminal_ack_eof_and_verified_zero_exit(self):
        for mode in (
            "finish_empty",
            "half_finish",
            "finish_refuse",
            "finish_wrong_version",
            "finish_bad_status",
            "finish_junk",
            "late_finish_junk",
            "finish_no_exit",
            "finish_no_eof",
            "finish_nonzero",
            "finish_signalled",
        ):
            with self.subTest(mode=mode), self._peer(mode) as children:
                backend = self._worker(io_timeout=0.3)
                _, attempt = backend.start(**_request())
                before = time.monotonic()
                self.assertFalse(attempt.finish(b"3" * 64))
                self.assertLess(time.monotonic() - before, 2)
                self.assertFalse(attempt.finish(b"3" * 64))
                attempt.discard()
                self._assert_reaped(children)
                self.assertTrue(backend.close())

    def test_discard_is_idempotent_and_releases_cap_only_after_reaping(self):
        backend = self._worker(max_workers=1)
        with self._peer("discard_eof") as children:
            _, attempt = backend.start(**_request())
            with self._assert_refusal():
                backend.start(**_request())
            attempt.discard()
            attempt.discard()
            self.assertFalse(attempt.finish(b"3" * 64))
            self._assert_reaped(children)
            self.assertEqual(children[0].returncode, 0)
        with self._peer("fragment") as children:
            _, next_attempt = backend.start(**_request())
            self.assertTrue(next_attempt.finish(b"3" * 64))
            self._assert_reaped(children)

    def test_caller_absolute_deadline_reaps_an_idle_uncooperative_child(self):
        backend = self._worker(attempt_timeout=10)
        with self._peer("idle_hang") as children:
            _, attempt = backend.start(**_request(deadline=time.monotonic() + 0.6))
            end = time.monotonic() + 2
            while children[0].returncode is None and time.monotonic() < end:
                time.sleep(0.01)
            self._assert_reaped(children)
            self.assertEqual(children[0].returncode, -signal.SIGKILL)
            self.assertFalse(attempt.finish(b"3" * 64))

    def test_concurrent_discard_cancels_finish_without_a_second_proof(self):
        backend = self._worker(io_timeout=2)
        finishing = threading.Event()
        results = []
        original = worker.PakeWorkerAttempt._status

        def announce(attempt, opcode, deadline):
            if opcode == b"OCOP\x01\x02":
                finishing.set()
            return original(attempt, opcode, deadline)

        with self._peer("finish_no_eof") as children:
            _, attempt = backend.start(**_request())
            with patch.object(worker.PakeWorkerAttempt, "_status", announce):
                thread = threading.Thread(
                    target=lambda: results.append(attempt.finish(b"3" * 64))
                )
                thread.start()
                try:
                    self.assertTrue(finishing.wait(1))
                    self.assertFalse(attempt.finish(b"3" * 64))
                finally:
                    attempt.discard()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [False])
            self._assert_reaped(children)

    def test_close_cancels_in_flight_start_and_refuses_new_attempts(self):
        backend = self._worker(io_timeout=2)
        spawned = threading.Event()
        results = []

        def start():
            try:
                backend.start(**_request())
            except worker.PakeWorkerError:
                results.append("refused")

        with self._peer("hang_start", spawned=spawned) as children:
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(spawned.wait(1))
                self.assertTrue(backend.close())
            finally:
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, ["refused"])
            self._assert_reaped(children)
            with self._assert_refusal():
                backend.start(**_request())

    def test_unverified_exit_fences_worker_until_explicit_cleanup_recheck(self):
        backend = self._worker()
        with self._peer("fragment") as children:
            _, attempt = backend.start(**_request())
            with patch.object(
                worker.os, "waitpid", side_effect=ChildProcessError(errno.ECHILD)
            ):
                self.assertFalse(attempt.finish(b"3" * 64))
                attempt.discard()  # No cleanup diagnostic escapes.
                with self._assert_refusal():
                    backend.start(**_request())
                self.assertFalse(backend.close())
            self.assertTrue(backend.close())
            self._assert_reaped(children)


if __name__ == "__main__":
    unittest.main()
