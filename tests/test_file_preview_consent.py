"""Separate host TTY confirmation; use a synthetic PTY, never a desktop."""

import base64
import binascii
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import struct
import sys
import time
import unittest
import zlib


PACKAGE = Path(__file__).resolve().parents[1] / "src"
DRIVER = """
import sys, tty
from oc_box import file_preview_consent
tty.setraw(0)
file_preview_consent._TIMEOUT = 2
result = file_preview_consent.confirm(0, 1)
print("RESULT: " + str(result), flush=True)
"""

RELAY_DRIVER = """
import sys
from oc_box import terminal_ui, file_preview_host, file_preview_consent, host_drop_input
file_preview_consent._TIMEOUT = 3
def opener(data):
    with open(sys.argv[2], "xb") as out:
        out.write(data)
class Drop:
    input = host_drop_input.PasteInput(lambda _: None)
    def pending_output(self): return b""
    def output_bytes(self, data): return data
    def input_bytes(self, data): return self.input.feed(data)
    def finish_output(self): return b""
    def finish_input(self): return self.input.finish()
    def start(self): pass
    def stop(self): return b""
terminal_ui.run_tui(
    [sys.executable, "-B", "-c", sys.argv[1]],
    show_loading=False,
    file_drops=Drop(),
    preview_relay=file_preview_host.Relay(),
    preview_open=opener,
)
"""


class PreviewConsentTests(unittest.TestCase):
    def confirm(self, respond):
        master, slave = pty.openpty()
        try:
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", DRIVER],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={"PYTHONPATH": str(PACKAGE)},
            )
        finally:
            os.close(slave)
        data = bytearray()

        def read_until(pattern):
            end = time.monotonic() + 4
            while time.monotonic() < end:
                found = re.search(pattern, data)
                if found:
                    return found
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    data.extend(os.read(master, 4096))
                except OSError:
                    break
            self.fail("Host confirmation did not emit the expected fixed marker.")

        try:
            challenge = read_until(rb"snapshot: ([a-z]{6})\r\n")[1]
            if (answer := respond(challenge)) is not None:
                os.write(master, answer)
            result = read_until(rb"RESULT: (True|False)")[1] == b"True"
            self.assertEqual(child.wait(timeout=3), 0)
            self.assertEqual(data.count(challenge), 1)
            self.assertIn(b"\x1b[?47h", data)
            return result
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            os.close(master)

    def test_accepts_fresh_code(self):
        self.assertTrue(self.confirm(lambda challenge: challenge + b"\r"))

    def test_refuses_blind_enter_old_data_paste_batched_input_and_escape(self):
        for answer in (
            lambda _: b"\r",
            lambda _: b"000000\r",
            lambda code: b"\x1b[200~" + code + b"\x1b[201~\r",
            lambda code: code + b"\rmore",
            lambda _: b"\x1b",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.confirm(answer))

    def test_pty_relay_never_passes_host_approval_to_child_or_opens_without_it(self):
        # Tests the production relay and its separation from native input.
        # The recorder substitutes for the desktop application only.
        import tempfile

        from oc_box import file_preview_png

        def chunk(tag, data):
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", binascii.crc32(tag + data))
            )

        image = file_preview_png.normalize(
            file_preview_png.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b"")
        )[0]
        prefix = b"\x1b]778;oc-box-png:v1:" + b"c" * 32 + b":"
        wire = (
            prefix
            + f"begin:{len(image)}\a".encode()
            + prefix
            + b"data:"
            + base64.b64encode(image)
            + b"\a"
            + prefix
            + b"end\a"
        )
        native = f"""
import os, tty
tty.setraw(0)
os.write(1, {wire!r})
os.write(1, b'CHILD_PROPOSED_PNG')
while True:
    data = os.read(0, 128)
    if b'Z' in data:
        os.write(1, b'CHILD_RECEIVED_NEXT_INPUT')
        break
    if data:
        os.write(1, b'CHILD_RECEIVED_UNEXPECTED_INPUT')
"""
        for approve in (False, True):
            with (
                self.subTest(approve=approve),
                tempfile.TemporaryDirectory() as directory,
            ):
                marker = Path(directory) / "opener"
                master, slave = pty.openpty()
                try:
                    child = subprocess.Popen(
                        [sys.executable, "-B", "-c", RELAY_DRIVER, native, str(marker)],
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        env={"PYTHONPATH": str(PACKAGE), "PATH": "/usr/bin:/bin"},
                    )
                finally:
                    os.close(slave)
                output = bytearray()

                def await_bytes(pattern):
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        if match := re.search(pattern, output):
                            return match
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                output.extend(os.read(master, 4096))
                            except OSError:
                                break
                    self.fail("Expected fixed relay marker was not emitted.")

                try:
                    await_bytes(rb"CHILD_PROPOSED_PNG")
                    self.assertFalse(marker.exists())
                    self.assertNotIn(b"oc-box HOST", output)
                    # F8-looking bytes in a terminal reply are just data.
                    os.write(master, b"\x1b]0;synthetic\x1b[19~\x07")
                    await_bytes(rb"CHILD_RECEIVED_UNEXPECTED_INPUT")
                    self.assertNotIn(b"oc-box HOST", output)
                    output.clear()
                    self.assertFalse(marker.exists())
                    os.write(master, b"\x1b[19;1:2~")
                    await_bytes(rb"CHILD_RECEIVED_UNEXPECTED_INPUT")
                    self.assertNotIn(b"oc-box HOST", output)
                    output.clear()
                    os.write(master, b"\x1b[19~")
                    code = await_bytes(rb"snapshot: ([a-z]{6})\r\n")[1]
                    self.assertFalse(marker.exists())
                    os.write(master, code + b"\r" if approve else b"\x1b")
                    await_bytes(rb"\x1b\[\?47h")
                    os.write(master, b"Z")
                    await_bytes(rb"CHILD_RECEIVED_NEXT_INPUT")
                    self.assertNotIn(b"CHILD_RECEIVED_UNEXPECTED_INPUT", output)
                    self.assertNotIn(prefix, output)
                    self.assertEqual(child.wait(timeout=3), 0)
                    self.assertEqual(
                        marker.read_bytes() if marker.exists() else None,
                        image if approve else None,
                    )
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=3)
                    os.close(master)
