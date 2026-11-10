"""Finite synthetic PTY/pipe regressions; no host terminal or native service."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from oc_box import terminal_ui as ui


BANNER = re.compile(rb"\x1b7\x1b\[\d+;1H\x1b\[0m\x1b\[2K[^\x1b]*\x1b8")
RELAY = r"""
import ctypes, json, os, signal, subprocess, sys, termios, time
from pathlib import Path
from oc_box import terminal_ui as ui
root = Path(sys.argv[1])
# Reap only this fixture's orphaned descendants, including killed group members.
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"], start_new_session=True)
def previous(*_):
    pass
signals = (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)
for sig in signals:
    signal.signal(sig, previous)
before = termios.tcgetattr(0) if os.isatty(0) else None
def expired(*_):
    raise TimeoutError("synthetic relay watchdog")
signal.signal(signal.SIGALRM, expired)
signal.alarm(3)
if sys.argv[2] == "cancel":
    original_select = ui.select.select
    def cancel_when_ready(*args):
        if (root / "ready").exists():
            raise KeyboardInterrupt
        return original_select(*args)
    ui.select.select = cancel_when_ready
result = {}
try:
    result["returncode"] = ui.run_tui(
        [sys.executable, "-B", "-c", sys.argv[3], str(root)],
        refresh=0.02, show_loading=sys.argv[4] == "loading")
except BaseException as exc:
    result["error"] = type(exc).__name__
