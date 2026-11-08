import os
import socket
import struct
import unittest
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
