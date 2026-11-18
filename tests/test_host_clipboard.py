"""Four focused mock-only checks; never read the real desktop clipboard."""

import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from oc_box import host_clipboard, host_drop, host_drop_input, installation
from oc_box import oc_box as box


PNG = b"\x89PNG\r\n\x1a\nsynthetic-only"
KEY = b"\x1b[118;8u"
PASTE = b"\x1b[200~"
END = b"\x1b[201~"


class HostClipboardTests(unittest.TestCase):
    def test_helper_discovery_is_read_free_and_reads_are_bounded_and_private(self):
        environment = {
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "LANG": "C.UTF-8",
            "SYNTHETIC_API_KEY": "not-forwarded",
            "LC_FAKE_SECRET": "not-forwarded",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                host_clipboard.shutil, "which", return_value="/usr/bin/wl-paste"
            ),
            patch.object(host_clipboard.subprocess, "Popen") as start,
        ):
            reader = host_clipboard.preflight()
            start.assert_not_called()
            self.assertEqual(
                reader.command,
                ("/usr/bin/wl-paste", "--no-newline", "--type", "image/png"),
            )
            self.assertNotIn("SYNTHETIC_API_KEY", reader.env)
            self.assertNotIn("LC_FAKE_SECRET", reader.env)
        with (
            patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True),
            patch.object(host_clipboard.shutil, "which", return_value="/usr/bin/xclip"),
        ):
            self.assertEqual(
                host_clipboard.preflight().command,
                (
                    "/usr/bin/xclip",
                    "-selection",
                    "clipboard",
                    "-target",
                    "image/png",
                    "-out",
                ),
            )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(host_clipboard.shutil, "which", return_value=None) as which,
        ):
            with self.assertRaises(host_clipboard.ClipboardError) as error:
                host_clipboard.preflight()
            self.assertIn("'wl-paste'", str(error.exception))
            self.assertIn("'wl-clipboard'", str(error.exception))
            self.assertIn("on the host, not inside oc-box", str(error.exception))
            which.assert_called_once_with("wl-paste")  # Never read X11 as fallback.
        with (
            patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True),
            patch.object(host_clipboard.shutil, "which", return_value=None),
            self.assertRaises(host_clipboard.ClipboardError) as error,
        ):
            host_clipboard.preflight()
        self.assertIn("'xclip'", str(error.exception))
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(host_clipboard.ClipboardError),
        ):
            host_clipboard.preflight()
        for case in ("png", "not-image", "oversize", "timeout"):
            with self.subTest(case=case):
                process = Mock()
                process.stdout.fileno.return_value = 41
                process.wait.return_value = 0
                process.poll.return_value = 0 if case in ("png", "not-image") else None
                content = b"private text" if case == "not-image" else PNG
                ready = ([], [], []) if case == "timeout" else ([41], [], [])
                maximum = len(PNG) - 1 if case == "oversize" else 8 * 1024 * 1024
                with (
                    patch.object(
                        host_clipboard.subprocess, "Popen", return_value=process
                    ) as start,
                    patch.object(host_clipboard.os, "set_blocking"),
                    patch.object(host_clipboard.select, "select", return_value=ready),
                    patch.object(host_clipboard.os, "read", side_effect=[content, b""]),
                    patch.object(host_clipboard, "MAX_BYTES", maximum),
                ):
                    if case == "png":
                        self.assertEqual(reader.read_png(), PNG)
                    else:
                        with self.assertRaises(host_clipboard.ClipboardError) as error:
                            reader.read_png()
                        self.assertNotIn("private text", str(error.exception))
                    self.assertFalse(start.call_args.kwargs["shell"])
                    self.assertEqual(
                        start.call_args.kwargs["stderr"],
                        host_clipboard.subprocess.DEVNULL,
                    )
                    process.stdout.close.assert_called_once()
                    if case in ("oversize", "timeout"):
                        process.kill.assert_called_once()

    def test_only_complete_opted_in_host_shortcuts_grant_a_read(self):
        for key in (KEY, b"\x1b[118;8:1u", b"\x1b[27;8;86~", b"\x1b[118:86:118;200u"):
            with self.subTest(key=key):
                action = Mock(return_value=b"/tmp/oc-box-drop-test/drop_image.png")
                parser = host_drop_input.PasteInput(lambda _: None, on_clipboard=action)
                self.assertEqual(parser.feed(key[:-1]), b"")
                action.assert_not_called()
                result = parser.feed(key[-1:] + b"x")
                self.assertEqual(result, PASTE + action.return_value + END + b"x")
                self.assertNotIn(b"\r", result)
                self.assertNotIn(b"\n", result)
                action.assert_called_once_with()
        parser = host_drop_input.PasteInput(lambda _: None)
        self.assertEqual(parser.feed(KEY), KEY)  # No opt-in: byte-transparent.
        ordinary = b"\x16\x1b[118;6u\x1b[99;8u"
        action = Mock(return_value=b"unexpected")
        parser = host_drop_input.PasteInput(lambda _: None, on_clipboard=action)
        self.assertEqual(parser.feed(ordinary), ordinary)
        self.assertEqual(parser.feed(b"\x1b[118;8:2u\x1b[118;8:3u"), b"")
        action.assert_not_called()
        for content in (
            PASTE + KEY + END,
            *(
                b"\x1b" + prefix + b"reply\x07" + KEY + b"\x1b\\"
                for prefix in (b"P", b"X", b"^", b"_")
            ),
            b"\x1b]title;" + KEY + b"\x1b\\",
            b"\x9b118;8u",
            b"\xc2\x9b118;8u",
            b"\xff" + KEY,
        ):
            with self.subTest(content=content):
                action = Mock(return_value=b"unexpected")
                parser = host_drop_input.PasteInput(lambda _: None, on_clipboard=action)
                parser.feed(content)
                parser.finish()
                action.assert_not_called()

    def test_clipboard_reuses_staging_without_reenabling_file_drops_or_sending(self):
        facade = SimpleNamespace(
            APP=Path("/fixture/application"),
            state_root=lambda: Path("/fixture/state"),
            vault=SimpleNamespace(vault_path=lambda: Path("/fixture/vault/vault.age")),
        )
        transfer = MagicMock(files=0, total=0, cleanup_verified=True)
        transfer.put.return_value = b"/tmp/oc-box-drop-test/drop_image.png"
        reader = Mock()
        reader.read_png.return_value = PNG
        errors = io.StringIO()
        with (
            patch.object(host_drop, "Transfer", return_value=transfer),
            contextlib.redirect_stderr(errors),
            host_drop.Relay(
                facade,
                {"opencode_version": "2.3.0"},
                {},
                profile="konsole",
                paths_enabled=False,
                clipboard=reader,
            ) as relay,
        ):
            reader.read_png.assert_not_called()
            self.assertEqual(relay.output_bytes(KEY), KEY)
            reader.read_png.assert_not_called()
            path = PASTE + b"/tmp/host-image.png" + END
            with patch.object(host_drop.sharing, "Export") as export:
                self.assertEqual(relay.input_bytes(path), path)
                export.assert_not_called()
            self.assertEqual(
                relay.input_bytes(KEY), PASTE + transfer.put.return_value + END
            )
            reader.read_png.assert_called_once_with()
            transfer.put.assert_called_once_with([("image/png", PNG)])
            reader.reset_mock()
            transfer.put.reset_mock()
            reader.read_png.return_value = b"private text"
            refused = relay.input_bytes(KEY)
            self.assertEqual(refused, PASTE + host_drop.CLIPBOARD_REFUSED + END)
            self.assertNotIn(b"private text", refused)
            transfer.put.assert_not_called()
            reader.reset_mock()
            transfer.files = host_drop.MAX_FILES
            self.assertEqual(
                relay.input_bytes(KEY), PASTE + host_drop.CLIPBOARD_REFUSED + END
            )
            reader.read_png.assert_not_called()
        transfer.__exit__.assert_called_once()
        self.assertNotIn("private text", errors.getvalue())

    def test_cli_client_scope_preflight_and_shipping(self):
        self.assertFalse(box.parser().parse_args([]).host_clipboard)
        args = box.parser().parse_args(["--host-clipboard", "--no-host-drop"])
        reader = Mock()
        with (
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(host_clipboard, "preflight", return_value=reader) as discover,
        ):
            self.assertIs(box.configure_host_clipboard(args, "2.3.0"), reader)
            self.assertIs(box.configure_host_clipboard(args, "2.3.0"), reader)
            discover.assert_called_once()
            reader.read_png.assert_not_called()
            self.assertIs(args.host_drop, False)
        with (
            patch.object(host_clipboard, "preflight") as discover,
            self.assertRaises(box.BoxError),
        ):
            box.configure_host_clipboard(
                box.parser().parse_args(["--host-clipboard"]), "1.19.5"
            )
        discover.assert_not_called()
        for arguments in (
            ["--host-clipboard", "sync"],
            ["--host-clipboard", "web"],
            ["--host-clipboard", "terminal"],
            ["--host-clipboard", "tui"],
            ["--host-clipboard", "--status"],
            ["--host-clipboard", "--version"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(host_clipboard, "preflight") as discover,
            ):
                with self.assertRaises(box.BoxError):
                    box.main(arguments)
                discover.assert_not_called()
        relay = MagicMock()
        relay.__enter__.return_value = relay
        with (
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(host_drop, "Relay", return_value=relay) as wrapper,
            patch.object(box, "run_tui", return_value=0) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.run_native_tui(
                    {"opencode_version": "2.3.0"},
                    ["native"],
                    {},
                    host_drop_enabled=False,
                    host_drop_required=False,
                    host_clipboard_reader=reader,
                ),
                0,
            )
            self.assertIs(wrapper.call_args.kwargs["paths_enabled"], False)
            self.assertIs(wrapper.call_args.kwargs["clipboard"], reader)
            run.assert_called_once_with(["native"], file_drops=relay)
        self.assertIn("host_clipboard.py", installation.PROGRAM_FILES)
        self.assertIn("host-clipboard-testing.md", installation.MANUAL_FILES)


if __name__ == "__main__":
    unittest.main()
