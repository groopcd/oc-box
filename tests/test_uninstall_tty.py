import errno
import os
import pty
import select
import signal
import unittest
from dataclasses import replace
from time import monotonic
from unittest.mock import patch

from oc_box import uninstall_plan, uninstall_review, uninstall_tty

from test_uninstall_plan import INSTALLATION


def snapshot(*args):
    request = uninstall_review.parse(["--uninstall", *args])
    item = uninstall_review.ReviewItem("release", str(INSTALLATION.release))
    return uninstall_plan.UninstallSnapshot(request, INSTALLATION, None, (item,), ())


class UninstallTTYTests(unittest.TestCase):
    def test_preview_blocked_and_unreviewed_full_never_open_a_terminal(self):
        with patch.object(uninstall_tty.os, "open") as opened:
            for plan in (
                snapshot("--dry-run"),
                snapshot("--full"),
                replace(snapshot(), blockers=("Synthetic scope refusal.",)),
                replace(snapshot(), installation=None),
            ):
                self.assertFalse(uninstall_tty.confirm(plan))
        opened.assert_not_called()

    def test_missing_ctty_refuses_without_reading_stdin(self):
        with (
            patch.object(uninstall_tty.os, "open", side_effect=OSError(errno.ENXIO)),
            patch.object(uninstall_tty.os, "read") as read,
        ):
            self.assertFalse(uninstall_tty.confirm(snapshot()))
        read.assert_not_called()

    def on_terminal(self, answer, *, queued=b""):
        plan = snapshot()
        start, release = os.pipe()
        pid, master = pty.fork()
        if pid == 0:
            result = 2
            try:
                os.close(release)
                if os.read(start, 1) != b"1":
                    os._exit(result)
                os.close(start)
                # The destination of fd 0 is not the approval authority.
                with open(os.devnull, "rb") as devnull:
                    os.dup2(devnull.fileno(), 0)
                    result = int(not uninstall_tty.confirm(plan))
            finally:
                os._exit(result)
        os.close(start)
        status = None
        try:
            try:
                if queued:
                    os.write(master, queued)
                os.write(release, b"1")
            finally:
                os.close(release)
            deadline = monotonic() + 3
            data = bytearray()
            while not data.endswith(uninstall_tty._PROMPT):
                self.assertGreater(deadline, monotonic())
                self.assertLess(len(data), 32768)
                ready, _, _ = select.select(
                    [master], [], [], max(0, deadline - monotonic())
                )
                self.assertEqual(ready, [master])
                data.extend(os.read(master, 2048))
            self.assertIn(b"oc-box uninstall review", data)
            self.assertIn(str(INSTALLATION.release).encode("ascii"), data)
            self.assertIn(b"All oc-box data and Podman workspaces are kept.", data)
            os.write(master, answer)
            while select.select([master], [], [], max(0, deadline - monotonic()))[0]:
                try:
                    if not os.read(master, 2048):
                        break
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
            exited, code = os.waitpid(pid, os.WNOHANG)
            # Closing the PTY yields EOF just before waitpid sees exit on
            # Linux. Use the already exhausted terminal, not a second read.
            if exited == 0:
                self.assertGreater(deadline, monotonic())
                exited, code = os.waitpid(pid, 0)
            status = code
            self.assertEqual(exited, pid)
            return os.waitstatus_to_exitcode(status)
        finally:
            os.close(master)
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_only_exact_fresh_phrase_on_actual_ctty_is_accepted(self):
        self.assertEqual(self.on_terminal(b"UNINSTALL OC-BOX\n"), 0)
        self.assertEqual(self.on_terminal(b"UNINSTALL OC-BOX \n"), 1)
        self.assertEqual(self.on_terminal(b"cancel\n", queued=b"UNINSTALL OC-BOX\n"), 1)
