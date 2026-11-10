import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from oc_box import secret_prompt

APP = Path(__file__).resolve().parents[1]
VALUE = "synthetic-PromptValue-0987"


class PassphrasePolicyTests(unittest.TestCase):
    def test_separate_saved_minimums_and_command_override_with_eight_character_floor(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="oc-box-password-policy-") as temporary:
            config = Path(temporary) / "oc-box/config.json"
            config.parent.mkdir()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                self.assertEqual(secret_prompt.passphrase_minimum("vault"), 8)
                self.assertEqual(secret_prompt.passphrase_minimum("backup"), 8)
                config.write_text(
                    '{"vault_min_passphrase_length":12,"backup_min_passphrase_length":20}'
                )
                self.assertEqual(secret_prompt.passphrase_minimum("vault"), 12)
                self.assertEqual(secret_prompt.passphrase_minimum("backup"), 20)
                self.assertEqual(secret_prompt.passphrase_minimum("backup", 8), 8)
                for bad in (7, 1025, True, 8.0, "8", None):
                    config.write_text(json.dumps({"vault_min_passphrase_length": bad}))
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        secret_prompt.passphrase_minimum("vault")
                config.write_text("{")
                with self.assertRaisesRegex(ValueError, "Cannot read"):
                    secret_prompt.passphrase_minimum("backup")

    def test_minimum_counts_characters_and_preserves_utf8_byte_bound(self):
        for value, minimum, accepted in (
            ("é" * 8, 8, True),
            ("é" * 7, 8, False),
            ("x" * 16, 16, True),
            ("x" * 15, 16, False),
            ("x" * 1024, 1024, True),
            ("é" * 513, 8, False),
            ("line\nbreak", 8, False),
        ):
            with (
                self.subTest(minimum=minimum, accepted=accepted),
                patch.object(secret_prompt, "read_secret", return_value=value),
            ):
                if accepted:
                    self.assertEqual(
                        secret_prompt.new_passphrase("backup", minimum), value
                    )
                else:
                    with self.assertRaises(ValueError):
                        secret_prompt.new_passphrase("backup", minimum)


