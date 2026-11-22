"""Synthetic host/child PTYs with the actual launcher relay and a fake opener."""

import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
DRIVER = """
import sys
from pathlib import Path
from oc_box import terminal_ui, file_preview_host
def open_candidate(kind, content):
    assert kind == "md"
    with Path(sys.argv[2]).open("xb") as marker:
        marker.write(content)
raise SystemExit(terminal_ui.run_tui(
    [sys.executable, "-I", "-B", "-c", sys.argv[1]],
    show_loading=False,
    preview_relay=file_preview_host.Relay(),
    preview_open=open_candidate,
))
"""
NATIVE = """
import base64, os, re, tty
tty.setraw(0)
def transfer(token, content):
    prefix = b'\\x1b]778;oc-box-open:v1:' + token + b':'
    for frame in (b'begin:md:' + str(len(content)).encode(),
                  b'data:' + base64.b64encode(content), b'end'):
        os.write(1, prefix + frame + b'\\x07')
transfer(b'1' * 32, b'forged')
os.write(1, b'READY')
content = b''
saw_grant = False
while True:
    content += os.read(0, 4096)
    if b'Q' in content:
        break
    grants = re.compile(rb'\\x1b\\]778;oc-box-click:v1:([0-9a-f]{32}):6:8\\x07')
    if b'CHECK-NO-GRANT' in content:
        assert not saw_grant and not grants.search(content), "unexpected grant before fresh click"
        os.write(1, b'NO-PREMATURE-GRANT')
        content = b''
    for found in grants.finditer(content):
        saw_grant = True
        os.write(1, found[0])
        transfer(found[1], b'actual artifact')
        transfer(found[1], b'replayed')
        os.write(1, b'TRANSFERRED')
    if grants.search(content):
        content = b''
"""

SLOW_DRIVER = DRIVER.replace(
    '    assert kind == "md"\n',
    """    import time
    print("OPENER-STARTED", flush=True)
    release = Path(sys.argv[2]).with_name("release")
    deadline = time.monotonic() + 8
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert release.exists(), "test did not release opener"
    assert kind == "md"
""",
)
FAILING_NATIVE = NATIVE.replace(
    "    if b'Q' in content:\n",
    """    if b'FAIL-FRAME' in content:
        os.write(1, b'\\x1b]778;oc-box-open:v1:' + b'x' * 8192)
        content = b''
    if b'Q' in content:
""",
)


class TerminalArtifactTests(unittest.TestCase):
    def test_only_fresh_host_click_can_launch_worker(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-open-relay-") as root:
            marker = Path(root) / "opened"
            master, slave = pty.openpty()
            try:
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", DRIVER, NATIVE, str(marker)],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={"PYTHONPATH": str(SOURCE), "PATH": "/usr/bin:/bin"},
                )
            finally:
                os.close(slave)
            visible = bytearray()

            def await_visible(item, timeout=4):
                end = time.monotonic() + timeout
                while item not in visible and time.monotonic() < end:
                    if select.select([master], [], [], 0.1)[0]:
                        visible.extend(os.read(master, 4096))
                self.assertIn(item, visible)

            try:
                await_visible(b"READY")
                self.assertFalse(marker.exists())
                press, release = b"\x1b[<16;6;8M", b"\x1b[<16;6;8m"
                # Invalid interference, a control string, and pasted literal
                # mouse bytes must not be interpreted as a visible click.
                os.write(master, press + b"\x1b[I" + release)
                os.write(master, b"\x1b]0;" + press + release + b"\a")
                # Do not include ESC in this paste: malformed bracketed-paste
                # framing is intentionally a separate, fail-closed condition.
                os.write(master, b"\x1b[200~[<16;6;8M[<16;6;8m\x1b[201~")
                # Escape plus a control key doesn't kill the relay. The
                # recovery report itself never completes an authorized click.
                os.write(master, press + b"\x1b\x1b\x19" + release)
                os.write(master, b"CHECK-NO-GRANT")
                await_visible(b"NO-PREMATURE-GRANT")
                self.assertFalse(marker.exists())
                os.write(master, press + release)
                await_visible(b"TRANSFERRED")
                os.write(master, b"Q")
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(marker.read_bytes(), b"actual artifact")
                self.assertNotIn(b"oc-box-open:v1:", visible)
                self.assertNotIn(b"oc-box-click:v1:", visible)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=4)
                os.close(master)

    def test_bad_frame_restores_terminal_while_submitted_open_is_still_running(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-open-teardown-") as root:
            marker = Path(root) / "opened"
            release = marker.with_name("release")
            master, slave = pty.openpty()
            child = None
            visible = bytearray()

            def await_visible(text):
                deadline = time.monotonic() + 4
                while text not in visible and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        visible.extend(os.read(master, 4096))
                self.assertIn(text, visible)

            try:
                before = termios.tcgetattr(slave)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        SLOW_DRIVER,
                        FAILING_NATIVE,
                        str(marker),
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={"PYTHONPATH": str(SOURCE), "PATH": "/usr/bin:/bin"},
                )
                await_visible(b"READY")
                self.assertNotEqual(termios.tcgetattr(slave), before)
                os.write(master, b"\x1b[<16;6;8M\x1b[<16;6;8m")
                await_visible(b"OPENER-STARTED")

                os.write(master, b"FAIL-FRAME")
                await_visible(b"an already submitted open can still finish")
                self.assertEqual(termios.tcgetattr(slave), before)
                self.assertIsNone(child.poll())
                self.assertFalse(marker.exists())

                release.touch()
                self.assertEqual(child.wait(timeout=5), 1)
                self.assertEqual(marker.read_bytes(), b"actual artifact")
            finally:
                if child is not None and child.poll() is None:
                    release.touch()
                    child.kill()
                    child.wait(timeout=4)
                os.close(master)
                os.close(slave)
