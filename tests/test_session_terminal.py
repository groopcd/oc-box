import base64
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import tempfile
import termios
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from oc_box import session_terminal as terminal


def frame(payload, opcode=2, final=True):
    start = bytes([(0x80 if final else 0) | opcode])
    length = len(payload)
    return (
        start
        + (
            bytes([length])
            if length < 126
            else b"\x7e" + struct.pack("!H", length)
            if length < 65536
            else b"\x7f" + struct.pack("!Q", length)
        )
        + payload
    )


def event(kind, **values):
    return frame(json.dumps({"type": kind, **values}).encode(), 1)


ATTACHED = event("attached", inputProtocol=1, role="controller")
READY = ATTACHED + event("replay_complete")


class TerminalPeer:
    """Finite private HTTP/WebSocket peer; no native service or host state."""

    def __init__(self, initial=b"", script=None, handshake="valid"):
        self.initial, self.script, self.handshake = initial, script, handshake
        self.frames, self.requests, self.errors = [], [], []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(3)
        self.address = "http://127.0.0.1:" + str(self.listener.getsockname()[1])
        self.master, self.slave = os.openpty()

    def exact(self, peer, size):
        data = bytearray()
        while len(data) < size:
            chunk = peer.recv(size - len(data))
            if not chunk:
                raise AssertionError("Synthetic client closed before its frame.")
            data.extend(chunk)
        return bytes(data)

    def receive(self, peer):
        first, second = self.exact(peer, 2)
        if not second & 0x80:
            raise AssertionError("Client WebSocket frame was not masked.")
        length = second & 127
        if length >= 126:
            length = struct.unpack(
                "!H" if length == 126 else "!Q",
                self.exact(peer, 2 if length == 126 else 8),
            )[0]
        if length > 65536:
            raise AssertionError("Unexpected synthetic client frame size.")
        mask = self.exact(peer, 4)
        data = self.exact(peer, length)
        result = (first & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(data)))
        self.frames.append(result)
        return result

    def drain(self, peer):
        while self.receive(peer)[0] != 8:
            pass

    def serve(self):
        responses = [
            {"capabilities": {"persistentPty": True}},
            {"data": [{"id": "pty_current", "status": "running"}]},
            {"data": {"ptyID": "pty_current"}},
            {
                "data": {
                    "info": {"output": {"tail": 17}},
                    "checkpoint": "Y2hlY2twb2ludA==",
                }
            },
        ]
        try:
            for index in range(5):
                peer, _ = self.listener.accept()
                with peer:
                    peer.settimeout(3)
                    headers = bytearray()
                    while b"\r\n\r\n" not in headers:
                        headers.extend(self.exact(peer, 1))
                        if len(headers) > 8192:
                            raise AssertionError("Oversized synthetic request.")
                    self.requests.append(bytes(headers))
                    if index < 4:
                        data = json.dumps(responses[index]).encode()
                        peer.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            + b"Content-Length: "
                            + str(len(data)).encode()
                            + b"\r\nConnection: close\r\n\r\n"
                            + data
                        )
                        continue
                    if self.handshake == "eof":
                        peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\n")
                        return
                    key = next(
                        line.split(b":", 1)[1].strip()
                        for line in bytes(headers).split(b"\r\n")
                        if line.lower().startswith(b"sec-websocket-key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
                            usedforsecurity=False,
                        ).digest()
                    )
                    if self.handshake == "refused":
                        accept = b"synthetic-private-refusal"
                    peer.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        + b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                        + accept
                        + b"\r\n\r\n"
                        + self.initial
                    )
                    if self.handshake == "refused":
                        return
                    if self.script:
                        self.script(self, peer)
                    else:
                        self.drain(peer)
        except BaseException as exc:
            self.errors.append(exc)

    def run(self, testcase, stdin_eof=False):
        before = termios.tcgetattr(self.slave)
        winch = signal.getsignal(signal.SIGWINCH)
        alarm = signal.getsignal(signal.SIGALRM)
        thread = threading.Thread(target=self.serve, daemon=True)

        def timeout(*_):
            raise TimeoutError("Synthetic terminal exceeded its test deadline.")

        try:
            with ExitStack() as stack:
                if stdin_eof:
                    read_fd, write_fd = os.pipe()
                    os.close(write_fd)
                else:
                    read_fd = os.dup(self.slave)
                stdin = stack.enter_context(os.fdopen(read_fd, "rb", buffering=0))
                stdout = stack.enter_context(tempfile.TemporaryFile())
                stack.enter_context(patch.object(sys, "stdin", stdin))
                stack.enter_context(patch.object(sys, "stdout", stdout))
                stack.enter_context(
                    patch.dict(os.environ, {"OPENCODE_PASSWORD": "synthetic-password"})
                )
                signal.signal(signal.SIGALRM, timeout)
                previous_timer = signal.setitimer(signal.ITIMER_REAL, 5)
                thread.start()
                try:
                    return terminal.run(self.address, "ses_fixture", "/project")
                finally:
                    signal.setitimer(signal.ITIMER_REAL, *previous_timer)
                    stdout.seek(0)
                    self.output = stdout.read()
        finally:
            signal.signal(signal.SIGALRM, alarm)
            self.listener.close()
            thread.join(4)
            try:
                testcase.assertFalse(thread.is_alive(), "Synthetic peer did not stop.")
                testcase.assertEqual(self.errors, [])
                testcase.assertEqual(termios.tcgetattr(self.slave), before)
                testcase.assertEqual(signal.getsignal(signal.SIGWINCH), winch)
            finally:
                # Do not let a deliberately red restoration regression affect
                # later tests or leave this test runner with a stale handler.
                signal.signal(signal.SIGWINCH, winch)
                termios.tcsetattr(self.slave, termios.TCSANOW, before)
                os.close(self.master)
                os.close(self.slave)


