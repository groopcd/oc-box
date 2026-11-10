"""Synthetic local PTYs only: no Podman, service, or real user terminal."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from oc_box import tui_terminal as terminal


def child(source):
    return [sys.executable, "-u", "-c", source]


class LocalPTYTests(unittest.TestCase):
    def setUp(self):
        self.transport = terminal.LocalPTY()
        self.addCleanup(self.transport.disconnect)

    def collect(self, predicate, *, timeout=4, limit=terminal.MAX_READ):
        content = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.transport.read(limit)
            self.assertLessEqual(len(chunk), min(limit, terminal.MAX_READ))
            content.extend(chunk)
            if predicate(bytes(content)):
                return bytes(content)
            time.sleep(0.005)
        self.fail("Synthetic PTY child did not reach its bounded checkpoint")

    def test_import_and_transport_without_optional_dependencies(self):
        source_root = str(Path(terminal.__file__).resolve().parents[1])
        code = """
from oc_box.tui_terminal import LocalPTY, NativeTerminal, TerminalError, pyte
assert pyte is None
assert LocalPTY().state == 'disconnected'
try:
    NativeTerminal()
except TerminalError as error:
    assert 'requires textual' in str(error)
else:
    raise AssertionError('optional dependency refusal absent')
"""
        result = subprocess.run(
            [sys.executable, "-S", "-c", code],
            env={"PYTHONPATH": source_root},
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"")

    def test_explicit_environment_tty_and_suppressed_private_stderr(self):
        source = """
import os, sys
os.write(2, b'PRIVATE_SYNTHETIC_DIAGNOSTIC')
assert os.isatty(0) and os.isatty(1)
assert 'SYNTHETIC_AMBIENT_SECRET' not in os.environ
assert os.environ['SAFE_FIXTURE'] == 'yes'
assert os.environ['TERM'] == 'xterm-256color'
os.write(1, b'PUBLIC_READY')
"""
        env = {"SAFE_FIXTURE": "yes"}
        with patch.dict(os.environ, {"SYNTHETIC_AMBIENT_SECRET": "not-inherited"}):
            self.transport.connect(child(source), env)
            content = self.collect(lambda data: self.transport.state == "exited")
        self.assertEqual(content, b"PUBLIC_READY")
        self.assertEqual(env, {"SAFE_FIXTURE": "yes"})

    def test_raw_control_input_does_not_signal_ui_and_sigwinch_is_unchanged(self):
        source = """
import os
os.write(1, b'READY')
value = bytearray()
while len(value) < 9:
    value.extend(os.read(0, 9-len(value)))
os.write(1, value.hex().encode())
"""
        before = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGWINCH)
        }
        self.transport.connect(child(source), {})
        self.collect(lambda data: b"READY" in data)
        payload = b"\x03\x1b[A\r" + "中".encode() + b"!"
        self.assertEqual(len(payload), 9)
        self.assertTrue(self.transport.send(payload))
        content = self.collect(lambda data: payload.hex().encode() in data)
        self.assertIn(payload.hex().encode(), content)
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_resize_sets_dimensions_and_signals_only_client_group(self):
        source = """
import os, signal, time
def resized(*args):
    size = os.get_terminal_size(1)
    os.write(1, ('SIZE:%s:%s' % (size.columns, size.lines)).encode())
signal.signal(signal.SIGWINCH, resized)
resized()
while True:
    time.sleep(.01)
