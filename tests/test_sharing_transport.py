"""Bounded real pipe transport, without Podman, native services or credentials."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, sharing


class SharingPipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-share-pipes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "example.txt").write_text("synthetic shared content")

    def process(self, code, arguments=(), environment=None):
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", code, *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment or {"PATH": os.defpath, "LANG": "C.UTF-8"},
        )

        def cleanup():
            process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()

        self.addCleanup(cleanup)
        return process

    def test_real_jsonrpc_pipe_handshake_listing_read_and_eof(self):
        result = self.root / "result.json"
        code = """
import json, sys
def call(ident, method, params=None):
    print(json.dumps({'jsonrpc':'2.0', 'id':ident, 'method':method, 'params':params or {}}), flush=True)
    response = json.loads(sys.stdin.readline())
    assert response['id'] == ident
    return response['result']
results = [call(1, 'initialize'), call(2, 'tools/list'), call(3, 'tools/call', {'name':'read_file', 'arguments':{'path':'example.txt'}})]
with open(RESULT, 'w') as stream:
    json.dump(results, stream)
""".replace("RESULT", repr(str(result)))
        process = self.process(code)
        ready = []
        with sharing.Export(self.source) as export:
            sharing.serve(process, export, ready=lambda: ready.append(True))
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(ready, [True])
        initialized, catalog, read = json.loads(result.read_text())
        self.assertEqual(initialized["capabilities"], {"tools": {}})
        self.assertEqual(
            {tool["name"] for tool in catalog["tools"]},
            {"list_directory", "read_file", "read_image"},
        )
        self.assertEqual(
            read, {"content": [{"type": "text", "text": "synthetic shared content"}]}
        )

    def test_incomplete_or_malformed_native_requests_fail_closed(self):
        cases = (
            ("pass", "unavailable"),
            ("print('not json', flush=True)", None),
            ("print('x' * 65537, flush=True)", "Oversized"),
            ("import os; os.write(1, b'x' * 65537)", "Oversized"),
            ("print('[]', flush=True)", "Invalid"),
        )
        for code, reason in cases:
            with self.subTest(code=code), sharing.Export(self.source) as export:
                process = self.process(code)
                if reason:
                    with self.assertRaisesRegex(sharing.ShareError, reason):
                        sharing.serve(process, export)
                else:
                    with self.assertRaises(json.JSONDecodeError):
                        sharing.serve(process, export)

    def test_startup_timeout_is_bounded(self):
        process = self.process("import sys; sys.stdin.read()")
        with (
            sharing.Export(self.source) as export,
            patch.object(sharing.time, "monotonic", side_effect=[0, 31]),
        ):
            with self.assertRaisesRegex(sharing.ShareError, "timed out"):
                sharing.serve(process, export)

    def test_response_queue_is_bounded_without_logging_content(self):
        process = self.process(
            'print(\'{"jsonrpc":"2.0","id":1,"method":"initialize"}\', flush=True); import sys; sys.stdin.read()'
        )
        with (
            sharing.Export(self.source) as export,
            patch.object(sharing, "MAX_RESPONSE", 8),
        ):
            with self.assertRaisesRegex(
                sharing.ShareError, "exceeded its bound"
            ) as refusal:
                sharing.serve(process, export)
        self.assertNotIn("synthetic shared content", str(refusal.exception))

    def test_native_websocket_receiver_stall_has_a_write_and_close_deadline(self):
        # A real peer accepts the authenticated upgrade and requests an image,
        # then stops reading. The full response fits the host's queue but not
        # the peer socket, so pipe-only deadlines cannot cover this case.
        image = self.source / "image.gif"
        image.write_bytes(b"GIF89a" + b"x" * (8 * 1024 * 1024 - 6))
        finished = threading.Event()
        failures = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(10)

            def peer():
                try:
                    with listener.accept()[0] as stream:
                        stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                        stream.settimeout(10)
                        headers = bytearray()
                        while b"\r\n\r\n" not in headers:
                            headers.extend(stream.recv(4096))
                            if len(headers) > 65536:
                                raise AssertionError("Oversized synthetic handshake")
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
                        stream.sendall(
                            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                            + accept
                            + b"\r\n\r\n"
                        )
                        for ident, method, params in (
                            (1, "initialize", {}),
                            (2, "tools/list", {}),
                            (
                                3,
                                "tools/call",
                                {
                                    "name": "read_image",
                                    "arguments": {"path": "image.gif"},
                                },
                            ),
                        ):
                            payload = json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": ident,
                                    "method": method,
                                    "params": params,
                                }
                            ).encode()
                            header = (
                                bytes([0x81, len(payload)])
                                if len(payload) < 126
                                else b"\x81\x7e" + len(payload).to_bytes(2, "big")
                            )
                            stream.sendall(header + payload)
                        if not finished.wait(15):
                            failures.append("Relay did not honor its socket deadline")
                except Exception as exc:
                    failures.append(type(exc).__name__)

            worker = threading.Thread(target=peer, daemon=True)
            worker.start()
            started = time.monotonic()
            try:
                with patch.object(sharing, "TRANSPORT_IO_TIMEOUT", 0.2):
                    source = sharing.transport_source(box)
                process = self.process(
                    source,
                    (
                        "http://127.0.0.1:" + str(listener.getsockname()[1]),
                        str(self.root),
                        "oc_box_share_stall",
                    ),
                    {
                        "PATH": os.defpath,
                        "LANG": "C.UTF-8",
                        "OPENCODE_PASSWORD": "synthetic-private-control-password",
                    },
                )
                with sharing.Export(self.source) as export:
                    sharing.serve(process, export)
                process.wait(timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertLess(time.monotonic() - started, 10)
            finally:
                finished.set()
                worker.join(timeout=3)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
