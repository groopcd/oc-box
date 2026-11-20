"""Focused clipboard/file checks; never read the real desktop clipboard."""

import base64
import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from oc_box import host_clipboard, host_drop, host_drop_input, installation
from oc_box import oc_box as box


PNG = b"\x89PNG\r\n\x1a\nsynthetic-only"
KEY = b"\x19"
PASTE = b"\x1b[200~"
END = b"\x1b[201~"


def refusal(label):
    return (
        PASTE
        + b"[oc-box: host clipboard import refused: "
        + label.encode()
        + b"]"
        + END
    )


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

    def test_selected_targets_never_request_plaintext_or_retry_another_format(self):
        examples = (
            (
                b"text/plain\nimage/png\ntext/uri-list\n",
                b"file:///tmp/a.pdf\r\n",
                "text/uri-list",
            ),
            (
                b"TARGETS x-special/gnome-copied-files",
                b"copy\nfile:///tmp/a.pdf",
                "x-special/gnome-copied-files",
            ),
            (b"image/png\n", PNG, "image/png"),
        )
        for desktop, helper in (
            ({"WAYLAND_DISPLAY": "wayland-0"}, "wl-paste"),
            ({"DISPLAY": ":0"}, "xclip"),
        ):
            with (
                patch.dict(os.environ, desktop, clear=True),
                patch.object(
                    host_clipboard.shutil, "which", return_value="/usr/bin/" + helper
                ),
            ):
                reader = host_clipboard.preflight()
            for offered, payload, mime in examples:
                with (
                    self.subTest(helper=helper, mime=mime),
                    patch.object(
                        host_clipboard.Reader, "_read", side_effect=[offered, payload]
                    ) as read,
                ):
                    self.assertEqual(reader.read_selection(), (mime, payload))
                    self.assertEqual(read.call_count, 2)
                    metadata, selected = read.call_args_list
                    self.assertIn(
                        "--list-types" if helper == "wl-paste" else "TARGETS",
                        metadata.args[0],
                    )
                    self.assertIn(mime, selected.args[0])
                    self.assertNotIn("text/plain", selected.args[0])
                    self.assertEqual(
                        metadata.args[-1], selected.args[-1]
                    )  # One deadline.
                    self.assertEqual(
                        selected.args[1],
                        host_clipboard.MAX_BYTES
                        if mime == "image/png"
                        else host_clipboard.MAX_METADATA_BYTES,
                    )
            for offered in (b"text/plain", b"\xff", b" ".join([b"unknown"] * 129)):
                with (
                    self.subTest(offered=offered[:40]),
                    patch.object(
                        host_clipboard.Reader, "_read", return_value=offered
                    ) as read,
                ):
                    with self.assertRaises(host_clipboard.ClipboardError):
                        reader.read_selection()
                    self.assertEqual(read.call_count, 1)
            with patch.object(
                host_clipboard.Reader,
                "_read",
                side_effect=[
                    b"text/uri-list\nimage/png\n",
                    host_clipboard.ClipboardError("fixture"),
                ],
            ) as read:
                with self.assertRaises(host_clipboard.ClipboardError):
                    reader.read_selection()
                self.assertEqual(
                    read.call_count, 2
                )  # No PNG fallback after file-list refusal.

    def test_copied_pdf_and_image_files_use_guarded_capture_and_never_move(self):
        with tempfile.TemporaryDirectory(prefix="host-clipboard-files-") as work:
            root = Path(work)
            pdf = root / "report with spaces.pdf"
            pdf_data = b"%PDF-1.4\nsynthetic-only\n%%EOF\n"
            pdf.write_bytes(pdf_data)
            image = root / "picture.png"
            image.write_bytes(PNG)
            link = root / "linked.pdf"
            link.symlink_to(pdf)
            directory_link = root / "folder shortcut"
            directory_link.symlink_to(".", target_is_directory=True)
            application = root / "application"
            application.mkdir()
            protected = application / "private.pdf"
            protected.write_bytes(pdf_data)
            facade = SimpleNamespace(
                APP=application,
                state_root=lambda: root / "state",
                vault=SimpleNamespace(vault_path=lambda: root / "vault/vault.age"),
            )
            transfer = MagicMock(
                files=0, total=0, failed=False, closed=False, cleanup_verified=True
            )
            transfer.put.return_value = (
                b"/tmp/oc-box-drop-test/drop_one.pdf /tmp/oc-box-drop-test/drop_two.png"
            )
            reader = Mock()
            uris = (pdf.as_uri() + "\r\n" + image.as_uri() + "\r\n").encode()
            before = {
                p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in (pdf, image)
            }
            with (
                patch.object(host_drop, "Transfer", return_value=transfer),
                contextlib.redirect_stderr(io.StringIO()),
                host_drop.Relay(
                    facade,
                    {"opencode_version": "2.3.2"},
                    {},
                    profile="konsole",
                    paths_enabled=False,
                    clipboard=reader,
                ) as relay,
            ):
                for mime, payload in (
                    ("text/uri-list", uris),
                    ("x-special/gnome-copied-files", b"copy\n" + uris),
                    ("x-special/gnome-copied-files", b"cut\r\n" + uris),
                    ("text/uri-list", (link.as_uri() + "\n" + image.as_uri()).encode()),
                    (
                        "text/uri-list",
                        (
                            (directory_link / pdf.name).as_uri()
                            + "\n"
                            + (directory_link / image.name).as_uri()
                        ).encode(),
                    ),
                ):
                    with self.subTest(mime=mime, operation=payload[:4]):
                        transfer.put.reset_mock()
                        reader.read_selection.return_value = (mime, payload)
                        result = relay.input_bytes(KEY)
                        self.assertEqual(
                            result, PASTE + transfer.put.return_value + END
                        )
                        transfer.put.assert_called_once_with(
                            [
                                ("application/pdf", pdf_data),
                                ("image/png", PNG),
                            ]
                        )
                        self.assertNotIn(str(root).encode(), result)
                        self.assertNotIn(b"\r", result)
                        self.assertNotIn(b"\n", result)
                for mime, payload, category in (
                    ("text/plain", str(pdf).encode(), "clipboard-types"),
                    ("text/uri-list", b"file://foreign/tmp/a.pdf", "file-uri"),
                    ("text/uri-list", b"file:///tmp/a.docx", "content-type"),
                    ("text/uri-list", protected.as_uri().encode(), "source-guard"),
                    ("text/uri-list", uris * 3, "file-list-limit"),
                    ("x-special/gnome-copied-files", b"move\n" + uris, "file-list"),
                ):
                    with self.subTest(mime=mime, payload=payload[:40]):
                        transfer.put.reset_mock()
                        reader.reset_mock()
                        reader.read_selection.return_value = (mime, payload)
                        self.assertEqual(
                            relay.input_bytes(KEY),
                            refusal(category),
                        )
                        transfer.put.assert_not_called()
                        reader.read_selection.assert_called_once_with()
                reader.read_selection.return_value = (
                    "text/uri-list",
                    (pdf.as_uri() + "\n" + (root / "missing.pdf").as_uri()).encode(),
                )
                with patch.object(host_drop.os, "pread") as read:
                    self.assertEqual(
                        relay.input_bytes(KEY),
                        refusal("source-guard"),
                    )
                    read.assert_not_called()  # Whole batch admitted before reading any file.
                self.assertEqual(pdf.read_bytes(), pdf_data)
                self.assertEqual(image.read_bytes(), PNG)
                self.assertEqual(
                    before,
                    {p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in before},
                )

    def test_redirected_sources_keep_strict_optout_protection_and_descriptor_binding(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="host-clipboard-links-") as work:
            root = Path(work)
            pdf_data = b"%PDF-1.4\nsynthetic-only\n%%EOF\n"
            pdf = root / "source.pdf"
            pdf.write_bytes(pdf_data)
            private = root / "private"
            private.mkdir()
            secret = private / "secret.pdf"
            secret.write_bytes(b"%PDF-1.4\nsynthetic-private\n")
            alias = root / "linked.pdf"
            alias.symlink_to(pdf)
            directory = root / "linked folder"
            directory.symlink_to(root, target_is_directory=True)
            facade = SimpleNamespace(
                APP=private,
                state_root=lambda: root / "state",
                vault=SimpleNamespace(vault_path=lambda: root / "vault/vault.age"),
            )
            transfer = MagicMock(
                files=0, total=0, failed=False, closed=False, cleanup_verified=True
            )
            transfer.put.return_value = b"/tmp/oc-box-drop-test/drop_one.pdf"
            reader = Mock()
            with (
                patch.object(host_drop, "Transfer", return_value=transfer),
                contextlib.redirect_stderr(io.StringIO()),
                host_drop.Relay(
                    facade,
                    {},
                    {},
                    profile="konsole",
                    clipboard=reader,
                    clipboard_symlinks=False,
                ) as strict,
            ):
                for path in (alias, directory / pdf.name, secret, root / "absent.pdf"):
                    reader.read_selection.return_value = (
                        "text/uri-list",
                        path.as_uri().encode(),
                    )
                    with (
                        self.subTest(strict=path.name),
                        patch.object(host_drop.os, "pread") as read,
                    ):
                        self.assertEqual(
                            strict.input_bytes(KEY), refusal("source-guard")
                        )
                        read.assert_not_called()
                transfer.put.assert_not_called()
            protected_alias = root / "protected-alias.pdf"
            protected_alias.symlink_to(secret)
            private_alias = private / "public-alias.pdf"
            private_alias.symlink_to(pdf)
            dangling = root / "dangling.pdf"
            dangling.symlink_to(root / "absent.pdf")
            loop = root / "loop.pdf"
            loop.symlink_to(loop)
            hard_source = root / "hard-source.pdf"
            hard_source.write_bytes(pdf_data)
            hard_link = root / "hard-link.pdf"
            os.link(hard_source, hard_link)
            fifo = root / "pipe.pdf"
            os.mkfifo(fifo)
            with (
                patch.object(host_drop, "Transfer", return_value=transfer),
                contextlib.redirect_stderr(io.StringIO()),
                host_drop.Relay(
                    facade, {}, {}, profile="konsole", clipboard=reader
                ) as relay,
                pdf.open("rb") as descriptor,
            ):
                magic = root / "magic.pdf"
                magic.symlink_to(f"/proc/self/fd/{descriptor.fileno()}")
                for path in (
                    protected_alias,
                    private_alias,
                    dangling,
                    loop,
                    hard_link,
                    fifo,
                    magic,
                ):
                    reader.read_selection.return_value = (
                        "text/uri-list",
                        path.as_uri().encode(),
                    )
                    with (
                        self.subTest(source=path.name),
                        patch.object(host_drop.os, "pread") as read,
                    ):
                        self.assertEqual(
                            relay.input_bytes(KEY), refusal("source-guard")
                        )
                        read.assert_not_called()
                transfer.put.assert_not_called()
                for name, target in (
                    ("mismatched.png", pdf),
                    ("unsupported-target.pdf", root / "payload.bin"),
                ):
                    if not target.exists():
                        target.write_bytes(pdf_data)
                    mismatch = root / name
                    mismatch.symlink_to(target)
                    reader.read_selection.return_value = (
                        "text/uri-list",
                        mismatch.as_uri().encode(),
                    )
                    self.assertEqual(relay.input_bytes(KEY), refusal("content-type"))
                transfer.put.assert_not_called()
                reader.read_selection.return_value = (
                    "text/uri-list",
                    alias.as_uri().encode(),
                )
                with patch.object(
                    host_drop.sharing.platform, "machine", return_value="unsupported"
                ):
                    self.assertEqual(relay.input_bytes(KEY), refusal("source-guard"))
                # Retargeting the alias after kernel selection cannot redirect reads.
                open_path = host_drop.sharing._open_path

                def retarget(*args, **kwargs):
                    fd = open_path(*args, **kwargs)
                    alias.unlink()
                    alias.symlink_to(secret)
                    return fd

                reader.read_selection.return_value = (
                    "text/uri-list",
                    alias.as_uri().encode(),
                )
                with patch.object(
                    host_drop.sharing, "_open_path", side_effect=retarget
                ):
                    relay.input_bytes(KEY)
                transfer.put.assert_called_once_with([("application/pdf", pdf_data)])
                transfer.put.reset_mock()
                # Reopening a replaced canonical pathname must not switch objects.
                export = host_drop.sharing.Export

                def replace(target, **kwargs):
                    pdf.rename(root / "displaced.pdf")
                    pdf.write_bytes(pdf_data + b"replacement")
                    return export(target, **kwargs)

                reader.read_selection.return_value = (
                    "text/uri-list",
                    pdf.as_uri().encode(),
                )
                with (
                    patch.object(host_drop.sharing, "Export", side_effect=replace),
                    patch.object(host_drop.os, "pread") as read,
                ):
                    self.assertEqual(relay.input_bytes(KEY), refusal("source-guard"))
                    read.assert_not_called()
                transfer.put.assert_not_called()

    def test_only_complete_opted_in_host_shortcuts_grant_a_read(self):
        for key in (
            KEY,
            b"\x1b[121;5u",
            b"\x1b[121;5:1u",
            b"\x1b[27;5;121~",
            b"\x1b[121:89;69u",
            b"\x1b[121;133u",
            b"\x1b[121:89:121;197u",
        ):
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
        ordinary = (
            b"yY;\x16\x11\x13"  # Text, Ctrl+V/Q/S stay native controls.
            b"\x1b[121;3u\x1b[121;6u"  # Alt+Y and Ctrl+Shift+Y are not the shortcut.
            b"\x1b[118;8u\x1b[27;8;86~"  # Old shortcut no longer grants a read.
        )
        action = Mock(return_value=b"unexpected")
        parser = host_drop_input.PasteInput(lambda _: None, on_clipboard=action)
        self.assertEqual(parser.feed(ordinary), ordinary)
        self.assertEqual(parser.feed(b"\x1b[121;5:2u\x1b[121;5:3u"), b"")
        action.assert_not_called()
        for content in (
            PASTE + KEY + END,
            *(
                b"\x1b" + prefix + b"reply\x07" + KEY + b"\x1b\\"
                for prefix in (b"P", b"X", b"^", b"_")
            ),
            b"\x1b]title;" + KEY + b"\x1b\\",
            b"\x9b121;5u",
            b"\xc2\x9b121;5u",
            b"\x1b" + KEY,  # Alt-prefixed controls are not top-level Ctrl+Y.
            b"\x1b[0" + KEY + b"n",  # No control-byte action inside CSI replies.
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
        transfer = MagicMock(
            files=0, total=0, failed=False, closed=False, cleanup_verified=True
        )
        transfer.put.return_value = b"/tmp/oc-box-drop-test/drop_image.png"
        reader = Mock()
        reader.read_selection.return_value = ("image/png", PNG)
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
            reader.read_selection.assert_not_called()
            self.assertEqual(relay.output_bytes(KEY), KEY)
            reader.read_selection.assert_not_called()
            path = PASTE + b"/tmp/host-image.png" + END
            with patch.object(host_drop.sharing, "Export") as export:
                self.assertEqual(relay.input_bytes(path), path)
                export.assert_not_called()
            self.assertEqual(
                relay.input_bytes(KEY), PASTE + transfer.put.return_value + END
            )
            reader.read_selection.assert_called_once_with()
            transfer.put.assert_called_once_with([("image/png", PNG)])
            reader.reset_mock()
            transfer.put.reset_mock()
            reader.read_selection.return_value = ("image/png", b"private text")
            refused = relay.input_bytes(KEY)
            self.assertEqual(refused, refusal("content-type"))
            self.assertNotIn(b"private text", refused)
            transfer.put.assert_not_called()
            reader.reset_mock()
            transfer.files = host_drop.MAX_FILES
            self.assertEqual(relay.input_bytes(KEY), refusal("client-limit"))
            reader.read_selection.assert_not_called()
            transfer.files = 0
            for error, category in (
                (
                    host_clipboard.ClipboardError("Host clipboard read timed out."),
                    "clipboard-timeout",
                ),
                (
                    ValueError("/private/host/path must never be shown"),
                    "clipboard-read",
                ),
            ):
                with self.subTest(category=category):
                    reader.read_selection.side_effect = error
                    result = relay.input_bytes(KEY)
                    self.assertEqual(result, refusal(category))
                    self.assertNotIn(b"/private/host/path", result)
            reader.read_selection.side_effect = None
            reader.read_selection.return_value = ("image/png", PNG)
            transfer.put.side_effect = host_drop.DropError("private transfer detail")
            self.assertEqual(relay.input_bytes(KEY), refusal("transfer"))
        transfer.__exit__.assert_called_once()
        self.assertNotIn("private text", errors.getvalue())

    def test_cli_client_scope_preflight_and_shipping(self):
        self.assertIsNone(box.parser().parse_args([]).host_clipboard)
        self.assertIsNone(box.parser().parse_args([]).host_clipboard_symlinks)
        self.assertTrue(
            box.effective_host_clipboard_symlinks(box.parser().parse_args([]))
        )
        self.assertFalse(
            box.parser()
            .parse_args(["--host-clipboard", "--no-host-clipboard-symlinks"])
            .host_clipboard_symlinks
        )
        args = box.parser().parse_args(["--host-clipboard", "--no-host-drop"])
        reader = Mock()
        with (
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(host_clipboard, "preflight", return_value=reader) as discover,
        ):
            self.assertIs(box.configure_host_clipboard(args, "2.3.2"), reader)
            self.assertIs(box.configure_host_clipboard(args, "2.3.2"), reader)
            discover.assert_called_once()
            reader.read_selection.assert_not_called()
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
            ["--no-host-clipboard-symlinks", "--status"],
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
                    host_clipboard_symlinks=False,
                ),
                0,
            )
            self.assertIs(wrapper.call_args.kwargs["paths_enabled"], False)
            self.assertIs(wrapper.call_args.kwargs["clipboard"], reader)
            self.assertIs(wrapper.call_args.kwargs["clipboard_symlinks"], False)
            run.assert_called_once_with(["native"], file_drops=relay)
            run.reset_mock()
            relay.__enter__.side_effect = host_drop.DropError("private-transfer-shape")
            with self.assertRaises(box.BoxError):
                box.run_native_tui(
                    {"opencode_version": "2.3.2"},
                    ["native"],
                    {},
                    host_drop_enabled=False,
                    host_drop_required=False,
                    host_clipboard_reader=reader,
                )
            run.assert_not_called()  # A changed transfer contract still refuses.
            reader.read_selection.assert_not_called()
        self.assertIn("host_clipboard.py", installation.PROGRAM_FILES)
        self.assertIn("host-clipboard-testing.md", installation.MANUAL_FILES)


class HostClipboardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.facade = SimpleNamespace(
            APP=Path("/fixture/application"),
            state_root=lambda: Path("/fixture/state"),
            vault=SimpleNamespace(vault_path=lambda: Path("/fixture/vault/vault.age")),
        )
        self.errors = io.StringIO()
        self.secret = "synthetic-secret-not-for-terminal"

    @contextlib.contextmanager
    def client(self, reader, *, profile="konsole"):
        transfer = MagicMock(
            files=0, total=0, failed=False, closed=False, cleanup_verified=True
        )
        transfer.put.return_value = b"/tmp/oc-box-drop-test/drop_image.png"
        with (
            patch.object(host_drop, "Transfer", return_value=transfer) as start,
            contextlib.redirect_stderr(self.errors),
            host_drop.Relay(
                self.facade, {}, {}, profile=profile, clipboard=reader
            ) as relay,
        ):
            yield relay, transfer
        start.assert_called_once_with(self.facade, {}, {})
        transfer.__exit__.assert_called_once_with()
        self.assertNotIn(self.secret, self.errors.getvalue())

    def test_unexpected_read_recovery_requires_a_later_press_and_keeps_its_budget(self):
        press = b"\x1b[121;5:1u"
        repeat = b"\x1b[121;5:2u"
        release = b"\x1b[121;5:3u"
        for first, later in ((KEY, press), (press, KEY)):
            with self.subTest(first=first, later=later):
                reader = Mock(cleanup_verified=True)
                reader.read_selection.side_effect = [
                    RuntimeError(self.secret),
                    ("image/png", PNG),
                    RuntimeError(self.secret),
                ]
                with self.client(reader) as (relay, transfer):
                    reader.read_selection.assert_not_called()
                    self.assertEqual(
                        relay.input_bytes(first), refusal("clipboard-unexpected-read")
                    )
                    reader.read_selection.assert_called_once_with()
                    transfer.put.assert_not_called()
                    self.assertEqual(relay.input_bytes(repeat), b"")
                    self.assertEqual(relay.input_bytes(release), b"")
                    reader.read_selection.assert_called_once_with()

                    self.assertEqual(
                        relay.input_bytes(later),
                        PASTE + transfer.put.return_value + END,
                    )
                    self.assertEqual(reader.read_selection.call_count, 2)
                    transfer.put.assert_called_once_with([("image/png", PNG)])
                    self.assertEqual(
                        relay.input_bytes(first), refusal("clipboard-internal")
                    )
                    self.assertEqual(reader.read_selection.call_count, 3)
                    self.assertEqual(relay.input_bytes(repeat + release), b"")
                    for key in (KEY, press):
                        self.assertEqual(
                            relay.input_bytes(key), refusal("clipboard-disabled")
                        )
                    self.assertEqual(reader.read_selection.call_count, 3)
                    self.assertEqual(relay.clipboard_refused, 4)
                    transfer.put.assert_called_once_with([("image/png", PNG)])

    def test_unverified_helper_cleanup_blocks_recovery_and_further_spawns(self):
        process = Mock()
        process.stdout.fileno.return_value = 41
        process.poll.return_value = None
        process.wait.side_effect = host_clipboard.subprocess.TimeoutExpired(
            "fixture-helper", host_clipboard.REAP_TIMEOUT
        )
        for error_type in (OSError, RuntimeError):
            with self.subTest(error_type=error_type):
                reader = host_clipboard.Reader(
                    ("/fixture/wl-paste",), {}, helper="wl-paste"
                )
                process.reset_mock()
                with (
                    patch.object(
                        host_clipboard.subprocess, "Popen", return_value=process
                    ) as spawn,
                    patch.object(
                        host_clipboard.os,
                        "set_blocking",
                        side_effect=error_type(self.secret),
                    ) as setup_pipe,
                    self.client(reader) as (relay, transfer),
                ):
                    spawn.assert_not_called()
                    self.assertIs(reader.cleanup_verified, True)
                    self.assertEqual(
                        relay.input_bytes(KEY), refusal("clipboard-cleanup")
                    )
                    self.assertIs(reader.cleanup_verified, False)
                    process.kill.assert_called_once_with()
                    process.wait.assert_called_once_with(
                        timeout=host_clipboard.REAP_TIMEOUT
                    )
                    process.stdout.close.assert_called_once_with()
                    self.assertEqual(
                        relay.input_bytes(KEY), refusal("clipboard-disabled")
                    )
                    # A later truncated reply must not hide the earlier failure.
                    self.assertEqual(
                        relay.input_bytes(b"\x1bPtruncated"), b"\x1bPtruncated"
                    )

                    # The reader also refuses before spawning, apart from the latch.
                    with self.assertRaises(host_clipboard.ClipboardError) as error:
                        reader.read_selection()
                    self.assertEqual(
                        str(error.exception),
                        "Host clipboard helper cleanup is unverified.",
                    )
                    spawn.assert_called_once()
                    setup_pipe.assert_called_once_with(41, False)
                    transfer.put.assert_not_called()
                self.assertIn(
                    "for this client (clipboard-cleanup).", self.errors.getvalue()
                )
                self.assertNotIn("input-incomplete", self.errors.getvalue())

    def test_bad_reader_return_is_not_a_retryable_read_exception(self):
        reader = Mock(cleanup_verified=True)
        reader.read_selection.return_value = None
        with self.client(reader) as (relay, transfer):
            self.assertEqual(relay.input_bytes(KEY), refusal("clipboard-internal"))
            self.assertEqual(relay.input_bytes(KEY), refusal("clipboard-disabled"))
            reader.read_selection.assert_called_once_with()
            transfer.put.assert_not_called()

    def test_unacknowledged_transfer_refuses_further_clipboard_reads(self):
        reader = Mock(cleanup_verified=True)
        reader.read_selection.return_value = ("image/png", PNG)
        with self.client(reader) as (relay, transfer):

            def no_ack(_images):
                transfer.failed = transfer.closed = True
                raise host_drop.DropError(self.secret)

            transfer.put.side_effect = no_ack
            self.assertEqual(relay.input_bytes(KEY), refusal("transfer"))
            self.assertEqual(relay.input_bytes(KEY), refusal("clipboard-disabled"))
            with patch.object(host_drop, "capture") as capture:
                self.assertEqual(
                    relay.input_bytes(PASTE + b"/tmp/host-image.png" + END),
                    PASTE + host_drop.REFUSED + END,
                )
                capture.assert_not_called()
            reader.read_selection.assert_called_once_with()
            transfer.put.assert_called_once_with([("image/png", PNG)])

    def test_unexpected_clipboard_transfer_failure_blocks_all_import_routes(self):
        reader = Mock(cleanup_verified=True)
        images = [("image/png", PNG)]
        reader.read_selection.return_value = ("image/png", PNG)
        with (
            self.client(reader, profile="kitty") as (relay, transfer),
            patch.object(host_drop, "capture", return_value=images) as capture,
        ):
            relay.start()

            def packet(kind, fields=b"", payload=b""):
                return (
                    b"\x1b]72;t="
                    + kind
                    + b":i="
                    + relay.ident.encode()
                    + fields
                    + b";"
                    + payload
                    + b"\x1b\\"
                )

            # Have a structured drop already in flight but not yet staged.
            self.assertEqual(relay.input_bytes(packet(b"q")), b"")
            self.assertEqual(
                relay.input_bytes(packet(b"M", payload=b"text/uri-list")), b""
            )
            self.assertEqual(
                relay.input_bytes(
                    packet(
                        b"r", b":x=1", base64.b64encode(b"file:///tmp/host-image.png")
                    )
                ),
                b"",
            )
            transfer.put.assert_not_called()

            transfer.put.side_effect = RuntimeError(self.secret)
            self.assertEqual(relay.input_bytes(KEY), refusal("clipboard-internal"))
            self.assertIs(relay.broken, True)
            self.assertIs(transfer.failed, True)
            self.assertEqual(relay.input_bytes(KEY), refusal("clipboard-disabled"))
            denied = PASTE + host_drop.REFUSED + END
            self.assertEqual(
                relay.input_bytes(PASTE + b"/tmp/host-image.png" + END), denied
            )
            self.assertEqual(relay.input_bytes(packet(b"r", b":x=1")), b"")
            self.assertEqual(relay.input_bytes(packet(b"M", payload=b"image/png")), b"")
            self.assertEqual(
                relay.pending_output(),
                b"\x1b]72;t=r:o=0:i="
                + relay.ident.encode()
                + b";\x1b\\"
                + packet(b"A"),
            )
            self.assertEqual(relay.pending_output(), b"")
            reader.read_selection.assert_called_once_with()
            capture.assert_not_called()
            transfer.put.assert_called_once_with(images)


if __name__ == "__main__":
    unittest.main()
