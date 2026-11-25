"""Use real owned local processes, private framed pipes and synthetic HTTP."""

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from oc_box import opencode_v2_io


TOKEN = "a" * 32
SESSION = "ses_guest_trial"


class GuardedSourceBinaryTests(unittest.TestCase):
    def test_digest_refusal_precedes_native_execution(self):
        executable = Path(sys.executable).resolve()
        with self.assertRaisesRegex(opencode_v2_io.HistorySourceError, "identity"):
            with opencode_v2_io._pinned_history_source_binary(
                executable, "0" * 64, time.monotonic() + 5
            ):
                self.fail("admitted an unmatched executable")

    def test_verified_descriptor_can_execute_and_detects_inode_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            copied = Path(name) / "native"
            body = Path(sys.executable).resolve().read_bytes()
            copied.write_bytes(body)
            copied.chmod(0o700)
            digest = hashlib.sha256(body).hexdigest()
            with opencode_v2_io._pinned_history_source_binary(
                copied,
                digest,
                time.monotonic() + 5,
            ) as (pinned, recheck):
                result = subprocess.run(
                    [pinned, "-I", "-S", "-B", "-c", "print('pinned')"],
                    env={"PATH": os.defpath},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                )
                self.assertEqual((result.returncode, result.stdout), (0, b"pinned\n"))
                copied.chmod(0o500)
                with self.assertRaisesRegex(
                    opencode_v2_io.HistorySourceError, "identity"
                ):
                    recheck()
            self.assertFalse(Path(pinned).exists())
            copied.with_name("alias").symlink_to(copied)
            with self.assertRaisesRegex(opencode_v2_io.HistorySourceError, "identity"):
                with opencode_v2_io._pinned_history_source_binary(
                    copied.with_name("alias"),
                    digest,
                    time.monotonic() + 5,
                ):
                    self.fail("admitted an executable symlink")


def exact(fd, count, deadline):
    parts = bytearray()
    while len(parts) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], (), (), remaining)[0]:
            raise AssertionError("timed out waiting for fixture output")
        part = os.read(fd, count - len(parts))
        if not part:
            raise AssertionError("premature fixture EOF")
        parts.extend(part)
    return bytes(parts)


class GuestBarrierTests(unittest.TestCase):
    def setUp(self):
        reached, release = self.reached, self.release = (
            threading.Event(),
            threading.Event(),
        )
        calls = self.calls = []
        example = {
            "id": "msg_guest_trial",
            "type": "user",
            "text": "synthetic",
            "time": {"created": 1},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                calls.append(handler.path)
                reached.set()
                if not release.wait(5):
                    return
                if "/message?" in handler.path:
                    value = {"data": [example], "cursor": {}}
                elif handler.path.endswith("/export"):
                    value = {"data": {"info": {"id": SESSION}, "messages": [example]}}
                elif handler.path.startswith("/api/session?"):
                    value = {
                        "data": [{"id": SESSION, "location": {"directory": "/trial"}}],
                        "cursor": {},
                    }
                else:
                    handler.send_error(404)
                    return
                body = json.dumps(value).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                try:
                    handler.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01)
        )
        thread.start()

        def finish():
            release.set()
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)

        self.addCleanup(finish)

    @contextlib.contextmanager
    def blocked(self, *, inventory=False):
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = (
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from oc_box import history_overlap_guest as guest, opencode_v2_io as bridge\n"
            "try: guest.supervise(sys.argv[2], bridge, timeout=5)\n"
            "except ValueError: sys.exit(2)\n"
        )
        command = [sys.executable, "-I", "-S", "-B", "-c", script, source, TOKEN]
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={"PATH": os.defpath},
        ) as controller:
            try:
                body = json.dumps(
                    {
                        **(
                            {"directory": "/trial"}
                            if inventory
                            else {"session": SESSION}
                        ),
                        "endpoint": {
                            "url": f"http://127.0.0.1:{self.server.server_port}",
                            "password": "synthetic-only",
                        },
                    }
                ).encode()
                controller.stdin.write(len(body).to_bytes(4, "big") + body)
                deadline = time.monotonic() + 4
                marker = bytearray()
                while not marker.endswith(b"\n"):
                    if len(marker) > 256:
                        self.fail("overlong ready record")
                    marker.extend(exact(controller.stdout.fileno(), 1, deadline))
                protocol, token, parent, _parent_start, child, _child_start = (
                    marker.decode().split()
                )
                self.assertEqual(
                    (protocol, token, int(parent)),
                    ("OC-BOX-HISTORY-READY/1", TOKEN, controller.pid),
                )
                pin = os.pidfd_open(int(child))
                try:
                    self.assertFalse(select.select([pin], (), (), 0)[0])
                    self.assertEqual(self.calls, [])
                    yield controller, pin
                finally:
                    os.close(pin)
            finally:
                controller.stdin.close()
                controller.wait(timeout=8)

    def go(self, parent, token=TOKEN):
        parent.stdin.write(f"OC-BOX-HISTORY-GO/1 {token}\n".encode())

    def test_exact_ready_go_releases_only_one_checked_frame(self):
        self.release.set()
        with self.blocked() as (parent, pin):
            self.go(parent)
            deadline = time.monotonic() + 4
            fd = parent.stdout.fileno()
            length = int.from_bytes(exact(fd, 4, deadline), "big")
            data = exact(fd, length, deadline)
            self.assertEqual(parent.wait(timeout=4), 0)
            self.assertTrue(select.select([pin], (), (), 0)[0])
        self.assertEqual(data[:1], b"E")
        self.assertEqual(json.loads(data[1:])["data"]["info"]["id"], SESSION)
        self.assertEqual(len(self.calls), 2)

    def test_cancel_before_go_refuses_any_native_read(self):
        with self.blocked() as (parent, pin):
            parent.stdin.close()
            self.assertEqual(parent.wait(timeout=4), 2)
            self.assertTrue(select.select([pin], (), (), 0)[0])
            self.assertEqual(parent.stdout.read(), b"")
        self.assertEqual(self.calls, [])

    def test_inventory_uses_the_same_barrier_and_a_distinct_reply(self):
        self.release.set()
        with self.blocked(inventory=True) as (parent, pin):
            self.go(parent)
            deadline = time.monotonic() + 4
            fd = parent.stdout.fileno()
            length = int.from_bytes(exact(fd, 4, deadline), "big")
            data = exact(fd, length, deadline)
            self.assertEqual(parent.wait(timeout=4), 0)
            self.assertTrue(select.select([pin], (), (), 0)[0])
        self.assertEqual(data[:1], b"I")
        self.assertEqual([row["id"] for row in json.loads(data[1:])], [SESSION])
        self.assertEqual(
            self.calls, ["/api/session?directory=%2Ftrial&order=asc&limit=100"]
        )

    def test_foreign_go_never_releases_the_blocked_child(self):
        with self.blocked() as (parent, pin):
            self.go(parent, "b" * 32)
            self.assertEqual(parent.wait(timeout=4), 2)
            self.assertTrue(select.select([pin], (), (), 0)[0])
        self.assertEqual(self.calls, [])

    def test_in_flight_eof_retires_the_exact_read_child_without_reply(self):
        with self.blocked() as (parent, pin):
            self.go(parent)
            self.assertTrue(self.reached.wait(2))
            parent.stdin.close()
            self.assertEqual(parent.wait(timeout=4), 2)
            self.assertTrue(select.select([pin], (), (), 0)[0])
            self.assertEqual(parent.stdout.read(), b"")
        self.assertEqual(len(self.calls), 1)