finally:
    signal.alarm(0)
    result["terminal_restored"] = before is None or termios.tcgetattr(0) == before
    result["handlers_restored"] = all(signal.getsignal(sig) is previous for sig in signals)
    result["unrelated_alive"] = unrelated.poll() is None
    unrelated.kill()
    unrelated.wait(timeout=1)
    deadline = time.monotonic() + 0.3
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            result["no_children"] = True
            break
        if not pid:
            if time.monotonic() >= deadline:
                result["no_children"] = False
                break
            time.sleep(0.01)
    (root / "result.json").write_text(json.dumps(result))
    # A red regression must not leave the synthetic children it exposed behind.
    if not result["no_children"]:
        for pid in json.loads((root / "children.json").read_text()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break
"""


@unittest.skipUnless(sys.platform == "linux", "Linux PTY/process-group integration")
class TerminalUIFailures(unittest.TestCase):
    def launch(self, fixture, *, interactive=True, cancel=False, loading=True):
        temporary = tempfile.TemporaryDirectory(
            prefix="tui-failure-", dir="/tmp/opencode"
        )
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        master = slave = None
        if interactive:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                RELAY,
                str(root),
                "cancel" if cancel else "normal",
                fixture,
                "loading" if loading else "plain",
            ],
            stdin=slave if interactive else subprocess.DEVNULL,
            stdout=slave if interactive else subprocess.PIPE,
            stderr=slave if interactive else subprocess.STDOUT,
            start_new_session=True,
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(Path(ui.__file__).resolve().parents[1]),
                "HOME": str(root),
                "TMPDIR": str(root),
            },
        )

        def cleanup():
            # These IDs come only from the finite fixture we launched.
            metadata = root / "children.json"
            if process.poll() is None and metadata.exists():
                for pid in json.loads(metadata.read_text()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()

        self.addCleanup(cleanup)
        return root, process, master if interactive else process.stdout.fileno(), slave

    def collect(self, process, fd, *, observe=None, timeout=6):
        output = bytearray()
        deadline = time.monotonic() + timeout
        eof = False
        while time.monotonic() < deadline:
            if not eof and select.select([fd], [], [], 0.02)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                output.extend(chunk)
                eof = not chunk
            elif eof:
                time.sleep(0.01)
            if observe:
                observe(bytes(output))
            if process.poll() is not None:
                while not eof and select.select([fd], [], [], 0)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    output.extend(chunk)
                    eof = not chunk
                self.assertEqual(process.returncode, 0, bytes(output))
                return bytes(output)
        self.fail("synthetic relay did not finish before fixture deadline")

    def assert_restored(self, root):
        result = json.loads((root / "result.json").read_text())
        self.assertTrue(result["terminal_restored"], result)
        self.assertTrue(result["handlers_restored"], result)
        self.assertTrue(result["unrelated_alive"], result)
        self.assertTrue(result["no_children"], result)
        return result

    def test_fragmented_first_output_keeps_controls_and_utf8_contiguous(self):
        cases = (
            ("csi", (b"\x1b[", b"31mUI_READY"), {0, 5}),
            ("utf8", (b"\xe2", b"\x96", b"\x80UI_READY"), {0, 3}),
            (
                "osc_st_csi",
                (b"\x1b]0;ti", b"tle\x1b", b"\\\x1b[", b"32mUI_READY"),
                {0, 11, 16},
            ),
            ("osc_bel", (b"\x1b]title", b"\x07UI_READY"), {0, 8}),
            ("dcs_st", (b"\x1bPquery", b"\x1b", b"\\UI_READY"), {0, 9}),
        )
        for label, chunks, safe_prefixes in cases:
            with self.subTest(case=label):
                fixture = (
                    "import os,time; time.sleep(0.1); "
                    f"chunks={chunks!r}; "
                    "\nfor chunk in chunks:\n os.write(1,chunk); time.sleep(0.09)"
                )
                root, process, fd, _ = self.launch(fixture)
                output = self.collect(process, fd)
                application = b"".join(chunks)
                self.assertEqual(BANNER.sub(b"", output), application)
                offset = 0
                cursor = 0
                banners = list(BANNER.finditer(output))
                self.assertGreaterEqual(len(banners), 2)
                for match in banners:
                    offset += match.start() - cursor
                    self.assertIn(offset, safe_prefixes, (label, offset, output))
                    cursor = match.end()
                self.assertNotIn(b"\x1b7", output.split(b"UI_READY", 1)[1])
                result = self.assert_restored(root)
                self.assertEqual(result.get("returncode"), 0, result)

    def test_incomplete_invisible_fragments_are_not_swallowed_at_eof(self):
        for fragment in (b"\x1b[", b"\xe2\x96", b"\x1b]unfinished\x1b"):
            with self.subTest(fragment=fragment):
                root, process, fd, _ = self.launch(
                    f"import os,time; time.sleep(0.1); os.write(1,{fragment!r})"
                )
                output = self.collect(process, fd)
                self.assertEqual(BANNER.sub(b"", output), fragment)
                self.assert_restored(root)

    def test_long_unterminated_string_is_forwarded_without_waiting_for_terminator(self):
        payload = b"\x1b]" + b"x" * 32768
        fixture = (
            "import os,sys,time; from pathlib import Path; root=Path(sys.argv[1]); "
            f"os.write(1,{payload!r}); deadline=time.monotonic()+2; "
            "\nwhile not (root/'forwarded').exists() and time.monotonic()<deadline: time.sleep(0.01)"
            "\nassert (root/'forwarded').exists(); os.write(1,bytes([27,92])+b'UI_READY')"
        )
        root, process, fd, _ = self.launch(fixture)

        def acknowledge(output):
            if BANNER.sub(b"", output).count(b"x") >= 8192:
                (root / "forwarded").touch()

        output = self.collect(process, fd, observe=acknowledge)
        self.assertEqual(BANNER.sub(b"", output), payload + b"\x1b\\UI_READY")
        self.assertEqual(self.assert_restored(root).get("returncode"), 0)

    def test_loading_disabled_is_exact_byte_relay(self):
        content = b"\x1b[31m\xe2\x96\x80\x1b]title\x1b\\UI_READY"
        root, process, fd, _ = self.launch(
            f"import os; os.write(1,{content!r})", loading=False
        )
        self.assertEqual(self.collect(process, fd), content)
        self.assertEqual(self.assert_restored(root).get("returncode"), 0)

    def child_fixture(self, *, ignoring, descendant=False, leader_exits=False):
        return (
            "import json,os,signal,subprocess,sys,time; from pathlib import Path; "
            "root=Path(sys.argv[1]); "
            f"signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignoring else 'lambda *_: sys.exit(0)'}); "
            "children=[os.getpid()]; "
            + (
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "signal.signal(signal.SIGHUP,signal.SIG_IGN); time.sleep(4.5)']); "
                "children.append(child.pid); time.sleep(0.1); "
                if descendant
                else ""
            )
            + "(root/'children.json').write_text(json.dumps(children)); "
            "(root/'ready').touch(); os.write(1,b'CHILD_READY'); "
            + ("sys.exit(0)" if leader_exits else "time.sleep(4.5)")
        )

    def test_sigterm_restores_terminal_and_handlers_with_cooperative_or_ignoring_child(
        self,
    ):
        for interactive in (True, False):
            for ignoring in (False, True):
                with self.subTest(interactive=interactive, ignoring=ignoring):
                    root, process, fd, _ = self.launch(
                        self.child_fixture(ignoring=ignoring), interactive=interactive
                    )
                    requested = []

                    def terminate(output):
                        if b"CHILD_READY" in output and not requested:
                            requested.append(time.monotonic())
                            os.kill(process.pid, signal.SIGTERM)

                    self.collect(process, fd, observe=terminate)
                    self.assertTrue(requested)
                    self.assertLess(time.monotonic() - requested[0], 2.8)
                    result = self.assert_restored(root)
                    self.assertEqual(
                        result.get("returncode"),
                        -signal.SIGKILL if ignoring else 0,
                        result,
                    )

    def test_sigterm_cleans_owned_descendant_when_leader_exits(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                root, process, fd, _ = self.launch(
                    self.child_fixture(ignoring=False, descendant=True),
                    interactive=interactive,
                )
                requested = []

                def terminate(output):
                    if b"CHILD_READY" in output and not requested:
                        requested.append(time.monotonic())
                        os.kill(process.pid, signal.SIGTERM)

                self.collect(process, fd, observe=terminate)
                self.assertLess(time.monotonic() - requested[0], 2.8)
                self.assertEqual(self.assert_restored(root).get("returncode"), 0)

    def test_normal_leader_exit_cleans_owned_group(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                root, process, fd, _ = self.launch(
                    self.child_fixture(
                        ignoring=False, descendant=True, leader_exits=True
                    ),
                    interactive=interactive,
                )
                self.collect(process, fd)
                self.assertEqual(self.assert_restored(root).get("returncode"), 0)

    def test_exception_cancellation_restores_terminal_and_cleans_owned_group(self):
        root, process, fd, _ = self.launch(
            self.child_fixture(ignoring=True, descendant=True), cancel=True
        )
        self.collect(process, fd)
        self.assertEqual(self.assert_restored(root).get("error"), "KeyboardInterrupt")


if __name__ == "__main__":
    unittest.main()
