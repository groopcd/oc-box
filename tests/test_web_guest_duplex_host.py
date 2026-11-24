"""Exercise exact private pipes and child retirement without Podman or Gateway."""

from contextlib import contextmanager
import gc
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import patch

from oc_box import web_guest_duplex_host as host
from oc_box import web_guest_duplex_worker as worker


_SCRIPT = Path(worker.__file__).resolve()
_FAKE = """
import os, sys, time
with sys.stdin.buffer as inp:
    framed = inp.read()
tag = framed[-16:]
mode = sys.argv[1]
receipt = b"OGD\\x01\\x07\\x00\\x11" + tag + b"\\0"
if mode == "wrong-tag":
    receipt = receipt[:7] + bytes([tag[0] ^ 1]) + tag[1:] + b"\\0"
elif mode == "wrong-disposition":
    receipt = receipt[:-1] + b"\\1"
elif mode == "short":
    receipt = receipt[:-1]
elif mode == "missing":
    receipt = b""
elif mode == "duplicate":
    receipt += receipt
os.write(1, receipt)
if mode in ("surplus", "keep-open"):
    time.sleep(0.1 if mode == "surplus" else 1)
    if mode == "surplus":
        os.write(1, b"x")
if mode == "nonzero":
    sys.exit(3)
"""


class DuplexHostTests(unittest.TestCase):
    @contextmanager
    def child(self, arguments):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            bufsize=0,
        )
        tunnel = None
        try:
            tunnel = host.DuplexHost(proc)
            yield proc, tunnel
        finally:
            if tunnel is not None:
                tunnel.abandon()
            # Bounded test-only fallback for a local synthetic child.
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                stream.close()

    @contextmanager
    def listener(self):
        with socket.socket() as peer:
            peer.bind(("127.0.0.1", 0))
            peer.listen(1)
            peer.settimeout(2)
            with self.child([str(_SCRIPT), str(peer.getsockname()[1])]) as active:
                yield peer, *active

    def advance_until(self, tunnel, predicate):
        end = monotonic() + 3
        while not predicate():
            self.assertLess(monotonic(), end)
            tunnel.advance(milliseconds=20)

    def open(self, peer, tunnel):
        tunnel.open()
        self.advance_until(tunnel, lambda: tunnel.ready)
        native, _ = peer.accept()
        native.settimeout(2)
        return native

    def test_cancel_before_emitting_open_never_dials_and_reaps_cleanly(self):
        for queued in (False, True):
            with self.subTest(queued=queued), self.listener() as (peer, proc, tunnel):
                if queued:
                    tunnel.open()
                self.assertTrue(tunnel.close())
                self.assertTrue(tunnel.close())
                self.assertEqual(proc.returncode, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(proc.pid, os.WNOHANG)
                peer.settimeout(0.05)
                with self.assertRaises(TimeoutError):
                    peer.accept()

    def test_duplex_credits_keep_opaque_bytes_and_idle_cancel_retires(self):
        with self.listener() as (peer, proc, tunnel), self.open(peer, tunnel) as native:
            a = b"upgrade\x00\xff\r\n"
            b = b"native\x82\x00\x81"
            self.assertTrue(tunnel.send(a))
            self.assertFalse(tunnel.send(b"unspent credit"))
            native.sendall(b)
            self.advance_until(tunnel, lambda: tunnel.receive() is not None)
            self.assertEqual(tunnel.receive(), b)
            self.assertEqual(native.recv(4096), a)
            tunnel.consumed()
            self.advance_until(tunnel, lambda: tunnel._queued_data is None)
            # The upgrade can be idle with no data to flush before cancellation.
            self.assertTrue(tunnel.close())
            self.assertEqual(native.recv(1), b"")
            self.assertEqual(proc.returncode, 0)
            with self.assertRaises(host.DuplexHostError):
                tunnel.receive()

    def test_close_retires_with_backpressured_output_and_discards_pending_bytes(self):
        with self.listener() as (peer, proc, tunnel), self.open(peer, tunnel) as native:
            native.sendall(b"x" * (4 * worker.MAX_DATA))
            self.advance_until(tunnel, lambda: tunnel.receive() is not None)
            self.assertIsNotNone(tunnel._rx_credit)
            self.assertTrue(tunnel.close())
            try:
                self.assertEqual(native.recv(1), b"")
            except ConnectionResetError:
                # Native still had unread bytes when the worker closed.
                pass
            self.assertEqual(proc.returncode, 0)

    def test_native_eof_is_not_a_receipt_and_still_needs_cancel(self):
        with self.listener() as (peer, proc, tunnel), self.open(peer, tunnel) as native:
            native.shutdown(socket.SHUT_WR)
            self.advance_until(tunnel, lambda: tunnel.ended)
            self.assertFalse(tunnel._receipt)
            self.assertTrue(tunnel.close())
            self.assertEqual(proc.returncode, 0)

    def test_wrong_missing_late_or_unreaped_retirement_stays_false(self):
        for mode in (
            "wrong-tag",
            "wrong-disposition",
            "short",
            "missing",
            "surplus",
            "duplicate",
            "keep-open",
            "nonzero",
        ):
            with (
                self.subTest(mode=mode),
                self.child(["-c", _FAKE, mode]) as (_, tunnel),
            ):
                self.assertFalse(tunnel.close(timeout=0.5))
                tunnel.abandon()
                self.assertFalse(tunnel.close())

    def test_receipt_plus_eof_does_not_accept_lost_kernel_wait_evidence(self):
        waitpid = os.waitpid
        with self.child(["-c", _FAKE, "clean"]) as (_, tunnel):

            def stolen(pid, options):
                found = waitpid(pid, options)
                if found[0]:
                    raise ChildProcessError()
                return found

            with patch.object(host.os, "waitpid", side_effect=stolen):
                self.assertFalse(tunnel.close())
            self.assertFalse(tunnel.close())

    def test_an_unexpected_duplicate_stdin_writer_is_not_eof(self):
        with self.listener() as (peer, proc, tunnel), self.open(peer, tunnel) as native:
            leaked = os.dup(tunnel._tx_pin[0])
            try:
                self.assertFalse(tunnel.close(timeout=0.25))
                self.assertFalse(tunnel._receipt)
                self.assertEqual(native.recv(1), b"")
            finally:
                os.close(leaked)
            self.assertFalse(tunnel.close())
            tunnel.abandon()
            self.assertIsNotNone(proc.returncode)

    def test_substituted_pipe_survives_abandon_and_object_collection(self):
        read, write = os.pipe()
        displaced = None
        try:
            with self.child(["-c", _FAKE, "clean"]) as (proc, tunnel):
                displaced = tunnel._rx_pin[0]
                os.dup2(read, displaced, inheritable=False)
                self.assertFalse(tunnel.close())
                tunnel.abandon()
                self.assertIsNotNone(proc.returncode)
            del tunnel, proc
            gc.collect()
            os.write(write, b"still open")
            self.assertEqual(os.read(displaced, 10), b"still open")
        finally:
            for fd in (displaced, read, write):
                if fd is not None:
                    os.close(fd)

    def test_foreign_thread_cannot_spend_original_cancellation(self):
        with self.listener() as (_, proc, tunnel):
            result = []

            def attempt():
                try:
                    tunnel.close()
                except host.DuplexHostError:
                    result.append("refused")

            thread = Thread(target=attempt)
            thread.start()
            thread.join(2)
            self.assertEqual(result, ["refused"])
            self.assertTrue(tunnel.close())
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
