import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
from oc_box import oc_box as box, sync_config as sync, terminal_ui as ui

APP = Path(__file__).resolve().parents[1]


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, value in files.items():
            data = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


class ConfigCacheTests(unittest.TestCase):
    def test_cold_sync_uses_runtime_sdk_and_drops_incompatible_host_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}',
                "package-lock.json": "old host resolution",
            }
            payload = archive(host)
            self.assertFalse(sync.sync_config(root, payload, "1.19.5"))
            config = root / ".config/opencode"
            effective = json.loads((config / "package.json").read_text())
            self.assertEqual(
                effective["dependencies"],
                {"@opencode-ai/plugin": "1.19.5", "custom": "2"},
            )
            self.assertFalse((config / "package-lock.json").exists())
            self.assertIn("1.15.11", host["package.json"])

    def test_cold_sync_keeps_lock_for_matching_sdk(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}',
                "package-lock.json": "matching resolution",
            }
            sync.sync_config(root, archive(host), "1.19.5")
            self.assertEqual(
                (root / ".config/opencode/package-lock.json").read_text(),
                "matching resolution",
            )

    def test_unchanged_dependencies_survive_but_stale_plugins_are_removed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            modules = config / "node_modules"
            modules.mkdir(parents=True)
            manifest = json.dumps({"dependencies": {"@opencode-ai/plugin": "1.19.5"}})
            (config / "package.json").write_text(manifest)
            (modules / "cached").write_text("already installed")
            (config / "stale-plugin.ts").write_text("do not load again")
            sync.sync_config(
                root,
                archive(
                    {"package.json": manifest, "plugins/current.ts": "current plugin"}
                ),
            )
            self.assertEqual((modules / "cached").read_text(), "already installed")
            self.assertFalse((config / "stale-plugin.ts").exists())
            self.assertTrue((config / "plugins/current.ts").is_file())

    def test_manifest_lock_and_runtime_changes_invalidate_cold_and_warm_caches(self):
        manifest = '{"dependencies":{"example":"^1"}}'
        sdk = '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
        cases = (
            (
                "cold manifest",
                {"package.json": '{"dependencies":{"old":"1"}}'},
                {"package.json": '{"dependencies":{"new":"2"}}'},
                None,
                None,
                False,
            ),
            (
                "cold lock",
                {"package.json": manifest, "package-lock.json": "old resolution"},
                {"package.json": manifest, "package-lock.json": "new resolution"},
                None,
                None,
                False,
            ),
            (
                "recorded lock",
                {"package.json": manifest, "package-lock.json": "old resolution"},
                {"package.json": manifest, "package-lock.json": "new resolution"},
                "1.19.5",
                "1.19.5",
                True,
            ),
            (
                "recorded runtime",
                {"package.json": sdk},
                {"package.json": sdk},
                "1.19.5",
                "1.19.6",
                True,
            ),
        )
        for label, before, after, runtime, replacement, recorded in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / ".config/opencode"
                if recorded:
                    sync.sync_config(root, archive(before), runtime)
                else:
                    config.mkdir(parents=True)
                    for name, content in before.items():
                        (config / name).write_text(content)
                modules = config / "node_modules"
                modules.mkdir()
                (modules / "cached").write_text("old")
                self.assertFalse(sync.sync_config(root, archive(after), replacement))
                self.assertFalse(modules.exists())

    def test_unsafe_archive_does_not_remove_existing_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            config.mkdir(parents=True)
            (config / "keep").write_text("original")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                sync.sync_config(root, archive({"../../escaped": "bad"}))
            self.assertEqual((config / "keep").read_text(), "original")

    def test_copied_config_directory_is_private(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            sync.sync_config(root, archive({"opencode.json": "{}"}))
            self.assertEqual((root / ".config/opencode").stat().st_mode & 0o777, 0o700)

    def test_warm_sync_preserves_runtime_generated_sdk_and_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}',
                "package-lock.json": '{"host":"old sdk lock"}',
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            # Mainline upgrades the SDK, manifest, and lock after the first sync.
            runtime = '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            lock = '{"container":"runtime sdk lock"}'
            (config / "package.json").write_text(runtime)
            (config / "package-lock.json").write_text(lock)
            (config / "node_modules").mkdir()
            (config / "node_modules/cached").write_text("already installed")
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertEqual((config / "package.json").read_text(), runtime)
            self.assertEqual((config / "package-lock.json").read_text(), lock)
            self.assertEqual(
                (config / "node_modules/cached").read_text(), "already installed"
            )
            # Warm sync must remain idempotent over many launches.
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))

    def test_sdk_only_existing_workspace_adopts_installed_runtime_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            )
            (config / "package-lock.json").write_text('{"container":"generated"}')
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
            }
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertTrue(plugin.is_dir())
            self.assertEqual(
                json.loads((config / "package.json").read_text())["dependencies"][
                    "@opencode-ai/plugin"
                ],
                "1.19.5",
            )

    def test_arbitrary_custom_dependencies_are_not_adopted_on_sdk_match(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5","custom":"1"}}'
            )
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}'
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertFalse(plugin.exists())