class EditorTests(unittest.TestCase):
    def test_masked_count_and_explicit_reveal_never_change_input(self):
        editor = secret_prompt.Editor(65536, True)
        editor.insert(VALUE)
        masked, _ = editor.line("Secret value: ", 120)
        self.assertIn("*", masked)
        self.assertIn(str(len(VALUE)) + " chars", masked)
        self.assertNotIn(VALUE, masked)
        editor.key("reveal")
        revealed, _ = editor.line("Secret value: ", 120)
        self.assertIn(VALUE, revealed)
        self.assertIn("visible", revealed)
        editor.key("reveal")
        self.assertNotIn(VALUE, editor.line("Secret value: ", 120)[0])
        self.assertEqual("".join(editor.chars), VALUE)

    def test_passphrases_have_feedback_but_cannot_be_revealed(self):
        editor = secret_prompt.Editor(1024, False)
        editor.insert(VALUE)
        editor.key("reveal")
        text, _ = editor.line("Vault passphrase: ", 120)
        self.assertIn("masked", text)
        self.assertIn(str(len(VALUE)), text)
        self.assertNotIn(VALUE, text)
        self.assertNotIn("Ctrl+R", text)

    def test_unicode_input_stays_exact_and_rendering_stays_within_columns(self):
        editor = secret_prompt.Editor(65536, True)
        value = "猫éa\u0301\u202e" * 100
        editor.insert(value)
        for reveal in (False, True):
            editor.visible = reveal
            for columns in (1, 2, 8, 24, 80, 120):
                for cursor in (0, len(value) // 2, len(value)):
                    with self.subTest(reveal=reveal, columns=columns, cursor=cursor):
                        editor.cursor = cursor
                        text, position = editor.line("Secret value: ", columns)
                        self.assertLessEqual(
                            secret_prompt.width(text), max(1, columns - 1)
                        )
                        self.assertLessEqual(position, max(0, columns - 2))
                        self.assertNotIn("\x1b", text)
                        self.assertNotIn("\u202e", text)
                        self.assertNotIn("\u0301", text)
        self.assertEqual("".join(editor.chars), value)

    def test_editing_and_byte_limits_do_not_silently_truncate(self):
        editor = secret_prompt.Editor(8, True)
        editor.insert("éabc")
        self.assertEqual(editor.bytes, 5)
        editor.key("left")
        editor.key("backspace")
        editor.insert("X")
        self.assertEqual("".join(editor.chars), "éaXc")
        editor.key("home")
        editor.key("delete")
        self.assertEqual("".join(editor.chars), "aXc")
        editor.key("end")
        editor.insert("too large")
        self.assertEqual("".join(editor.chars), "aXc")
        self.assertIn("limit", editor.notice)
        editor.key("reveal")
        self.assertIn("limit", editor.notice)  # cannot dismiss failed input with reveal
        editor.key("clear")
        self.assertEqual(editor.bytes, 0)
        self.assertEqual(editor.notice, "")

    def test_control_characters_in_bracketed_paste_are_rejected_as_a_whole(self):
        for value in ("one\ntwo", "one\rtwo", "a\x12b", "a\x1b[31mb", "a\0b"):
            with self.subTest(value=repr(value)):
                editor = secret_prompt.Editor(65536, True)
                editor.insert(value)
                self.assertEqual(editor.chars, [])
                self.assertFalse(editor.visible)
                self.assertIn("Paste rejected", editor.notice)

    def test_redraw_write_handles_partial_os_writes(self):
        written = bytearray()

        def partial(fd, data):
            written.extend(bytes(data[:2]))
            return min(2, len(data))

        with patch.object(secret_prompt.os, "write", side_effect=partial):
            secret_prompt.write(1, "example")
        self.assertEqual(written, b"example")


class InteractivePromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-prompt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.answer = self.root / "answer.json"

    def begin(self, *, reveal=True, maximum=65536, prompts=1):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        original = termios.tcgetattr(slave)
        program = """import fcntl,json,os,signal,sys,termios
os.setsid()
fcntl.ioctl(0,termios.TIOCSCTTY,0)
sys.path.insert(0,sys.argv[1]+'/src')
from oc_box.secret_prompt import read_secret
from pathlib import Path
try:
    values=[read_secret('Secret value: ',allow_reveal=sys.argv[3]=='yes',maximum=int(sys.argv[4])) for _ in range(int(sys.argv[5]))]
except (KeyboardInterrupt,EOFError,OSError,ValueError) as exc:
    Path(sys.argv[2]).write_text(json.dumps({'error':type(exc).__name__}))
else:
    Path(sys.argv[2]).write_text(json.dumps({'values':values}))
"""
        # The result file contains only synthetic fixture data; real prompts
        # return directly to the vault and never write plaintext here.
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "PYTHONNOUSERSITE": "1",
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                program,
                str(APP),
                str(self.answer),
                "yes" if reveal else "no",
                str(maximum),
                str(prompts),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        self.master, self.slave, self.process = master, slave, process
        self.session = {
            "master": master,
            "slave": slave,
            "process": process,
            "cleaned": False,
        }
        self.output = bytearray()
        self.original = original
        self.addCleanup(self.cleanup, self.session)
        self.wait_for("masked")

    def cleanup(self, session=None):
        session = self.session if session is None else session
        if session["cleaned"]:
            return
        process = session["process"]
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        os.close(session["master"])
        os.close(session["slave"])
        session["cleaned"] = True

    def collect(self, wait=0.1):
        if select.select([self.master], [], [], wait)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def wait_for(self, expected, *, start=0):
        expected = expected.encode() if isinstance(expected, str) else expected
        deadline = time.monotonic() + 5
        while expected not in self.output[start:] and time.monotonic() < deadline:
            self.collect()
            if self.process.poll() is not None:
                self.collect(0)
                break
        self.assertIn(expected, self.output[start:], bytes(self.output))

    def send(self, value):
        os.write(self.master, value.encode() if isinstance(value, str) else value)

    def finish(self):
        deadline = time.monotonic() + 5
        while self.process.poll() is None and time.monotonic() < deadline:
            self.collect()
        self.assertIsNotNone(self.process.poll(), bytes(self.output))
        while select.select([self.master], [], [], 0)[0]:
            self.collect(0)
        self.assertEqual(self.process.returncode, 0, bytes(self.output))
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.assertIn(b"\x1b[?2004l", self.output)
        return json.loads(self.answer.read_text())

    def test_masked_typing_paste_and_confirmation_start_masked(self):
        self.begin(prompts=2)
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.assertNotIn(VALUE.encode(), self.output)
        start = len(self.output)
        self.send("\n")
        self.wait_for("[accepted]", start=start)
        self.wait_for("0 chars", start=start)
        start = len(self.output)
        self.send(secret_prompt.PASTE_START + VALUE.encode() + secret_prompt.PASTE_END)
        self.wait_for(f"{len(VALUE)} chars", start=start)
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE, VALUE]})
        self.assertNotIn(VALUE.encode(), self.output)

    def test_reveal_toggle_is_explicit_and_clears_the_line_before_return(self):
        self.begin()
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.assertNotIn(VALUE.encode(), self.output)
        self.send(b"\x12")
        self.wait_for(VALUE)
        self.wait_for("visible")
        start = len(self.output)
        self.send(b"\x12")
        self.wait_for("masked", start=start)
        self.assertNotIn(VALUE.encode(), self.output[start:])
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE]})
        self.assertIn(b"\r\x1b[2KSecret value: [accepted]", self.output)

    def test_confirmation_does_not_inherit_the_first_prompts_reveal_state(self):
        self.begin(prompts=2)
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.send(b"\x12")
        self.wait_for(VALUE)
        start = len(self.output)
        self.send("\n")
        self.wait_for("0 chars, masked", start=start)
        start = len(self.output)
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars", start=start)
        self.assertNotIn(VALUE.encode(), self.output[start:])
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE, VALUE]})

    def test_passphrase_toggle_cannot_reveal_or_modify_value(self):
        self.begin(reveal=False)
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.send(b"\x12")
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE]})
        self.assertNotIn(VALUE.encode(), self.output)
        self.assertNotIn(b"visible", self.output)

    def test_unicode_cursor_delete_backspace_and_clear(self):
        self.begin()
        self.send("éab錯")
        self.wait_for("4 chars")
        self.send(b"\x1b[D\x7fX\x1b[3~\n")
        self.assertEqual(self.finish(), {"values": ["éaX"]})
        self.assertNotIn("éaX".encode(), self.output)

    def test_word_clear_home_end_and_tail_editing(self):
        self.begin()
        self.send(b"incorrect\x15one two\x17three\x01\x1b[C\x0btail\n")
        self.assertEqual(self.finish(), {"values": ["otail"]})

    def test_large_paste_and_oversize_rejection_require_reentry(self):
        self.begin(maximum=16)
        self.send(secret_prompt.PASTE_START + b"x" * 10000 + secret_prompt.PASTE_END)
        self.wait_for("Paste rejected: input limit")
        self.send(b"\x12\n")
        self.collect()
        self.assertIsNone(self.process.poll())
        self.send(
            b"\x15"
            + secret_prompt.PASTE_START
            + VALUE[:16].encode()
            + secret_prompt.PASTE_END
        )
        self.wait_for("16 chars")
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE[:16]]})

    def test_multiline_and_control_pastes_do_not_submit_or_toggle(self):
        self.begin()
        self.send(
            secret_prompt.PASTE_START
            + b"first\nsecond\x12"
            + secret_prompt.PASTE_END
            + b"\n"
        )
        self.wait_for("Paste rejected")
        self.assertIsNone(self.process.poll())
        self.send(b"\x15" + VALUE.encode())
        self.wait_for(f"{len(VALUE)} chars")
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE]})
        self.assertNotIn(b"visible", self.output)

    def test_maximum_sized_paste_is_not_kernel_canonical_truncated(self):
        self.begin()
        value = "x" * 65536
        # Write/pump concurrently enough to avoid filling the PTY buffers.
        payload = memoryview(
            secret_prompt.PASTE_START + value.encode() + secret_prompt.PASTE_END
        )
        while payload:
            count = os.write(self.master, payload[:1024])
            payload = payload[count:]
            self.collect(0)
        self.wait_for("65536 chars")
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [value]})

    def test_cancel_eof_and_termination_restore_terminal_after_reveal(self):
        for ending in (b"\x03", b"\x04", signal.SIGTERM):
            with self.subTest(ending=ending):
                self.begin()
                self.send(VALUE)
                self.wait_for(f"{len(VALUE)} chars")
                self.send(b"\x12")
                self.wait_for(VALUE)
                if isinstance(ending, bytes):
                    self.send(ending)
                else:
                    os.kill(self.process.pid, ending)
                self.assertIn(self.finish()["error"], ("KeyboardInterrupt", "EOFError"))
                self.assertIn(b"\r\x1b[2KSecret value: [cancelled]", self.output)
                self.cleanup()

    def test_fragmented_unicode_escape_and_paste_markers(self):
        self.begin()
        data = "é".encode()
        self.send(data[:1])
        time.sleep(0.15)
        self.send(data[1:])
        self.wait_for("1 chars")
        self.send(b"\x1b[")
        time.sleep(0.15)
        self.send(b"D")
        for byte in secret_prompt.PASTE_START + b"a" + secret_prompt.PASTE_END:
            self.send(bytes([byte]))
            time.sleep(0.005)
        self.wait_for("2 chars")
        self.send("\n")
        self.assertEqual(self.finish(), {"values": ["aé"]})

    def test_malformed_unbracketed_utf8_aborts_instead_of_accepting_valid_suffix(self):
        for prefix, malformed in (
            (b"", b"\xffsuffix\n"),
            (b"", b"\xc2Xsuffix\n"),
            (b"\xe2", b"\x82Xsuffix\n"),
        ):
            with self.subTest(prefix=prefix, malformed=malformed):
                self.begin()
                self.send(VALUE)
                self.wait_for(f"{len(VALUE)} chars")
                if prefix:
                    self.send(prefix)
                    self.collect()
                self.send(malformed)
                self.assertEqual(self.finish(), {"error": "ValueError"})
                self.assertNotIn(b"[accepted]", self.output)
                self.assertNotIn(VALUE.encode(), self.output)
                self.assertIn(b"[cancelled]", self.output)
                self.cleanup()

    def test_malformed_utf8_after_reveal_erases_field_and_restores_terminal(self):
        self.begin()
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.send(b"\x12")
        self.wait_for(VALUE)
        start = len(self.output)
        self.send(b"\xffsuffix\n")
        self.assertEqual(self.finish(), {"error": "ValueError"})
        self.assertIn(b"\r\x1b[2KSecret value: [cancelled]", self.output[start:])
        self.assertNotIn(VALUE.encode(), self.output[start:])

    def test_resize_redraw_hides_fields_within_new_width(self):
        self.begin()
        self.send(VALUE)
        self.wait_for(f"{len(VALUE)} chars")
        self.send(b"\x12")
        self.wait_for(VALUE)
        start = len(self.output)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 20, 0, 0))
        self.wait_for("vis", start=start)
        self.assertNotIn(VALUE.encode(), self.output[start:])
        self.send("\n")
        self.assertEqual(self.finish(), {"values": [VALUE]})

    def test_no_controlling_terminal_does_not_read_stdin_or_echo(self):
        program = "import sys; sys.path.insert(0,sys.argv[1]+'/src'); from oc_box.secret_prompt import read_secret; read_secret('Secret: ')"
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, str(APP)],
            input=VALUE + "\n",
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(VALUE, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
