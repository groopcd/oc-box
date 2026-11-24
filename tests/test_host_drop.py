"""Host-selected file transfer, protocol and launch contracts; no live Podman."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import host_drop, oc_box as box, sharing

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PDF = b"%PDF-1.4\n% synthetic fixture, not a document decoder assertion\n%%EOF\n"


class HostDropTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-drop-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "Screenshot with spaces.png"
        self.source.write_bytes(PNG)
        self.commands = []

        def client_command(facade, data, connection, arguments):
            self.commands.append(arguments)
            return box.ContainerCommand(
                [sys.executable, *arguments[1:]],
                {"PATH": os.defpath, "LANG": "C.UTF-8"},
            )

        self.facade = SimpleNamespace(
            PACKAGE=box.PACKAGE,
            APP=self.root / "application",
            state_root=lambda: self.root / "state",
            vault=SimpleNamespace(vault_path=lambda: self.root / "vault/vault.age"),
            workspace_service=SimpleNamespace(client_command=client_command),
        )
        self.data = {"opencode_version": "2.3.0"}
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-private-control",
        }

    def test_exact_complete_path_paste_grammar_and_no_shell_expansion(self):
        for payload, expected in (
            (b"/tmp/Screenshot.png", [Path("/tmp/Screenshot.png")]),
            (
                b"'/tmp/Screenshot with spaces.png' ",
                [Path("/tmp/Screenshot with spaces.png")],
            ),
            (
                b"/tmp/Screenshot\\ with\\ spaces.png",
                [Path("/tmp/Screenshot with spaces.png")],
            ),
            (b'"/tmp/a.png" "/tmp/b.pdf"', [Path("/tmp/a.png"), Path("/tmp/b.pdf")]),
            ("/tmp/截图.png".encode(), [Path("/tmp/截图.png")]),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(host_drop.pasted_paths(payload), expected)
        for payload in (
            b"",
            b"look at /tmp/a.png",
            b"/tmp/a.png\n",
            b"/tmp/a.png\t",
            b"relative.png",
            b"~/a.png",
            b"file:///tmp/a.png",
            b"//tmp/a.png",
            b"/tmp/../a.png",
            b"/tmp/./a.png",
            b"/tmp//a.png",
            b"/tmp/a.docx",
            b"/tmp/a.json",
            b"/tmp/a.png; echo bad",
            b"/tmp/a.png\x00",
            b"/tmp/\xff.png",
            b"'" + b"x" * 17000 + b"'",
            b"/tmp/1.png /tmp/2.png /tmp/3.png /tmp/4.png /tmp/5.png",
        ):
            with self.subTest(payload=payload[:80]):
                self.assertIsNone(host_drop.pasted_paths(payload))

    def test_structured_uri_list_has_no_remote_fetch_or_traversal(self):
        self.assertEqual(
            host_drop.uri_paths(
                b"# selection\r\nfile:///tmp/one%20image.png\r\nfile://localhost/tmp/two.pdf\r\n"
            ),
            [Path("/tmp/one image.png"), Path("/tmp/two.pdf")],
        )
        for value in (
            b"https://example.invalid/a.png",
            b"file://foreign/tmp/a.png",
            b"file:///tmp/a.png?query=1",
            b"file:///tmp/a.png#part",
            b"file:///tmp/%2e%2e/a.png",
            b"file:///tmp/%00a.png",
            b"file:///tmp/%zz.png",
            b"file:///tmp/a.docx",
        ):
            with self.subTest(value=value), self.assertRaises(host_drop.DropError):
                host_drop.uri_paths(value)

    def test_capture_is_bounded_content_identification_not_extension_or_decoder(self):
        for number, (content, mime) in enumerate(
            (
                (PNG, "image/png"),
                (b"\xff\xd8\xffsynthetic", "image/jpeg"),
                (b"GIF89asynthetic", "image/gif"),
                (b"RIFF1234WEBPsynthetic", "image/webp"),
                (PDF, "application/pdf"),
            )
        ):
            path = self.root / f"source-{number}.{mime.split('/', 1)[1]}"
            path.write_bytes(content)
            before = path.stat()
            self.assertEqual(host_drop.capture([path], ()), [(mime, content)])
            after = path.stat()
            self.assertEqual(
                (before.st_mtime_ns, before.st_ctime_ns, before.st_mode),
                (after.st_mtime_ns, after.st_ctime_ns, after.st_mode),
            )
        bad = self.root / "not-an-image.png"
        bad.write_bytes(b"synthetic private plaintext")
        with self.assertRaises(host_drop.DropError):
            host_drop.capture([bad], ())

    def test_selected_extension_and_signature_agree_with_jpeg_aliases(self):
        for suffix, content in (
            (".png", PDF),
            (".pdf", PNG),
            (".jpeg", PNG),
            (".gif", PNG),
        ):
            path = self.root / ("mismatched" + suffix)
            path.write_bytes(content)
            with self.subTest(suffix=suffix), self.assertRaises(host_drop.DropError):
                host_drop.capture([path], ())
        for suffix in (".jpg", ".jpeg", ".JPG", ".JPEG"):
            path = self.root / ("alias" + suffix)
            path.write_bytes(b"\xff\xd8\xffsynthetic")
            self.assertEqual(host_drop.capture([path], ())[0][0], "image/jpeg")

    def test_all_sources_are_pinned_before_any_batch_bytes_are_read(self):
        missing = self.root / "missing.png"
        with patch.object(host_drop.os, "pread") as read:
            with self.assertRaises(host_drop.DropError):
                host_drop.capture([self.source, missing], ())
            read.assert_not_called()
        symlink = self.root / "alias.png"
        symlink.symlink_to(self.source)
        for source in (symlink, self.root):
            with self.subTest(source=source), self.assertRaises(host_drop.DropError):
                host_drop.capture([source], ())
        with self.assertRaises(host_drop.DropError):
            host_drop.capture([self.source], (self.root,))

    def test_source_changes_and_per_file_batch_limits_refuse_before_staging(self):
        original = os.pread

        def mutate(fd, size, offset):
            content = original(fd, size, offset)
            if offset == 0:
                self.source.write_bytes(PNG + b"changed")
            return content

        with patch.object(host_drop.os, "pread", side_effect=mutate):
            with self.assertRaises(host_drop.DropError):
                host_drop.capture([self.source], ())
        self.source.write_bytes(PNG + b"x" * 128)
        with patch.object(host_drop, "MAX_FILE", 64):
            with self.assertRaises(host_drop.DropError):
                host_drop.capture([self.source], ())
        with patch.object(host_drop, "MAX_BATCH_BYTES", 64):
            with self.assertRaises(host_drop.DropError):
                host_drop.capture([self.source], ())

    def test_real_private_pipe_transfer_uses_generated_names_and_removes_only_its_root(
        self,
    ):
        with host_drop.Transfer(self.facade, self.data, self.connection) as transfer:
            root = Path(transfer.root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            result = transfer.put([("image/png", PNG), ("application/pdf", PDF)])
            paths = [Path(word) for word in result.decode().split()]
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(path.parent == root for path in paths))
            self.assertEqual([path.read_bytes() for path in paths], [PNG, PDF])
            self.assertTrue(
                all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
            )
            self.assertNotIn(str(self.source), result.decode())
            command = json.dumps(self.commands)
            for value in (
                base64.b64encode(PNG).decode(),
                self.connection["password"],
                str(self.source),
            ):
                self.assertNotIn(value, command)
        self.assertTrue(transfer.cleanup_verified)
        self.assertFalse(root.exists())
        self.assertEqual(self.source.read_bytes(), PNG)

    def test_transfer_quota_refuses_without_reusing_or_overwriting_previous_file(self):
        with host_drop.Transfer(self.facade, self.data, self.connection) as transfer:
            first = Path(transfer.put([("image/png", PNG)]).decode())
            transfer.files = host_drop.MAX_FILES
            with self.assertRaises(host_drop.DropError):
                transfer.put([("image/png", PNG)])
            self.assertEqual(first.read_bytes(), PNG)
        self.assertFalse(first.exists())

    def test_unreaped_transfer_cleanup_is_bounded_and_not_retried(self):
        transfer = host_drop.Transfer(self.facade, self.data, self.connection)
        process = Mock(pid=4343, returncode=None)
        process.wait.side_effect = host_drop.subprocess.TimeoutExpired("synthetic", 3)
        transfer.process = process
        transfer.failed = True
        with patch.object(host_drop.os, "killpg") as kill:
            transfer.close()
            transfer.close()
        self.assertTrue(transfer.closed)
        self.assertFalse(transfer.cleanup_verified)
        self.assertEqual(process.wait.call_count, 3)
        self.assertTrue(
            all(call.kwargs == {"timeout": 3} for call in process.wait.call_args_list)
        )
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [
                (process.pid, host_drop.signal.SIGTERM),
                (process.pid, host_drop.signal.SIGKILL),
            ],
        )
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_transfer_teardown_fault_does_not_mask_client_outcome(self):
        for fault in ("wait", "signal", "stdout"):
            with self.subTest(fault=fault):
                relay = host_drop.Relay(
                    self.facade, self.data, self.connection, profile="konsole"
                )
                process = Mock(pid=4343, returncode=0)
                process.poll.return_value = None
                process.wait.return_value = 0
                relay.transfer.process = process
                errors = io.StringIO()
                failure = OSError("synthetic private detail")
                if fault == "wait":
                    process.wait.side_effect = failure
                elif fault == "signal":
                    process.wait.side_effect = [
                        host_drop.subprocess.TimeoutExpired("synthetic", 3),
                        0,
                    ]
                else:
                    process.stdout.close.side_effect = failure
                with (
                    contextlib.redirect_stderr(errors),
                    patch.object(host_drop.os, "killpg", side_effect=failure),
                    patch.object(
                        relay.transfer,
                        "exchange",
                        return_value={"ok": True, "closed": True},
                    ),
                    self.assertRaisesRegex(RuntimeError, "^client result$"),
                ):
                    try:
                        raise RuntimeError("client result")
                    finally:
                        relay.__exit__()
                self.assertFalse(relay.transfer.cleanup_verified)
                process.stdout.close.assert_called_once_with()
                self.assertIn(
                    "Temporary import cleanup could not be verified", errors.getvalue()
                )
                self.assertNotIn(str(failure), errors.getvalue())

    def test_only_kitty_requires_matching_structured_drop_confirmation(self):
        for profile, reply, fallback in (
            ("konsole", None, False),
            ("kitty", None, True),
            ("kitty", "wrong", True),
            ("kitty", "matched", False),
        ):
            with self.subTest(profile=profile, reply=reply):
                relay = host_drop.Relay(
                    self.facade, self.data, self.connection, profile=profile
                )
                relay.start()
                if reply is not None:
                    ident = relay.ident if reply == "matched" else "0"
                    relay.control(f"72;t=q:i={ident};".encode())
                relay.stop()
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    relay.__exit__()
                self.assertEqual(
                    "did not confirm structured file drops" in errors.getvalue(),
                    fallback,
                )

    def test_drop_only_framing_refusal_is_reported_after_exit(self):
        relay = host_drop.Relay(
            self.facade, self.data, self.connection, profile="konsole"
        )
        # A nested ESC is not a valid input boundary for a framed pathname paste.
        relay.input_bytes(b"\x1b[200~invalid\x1bX")
        self.assertIsNotNone(relay.input.disable_reason)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            relay.__exit__()
        self.assertIn("Host pathname importing was disabled", errors.getvalue())

    def test_clipboard_framing_refusal_reports_one_fixed_warning_on_client_exit(self):
        clipboard = Mock()
        relay = host_drop.Relay(
            self.facade,
            self.data,
            self.connection,
            profile="konsole",
            clipboard=clipboard,
        )
        errors = io.StringIO()
        self.assertEqual(relay.input_bytes(b"\x1b"), b"")
        # Unknown escape input is still refused; a later shortcut cannot grant.
        self.assertEqual(relay.input_bytes(b"\x01"), b"\x1b\x01")
        self.assertEqual(relay.input_bytes(b"\x19"), b"\x19")
        with contextlib.redirect_stderr(errors):
            relay.__exit__()
        clipboard.read_selection.assert_not_called()
        self.assertEqual(
            errors.getvalue(),
            "oc-box: Host clipboard importing disabled for this client (input-framing). "
            "Open a fresh client to try again; nothing was restarted.\n"
            "oc-box: Host input framing stopped at: escape-prefix.\n",
        )

    def test_clipboard_framing_recovery_waits_for_fresh_shortcut_in_same_client(self):
        clipboard = Mock()
        clipboard.read_selection.return_value = ("image/png", PNG)
        relay = host_drop.Relay(
            self.facade,
            self.data,
            self.connection,
            profile="konsole",
            clipboard=clipboard,
        )
        errors = io.StringIO()
        with (
            patch.object(
                relay.transfer, "put", return_value=b"/tmp/oc-box-drop-fixture/drop.png"
            ) as put,
            contextlib.redirect_stderr(errors),
        ):
            for frame in (
                b"\x1b[1\x19;2R",
                b"\x1b[200~synthetic\x19\x1b[201~",
                b"\x1b\x1b\x1bOP",
                b"\x1b\x1b\x1b[200~synthetic\x19\x1b[201~",
                b"\x1b\x1b\x19\r\x19",
                b"\x1b[D",
            ):
                self.assertEqual(relay.input_bytes(frame), frame)
            clipboard.read_selection.assert_not_called()
            put.assert_not_called()
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(
                relay.input_bytes(b"\x16\x19"),
                b"\x16\x1b[200~/tmp/oc-box-drop-fixture/drop.png\x1b[201~",
            )
            clipboard.read_selection.assert_called_once_with()
            put.assert_called_once_with([("image/png", PNG)])
            relay.__exit__()
        self.assertEqual(
            errors.getvalue(),
            "oc-box: Host clipboard input framing recovered (input-framing-recovered). "
            "No clipboard read was retried and nothing was restarted.\n",
        )

    def test_incomplete_opaque_reply_does_not_read_clipboard_or_disclose_diagnostics(
        self,
    ):
        clipboard = Mock()
        relay = host_drop.Relay(
            self.facade,
            self.data,
            self.connection,
            profile="kitty",
            clipboard=clipboard,
        )
        reply = b"\x1bPsynthetic-private-data\x19"
        self.assertEqual(relay.input_bytes(reply), reply)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            relay.__exit__()
        clipboard.read_selection.assert_not_called()
        self.assertEqual(
            errors.getvalue(),
            "oc-box: Host clipboard importing disabled for this client (input-incomplete). "
            "Open a fresh client to try again; nothing was restarted.\n",
        )

    def test_paste_only_selection_does_not_read_prose_or_server_path_requests(self):
        with host_drop.Relay(
            self.facade, self.data, self.connection, profile="konsole"
        ) as relay:
            with patch.object(sharing, "Export") as opened:
                self.assertEqual(
                    relay.input_bytes(b"Please read /tmp/private.png"),
                    b"Please read /tmp/private.png",
                )
                self.assertEqual(
                    relay.input_bytes(
                        b"\x1b]l\x1b[200~/tmp/private.png\x1b[201~\x1b\\"
                    ),
                    b"\x1b]l\x1b[200~/tmp/private.png\x1b[201~\x1b\\",
                )
                opened.assert_not_called()
            frame = (
                b"\x1b[200~" + ("'" + str(self.source) + "'").encode() + b"\x1b[201~"
            )
            rewritten = relay.input_bytes(frame + b"next keys")
            self.assertTrue(rewritten.startswith(b"\x1b[200~/tmp/oc-box-drop-"))
            self.assertTrue(rewritten.endswith(b"\x1b[201~next keys"))
            self.assertNotIn(str(self.source).encode(), rewritten)
            self.assertEqual(relay.transfer.files, 1)

    def test_native_output_cannot_trigger_file_import_or_issue_reserved_dnd_requests(
        self,
    ):
        with host_drop.Relay(
            self.facade, self.data, self.connection, profile="kitty"
        ) as relay:
            frame = b"\x1b[200~" + str(self.source).encode() + b"\x1b[201~"
            with patch.object(sharing, "Export") as opened:
                self.assertEqual(relay.output_bytes(frame), frame)
                filtered = relay.output_bytes(b"\x1b]72;t=r:x=1;\x1b\\")
                self.assertNotIn(b"\x1b]72", filtered)
                self.assertEqual(
                    relay.output_bytes(b"\x1b_Gf=100;ZGF0YQ==\x1b\\"),
                    b"\x1b_Gf=100;ZGF0YQ==\x1b\\",
                )
                opened.assert_not_called()

    def test_failed_eligible_import_is_visible_placeholder_without_enter_or_host_path(
        self,
    ):
        with host_drop.Relay(
            self.facade, self.data, self.connection, profile="konsole"
        ) as relay:
            frame = b"\x1b[200~/tmp/absent-screenshot.png\x1b[201~"
            self.assertEqual(
                relay.input_bytes(frame),
                b"\x1b[200~" + host_drop.REFUSED + b"\x1b[201~",
            )
            self.assertEqual(relay.transfer.files, 0)
            self.assertEqual(relay.refused, 1)

    def test_protocol_output_waits_for_complete_native_graphics_boundary(self):
        with host_drop.Relay(
            self.facade, self.data, self.connection, profile="kitty"
        ) as relay:
            relay.output_bytes(b"\x1b_Gm=1;partial")
            relay.start()
            self.assertEqual(relay.pending_output(), b"")
            relay.output_bytes(b"\x1b\\")
            self.assertIn(b"\x1b]72;t=q", relay.pending_output())
            teardown = relay.stop()
            self.assertIn(b"t=A", teardown)

    def test_konsole_uses_no_private_dnd_protocol_and_preserves_output(self):
        with host_drop.Relay(
            self.facade, self.data, self.connection, profile="konsole"
        ) as relay:
            relay.start()
            self.assertIsNone(relay.protocol)
            self.assertEqual(relay.pending_output(), b"")
            output = b"\x90opaque\x1b]72;t=A;\x07\x1b\\"
            self.assertEqual(relay.output_bytes(output), output)
            self.assertEqual(relay.stop(), b"")


class KittyDropTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.relay = SimpleNamespace(
            refused=0,
            protected=(),
            stage=Mock(return_value=b"/tmp/oc-box-drop-fixture/drop_image.png"),
        )
        self.now = 1.0
        self.protocol = host_drop.KittyDrop(
            self.relay, self.sent.append, clock=lambda: self.now
        )

    def packet(self, kind, payload=b"", **fields):
        metadata = ":".join(
            f"{key}={value}"
            for key, value in {"t": kind, "i": self.protocol.ident, **fields}.items()
        ).encode()
        return b"72;" + metadata + b";" + payload

    def register(self):
        self.protocol.start()
        self.protocol.receive(self.packet("q"))

    def test_query_and_motion_do_not_authorize_data_import(self):
        self.protocol.start()
        self.protocol.receive(b"72;t=q:i=0;")
        self.assertFalse(self.protocol.registered)
        self.protocol.receive(self.packet("q"))
        self.assertTrue(self.protocol.registered)
        self.protocol.receive(self.packet("m", b"image/png text/uri-list", x=1, y=1))
        self.assertIsNone(self.protocol.index)
        self.protocol.receive(self.packet("r", base64.b64encode(PNG), x=1))
        self.relay.stage.assert_not_called()

    def test_actual_drop_data_returns_only_a_native_paste_not_submission(self):
        self.register()
        self.protocol.receive(self.packet("M", b"image/png text/uri-list"))
        self.assertEqual(self.protocol.index, 1)
        self.assertIn(b"t=r", self.sent[-1])
        self.protocol.receive(self.packet("r", base64.b64encode(PNG), x=1))
        self.relay.stage.assert_not_called()
        result = self.protocol.receive(self.packet("r", x=1))
        self.relay.stage.assert_called_once_with([("image/png", PNG)])
        self.assertEqual(
            result, b"\x1b[200~/tmp/oc-box-drop-fixture/drop_image.png\x1b[201~"
        )
        self.assertIsNone(self.protocol.index)
        self.assertNotIn(b"\r", result)
        self.protocol.receive(self.packet("r", x=1))
        self.assertEqual(self.relay.stage.call_count, 1)

    def test_chunk_continuations_cannot_change_first_metadata_or_cross_file_indices(
        self,
    ):
        self.register()
        self.protocol.receive(self.packet("M", b"image/png"))
        encoded = base64.b64encode(PNG)
        self.protocol.receive(self.packet("r", encoded[:32], x=1, m=1))
        rest = b"72;i=" + self.protocol.ident.encode() + b":m=0;"
        self.protocol.receive(rest + encoded[32:])
        result = self.protocol.receive(self.packet("r", x=1))
        self.assertIsInstance(result, bytes)
        self.relay.stage.assert_called_once_with([("image/png", PNG)])
        self.protocol.receive(self.packet("M", b"image/png"))
        self.protocol.receive(self.packet("r", encoded[:32], x=1, m=1))
        continuation = self.protocol.receive(self.packet("r", encoded[32:], x=2, m=0))
        self.assertIs(continuation, True)
        self.assertEqual(self.protocol.index, 1)
        self.protocol.receive(self.packet("r", x=1))
        self.assertEqual(self.relay.stage.call_count, 2)

    def test_wrong_index_size_invalid_bytes_and_timeout_never_stage(self):
        self.register()
        for fields, payload in (
            ({"x": 2}, b"AA=="),
            ({"x": 1}, b"%%%"),
            ({"x": 1}, b"A" * 4097),
        ):
            self.protocol.receive(self.packet("M", b"image/png"))
            value = self.protocol.receive(self.packet("r", payload, **fields))
            self.assertIn(host_drop.REFUSED, value)
        self.protocol.receive(self.packet("M", b"image/png"))
        self.protocol.receive(self.packet("r", base64.b64encode(PNG), x=1))
        self.now += 30
        self.protocol.tick()
        self.assertIsNone(self.protocol.index)
        self.protocol.receive(self.packet("r", x=1))
        self.relay.stage.assert_not_called()

    def test_structured_offer_cannot_disguise_a_pdf_as_a_png(self):
        self.register()
        self.protocol.receive(self.packet("M", b"image/png"))
        self.protocol.receive(self.packet("r", base64.b64encode(PDF), x=1))
        refused = self.protocol.receive(self.packet("r", x=1))
        self.assertIn(host_drop.REFUSED, refused)
        self.relay.stage.assert_not_called()


class HostDropLaunchTests(unittest.TestCase):
    def test_default_and_restricted_commands_preserve_normal_lifecycle(self):
        self.assertFalse(box.parser().parse_args([]).host_drop)
        for command in ("sync", "terminal", "web", "list", "status", "purge"):
            with (
                self.subTest(command=command),
                patch.object(box, "check_backend") as backend,
            ):
                with self.assertRaises((box.BoxError, SystemExit)):
                    box.main([command, "--host-drop"])
                backend.assert_not_called()
        with patch.object(box, "run_tui", return_value=7) as run:
            self.assertEqual(box.run_native_tui({}, ["native-fixture"], None), 7)
            run.assert_called_once_with(["native-fixture"])

    def test_native_v2_series_and_terminal_contracts_not_patch_allowlist(self):
        with patch.object(host_drop.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(host_drop.DropError):
                host_drop.preflight("2.3.0")
        with (
            patch.object(host_drop.sys.stdin, "isatty", return_value=True),
            patch.object(host_drop.sys.stdout, "isatty", return_value=True),
            patch.dict(os.environ, {}, clear=True),
        ):
            with patch.dict(os.environ, {"KONSOLE_VERSION": "260803"}):
                for version in ("1.19.5", "3.0.0", "2.invalid", {}):
                    with (
                        self.subTest(version=version),
                        self.assertRaises(host_drop.DropError),
                    ):
                        host_drop.preflight(version)
                for version in (
                    "2.0.0",
                    "2.1.99",
                    "2.2.6",
                    "2.2.7",
                    "2.3.0",
                    "2.3.2",
                    "2.9.9",
                    "2.123.456+fixture",
                    "opencode v2.3.2",
                ):
                    with self.subTest(version=version):
                        self.assertEqual(host_drop.preflight(version), "konsole")
            for key in ("TMUX", "STY", "SSH_CONNECTION", "SSH_TTY"):
                with (
                    patch.dict(
                        os.environ, {key: "synthetic", "KONSOLE_VERSION": "260803"}
                    ),
                    self.assertRaises(host_drop.DropError),
                ):
                    host_drop.preflight("2.3.0")
            with self.assertRaises(host_drop.DropError):
                host_drop.preflight("2.3.0")
            with patch.dict(os.environ, {"KONSOLE_VERSION": "260803"}):
                self.assertEqual(host_drop.preflight("2.3.0"), "konsole")
            with patch.dict(os.environ, {"KITTY_WINDOW_ID": "1"}):
                self.assertEqual(host_drop.preflight("2.3.0"), "kitty")
            with patch.dict(
                os.environ, {"KONSOLE_VERSION": "260803", "KITTY_WINDOW_ID": "1"}
            ):
                with self.assertRaises(host_drop.DropError):
                    host_drop.preflight("2.3.0")

    def test_v1_flag_refusal_occurs_before_any_vault_unlock(self):
        args = SimpleNamespace(host_drop=True, command="start")
        with (
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "launch_environment") as launch,
        ):
            with self.assertRaises(box.BoxError):
                box.launch(Path("/synthetic/workspace.json"), {}, args)
            credentials.assert_not_called()
            launch.assert_not_called()