class StartupProgressTests(unittest.TestCase):
    class TTY(io.StringIO):
        def __init__(self, encoding="utf-8"):
            super().__init__()
            self._encoding = encoding
            self.updated = threading.Event()
            self.draws = 0

        @property
        def encoding(self):
            return self._encoding

        def isatty(self):
            return True

        def fileno(self):
            return 123

        def write(self, text):
            count = super().write(text)
            if "\r" in text and "elapsed" in text:
                self.draws += 1
                if self.draws >= 2:
                    self.updated.set()
            return count

    @contextlib.contextmanager
    def terminal(self, columns=80):
        with (
            mock.patch.dict(os.environ, {"TERM": "xterm", "COLUMNS": "999"}),
            mock.patch.object(
                box.os,
                "get_terminal_size",
                return_value=os.terminal_size((columns, 24)),
            ) as size,
        ):
            yield size

    @contextlib.contextmanager
    def workers(self):
        original = threading.Thread
        workers = []

        def create(**kwargs):
            worker = original(**kwargs)
            workers.append(worker)
            return worker

        with mock.patch.object(box.threading, "Thread", side_effect=create):
            yield workers
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_heartbeat_stops_after_stage_finishes(self):
        output = self.TTY()
        clock = [10.0]
        with (
            self.terminal(),
            self.workers() as workers,
            mock.patch.object(box.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            with box.loading_stage("Starting test", interval=0.01, stream=output):
                clock[0] = 16.0
                self.assertTrue(output.updated.wait(timeout=1), output.getvalue())
                self.assertNotIn("\n", output.getvalue())
            snapshot = output.getvalue()
            self.assertEqual(len(workers), 1)
            self.assertFalse(workers[0].is_alive())
        self.assertIn("6s elapsed", snapshot)
        self.assertTrue(snapshot.endswith("Starting test done (6.0s)\n"), snapshot)
        self.assertEqual(snapshot.count("\n"), 1)
        self.assertEqual(snapshot, output.getvalue())

    def test_spinner_rotates_with_unicode_and_ascii_fallback(self):
        for encoding, frames in (
            ("utf-8", "⠋⠙⠹"),
            ("ascii", "|/-"),
            (None, "|/-"),
            ("unknown-encoding", "|/-"),
        ):
            with self.subTest(encoding=encoding):
                output = self.TTY(encoding)
                with (
                    self.terminal(),
                    mock.patch.object(box.threading, "Thread") as worker,
                    mock.patch.object(box.threading, "Event") as event,
                    mock.patch.object(box.time, "monotonic", return_value=0.0),
                ):
                    with box.loading_stage("Spinning", stream=output):
                        heartbeat = worker.call_args.kwargs["target"]
                        for _ in range(2):
                            event.return_value.wait.side_effect = [False, True]
                            heartbeat()
                for frame in frames:
                    self.assertIn(
                        f"\roc-box: {frame} Spinning",
                        output.getvalue().replace("\x1b[K", ""),
                    )
                event.return_value.wait.assert_called_with(0.1)
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertTrue(
                    output.getvalue().endswith("oc-box: Spinning done (0.0s)\n")
                )
                self.assertNotIn("%", output.getvalue())

    def test_plain_output_has_start_and_final_lines_without_heartbeat(self):
        for kind in ("redirected", "dumb", "unknown width", "zero width"):
            with self.subTest(kind=kind):
                output = io.StringIO() if kind == "redirected" else self.TTY()
                with (
                    self.terminal() as size,
                    mock.patch.object(box.threading, "Thread") as worker,
                    mock.patch.object(box.time, "monotonic", side_effect=[10.0, 31.0]),
                    mock.patch.dict(
                        os.environ, {"TERM": "dumb" if kind == "dumb" else "xterm"}
                    ),
                ):
                    if kind == "unknown width":
                        size.side_effect = OSError("no terminal size")
                    elif kind == "zero width":
                        size.return_value = os.terminal_size((0, 0))
                    with box.loading_stage("Syncing session history", stream=output):
                        pass
                worker.assert_not_called()
                self.assertEqual(
                    output.getvalue(),
                    "oc-box: Syncing session history...\n"
                    "oc-box: Syncing session history done (21.0s)\n",
                )
                self.assertNotIn("\r", output.getvalue())
                self.assertNotIn("\x1b", output.getvalue())

    def test_stage_failure_is_not_reported_as_success(self):
        output = io.StringIO()
        failure = ValueError("failure")
        with (
            self.assertRaises(ValueError) as raised,
            box.loading_stage("Failing", stream=output),
        ):
            raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(output.getvalue().count("\n"), 2)
        self.assertIn("failed", output.getvalue())
        self.assertNotIn("done", output.getvalue())

    def test_tty_failure_and_interrupt_preserve_identity_and_restore_streams(self):
        for failure in (ValueError("failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                output = self.TTY()
                with (
                    self.terminal(),
                    self.workers() as workers,
                    mock.patch.object(sys, "stderr", output),
                ):
                    with self.assertRaises(type(failure)) as raised:
                        with box.loading_stage("Failing"):
                            self.assertIsNot(sys.stderr, output)
                            raise failure
                    self.assertIs(sys.stderr, output)
                    self.assertEqual(len(workers), 1)
                    self.assertFalse(workers[0].is_alive())
                self.assertIs(raised.exception, failure)
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertIn("Failing failed after ", output.getvalue())
                self.assertNotIn("done", output.getvalue())

    def test_tty_frames_use_stream_fd_and_fit_resized_terminal_cells(self):
        for columns in (2, 12, 24, 80):
            with self.subTest(columns=columns):
                output = self.TTY()
                with self.terminal() as size, self.workers():
                    with box.loading_stage(
                        "界e\u0301" * 50 + "\nunsafe", stream=output
                    ):
                        size.return_value = os.terminal_size((columns, 24))
                frames = output.getvalue().replace("\x1b[K", "").split("\r")[1:]
                self.assertEqual(len(frames), 2)
                for frame, limit in zip(frames, (79, columns - 1)):
                    cells = len(
                        frame.rstrip("\n").replace("界", "xx").replace("\u0301", "")
                    )
                    self.assertLessEqual(cells, limit, frame)
                self.assertIn("界", frames[0])
                self.assertIn("0s elapsed", frames[0])
                if columns >= 24:
                    self.assertIn("done (", frames[-1])
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertTrue(output.getvalue().endswith("\n"))
                self.assertTrue(
                    all(call == mock.call(123) for call in size.call_args_list)
                )

    def test_python_output_is_separated_and_partial_lines_suspend_redraws(self):
        output = self.TTY()
        with (
            self.terminal(),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(box.threading, "Thread") as worker,
            mock.patch.object(box.threading, "Event") as event,
            mock.patch.object(box.time, "monotonic", return_value=0.0),
        ):
            with box.loading_stage("Syncing"):
                heartbeat = worker.call_args.kwargs["target"]
                sys.stderr.write("warning: retained ")
                snapshot = output.getvalue()
                event.return_value.wait.side_effect = [False, True]
                heartbeat()
                self.assertEqual(snapshot, output.getvalue())
                sys.stderr.writelines(["original", " history\n"])
                print("actual stdout output", flush=True)
                event.return_value.wait.side_effect = [False, True]
                heartbeat()
                self.assertGreater(output.draws, 1)
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, output)
        self.assertIn("\r\x1b[Kwarning: retained original history\n", output.getvalue())
        self.assertIn("actual stdout output\n", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 3)
        self.assertTrue(output.getvalue().endswith("Syncing done (0.0s)\n"))
        event.return_value.set.assert_called_once_with()
        worker.return_value.join.assert_called_once_with()

    def test_partial_python_output_is_terminated_before_final_status(self):
        output = self.TTY()
        with self.terminal(), mock.patch.object(sys, "stderr", output), self.workers():
            with box.loading_stage("Syncing"):
                sys.stderr.write("actual output without a newline")
        self.assertIn("actual output without a newline\n\r\x1b[K", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 2)

    def test_nested_stages_restore_output_and_keep_both_results(self):
        output = self.TTY()
        with self.terminal(), mock.patch.object(sys, "stderr", output), self.workers():
            with box.loading_stage("Outer"):
                outer = sys.stderr
                with box.loading_stage("Inner"):
                    print("actual inner output", file=sys.stderr)
                self.assertIs(sys.stderr, outer)
            self.assertIs(sys.stderr, output)
        self.assertIn("actual inner output\n", output.getvalue())
        self.assertIn("Inner done (", output.getvalue())
        self.assertIn("Outer done (", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 3)

    def test_progress_output_failure_does_not_replace_operation_failure(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                output = self.TTY() if interactive else io.StringIO()
                failure = KeyboardInterrupt()
                with self.terminal(), self.workers() as workers:
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        with box.loading_stage("Failing", stream=output):
                            output.close()
                            raise failure
                    self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertIs(raised.exception, failure)

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_loading_stage_pty_resize_has_one_final_newline(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)

        def read_frame():
            self.assertTrue(select.select([master], [], [], 1)[0])
            return os.read(master, 65536)

        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 48, 0, 0))
            with (
                os.fdopen(os.dup(slave), "w", encoding="utf-8") as output,
                mock.patch.dict(os.environ, {"TERM": "xterm", "COLUMNS": "999"}),
            ):
                with box.loading_stage(
                    "Syncing " + "history " * 20, interval=60, stream=output
                ):
                    initial = read_frame()
                    self.assertNotIn(b"\n", initial)
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 32, 0, 0)
                    )
                final = read_frame()
            self.assertLessEqual(len(initial.decode().replace("\r\x1b[K", "")), 47)
            self.assertLessEqual(
                len(final.replace(b"\r\x1b[K", b"").rstrip(b"\r\n")), 31
            )
            self.assertIn(b"done (", final)
            self.assertEqual((initial + final).count(b"\n"), 1)
            self.assertTrue(final.endswith(b"\r\n"))
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(master)
            os.close(slave)

    def test_terminal_setup_is_not_mistaken_for_rendered_ui(self):
        detector = ui.VisibleOutput()
        for chunk in (
            b"\x1b[?1049h\x1b[",
            b"2J\x1b[38;2;0;0;0m  \r\n",
            b"\x1b]0;OpenCode",
            b"\x07",
            b"\x1bP+q544e\x1b",
            b"\\",
        ):
            self.assertFalse(detector.feed(chunk))
        self.assertTrue(detector.feed("OpenCode ▀".encode()))

    def test_loading_indicator_never_interrupts_partial_terminal_sequences(self):
        detector = ui.VisibleOutput()
        detector.feed(b"\x1b]0;Title")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"\x07\x1b[38;2;")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"0;0;0m")
        self.assertTrue(detector.safe_to_draw())
        detector.feed(b"\xe2\x96")
        self.assertFalse(detector.safe_to_draw())
        self.assertTrue(detector.feed(b"\x80"))
        self.assertTrue(detector.safe_to_draw())

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_tui_progress_handoff_input_resize_and_terminal_restore(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios

        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        fixture = (
            "import os,sys,time,tty; assert os.environ['OC_BOX_RELAY_CHECK']=='present'; tty.setraw(0); "
            "os.write(1,b'\\x1b[?1049h\\x1b[2J\\x1b]0;Booting\\x07'); "
            "time.sleep(0.2); os.write(1,b'UI_READY'); "
            "key=os.read(0,1); size=os.get_terminal_size(0); "
            "os.write(1,b'KEY='+key+(' SIZE=%dx%d'%(size.columns,size.lines)).encode()); "
            "time.sleep(0.15); os.write(1,b'\\x1b[?1049l')"
        )
        relay = "import os,sys; sys.path.insert(0,sys.argv[1]+'/src'); from oc_box.terminal_ui import run_tui; sys.exit(run_tui([sys.executable,'-c',sys.argv[2]],env={**os.environ,'OC_BOX_RELAY_CHECK':'present'},refresh=0.03))"
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", relay, str(APP), fixture],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        output = bytearray()
        sent = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
                if b"UI_READY" in output and not sent:
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0)
                    )
                    os.kill(process.pid, signal.SIGWINCH)
                    time.sleep(0.05)
                    os.write(master, b"z")
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertIsNotNone(process.poll(), bytes(output))
            self.assertEqual(process.returncode, 0, bytes(output))
            self.assertIn(b"Loading OpenCode, plugins and MCPs", output)
            self.assertIn(b"KEY=z SIZE=120x32", output)
            self.assertNotIn(b"oc-box:", bytes(output).split(b"UI_READY", 1)[1])
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