class SessionTerminalTests(unittest.TestCase):
    def websocket(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        result = terminal.WebSocket.__new__(terminal.WebSocket)
        result.socket, result.buffer = left, bytearray()
        result.fragment, result.fragment_type = bytearray(), None
        return result, right

    def test_websocket_partial_frames_unicode_binary_and_fragmentation(self):
        websocket, _ = self.websocket()
        payload = "Unicode terminal 中🙂\n".encode()
        encoded = frame(payload)
        for byte in encoded[:-1]:
            websocket.buffer.append(byte)
            self.assertEqual(websocket.frames(), [])
        websocket.buffer.append(encoded[-1])
        self.assertEqual(websocket.frames(), [(2, payload)])
        websocket.buffer.extend(frame(b"one", final=False) + frame(b"two", 0))
        self.assertEqual(websocket.frames(), [(2, b"onetwo")])
        for length in (126, 65536):
            websocket.buffer.extend(frame(b"x" * length))
            self.assertEqual(websocket.frames(), [(2, b"x" * length)])

    def test_client_frames_are_masked_and_ping_gets_a_pong(self):
        websocket, peer = self.websocket()
        payload = struct.pack("!BHH", 1, 80, 24) + b"pwd\r"
        websocket.send(payload)
        sent = peer.recv(8192)
        self.assertEqual(sent[:2], bytes([0x82, 0x80 | len(payload)]))
        mask = sent[2:6]
        self.assertEqual(
            bytes(value ^ mask[index % 4] for index, value in enumerate(sent[6:])),
            payload,
        )
        websocket.buffer.extend(frame(b"ping", 9))
        self.assertEqual(websocket.frames(), [])
        self.assertEqual(peer.recv(8192)[0], 0x8A)

    def test_invalid_frames_are_rejected_before_large_or_compressed_output(self):
        for encoded in (
            b"\xc2\x00",
            b"\x82\x80",
            b"\x00\x00",
            b"\x82\x7f" + struct.pack("!Q", terminal.MAX_FRAME + 1),
            b"\x09\x00",
        ):
            with self.subTest(frame=encoded):
                websocket, _ = self.websocket()
                websocket.buffer.extend(encoded)
                with self.assertRaises(ValueError):
                    websocket.frames()

    def test_selects_session_current_terminal_and_does_not_create_a_new_shell(self):
        terminals = [
            {"id": "pty_first", "status": "running"},
            {"id": "pty_current", "status": "running"},
            {"id": "pty_exited", "status": "exited"},
        ]
        request = Mock(
            side_effect=[{"data": terminals}, {"data": {"ptyID": "pty_first"}}]
        )
        self.assertEqual(
            terminal.choose_terminal(request, "ses_fixture", "/project")["id"],
            "pty_first",
        )
        self.assertEqual(
            [call.args[0] for call in request.call_args_list], ["GET", "GET"]
        )

    def test_new_session_shell_uses_server_environment_not_connecting_client_values(
        self,
    ):
        request = Mock(side_effect=[{"data": []}, {"data": {"id": "pty_created"}}])
        with patch.object(terminal, "terminal_size", return_value=(100, 30)):
            self.assertEqual(
                terminal.choose_terminal(request, "ses_fixture", "/project")["id"],
                "pty_created",
            )
        self.assertEqual(
            request.call_args.args,
            (
                "POST",
                "/api/experimental/session/ses_fixture/terminal",
                {
                    "args": [],
                    "cwd": "/project",
                    "title": "Terminal",
                    "env": {},
                    "size": {"cols": 100, "rows": 30},
                },
            ),
        )

    def test_zero_sized_pty_uses_usable_default_dimensions(self):
        with patch.object(
            os, "get_terminal_size", return_value=os.terminal_size((0, 0))
        ):
            self.assertEqual(terminal.terminal_size(), (80, 24))

    def test_error_close_codes_never_report_success_or_private_reason(self):
        for code in (1001, 1002, 1003, 1005, 1006, 1007, 1008, 1009, 1011, 1012, 4404):
            with self.subTest(code=code):
                peer = TerminalPeer(
                    READY + frame(struct.pack("!H", code) + b"private-reason", 8)
                )
                with self.assertRaisesRegex(
                    ValueError, "^Persistent session terminal became unavailable\\.$"
                ):
                    peer.run(self)
                self.assertEqual(peer.frames[-1], (8, struct.pack("!H", 1000)))

        for payload in (b"", b"x"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    TerminalPeer(READY + frame(payload, 8)).run(self)

    def test_normal_close_requires_attachment_and_completed_replay(self):
        for initial in (b"", ATTACHED, event("replay_complete")):
            with self.subTest(initial=initial):
                peer = TerminalPeer(initial + frame(struct.pack("!H", 1000), 8))
                with self.assertRaises(ValueError):
                    peer.run(self)
        # Native 2.2.6 queues 1000 on attachment end, even without an exited event.
        peer = TerminalPeer(READY + frame(struct.pack("!H", 1000), 8))
        self.assertEqual(peer.run(self), 0)

    def test_socket_eof_without_completed_replay_or_known_exit_is_failure(self):
        def eof(fixture, sock):
            sock.shutdown(socket.SHUT_WR)
            fixture.drain(sock)

        for initial in (b"", ATTACHED, READY):
            with self.subTest(initial=initial):
                peer = TerminalPeer(initial, script=eof)
                with self.assertRaisesRegex(
                    ValueError,
                    "^Persistent session terminal connection ended unexpectedly\\.$",
                ):
                    peer.run(self)

    def test_known_shell_exit_retains_its_status_after_replay(self):
        for code in (0, 7, 130):
            for ending in ("close", "eof"):
                with self.subTest(code=code, ending=ending):
                    initial = READY + event("exited", exitCode=code)
                    if ending == "close":
                        initial += frame(struct.pack("!H", 1000), 8)

                    def finish(fixture, sock):
                        if ending == "eof":
                            sock.shutdown(socket.SHUT_WR)
                        fixture.drain(sock)

                    self.assertEqual(
                        TerminalPeer(initial, script=finish).run(self), code
                    )

    def test_error_close_does_not_hide_behind_a_successful_shell_exit(self):
        peer = TerminalPeer(
            READY + event("exited", exitCode=0) + frame(struct.pack("!H", 1011), 8)
        )
        with self.assertRaises(ValueError):
            peer.run(self)

    def test_received_terminal_end_precedes_same_cycle_local_detach(self):
        cases = [
            (frame(struct.pack("!H", code) + b"private-reason", 8), None)
            for code in (1002, 1008, 1011)
        ] + [
            (event("exited", exitCode=7) + frame(struct.pack("!H", 1000), 8), 7),
            (event("exited", exitCode=7), 7),
            (frame(b"same-cycle-output"), 0),
        ]
        real_select = terminal.select.select
        for packet, expected in cases:
            for stdin_eof in (False, True):
                with self.subTest(packet=packet, stdin_eof=stdin_eof):
                    delivered = threading.Event()
                    co_ready = []

                    def send_after_replay(fixture, sock):
                        self.assertEqual(
                            fixture.receive(sock), (2, struct.pack("!BHH", 0, 80, 24))
                        )
                        # The ending cannot enter the HTTP handshake buffer:
                        # wait until run() has acknowledged completed replay.
                        sock.sendall(packet)
                        if not stdin_eof:
                            os.write(fixture.master, b"prefix\x1dsuffix")
                        delivered.set()
                        fixture.drain(sock)

                    def select_both(readers, *args):
                        if sys.stdin not in readers:
                            return real_select(readers, *args)
                        self.assertTrue(
                            delivered.wait(3), "Peer did not deliver ending."
                        )
                        result = real_select(readers, *args)
                        self.assertIn(readers[0], result[0])
                        self.assertIn(sys.stdin, result[0])
                        self.assertEqual(
                            readers[0].recv(len(packet), socket.MSG_PEEK), packet
                        )
                        co_ready.append(True)
                        return result

                    peer = TerminalPeer(READY, script=send_after_replay)
                    with patch.object(terminal.select, "select", select_both):
                        if expected is None:
                            with self.assertRaisesRegex(
                                ValueError,
                                "^Persistent session terminal became unavailable\\.$",
                            ):
                                peer.run(self, stdin_eof=stdin_eof)
                        else:
                            self.assertEqual(
                                peer.run(self, stdin_eof=stdin_eof), expected
                            )
                    self.assertEqual(co_ready, [True])
                    inputs = [
                        payload[5:]
                        for opcode, payload in peer.frames
                        if opcode == 2 and payload[0] == 1
                    ]
                    self.assertEqual(
                        inputs, [b"prefix"] if expected == 0 and not stdin_eof else []
                    )
                    if expected == 0:
                        self.assertEqual(peer.output, b"checkpointsame-cycle-output")

    def test_known_exit_eof_requires_a_complete_websocket_message_boundary(self):
        for truncated in (
            b"\x88",  # incomplete short header
            b"\x82\x7e\x00",  # incomplete extended header
            b"\x88\x05\x03\xf0",  # policy-close payload cut short
            b"\x82\x03x",  # incomplete binary payload
            frame(b"private-fragment", final=False),
            frame(b"", final=False),  # active, but empty fragment buffer
        ):
            with self.subTest(truncated=truncated):

                def truncate_after_replay(fixture, sock):
                    self.assertEqual(
                        fixture.receive(sock), (2, struct.pack("!BHH", 0, 80, 24))
                    )
                    sock.sendall(truncated)
                    sock.shutdown(socket.SHUT_WR)
                    fixture.drain(sock)

                peer = TerminalPeer(
                    READY + event("exited", exitCode=0), script=truncate_after_replay
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "^Persistent session terminal connection ended unexpectedly\\.$",
                ):
                    peer.run(self)

    def test_unknown_exit_does_not_turn_bare_eof_into_success(self):
        def eof(fixture, sock):
            sock.shutdown(socket.SHUT_WR)
            fixture.drain(sock)

        for exit_event in (event("exited"), event("exited", exitCode=None)):
            with self.subTest(exit_event=exit_event):
                with self.assertRaises(ValueError):
                    TerminalPeer(READY + exit_event, script=eof).run(self)

    def test_invalid_shell_exit_status_is_rejected_without_printing_it(self):
        for code in (True, "private-exit-status", -1, 256, 1.5):
            with self.subTest(code=code):
                peer = TerminalPeer(READY + event("exited", exitCode=code))
                with self.assertRaisesRegex(
                    ValueError, "^Invalid persistent terminal exit status\\.$"
                ):
                    peer.run(self)

    def test_stdin_eof_after_replay_is_a_local_detach(self):
        peer = TerminalPeer(READY)
        self.assertEqual(peer.run(self, stdin_eof=True), 0)
        self.assertEqual(peer.frames[-1], (8, struct.pack("!H", 1000)))

    def test_replay_timeout_restores_terminal_and_closes_connection(self):
        peer = TerminalPeer(ATTACHED)
        with patch.object(terminal.time, "monotonic", side_effect=(0, 31)):
            with self.assertRaisesRegex(
                ValueError, "^Persistent session terminal replay timed out\\.$"
            ):
                peer.run(self)
        self.assertEqual(peer.frames, [(8, struct.pack("!H", 1000))])

    def test_actual_handshake_replay_resize_and_binary_output_are_preserved(self):
        content = "terminal 中🙂\n".encode() + b"x" * 65536
        initial = (
            ATTACHED
            + frame(content[:3], final=False)
            + frame(b"ping", 9)
            + frame(content[3:], 0)
            + event("replay_complete")
        )

        def resize_and_end(fixture, sock):
            self.assertEqual(fixture.receive(sock), (10, b"ping"))
            self.assertEqual(fixture.receive(sock), (2, struct.pack("!BHH", 0, 80, 24)))
            sock.sendall(
                event(
                    "resized",
                    checkpoint=base64.b64encode(b"resized-checkpoint").decode(),
                )
            )
            os.kill(os.getpid(), signal.SIGWINCH)
            self.assertEqual(fixture.receive(sock), (2, struct.pack("!BHH", 0, 80, 24)))
            sock.sendall(frame(struct.pack("!H", 1000), 8))
            fixture.drain(sock)

        peer = TerminalPeer(initial, script=resize_and_end)
        self.assertEqual(peer.run(self), 0)
        self.assertEqual(peer.output, b"checkpoint" + content + b"resized-checkpoint")
        self.assertIn(
            b"GET /api/experimental/session/ses_fixture/terminal/read ",
            peer.requests[2],
        )
        self.assertIn(b"cursor=17&role=controller&attachment_id=", peer.requests[4])
        self.assertIn(b"&takeover=true&input_protocol=1 ", peer.requests[4])

    def test_detach_forwards_only_prefix_once_at_every_delimiter_position(self):
        for chunks in (
            (b"\x1d",),
            (b"\x1dsuffix",),
            (b"prefix\x1d",),
            (b"prefix\x1dsuffix\x1d",),
            (b"first", b"second\x1dsuffix"),
            (b"first", b"\x1dsuffix"),
        ):
            with self.subTest(chunks=chunks):

                def input_then_detach(fixture, sock):
                    self.assertEqual(
                        fixture.receive(sock), (2, struct.pack("!BHH", 0, 80, 24))
                    )
                    for chunk in chunks:
                        os.write(fixture.master, chunk)
                        if b"\x1d" not in chunk:
                            self.assertEqual(
                                fixture.receive(sock),
                                (2, struct.pack("!BHH", 1, 80, 24) + chunk),
                            )
                    fixture.drain(sock)

                peer = TerminalPeer(READY, script=input_then_detach)
                self.assertEqual(peer.run(self), 0)
                inputs = [
                    payload[5:]
                    for opcode, payload in peer.frames
                    if opcode == 2 and payload[0] == 1
                ]
                expected = [chunk.split(b"\x1d", 1)[0] for chunk in chunks]
                self.assertEqual(inputs, [chunk for chunk in expected if chunk])

    def test_handshake_failure_closes_socket_without_touching_tty(self):
        closed = []
        original_close = socket.socket.close

        def close(sock):
            closed.append(sock)
            return original_close(sock)

        for handshake in ("eof", "refused"):
            with self.subTest(handshake=handshake):
                with patch.object(socket.socket, "close", close):
                    with self.assertRaises(ValueError) as raised:
                        TerminalPeer(handshake=handshake).run(self)
                self.assertNotIn("synthetic-private-refusal", str(raised.exception))
        self.assertTrue(closed)
        self.assertTrue(all(sock.fileno() == -1 for sock in closed))

    def test_malformed_frame_restores_tty_handler_and_closes_connection(self):
        peer = TerminalPeer(b"\xc2\x00")
        with self.assertRaisesRegex(
            ValueError, "^Invalid terminal WebSocket frame\\.$"
        ):
            peer.run(self)
        self.assertEqual(peer.frames, [(8, struct.pack("!H", 1000))])

    def test_close_failure_still_restores_tty_and_signal_handler(self):
        original_close = terminal.WebSocket.close

        def close(websocket):
            original_close(websocket)
            raise OSError("synthetic-close-failure")

        peer = TerminalPeer(READY + frame(struct.pack("!H", 1000), 8))
        with patch.object(terminal.WebSocket, "close", close):
            with self.assertRaises(OSError):
                peer.run(self)
