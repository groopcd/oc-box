"""Unwired byte pump exercises on synthetic browser sockets and private pipes."""

from concurrent.futures import Future
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from oc_box import web_guest_duplex_host as host
from oc_box import web_guest_duplex_worker as wire
from oc_box import web_guest_worker_drain as drain
from oc_box import web_sessions


_SCRIPT = Path(wire.__file__).resolve()
_REQUEST = b"GET /native HTTP/1.1\r\nHost: fixture\r\n\r\n"
_UPGRADE = (
    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
    b"Connection: Upgrade\r\nKeep-Alive: untouched\r\n\r\n\x82\x02\x00\xff"
)

# A controlled peer holds request credit, emits a full-sized response frame,
# and refuses an early response ACK or any second request before CANCEL.
_WITHHOLD = """
import sys
from oc_box import web_guest_duplex_worker as wire
inp = sys.stdin.buffer
def read():
    op, size = wire.decode_header(inp.read(wire._HEADER.size))
    return op, inp.read(size)
def emit(op, data):
    sys.stdout.buffer.write(wire.encode(op, data))
    sys.stdout.buffer.flush()
op, tag = read()
assert op == wire.Op.OPEN
emit(wire.Op.READY, tag)
op, request = read()
assert op == wire.Op.DATA and request[wire._CREDIT_SIZE:] == b"GET /native HTTP/1.1\\r\\nHost: fixture\\r\\n\\r\\n"
head = b"HTTP/1.1 200 OK\\r\\n\\r\\n"
emit(wire.Op.DATA, bytes(7) + b"\\1" + bytes(range(16)) + head + b"x" * (wire.MAX_DATA - len(head)))
op, value = read()
assert op == wire.Op.CANCEL and value[:wire.TAG_SIZE] == tag
assert inp.read() == b""
if sys.argv[1] == "wrong":
    value = tag + tag
emit(wire.Op.CLOSED, value + b"\\1")
"""