"""
        self.transport.connect(child(source), {}, columns=90, rows=30)
        self.collect(lambda data: b"SIZE:90:30" in data)
        pid = self.transport.process.pid
        self.assertNotEqual(os.getpgrp(), pid)
        with patch.object(terminal.os, "killpg", wraps=os.killpg) as kill:
            self.transport.resize(112, 38)
            kill.assert_called_once_with(pid, signal.SIGWINCH)
        self.collect(lambda data: b"SIZE:112:38" in data)
        self.transport.resize(100000, 100000)
        self.collect(lambda data: b"SIZE:400:160" in data)

    def test_bounded_reads_apply_backpressure_without_losing_output(self):
        size = terminal.MAX_READ * 4 + 17
        self.transport.connect(
            child(
                f"import os; data=b'x'*{size}\nwhile data:\n n=os.write(1,data); data=data[n:]"
            ),
            {},
        )
        content = self.collect(
            lambda data: self.transport.state == "exited", limit=4096
        )
        self.assertEqual(content, b"x" * size)
        self.assertEqual(self.transport._pending, b"")

    def test_bounded_input_rejects_whole_paste(self):
        self.transport.connect(child("import time; time.sleep(5)"), {})
        self.assertFalse(self.transport.send(b"x" * (terminal.MAX_INPUT + 1)))
        self.assertEqual(self.transport._pending, b"")
        with patch.object(terminal.os, "write", side_effect=BlockingIOError):
            self.assertTrue(self.transport.send(b"x" * terminal.MAX_INPUT))
            self.assertFalse(self.transport.send(b"!"))
            self.assertEqual(len(self.transport._pending), terminal.MAX_INPUT)

    def test_disconnect_kills_reaps_client_and_releases_lease_once(self):
        callback = Mock()
        self.transport.connect(
            child(
                "import signal,time,os; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b'READY'); time.sleep(20)"
            ),
            {},
            callback,
        )
        self.collect(lambda data: b"READY" in data)
        process, fd = self.transport.process, self.transport.fd
        with patch.object(terminal.os, "killpg", wraps=os.killpg) as kill:
            self.transport.disconnect()
            self.assertEqual(
                [call.args for call in kill.call_args_list],
                [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
            )
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertIsNone(self.transport.process)
        self.assertIsNone(self.transport.fd)
        with self.assertRaises(OSError):
            os.fstat(fd)
        callback.assert_called_once_with()
        self.transport.disconnect()
        callback.assert_called_once_with()
        self.assertFalse(self.transport.send(b"exit\r"))

    def test_automatic_exit_releases_callback_and_reconnect_is_explicit(self):
        callback = Mock()
        self.transport.connect(child("print('done')"), {}, callback)
        self.collect(lambda data: self.transport.state == "exited")
        callback.assert_called_once_with()
        self.assertIsNone(self.transport.fd)
        self.transport.read()
        callback.assert_called_once_with()
        self.transport.connect(child("print('second')"), {})
        self.assertIn(
            b"second", self.collect(lambda data: self.transport.state == "exited")
        )

    def test_nonzero_client_exit_reports_fixed_status_without_stderr(self):
        self.transport.connect(
            child("import os,sys; os.write(2,b'PRIVATE_ERROR'); sys.exit(7)"), {}
        )
        self.assertEqual(self.collect(lambda data: self.transport.fd is None), b"")
        self.assertEqual(self.transport.returncode, 7)
        self.assertEqual(self.transport.state, "failed")
        self.assertEqual(
            self.transport.reason, "Native terminal client exited unsuccessfully."
        )

    def test_failed_spawn_and_callback_errors_have_only_fixed_diagnostics(self):
        callback = Mock()
        with self.assertRaisesRegex(
            terminal.TerminalError, "^Native terminal client could not start.$"
        ):
            self.transport.connect(
                ["/synthetic/private/missing-executable"], {}, callback
            )
        self.assertEqual(self.transport.state, "failed")
        callback.assert_called_once_with()
        self.assertIsNone(self.transport.fd)
        self.transport.connect(
            child("print('done')"),
            {},
            Mock(side_effect=RuntimeError("PRIVATE_CALLBACK_DETAIL")),
        )
        self.collect(lambda data: self.transport.fd is None)
        self.assertEqual(self.transport.reason, "Terminal lease release failed.")

    def test_invalid_specs_and_double_connect_cannot_replace_running_client(self):
        for command, env in (("shell string", {}), ([], {}), (["executable"], None)):
            with (
                self.subTest(command=command),
                self.assertRaises(terminal.TerminalError),
            ):
                self.transport.connect(command, env)
        callback = Mock()
        self.transport.connect(child("import time; time.sleep(5)"), {}, callback)
        process = self.transport.process
        with self.assertRaises(terminal.TerminalError):
            self.transport.connect(child("pass"), {})
        self.assertIs(self.transport.process, process)
        self.assertIsNone(process.poll())
        callback.assert_not_called()


class ControlFilterTests(unittest.TestCase):
    def test_split_unicode_and_ansi_preserve_cursor_and_text_only(self):
        value = terminal.ControlFilter()
        encoded = "A中e\u0301\x1b[2;4H\x1b[31mred\x1b[0m".encode()
        result = "".join(value.feed(bytes([byte])) for byte in encoded)
        self.assertEqual(result, encoded.decode())

    def test_osc_clipboard_links_titles_and_other_control_strings_never_escape(self):
        sequences = [
            b"\x1b]52;c;PRIVATE_CLIPBOARD\a",
            b"\x1b]8;;https://private.invalid\x1b\\",
            b"\x1b]0;PRIVATE_TITLE\a",
            b"\x1bPPRIVATE_DCS\x1b\\",
            b"\x1b_PRIVATE_APC\x1b\\",
            b"\x1b^PRIVATE_PM\x1b\\",
            b"\x1bXPRIVATE_SOS\x1b\\",
            "\x9d52;c;PRIVATE_C1\x9c".encode(),
        ]
        for sequence in sequences:
            with self.subTest(sequence=sequence):
                value = terminal.ControlFilter()
                result = "".join(
                    value.feed(bytes([byte]))
                    for byte in b"before" + sequence + b"after"
                )
                self.assertEqual(result, "beforeafter")
                self.assertEqual(value.escape, "")

    def test_unterminated_strings_and_overlong_csi_have_bounded_storage(self):
        value = terminal.ControlFilter()
        self.assertEqual(value.feed(b"\x1b]52;c;" + b"x" * 200000), "")
        self.assertEqual(value.escape, "")
        self.assertEqual(value.feed(b"\x1b\\safe"), "safe")
        self.assertEqual(value.feed(b"\x1b[" + b"1;" * 100000 + b"msafe"), "safe")
        self.assertEqual(value.escape, "")
        self.assertEqual(value.feed(b"\x00\x07\x7f"), "")

    def test_charset_escape_and_c1_csi_do_not_leak_control_bytes(self):
        value = terminal.ControlFilter()
        self.assertEqual(value.feed(b"\x1b(B\x1b#8"), "\x1b(B\x1b#8")
        self.assertEqual(value.feed("\x9b2J".encode()), "\x1b[2J")


class KeyTests(unittest.TestCase):
    def test_terminal_key_matrix(self):
        expected = {
            "ctrl+c": b"\x03",
            "ctrl+d": b"\x04",
            "ctrl+z": b"\x1a",
            "enter": b"\r",
            "up": b"\x1b[A",
            "left": b"\x1b[D",
            "home": b"\x1b[H",
            "tab": b"\t",
            "backspace": b"\x7f",
            "alt+x": b"\x1bx",
            "ctrl+left": b"\x1b[1;5D",
            "shift+up": b"\x1b[1;2A",
            "ctrl+alt+right": b"\x1b[1;7C",
            "ctrl+right_square_bracket": None,
            "ctrl+]": None,
            "unmapped": None,
        }
        for key, content in expected.items():
            with self.subTest(key=key):
                self.assertEqual(terminal.key_bytes(key), content)
        self.assertEqual(terminal.key_bytes("中", "中"), "中".encode())
        self.assertEqual(terminal.key_bytes("up", application_cursor=True), b"\x1bOA")


@unittest.skipIf(terminal.pyte is None, "optional pyte not installed")
class EmulatorTests(unittest.TestCase):
    def test_combining_only_output_cannot_grow_a_cell_without_bound(self):
        emulator = terminal.TerminalEmulator(20, 3)
        emulator.feed(b"x")
        for _ in range(10):
            emulator.feed(("\u0300" * 2000).encode())
        self.assertLessEqual(
            len(emulator.screen.buffer[0][0].data), terminal.MAX_CELL_CODEPOINTS
        )

    def test_malformed_commands_have_only_a_fixed_failure(self):
        emulator = terminal.TerminalEmulator()
        with self.assertRaisesRegex(
            terminal.TerminalError, "^Native terminal output is unsupported.$"
        ):
            emulator.feed(b"\x1b[1;2;3A")

    def test_ansi_cursor_colors_unicode_width_and_combining(self):
        emulator = terminal.TerminalEmulator(20, 4)
        encoded = "abc\rX\x1b[2;3H中e\u0301\x1b[31mR\x1b[0m".encode()
        for byte in encoded:
            emulator.feed(bytes([byte]))
        self.assertTrue(emulator.screen.display[0].startswith("Xbc"))
        self.assertEqual(emulator.screen.buffer[1][2].data, "中")
        self.assertEqual(emulator.screen.buffer[1][3].data, "")
        self.assertEqual(emulator.screen.buffer[1][4].data, "é")
        self.assertEqual(emulator.screen.buffer[1][5].fg, "red")

    def test_alternate_screen_restores_main_cursor_and_does_not_pollute_history(self):
        emulator = terminal.TerminalEmulator(12, 3)
        emulator.feed(b"main\x1b[?1049halternate\r\nline2\r\nline3\r\nline4")
        self.assertIs(emulator.screen, emulator.alternate)
        self.assertEqual(len(emulator.alternate.history.top), 0)
        emulator.feed(b"\x1b[?1049l!")
        self.assertIs(emulator.screen, emulator.main)
        self.assertTrue(emulator.screen.display[0].startswith("main!"))
        self.assertEqual(len(emulator.main.history.top), 0)
        emulator.feed(b"\x1b[?1049h")
        self.assertTrue(all(not line.strip() for line in emulator.screen.display))

    def test_bounded_scrollback_savepoints_modes_and_resize(self):
        emulator = terminal.TerminalEmulator(10, 3, history=7)
        emulator.feed(b"line\r\n" * 2000 + b"\x1b7" * 200)
        self.assertEqual(len(emulator.main.history.top), 7)
        self.assertLessEqual(len(emulator.main.savepoints), 16)
        for mode in range(500):
            emulator.feed(f"\x1b[?{mode}h".encode())
        self.assertLess(len(emulator.main.mode), 12)
        emulator.resize(100000, 100000)
        for screen in (emulator.main, emulator.alternate):
            self.assertEqual((screen.columns, screen.lines), (400, 160))
        emulator.feed(b"\x1b[?3h")  # Child cannot resize the pane via DECCOLM.
        self.assertEqual(emulator.screen.columns, 400)

    def test_osc_never_reaches_screen_links_or_output_and_reports_reply_locally(self):
        send = Mock()
        emulator = terminal.TerminalEmulator(20, 3, send=send)
        emulator.feed(
            b"\x1b]52;c;PRIVATE\a\x1b]8;;https://private.invalid\x1b\\label\x1b]8;;\x1b\\"
        )
        self.assertTrue(emulator.screen.display[0].startswith("label"))
        send.assert_not_called()
        emulator.feed(b"\x1b[6n")
        send.assert_called_once_with(b"\x1b[1;6R")
        emulator.feed(b"\x1b[?6n")
        self.assertEqual(send.call_args.args, (b"\x1b[?1;6R",))

    def test_bracketed_paste_and_application_keys_follow_child_modes(self):
        emulator = terminal.TerminalEmulator()
        self.assertEqual(emulator.paste("a\nb"), b"a\rb")
        emulator.feed(b"\x1b[?1h\x1b[?2004h")
        self.assertEqual(emulator.key("up"), b"\x1bOA")
        self.assertEqual(
            emulator.paste("a\r\nb\x1d\x1b[201~"), b"\x1b[200~a\rb[201~\x1b[201~"
        )
        emulator.feed(b"\x1b[?1l\x1b[?2004l")
        self.assertEqual(emulator.key("up"), b"\x1b[A")
        self.assertEqual(emulator.paste("x"), b"x")


try:
    from textual.app import App, ComposeResult
    from textual.widgets import Button
    from textual import events
    from textual.geometry import Offset
    from textual.selection import Selection
except ImportError:
    App = None


@unittest.skipIf(
    App is None or terminal.pyte is None, "optional Textual/pyte not installed"
)
class NativeTerminalTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        class TerminalApp(App):
            BINDINGS = [("ctrl+c", "count_ctrl_c", "Synthetic dashboard shortcut")]

            def __init__(self):
                super().__init__()
                self.terminal = terminal.NativeTerminal()
                self.ctrl_c_count = 0
                self.detached = False
                self.states = []

            def compose(self) -> ComposeResult:
                yield self.terminal
                yield Button("Other", id="other")

            def action_count_ctrl_c(self):
                self.ctrl_c_count += 1

            def on_native_terminal_detached(self, event):
                self.detached = True
                self.query_one("#other").focus()

            def on_native_terminal_state_changed(self, event):
                self.states.append(event.state)

        return TerminalApp()

    async def until(self, pilot, predicate):
        for _ in range(100):
            await pilot.pause(0.02)
            if predicate():
                return
        self.fail("Headless terminal did not reach its bounded checkpoint")

    async def test_mount_focus_and_resize_never_start_a_client(self):
        app = self.app()
        with patch.object(terminal.subprocess, "Popen") as popen:
            async with app.run_test(size=(80, 24)) as pilot:
                app.terminal.focus()
                await pilot.pause()
                await pilot.resize_terminal(100, 30)
                self.assertEqual(app.terminal.state, "disconnected")
                popen.assert_not_called()

    async def test_focused_keys_paste_and_detach_do_not_trigger_dashboard_bindings(
        self,
    ):
        app = self.app()
        callback = Mock()
        async with app.run_test(size=(80, 24)) as pilot:
            widget = app.terminal
            widget.connect(
                child("import os,time; os.write(1,b'READY'); time.sleep(20)"),
                {},
                callback,
            )
            widget.focus()
            await self.until(
                pilot, lambda: "READY" in widget.emulator.screen.display[0]
            )
            widget.emulator.feed(b"\x1b[?2004h")
            with patch.object(widget.transport, "send", return_value=True) as send:
                await pilot.press("ctrl+c", "up", "enter", "tab")
                widget.post_message(events.Paste("x\ny"))
                await pilot.pause()
                self.assertEqual(
                    [call.args[0] for call in send.call_args_list],
                    [b"\x03", b"\x1b[A", b"\r", b"\t", b"\x1b[200~x\ry\x1b[201~"],
                )
                self.assertEqual(app.ctrl_c_count, 0)
                self.assertTrue(widget.has_focus)
                await pilot.press("ctrl+right_square_bracket")
                await pilot.pause()
                self.assertEqual(len(send.call_args_list), 5)
            self.assertTrue(app.detached)
            self.assertEqual(widget.state, "disconnected")
            self.assertTrue(app.query_one("#other").has_focus)
            callback.assert_called_once_with()
            self.assertIn("connected", app.states)
            self.assertIn("disconnected", app.states)
            await pilot.press("ctrl+c")
            self.assertEqual(app.ctrl_c_count, 1)

    async def test_literal_cell_rendering_colors_wide_text_and_unmount_cleanup(self):
        app = self.app()
        callback = Mock()
        async with app.run_test(size=(60, 16)) as pilot:
            widget = app.terminal
            command = child(
                "import os,time; os.write(1, '[bold]中é\\x1b[31mR\\x1b[0m\\x1b]52;c;PRIVATE\\a'.encode()); time.sleep(20)"
            )
            widget.connect(command, {}, callback)
            await self.until(
                pilot, lambda: "[bold]" in widget.emulator.screen.display[0]
            )
            line = widget.render_line(0)
            self.assertTrue(line.text.startswith("[bold]中éR"))
            self.assertNotIn("PRIVATE", line.text)
            self.assertNotIn("\x1b", line.text)
            self.assertFalse(list(line)[0].style.bold)
            self.assertEqual(line.cell_length, widget.content_size.width)
            self.assertTrue(
                any(
                    segment.text == "R" and segment.style.color.name == "red"
                    for segment in line
                )
            )
            process = widget.transport.process
            await pilot.resize_terminal(90, 28)
            self.assertEqual(widget.emulator.screen.columns, widget.content_size.width)
            self.assertEqual(
                os.get_terminal_size(widget.transport.fd).columns,
                widget.content_size.width,
            )
        self.assertIsNotNone(process.poll())
        callback.assert_called_once_with()
        self.assertIsNone(widget.transport.fd)

    async def test_unsupported_output_disconnects_without_a_ui_exception(self):
        app = self.app()
        callback = Mock()
        async with app.run_test(size=(60, 16)) as pilot:
            widget = app.terminal
            widget.connect(
                child("import os,time; os.write(1,b'\\x1b[1;2;3A'); time.sleep(20)"),
                {},
                callback,
            )
            await self.until(pilot, lambda: widget.state == "failed")
            self.assertEqual(widget.reason, "Native terminal output is unsupported.")
            self.assertIsNone(widget.transport.fd)
            callback.assert_called_once_with()

    async def test_out_of_range_truecolor_output_cannot_crash_rich_rendering(self):
        app = self.app()
        async with app.run_test(size=(60, 16)):
            widget = app.terminal
            widget.emulator.feed(b"\x1b[38;2;256;0;0mX\x1b[48;2;999;0;0mY")
            line = widget.render_line(0)
            self.assertTrue(line.text.startswith("XY"))
            self.assertEqual(line.cell_length, widget.content_size.width)
            self.assertIsNone(widget._color("1000000"))
            self.assertIsNone(widget._color("not-a-color"))

    async def test_visible_output_selection_uses_literal_unicode_not_widget_name(self):
        app = self.app()
        async with app.run_test(size=(60, 16)) as pilot:
            widget = app.terminal
            with patch.object(app, "copy_to_clipboard") as copy:
                widget.emulator.feed("界e\u0301 text\x1b]52;c;PRIVATE\a".encode())
                app.screen.selections = {widget: Selection(Offset(0, 0), Offset(2, 0))}
                await pilot.pause()
                # pyte normalizes the combining sequence to its displayed NFC
                # cell; selection coordinates index that rendered text.
                self.assertEqual(app.screen.get_selected_text(), "界é")
                self.assertNotIn("PRIVATE", widget.render_line(0).text)
                copy.assert_not_called()

    async def test_paste_is_atomic_for_utf8_framing_and_full_queue_limits(self):
        app = self.app()
        async with app.run_test(size=(60, 16)) as pilot:
            widget = app.terminal
            widget.transport.state = "connected"
            widget.focus()
            with (
                patch.object(widget.transport, "send", return_value=True) as send,
                patch.object(widget, "notify") as notify,
            ):
                for content in (
                    "x" * (terminal.MAX_INPUT + 1),
                    "界" * (terminal.MAX_INPUT // 3 + 1),
                ):
                    with self.subTest(kind="encoded-limit", length=len(content)):
                        widget.post_message(events.Paste(content))
                        await pilot.pause()
                        send.assert_not_called()
                        self.assertEqual(
                            notify.call_args.args[0],
                            "Paste exceeds the terminal's 64 KiB input limit.",
                        )
                widget.emulator.feed(b"\x1b[?2004h")
                widget.post_message(events.Paste("x" * terminal.MAX_INPUT))
                await pilot.pause()
                send.assert_not_called()
                widget.post_message(events.Paste("x" * (terminal.MAX_INPUT - 12)))
                await pilot.pause()
                self.assertEqual(len(send.call_args.args[0]), terminal.MAX_INPUT)
                send.reset_mock()
                send.return_value = False
                widget.post_message(events.Paste("synthetic queue input"))
                await pilot.pause()
                send.assert_called_once()
                self.assertEqual(
                    notify.call_args.args[0],
                    "Terminal input is busy; the paste was not queued.",
                )
                self.assertEqual(widget.state, "connected")

    async def test_paste_transport_failure_warns_about_partial_delivery(self):
        app = self.app()
        callback = Mock()
        async with app.run_test(size=(60, 16)) as pilot:
            widget = app.terminal
            widget.connect(
                child("import os,time; os.write(1,b'READY'); time.sleep(20)"),
                {},
                callback,
            )
            widget.focus()
            await self.until(
                pilot, lambda: "READY" in widget.emulator.screen.display[0]
            )
            with (
                patch.object(
                    terminal.os,
                    "write",
                    side_effect=[2, BrokenPipeError("PRIVATE_TRANSPORT_DETAIL")],
                ) as write,
                patch.object(widget, "notify") as notify,
            ):
                widget.post_message(events.Paste("synthetic command"))
                await pilot.pause()
                self.assertEqual(write.call_count, 2)
                self.assertEqual(widget.state, "failed")
                self.assertEqual(
                    notify.call_args.args[0],
                    "Terminal input failed; paste may have been partly sent. Inspect the shell before retrying.",
                )
                self.assertNotIn("PRIVATE", notify.call_args.args[0])
            callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
