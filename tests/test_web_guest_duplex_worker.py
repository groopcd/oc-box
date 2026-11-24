"""Direct synthetic transport checks; no Gateway, Podman, or native service."""

import contextlib
import fcntl
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import termios
from time import monotonic
import unittest

from oc_box import web_guest_duplex_worker as worker


_SCRIPT = Path(worker.__file__).resolve()
_TAG = b"\x9a" * 16
_OTHER_TAG = b"\xbd" * 16
_HEADER = struct.Struct("!4sBH")
_SERIAL = struct.Struct("!Q")


def _wire(op, payload=b""):
    return _HEADER.pack(b"OGD\x01", op, len(payload)) + payload


def _data(serial, payload):
    return _wire(worker.Op.DATA, _SERIAL.pack(serial) + payload)


def _ack(serial):
    return _wire(worker.Op.ACK, _SERIAL.pack(serial))


class DuplexWorkerTests(unittest.TestCase):
    @contextlib.contextmanager
    def _process(self, port):
        with subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(_SCRIPT), str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env={},
        ) as proc:
            os.set_blocking(proc.stdin.fileno(), False)
            try:
                yield proc
            finally:
                if proc.returncode is None:
                    proc.stdin.close()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)

    @contextlib.contextmanager
    def _listener(self, *, backpressure=False):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if backpressure:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            listener.settimeout(3)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with self._process(listener.getsockname()[1]) as proc:
                yield listener, proc

    def _write(self, proc, data):
        remaining = memoryview(data)
        end = monotonic() + 3
        while remaining:
            self.assertTrue(
                select.select([], [proc.stdin], [], max(0, end - monotonic()))[1]
            )
            try:
                sent = os.write(proc.stdin.fileno(), remaining)
            except BlockingIOError:
                continue
            self.assertGreater(sent, 0)
            remaining = remaining[sent:]

    def _read(self, proc, size):
        end = monotonic() + 3
        payload = bytearray()
        while len(payload) < size:
            self.assertTrue(
                select.select([proc.stdout], [], [], max(0, end - monotonic()))[0]
            )
            chunk = os.read(proc.stdout.fileno(), size - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        return bytes(payload)

    def _frame(self, proc):
        head = self._read(proc, _HEADER.size)
        self.assertEqual(len(head), _HEADER.size)
        magic, code, size = _HEADER.unpack(head)
        self.assertEqual(magic, b"OGD\x01")
        self.assertLessEqual(size, worker.MAX_DATA + _SERIAL.size)
        payload = self._read(proc, size)
        self.assertEqual(len(payload), size)
        return worker.Op(code), payload

    @contextlib.contextmanager
    def _open(self, *, backpressure=False, small_output=False, fragment=False):
        with self._listener(backpressure=backpressure) as (listener, proc):
            if small_output:
                fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            frame = _wire(worker.Op.OPEN, _TAG)
            if fragment:
                for byte in frame:
                    self._write(proc, bytes([byte]))
            else:
                self._write(proc, frame)
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(3)
                self.assertEqual(self._frame(proc), (worker.Op.READY, _TAG))
                yield proc, peer

    def _cancel(self, proc):
        self._write(proc, _wire(worker.Op.CANCEL, _TAG))
        proc.stdin.close()

    def _retired(self, proc, status=worker.Closure.SOCKET_CLOSED, *, first=None):
        result = self._frame(proc) if first is None else first
        self.assertEqual(result, (worker.Op.CLOSED, _TAG + bytes([status])))
        self.assertEqual(self._read(proc, 1), b"")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(proc.stderr.read(1), b"")

    def _failed(self, proc):
        self.assertEqual(self._read(proc, 1), b"")
        self.assertEqual(proc.wait(timeout=3), 1)
        self.assertEqual(proc.stderr.read(1), b"")

    def _peer_read(self, peer, size):
        pending = bytearray()
        while len(pending) < size:
            part = peer.recv(size - len(pending))
            self.assertTrue(part)
            pending.extend(part)
        return bytes(pending)

    def test_protocol_record_sizes_and_version(self):
        for op, payload in (
            (worker.Op.OPEN, _TAG),
            (worker.Op.READY, _TAG),
            (worker.Op.DATA, _SERIAL.pack(1) + b"\x00" * worker.MAX_DATA),
            (worker.Op.ACK, _SERIAL.pack(1)),
            (worker.Op.END, b""),
            (worker.Op.CANCEL, _TAG),
            (worker.Op.CLOSED, _TAG + b"\x01"),
        ):
            with self.subTest(op=op):
                self.assertEqual(worker.encode(op, payload), _wire(op, payload))
                self.assertEqual(
                    worker.decode_header(_wire(op, payload)[: _HEADER.size]),
                    (op, len(payload)),
                )
        for raw in (
            b"OGD",
            _HEADER.pack(b"OGD\x02", worker.Op.OPEN, len(_TAG)),
            _HEADER.pack(b"OGD\x01", 255, 0),
            _HEADER.pack(
                b"OGD\x01", worker.Op.DATA, _SERIAL.size + worker.MAX_DATA + 1
            ),
            _HEADER.pack(b"OGD\x01", worker.Op.DATA, _SERIAL.size),
            _HEADER.pack(b"OGD\x01", worker.Op.ACK, 0),
            _HEADER.pack(b"OGD\x01", worker.Op.CLOSED, len(_TAG)),
        ):
            with self.assertRaises(worker.DuplexError):
                worker.decode_header(raw)

    def test_precancel_requires_control_eof_and_never_dials(self):
        with self._listener() as (listener, proc):
            self._write(proc, _wire(worker.Op.CANCEL, _TAG))
            self.assertFalse(select.select([listener, proc.stdout], [], [], 0.1)[0])
            proc.stdin.close()
            self._retired(proc, worker.Closure.NEVER_OPENED)
            self.assertFalse(select.select([listener], [], [], 0)[0])

    def test_open_then_immediate_cancel_never_forwards_bytes(self):
        with self._listener() as (listener, proc):
            self._write(
                proc, _wire(worker.Op.OPEN, _TAG) + _wire(worker.Op.CANCEL, _TAG)
            )
            proc.stdin.close()
            result = self._frame(proc)
            if result[0] == worker.Op.READY:
                self.assertEqual(result, (worker.Op.READY, _TAG))
                result = self._frame(proc)
            self._retired(proc, first=result)
            if select.select([listener], [], [], 0)[0]:
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(3)
                    self.assertEqual(peer.recv(1), b"")

    def test_ready_then_duplex_keeps_credits_independent_and_bytes_opaque(self):
        with self._open(fragment=True) as (proc, peer):
            response = b"HTTP/1.1 101 Switching Protocols\r\n\r\n\x82\x02\x00\xff"
            peer.sendall(response)
            op, payload = self._frame(proc)
            self.assertEqual(
                (op, payload), (worker.Op.DATA, _SERIAL.pack(1) + response)
            )
            request = b"\x82\x02\xff\x00"
            self._write(proc, _data(1, request))
            self.assertEqual(self._peer_read(peer, len(request)), request)
            self.assertEqual(self._frame(proc), (worker.Op.ACK, _SERIAL.pack(1)))
            # Native output is withheld until the exact credit returns.
            peer.sendall(b"\x00next")
            self.assertFalse(select.select([proc.stdout], [], [], 0.1)[0])
            self._write(proc, _ack(1))
            self.assertEqual(
                self._frame(proc), (worker.Op.DATA, _SERIAL.pack(2) + b"\x00next")
            )
            self._cancel(proc)
            self._retired(proc)
            self.assertEqual(peer.recv(1), b"")

    def test_native_eof_requires_explicit_cancel_and_drops_crossing_data(self):
        with self._open() as (proc, peer):
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(self._frame(proc), (worker.Op.END, b""))
            self.assertEqual(peer.recv(1), b"")
            self._write(proc, _data(1, b"discard") + _wire(worker.Op.CANCEL, _TAG))
            proc.stdin.close()
            self._retired(proc)
        with self._open() as (proc, peer):
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(self._frame(proc), (worker.Op.END, b""))
            proc.stdin.close()
            self._failed(proc)

    def test_cancel_deadline_does_not_wait_forever_for_stdin_eof(self):
        with self._open() as (proc, peer):
            self._write(proc, _wire(worker.Op.CANCEL, _TAG))
            self.assertEqual(peer.recv(1), b"")
            self.assertEqual(proc.wait(timeout=worker._CLOSE_SECONDS + 2), 1)
            self._failed(proc)

    def test_lost_stdout_cannot_turn_socket_close_into_success(self):
        with self._open() as (proc, peer):
            proc.stdout.close()
            self._cancel(proc)
            self.assertEqual(peer.recv(1), b"")
            self.assertEqual(proc.wait(timeout=3), 1)
            self.assertEqual(proc.stderr.read(1), b"")

    def test_cancel_closes_native_before_blocked_stdout_is_drained(self):
        with self._open(small_output=True) as (proc, peer):
            capacity = fcntl.fcntl(proc.stdout.fileno(), fcntl.F_GETPIPE_SZ)
            if capacity >= worker.MAX_DATA + _HEADER.size + _SERIAL.size:
                self.skipTest("Minimum pipe capacity exceeds one maximum data record.")
            payload = b"\x00" * worker.MAX_DATA
            peer.sendall(payload)
            self.assertTrue(select.select([proc.stdout], [], [], 2)[0])
            queued = struct.unpack(
                "i", fcntl.ioctl(proc.stdout.fileno(), termios.FIONREAD, bytes(4))
            )[0]
            self.assertEqual(queued, capacity)
            self._cancel(proc)
            # The native side must close while we still have not consumed
            # the backpressured response record or returned its credit.
            try:
                self.assertEqual(peer.recv(1), b"")
            except ConnectionResetError:
                pass  # Unread native bytes can make local TCP close reset.
            op, data = self._frame(proc)
            self.assertEqual(op, worker.Op.DATA)
            self.assertEqual(data[:8], _SERIAL.pack(1))
            self.assertTrue(data[8:] and payload.startswith(data[8:]))
            self._retired(proc)

    def test_cancel_bypasses_a_blocked_native_write(self):
        with self._open(backpressure=True) as (proc, peer):
            request = b"\xa5" * worker.MAX_DATA
            for serial in range(1, 513):
                self._write(proc, _data(serial, request))
                if not select.select([proc.stdout], [], [], 0.2)[0]:
                    break
                self.assertEqual(
                    self._frame(proc), (worker.Op.ACK, _SERIAL.pack(serial))
                )
            else:
                self.fail("Fixture could not fill the native TCP send path.")
            self.assertGreater(serial, 1)
            self._cancel(proc)
            # No peer reads or credit release are needed for the receipt.
            self._retired(proc)
            self.assertTrue(peer.recv(1))  # A receipt does not undo accepted writes.

    def test_bare_eof_and_malformed_control_never_acknowledge_closure(self):
        for data in (
            b"",
            _wire(worker.Op.OPEN, _TAG)[:-1],
            _wire(worker.Op.CANCEL, _TAG)[:-1],
            _wire(worker.Op.CANCEL, _TAG) + b"\x00",
            _wire(worker.Op.CANCEL, _TAG) * 2,
            _data(1, b"before open"),
            _HEADER.pack(b"OGD\x01", worker.Op.DATA, 0xFFFF),
        ):
            with self.subTest(size=len(data)), self._listener() as (listener, proc):
                self._write(proc, data)
                proc.stdin.close()
                self._failed(proc)
                self.assertFalse(select.select([listener], [], [], 0)[0])
        for data in (
            b"",
            _wire(worker.Op.CANCEL, _OTHER_TAG),
            _wire(worker.Op.OPEN, _TAG),
        ):
            with self.subTest(size=len(data)), self._open() as (proc, peer):
                self._write(proc, data)
                proc.stdin.close()
                self._failed(proc)
                self.assertEqual(peer.recv(1), b"")

    def test_bad_credits_fail_without_receipt(self):
        for bad in (_ack(2), _data(2, b"skip")):
            with self.subTest(op=bad[4]), self._open() as (proc, _):
                self._write(proc, bad)
                proc.stdin.close()
                self._failed(proc)
        with self._open() as (proc, peer):
            peer.sendall(b"first")
            self.assertEqual(
                self._frame(proc), (worker.Op.DATA, _SERIAL.pack(1) + b"first")
            )
            self._write(proc, _ack(1) + _ack(1) + _wire(worker.Op.CANCEL, _TAG))
            proc.stdin.close()
            self._failed(proc)

    def test_wrong_port_syntax_does_not_start_worker(self):
        for port in ("0", "00080", "+80", "65536", "127.0.0.1:80"):
            with self.subTest(port=port), self._process(port) as proc:
                proc.stdin.close()
                self._failed(proc)


if __name__ == "__main__":
    unittest.main()