class PumpTests(unittest.TestCase):
    @contextmanager
    def pump(
        self,
        initial=_REQUEST,
        *,
        pairing_path=None,
        deadline=None,
        synthetic=None,
        leave_activity=False,
        worker_handle=lambda use: use,
    ):
        workers = drain.AcceptedWorkers()
        verdict = Future()
        stream, browser = socket.socketpair()
        browser.settimeout(3)
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        self.assertLess(
            stream.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF), wire.MAX_DATA
        )
        with browser, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            if synthetic is None:
                args = ["-I", "-S", "-B", str(_SCRIPT), str(listener.getsockname()[1])]
                env = {}
            else:
                args = ["-S", "-B", "-c", _WITHHOLD, synthetic]
                env = {"PYTHONPATH": str(_SCRIPT.parents[1])}

            def run(use):
                proc = tunnel = None
                try:
                    activity = workers.admit(use, wake=Event().set)
                    self.assertIsNotNone(activity)
                    proc = subprocess.Popen(
                        [sys.executable, *args],
                        env=env,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    tunnel = host.DuplexHost(proc)
                    retired = host.pump_worker(
                        workers,
                        worker_handle(use),
                        tunnel,
                        initial,
                        pairing_path=pairing_path,
                        deadline=deadline,
                    )
                    self.assertTrue(tunnel.abandon())
                    if not leave_activity:
                        # Only this synthetic fixture knows it performed no
                        # application work. The pump must not spend this record.
                        self.assertEqual(
                            workers.complete(use, activity, confirmed=retired),
                            retired,
                        )
                    verdict.set_result((retired, proc.returncode, tunnel._opened))
                except BaseException as error:
                    verdict.set_exception(error)
                finally:
                    if tunnel is not None:
                        tunnel.abandon()
                    if proc is not None:
                        if proc.poll() is None:  # Test-only local cleanup fallback.
                            proc.kill()
                            proc.wait(timeout=2)
                        proc.stdin.close()
                        proc.stdout.close()

            if not workers.dispatch(stream, run):
                stream.close()
                self.fail("Synthetic socket dispatch failed")
            try:
                yield workers, browser, listener, verdict
            finally:
                drained = workers.close(timeout=6)
            retired, _, _ = verdict.result(timeout=2)
            self.assertEqual(drained, retired and not leave_activity)

    def read(self, stream, length):
        result = bytearray()
        while len(result) < length:
            data = stream.recv(length - len(result))
            self.assertTrue(data, "unexpected stream EOF")
            result.extend(data)
        return bytes(result)

    def test_pairing_preserves_order_cookie_and_coalesced_body_across_frames(self):
        request_head = (
            b"GET /auth/connect/fixture HTTP/1.1\r\nHost: fixture\r\nX-Pad: "
            + b"a" * (2 * wire.MAX_DATA)
            + b"\r\nContent-Length: 6\r\n\r\n"
        )
        initial = request_head + b"\x00\xff"
        interim = (
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
        )
        body = b"\x00\xffLocation: /\r\nConnection: keep-alive\r\n\r\n"
        response = (
            b"HTTP/1.1 302 Found\r\nlOcAtIoN: /\r\n"
            b"Set-Cookie: native=fixture; HttpOnly\r\n"
            b"Set-Cookie: another=fixture\r\n"
            b"X-Pad: "
            + b"b" * (3 * wire.MAX_DATA)
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n"
        )
        expected = (
            response.replace(
                b"lOcAtIoN: /", b"Location: " + web_sessions.INDEX.encode()
            )
            + b"Connection: close\r\n\r\n"
            + body
        )
        with self.pump(initial, pairing_path=web_sessions.INDEX) as (
            _,
            browser,
            listener,
            verdict,
        ):
            native, _ = listener.accept()
            with native:
                native.settimeout(3)
                browser.sendall(b"tail")
                self.assertEqual(self.read(native, len(initial) + 4), initial + b"tail")
                native.sendall(interim + response[:17])
                self.assertEqual(self.read(browser, len(interim)), interim)
                native.sendall(
                    response[17:]
                    + b"Connection: keep-alive\r\nkEeP-aLiVe: timeout=60\r\n\r\n"
                    + body
                )
                native.shutdown(socket.SHUT_WR)
                self.assertEqual(self.read(browser, len(expected)), expected)
                self.assertEqual(browser.recv(1), b"")
            self.assertEqual(verdict.result(timeout=3), (True, 0, True))

    def test_upgraded_stream_stays_idle_and_duplex_until_registry_fence(self):
        with patch.object(host, "_PUMP_STALL", 0.75):
            with self.pump(leave_activity=True) as (
                workers,
                browser,
                listener,
                verdict,
            ):
                native, _ = listener.accept()
                with native:
                    native.settimeout(3)
                    self.assertEqual(self.read(native, len(_REQUEST)), _REQUEST)
                    native.sendall(_UPGRADE)
                    self.assertEqual(self.read(browser, len(_UPGRADE)), _UPGRADE)
                    self.assertFalse(Event().wait(0.9))
                    self.assertFalse(verdict.done())
                    browser.sendall(b"\x82\x02\x00\xff")
                    native.sendall(b"\x82\x02\xff\x00")
                    self.assertEqual(self.read(native, 4), b"\x82\x02\x00\xff")
                    self.assertEqual(self.read(browser, 4), b"\x82\x02\xff\x00")
                    workers.fence()
                    self.assertEqual(browser.recv(1), b"")
                    self.assertEqual(native.recv(1), b"")
                    self.assertEqual(verdict.result(timeout=3), (True, 0, True))
                    # Retirement of the exact tunnel says nothing about the
                    # registered application's independent outstanding work.
                    self.assertFalse(workers.close(timeout=3))

    def test_blocked_browser_and_unreturned_request_credit_cancel_without_ack(self):
        for trigger, receipt in (
            ("fence", "exact"),
            ("fence", "wrong"),
            ("stall", "exact"),
            ("browser-eof", "exact"),
        ):
            with (
                self.subTest(trigger=trigger, receipt=receipt),
                patch.object(host, "_PUMP_STALL", 1),
                self.pump(synthetic=receipt) as active,
            ):
                workers, browser, _, verdict = active
                # Read a single byte from a frame larger than the browser
                # socket's kernel send buffer, then stop draining entirely.
                self.assertEqual(browser.recv(1), b"H")
                browser.sendall(b"must stay on browser side")
                if trigger == "fence":
                    workers.fence()
                elif trigger == "browser-eof":
                    browser.shutdown(socket.SHUT_WR)
                retired, status, opened = verdict.result(timeout=3)
                self.assertTrue(opened)
                self.assertEqual(retired, receipt == "exact")
                if retired:
                    self.assertEqual(status, 0)

    def test_absolute_deadline_fences_an_already_idle_upgrade(self):
        offset = [0]
        with patch.object(
            host, "monotonic", side_effect=lambda: monotonic() + offset[0]
        ):
            with self.pump(deadline=monotonic() + 10) as (
                _,
                browser,
                listener,
                verdict,
            ):
                native, _ = listener.accept()
                with native:
                    native.settimeout(3)
                    self.assertEqual(self.read(native, len(_REQUEST)), _REQUEST)
                    native.sendall(_UPGRADE)
                    self.assertEqual(self.read(browser, len(_UPGRADE)), _UPGRADE)
                    offset[0] = 11
                    browser.sendall(b"late")
                    self.assertEqual(native.recv(1), b"")
                    self.assertEqual(verdict.result(timeout=3), (True, 0, True))

    def test_expired_or_unregistered_worker_never_emits_open(self):
        for options in (
            {"deadline": 0},
            {"worker_handle": lambda use: drain.AcceptedWorker(use.stream)},
        ):
            with self.subTest(options=options), self.pump(**options) as active:
                _, browser, listener, verdict = active
                self.assertEqual(verdict.result(timeout=3), (True, 0, False))
                listener.settimeout(0)
                with self.assertRaises(BlockingIOError):
                    listener.accept()
                self.assertEqual(browser.recv(1), b"")

    def test_substituted_browser_descriptor_is_neither_used_nor_closed(self):
        foreign, other = socket.socketpair()
        substituted = []
        with foreign, other:
            other.settimeout(3)

            def replace(use):
                fd = use.stream.fileno()
                os.dup2(foreign.fileno(), fd, inheritable=False)
                substituted.append(fd)
                return use

            try:
                with self.pump(worker_handle=replace, leave_activity=True) as active:
                    _, browser, listener, verdict = active
                    self.assertEqual(verdict.result(timeout=3), (True, 0, False))
                    self.assertEqual(browser.recv(1), b"")
                    listener.settimeout(0)
                    with self.assertRaises(BlockingIOError):
                        listener.accept()
                os.write(substituted[0], b"foreign still open")
                self.assertEqual(other.recv(100), b"foreign still open")
            finally:
                for fd in substituted:
                    os.close(fd)

    def test_bad_http_response_never_falls_back_to_unfiltered_bytes(self):
        for response in (
            b"bad status\r\n\r\nsecret-shaped opaque fixture",
            b"HTTP/1.1 200 OK\r\nX-Huge: " + b"a" * 65536,
        ):
            with self.subTest(length=len(response)), self.pump() as active:
                _, browser, listener, verdict = active
                native, _ = listener.accept()
                with native:
                    native.settimeout(3)
                    self.assertEqual(self.read(native, len(_REQUEST)), _REQUEST)
                    native.sendall(response)
                    self.assertEqual(browser.recv(1), b"")
                self.assertEqual(verdict.result(timeout=3), (True, 0, True))


if __name__ == "__main__":
    unittest.main()
