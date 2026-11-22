"""Synthetic PTY/host-open tests: never launch a desktop process."""

import base64
import os
import re
import stat
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from oc_box import file_preview_host as host
from oc_box import file_preview_png as png

PREFIX = b"\x1b]778;oc-box-open:v1:"
GRANT_PREFIX = b"\x1b]778;oc-box-click:v1:"
FAKE_TOKEN = b"a" * 32
MIME = "/usr/bin/xdg-mime"
OPENER = "/usr/bin/xdg-open"


def frame(request, message, *, ending=b"\x07"):
    return PREFIX + request + b":" + message + ending


def begin(request, length, *, kind=b"png"):
    return frame(request, b"begin:" + kind + b":" + str(length).encode())


def data(request, image):
    return frame(request, b"data:" + base64.b64encode(image))


def proposal(request, image, *, kind=b"png"):
    return (
        begin(request, len(image), kind=kind)
        + b"".join(
            data(request, image[i : i + 4096]) for i in range(0, len(image), 4096)
        )
        + frame(request, b"end")
    )


def mouse(*, code=16, x=4, y=7, edge="M"):
    return f"\x1b[<{code};{x};{y}{edge}".encode("ascii")


def image_chunk(kind, body=b""):
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body))
    )


def example_png(*, metadata=False):
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + image_chunk(b"IHDR", header)
        + (image_chunk(b"tEXt", b"source\0synthetic") if metadata else b"")
        + image_chunk(b"IDAT", zlib.compress(b"\0\x12\x34\x56\xff"))
        + image_chunk(b"IEND")
    )


class RelayTests(unittest.TestCase):
    def grant(self, relay, *, x=4, y=7):
        press = mouse(x=x, y=y)
        release = mouse(x=x, y=y, edge="m")
        self.assertEqual(relay.mouse(press), press)
        authorized = relay.mouse(release)
        match = re.fullmatch(
            re.escape(release + GRANT_PREFIX)
            + rb"([a-f0-9]{32})"
            + f":{x}:{y}".encode("ascii")
            + b"\x07",
            authorized,
        )
        self.assertIsNotNone(match, authorized)
        return match[1]

    def test_exact_frame_removed_at_every_split_and_lookalikes_preserved(self):
        before = (
            b"\x1b]777;another protocol\x07\x1b]778;oc-box-open:v10:test\x07"
            b"\x1b]778;oc-box-png:v1:old\x07"
        )
        after = b"\x1b[1mordinary \xe2\x95\x90 text\x1b[m"
        length = len(before + proposal(FAKE_TOKEN, b"snapshot bytes") + after)
        for split in range(length + 1):
            with self.subTest(split=split):
                relay = host.Relay()
                wire = before + proposal(self.grant(relay), b"snapshot bytes") + after
                received = (
                    relay.output_bytes(wire[:split])
                    + relay.output_bytes(wire[split:])
                    + relay.finish_output()
                )
                self.assertEqual(received, before + after)
                self.assertEqual(relay.take(), ("png", b"snapshot bytes"))
                self.assertIsNone(relay.take())

    def test_child_only_frames_cannot_grant_or_transfer(self):
        relay = host.Relay()
        press = mouse()
        release = mouse(edge="m")
        forged_grant = GRANT_PREFIX + FAKE_TOKEN + b":4:7\x07"
        self.assertEqual(relay.output_bytes(press + release), press + release)
        self.assertEqual(relay.mouse(release), release)
        relay.output_bytes(forged_grant)
        self.assertEqual(relay.output_bytes(proposal(FAKE_TOKEN, b"child only")), b"")
        # An actual host press still cannot be completed by child output.
        self.assertEqual(relay.mouse(press), press)
        self.assertEqual(relay.output_bytes(release), release)
        self.assertEqual(
            relay.output_bytes(proposal(FAKE_TOKEN, b"child release")), b""
        )
        self.assertIsNone(relay.take())

    def test_mouse_does_not_grant_on_unmatched_release_drag_or_modifier_change(self):
        for packets in (
            (mouse(edge="m"),),
            (mouse(), mouse(x=5, edge="m")),
            (mouse(), mouse(y=8, edge="m")),
            (mouse(), mouse(code=48, x=5), mouse(edge="m")),
            (mouse(), mouse(code=48), mouse(edge="m")),
            (mouse(), mouse(code=0, edge="m"), mouse(edge="m")),
            (mouse(code=20), mouse(code=20, edge="m")),
            (mouse(code=17), mouse(code=17, edge="m")),
            (b"\x1b[<16;04;7M", mouse(edge="m")),
        ):
            with self.subTest(packets=packets):
                relay = host.Relay()
                for packet in packets:
                    self.assertEqual(relay.mouse(packet), packet)
                self.assertEqual(
                    relay.output_bytes(proposal(FAKE_TOKEN, b"no grant")), b""
                )
                self.assertIsNone(relay.take())

    def test_click_without_matching_transfer_is_inert(self):
        relay = host.Relay()
        self.grant(relay)
        self.assertIsNone(relay.take())
        self.assertEqual(relay.output_bytes(b"native output"), b"native output")
        self.assertIsNone(relay.take())

    def test_complete_multichunk_transfer_has_no_host_effects_even_when_consumed(self):
        # Deliberately not a valid PNG: shaping and opening are outside relay.
        payload = b"x" * 4096 + b"second chunk"
        relay = host.Relay()
        with (
            patch.object(host, "_prepare") as prepare,
            patch.object(host.os, "open") as open_file,
            patch.object(host.os, "mkdir") as mkdir,
            patch.object(host.os, "write") as write,
            patch.object(host.subprocess, "run") as run,
        ):
            wire = proposal(self.grant(relay), payload)
            for byte in wire[:-1]:
                self.assertEqual(relay.output_bytes(bytes((byte,))), b"")
                self.assertIsNone(relay.take())
            self.assertEqual(relay.output_bytes(wire[-1:]), b"")
            self.assertEqual(relay.take(), ("png", payload))
            self.assertIsNone(relay.take())
            self.assertEqual(relay.finish_output(), b"")
            prepare.assert_not_called()
            open_file.assert_not_called()
            mkdir.assert_not_called()
            write.assert_not_called()
            run.assert_not_called()

    def test_unauthorized_token_cannot_start_replace_or_replay_a_transfer(self):
        relay = host.Relay()
        token = self.grant(relay)
        wrong = b"b" * 32 if token == FAKE_TOKEN else FAKE_TOKEN
        wire = (
            proposal(wrong, b"wrong before begin")
            + begin(token, 1, kind=b"md")
            + proposal(wrong, b"wrong during transfer")
            + data(token, b"a")
            + frame(token, b"end")
            + proposal(token, b"replay before take")
        )
        self.assertEqual(relay.output_bytes(wire), b"")
        self.assertEqual(relay.take(), ("md", b"a"))
        self.assertEqual(relay.output_bytes(proposal(token, b"replay after take")), b"")
        self.assertIsNone(relay.take())

    def test_fresh_click_supersedes_earlier_inflight_or_pending_transfer(self):
        for old_state in ("inflight", "pending"):
            with self.subTest(old_state=old_state):
                relay = host.Relay()
                old = self.grant(relay)
                previous = begin(old, 1)
                if old_state == "pending":
                    previous += data(old, b"x") + frame(old, b"end")
                self.assertEqual(relay.output_bytes(previous), b"")
                fresh = self.grant(relay, x=12, y=18)
                self.assertNotEqual(fresh, old)
                self.assertIsNone(relay.take())
                self.assertEqual(
                    relay.output_bytes(data(old, b"x") + frame(old, b"end")), b""
                )
                self.assertEqual(relay.output_bytes(proposal(old, b"stale")), b"")
                self.assertIsNone(relay.take())
                self.assertEqual(
                    relay.output_bytes(proposal(fresh, b"<p>new</p>", kind=b"html")),
                    b"",
                )
                self.assertEqual(relay.take(), ("html", b"<p>new</p>"))

    def test_malformed_sequences_are_stripped_without_proposal(self):
        for label, malformed in (
            ("uppercase ID", lambda t: proposal(b"A" * 32, b"x")),
            ("nonhex ID", lambda t: proposal(b"a" * 31 + b"z", b"x")),
            ("short ID", lambda t: proposal(b"f", b"x")),
            ("oversized PNG", lambda t: begin(t, host.MAX_BYTES + 1)),
            ("oversized TXT", lambda t: begin(t, 256 * 1024 + 1, kind=b"txt")),
            ("oversized MD", lambda t: begin(t, 256 * 1024 + 1, kind=b"md")),
            ("oversized HTML", lambda t: begin(t, 256 * 1024 + 1, kind=b"html")),
            ("unsupported PDF", lambda t: proposal(t, b"x", kind=b"pdf")),
            ("unsupported SVG", lambda t: proposal(t, b"x", kind=b"svg")),
            ("unknown kind", lambda t: proposal(t, b"x", kind=b"PNG")),
            ("missing kind", lambda t: frame(t, b"begin:1") + data(t, b"f")),
            ("leading zero", lambda t: frame(t, b"begin:png:01") + data(t, b"f")),
            ("overlong decimal", lambda t: frame(t, b"begin:png:" + b"1" * 20)),
            ("zero length", lambda t: begin(t, 0)),
            ("non-ASCII", lambda t: begin(t, 1) + frame(t, b"data:\xff")),
            ("noncanonical pad bits", lambda t: begin(t, 1) + frame(t, b"data:Zh==")),
            ("missing padding", lambda t: begin(t, 1) + frame(t, b"data:Zg")),
            ("extra padding", lambda t: begin(t, 1) + frame(t, b"data:Zg===")),
            ("blank chunk", lambda t: begin(t, 1) + frame(t, b"data:")),
            ("newline in Base64", lambda t: begin(t, 1) + frame(t, b"data:Zg\n==")),
            ("too much data", lambda t: begin(t, 1) + data(t, b"12")),
            ("oversized chunk", lambda t: begin(t, 4097) + data(t, b"a" * 4097)),
            ("short early chunk", lambda t: begin(t, 4097) + data(t, b"a")),
            ("premature end", lambda t: begin(t, 4097) + data(t, b"a" * 4096)),
            (
                "end with parameter",
                lambda t: begin(t, 1) + data(t, b"a") + frame(t, b"end:"),
            ),
            ("data before begin", lambda t: data(t, b"a") + begin(t, 1)),
            ("duplicate begin", lambda t: begin(t, 1) + begin(t, 1) + data(t, b"a")),
            (
                "extra data after exact match",
                lambda t: begin(t, 1) + data(t, b"a") + data(t, b"b"),
            ),
        ):
            with self.subTest(label=label):
                relay = host.Relay()
                token = self.grant(relay)
                wire = b"left" + malformed(token) + frame(token, b"end") + b"right"
                self.assertEqual(relay.output_bytes(wire), b"leftright")
                self.assertIsNone(relay.take())
                fresh = self.grant(relay)
                self.assertEqual(
                    relay.output_bytes(proposal(fresh, b"fresh", kind=b"txt")), b""
                )
                self.assertEqual(relay.take(), ("txt", b"fresh"))

    def test_bad_begin_consumes_the_grant(self):
        relay = host.Relay()
        token = self.grant(relay)
        self.assertEqual(relay.output_bytes(begin(token, 1, kind=b"svg")), b"")
        self.assertEqual(relay.output_bytes(proposal(token, b"retry")), b"")
        self.assertIsNone(relay.take())

    def test_pressed_too_long_and_expired_grant_cannot_transfer(self):
        now = 0
        relay = host.Relay(clock=lambda: now)
        press = mouse()
        release = mouse(edge="m")
        self.assertEqual(relay.mouse(press), press)
        now += host.REQUEST_TIMEOUT
        self.assertEqual(relay.mouse(release), release)
        token = self.grant(relay)
        now += host.REQUEST_TIMEOUT
        self.assertEqual(relay.output_bytes(proposal(token, b"stale")), b"")
        self.assertIsNone(relay.take())

    def test_check_deadlines_over_inflight_and_pending_requests(self):
        now = 0
        relay = host.Relay(clock=lambda: now)
        token = self.grant(relay)
        self.assertEqual(relay.output_bytes(begin(token, 1)), b"")
        now += host.REQUEST_TIMEOUT
        self.assertEqual(
            relay.output_bytes(data(token, b"a") + frame(token, b"end")), b""
        )
        self.assertIsNone(relay.take())

        self.assertEqual(relay.output_bytes(proposal(self.grant(relay), b"b")), b"")
        now += host.PENDING_TIMEOUT - 1
        relay.check()
        self.assertEqual(relay.take(), ("png", b"b"))

        self.assertEqual(relay.output_bytes(proposal(self.grant(relay), b"c")), b"")
        now += host.PENDING_TIMEOUT
        relay.check()
        self.assertIsNone(relay.take())
        self.assertEqual(relay.output_bytes(proposal(self.grant(relay), b"d")), b"")
        self.assertEqual(relay.take(), ("png", b"d"))

    def test_only_inexact_prefix_is_flushed_on_eof(self):
        relay = host.Relay()
        self.assertEqual(relay.output_bytes(b"normal" + PREFIX[:-1]), b"normal")
        self.assertEqual(relay.finish_output(), PREFIX[:-1])  # not reserved

    def test_untrusted_oversized_timed_out_or_unclosed_frame_permanently_refused(self):
        for trigger in ("oversize", "timer", "eof"):
            with self.subTest(trigger=trigger):
                now = [0]
                relay = host.Relay(clock=lambda now=now: now[0])
                token = self.grant(relay)
                self.assertEqual(
                    relay.output_bytes(
                        proposal(token, b"now unsafe") + PREFIX + b"partial"
                    ),
                    b"",
                )
                self.assertIsNone(relay.take())
                with self.assertRaisesRegex(
                    host.RelayError, "lost the PTY frame boundary"
                ):
                    if trigger == "oversize":
                        relay.output_bytes(
                            b"x" * 100_000 + b"\x07" + proposal(token, b"b")
                        )
                    elif trigger == "timer":
                        now[0] += host.FRAME_TIMEOUT
                        relay.check()  # No incoming PTY bytes.
                    else:
                        relay.finish_output()
                for call in (
                    relay.take,
                    relay.finish_output,
                    lambda: relay.output_bytes(b"\x07" + proposal(token, b"b")),
                ):
                    with self.assertRaises(host.RelayError):
                        call()

    def test_osc_string_terminator_is_only_a_discard_boundary(self):
        for split in (False, True):
            relay = host.Relay()
            token = self.grant(relay)
            first = begin(token, 1) + frame(token, b"data:YQ==", ending=b"\x1b\\")
            ending = frame(token, b"end") + b"normal"
            if split:
                self.assertEqual(relay.output_bytes(first[:-1]), b"")
                first = first[-1:]
            self.assertEqual(relay.output_bytes(first + ending), b"normal")
            self.assertIsNone(relay.take())


class HostOpenerTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.spool = self.base / host._storage_name(os.geteuid())
        self.availability_check = host._opener_available
        self.available = self.enterContext(
            patch.object(host, "_opener_available", return_value=True)
        )
        self.enterContext(patch.object(host, "_trusted_exec", return_value=True))
        self.environment = {
            "HOME": "/home/test",
            "PATH": "/untrusted/bin",
            "XDG_CONFIG_HOME": "/host/desktop-config",
            "XDG_DATA_HOME": "/host/desktop-data",
            "XDG_CONFIG_DIRS": "/host/site-config:/etc/xdg",
            "XDG_DATA_DIRS": "/host/site-data:/usr/share",
            "WAYLAND_DISPLAY": "wayland-0",
            "KDE_SESSION_VERSION": "6",
            "KDE_FULL_SESSION": "true",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/123/bus",
            "XDG_RUNTIME_DIR": str(self.base),
            "XAUTHORITY": "/host/display-cookie",
            "SECRET_TOKEN": "not-forwarded",
            "LD_PRELOAD": "not-forwarded",
            "BASH_ENV": "not-forwarded",
            "LC_SYNTHETIC_SECRET": "not-forwarded",
            "TMPDIR": "/untrusted/temp",
        }
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))
        self.mime_result = subprocess.CompletedProcess(
            (MIME,), 0, stdout=b"image/png\n"
        )
        self.opener_result = subprocess.CompletedProcess((OPENER,), 0)
        self.timeout_program = None
        self.run_desktop = self.enterContext(
            patch.object(host.subprocess, "run", side_effect=self.fake_desktop)
        )
        self.canonical, _ = png.normalize(example_png())

    def fake_desktop(self, command, **_options):
        if command[0] == MIME:
            self.assertEqual(command, (MIME, "query", "filetype", command[-1]))
            result = self.mime_result
        else:
            self.assertEqual(command, (OPENER, command[-1]))
            result = self.opener_result
        self.assertTrue(Path(command[-1]).is_file())
        if command[0] == self.timeout_program:
            raise subprocess.TimeoutExpired(
                "synthetic private command", 5, stderr=b"synthetic private stderr"
            )
        return result

    def command_pair(self):
        self.assertEqual(self.run_desktop.call_count, 2)
        mime_call, open_call = self.run_desktop.call_args_list
        (query,) = mime_call.args
        (opener,) = open_call.args
        self.assertEqual(query, (MIME, "query", "filetype", opener[-1]))
        self.assertEqual(opener, (OPENER, query[-1]))
        return Path(opener[-1]), mime_call.kwargs, open_call.kwargs

    def test_fixed_mime_query_and_opener_use_same_private_png_and_clean_environment(
        self,
    ):
        project = self.base / "project"
        project.mkdir()
        host.open_artifact("png", self.canonical, host_mounts=(project,))
        path, mime_options, open_options = self.command_pair()
        self.assertEqual(path.parent, self.spool)
        self.assertRegex(path.name, r"^artifact-[0-9a-f]{32}\.png$")
        self.assertEqual(path.read_bytes(), self.canonical)
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(path.stat().st_uid, os.geteuid())
        env = {
            key: "/usr/bin:/bin" if key == "PATH" else value
            for key, value in self.environment.items()
            if key
            not in {
                "SECRET_TOKEN",
                "LD_PRELOAD",
                "BASH_ENV",
                "LC_SYNTHETIC_SECRET",
                "TMPDIR",
            }
        }
        common = {
            "env": env,
            "cwd": "/",
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "start_new_session": True,
            "timeout": host._OPEN_TIMEOUT,
            "check": False,
        }
        mime_options = dict(mime_options)
        self.assertFalse(mime_options.pop("shell", False))
        self.assertEqual(mime_options, dict(common, stdout=subprocess.PIPE))
        self.assertEqual(
            open_options, dict(common, stdout=subprocess.DEVNULL, shell=False)
        )

    def test_text_markdown_and_static_html_use_fixed_extensions_and_allowed_mime(self):
        for kind, source, mime in (
            ("txt", b"Plain text\r\n", b"text/plain\n"),
            ("md", b"# Markdown\n", b"text/markdown\n"),
            ("md", b"# Markdown\n", b"text/x-markdown\n"),
            ("md", b"# Markdown\n", b"text/plain\n"),
            (
                "html",
                b'<h1 onclick="synthetic()">Article</h1><script>remove me</script>',
                b"text/html\n",
            ),
        ):
            with self.subTest(kind=kind, mime=mime):
                self.run_desktop.reset_mock()
                self.mime_result.stdout = mime
                host.open_artifact(kind, source)
                path, _, _ = self.command_pair()
                self.assertEqual(path.parent, self.spool)
                self.assertRegex(path.name, rf"^artifact-[0-9a-f]{{32}}\.{kind}$")
                staged = path.read_bytes()
                if kind == "html":
                    self.assertTrue(staged.startswith(b"<!doctype html>"))
                    self.assertIn(b"<h1>Article</h1>", staged)
                    self.assertIn(b"default-src 'none'", staged)
                    self.assertNotIn(b"onclick", staged)
                    self.assertNotIn(b"remove me", staged)
                else:
                    self.assertEqual(staged, source)

    def test_nonmatching_or_invalid_mime_never_dispatches_to_opener(self):
        for kind, source, mime, returncode in (
            ("png", self.canonical, b"text/plain\n", 0),
            ("txt", b"plain\n", b"application/x-desktop\n", 0),
            ("md", b"# Article\n", b"application/x-shellscript\n", 0),
            ("html", b"<p>Article</p>", b"text/plain\n", 0),
            ("txt", b"plain\n", b"text/plain\n", 1),
            ("txt", b"plain\n", b"", 0),
            ("txt", b"plain\n", b"text/plain\nsynthetic-private\n", 0),
            ("txt", b"plain\n", b"text/plain\n\xffsynthetic-private\n", 0),
            ("txt", b"plain\n", b"text/plain" + b" " * 256, 0),
        ):
            with self.subTest(kind=kind, mime=mime, returncode=returncode):
                self.run_desktop.reset_mock()
                self.mime_result.stdout = mime
                self.mime_result.returncode = returncode
                with self.assertRaises(host.PreviewError) as error:
                    host.open_artifact(kind, source)
                self.assertEqual(
                    host.safe_open_failure(error.exception),
                    "mime-query-failed"
                    if returncode
                    else "mime-type-rejected"
                    if mime.strip()
                    else "mime-query-empty",
                )
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertNotIn(str(self.spool), str(error.exception))
                self.run_desktop.assert_called_once()
                (command,) = self.run_desktop.call_args.args
                self.assertEqual(command[:3], (MIME, "query", "filetype"))
                self.assertEqual(Path(command[-1]).parent, self.spool)
                self.assertFalse(Path(command[-1]).exists())
                self.assertEqual(list(self.spool.iterdir()), [])

    def test_only_recognized_host_kde_session_markers_are_forwarded(self):
        for value in ("bogus", "6;synthetic", "", "7"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        "KDE_SESSION_VERSION": value,
                        "KDE_FULL_SESSION": value,
                    },
                ),
            ):
                env = host._desktop_env()
                self.assertNotIn("KDE_SESSION_VERSION", env)
                self.assertNotIn("KDE_FULL_SESSION", env)

    def test_invalid_artifacts_never_staged_or_queried(self):
        for kind, candidate in (
            ("png", b"file:///home/synthetic/leak.png"),
            ("png", b""),
            ("png", b"x" * (host.MAX_BYTES + 1)),
            ("png", bytearray(self.canonical)),
            ("png", self.canonical + b"trailing payload"),
            ("png", example_png(metadata=True)),
            ("txt", b"#!/usr/bin/env example\n"),
            ("md", b"[Desktop Entry]\nExec=synthetic"),
            ("html", b"\xff"),
            ("pdf", b"%PDF-1.5"),
            ("svg", b"<svg/>"),
        ):
            with self.subTest(kind=kind, size=len(candidate)):
                with self.assertRaises(host.PreviewError):
                    host.open_artifact(kind, candidate)
                self.assertFalse(self.spool.exists())
        self.run_desktop.assert_not_called()

    def test_preflight_never_creates_state_and_rejects_exposed_spool(self):
        project = self.base / "project"
        project.mkdir()
        self.assertTrue(host.ready(host_mounts=(project,)))
        self.assertFalse(host.ready(host_mounts=(self.base,)))
        self.assertEqual(
            host.readiness_reason(host_mounts=(self.base,)),
            "runtime-staging-overlaps-mount",
        )
        self.assertFalse(host.ready(host_mounts=(self.spool,)))
        self.assertFalse(host.ready(host_mounts=(self.spool / "subdir",)))
        self.assertFalse(host.ready(host_mounts=("relative",)))
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_artifact("png", self.canonical, host_mounts=(self.base,))
        self.assertFalse(self.spool.exists())
        self.run_desktop.assert_not_called()

    def test_missing_inactive_paths_are_checked_but_active_paths_stay_strict(self):
        missing = self.base / "old-project" / "old-bind"
        inactive = host.InactiveMountSource
        self.assertEqual(
            host.readiness_reason(host_mounts=(missing,)),
            "runtime-staging-unverified",
        )
        self.assertTrue(host.ready(host_mounts=(inactive(missing),)))
        self.assertEqual(
            host.readiness_reason(host_mounts=(inactive(self.spool / "absent"),)),
            "runtime-staging-overlaps-mount",
        )
        dangling = self.base / "broken-alias"
        dangling.symlink_to(self.base / "missing")
        self.assertEqual(
            host.readiness_reason(host_mounts=(inactive(dangling / "subdir"),)),
            "runtime-staging-unverified",
        )
        self.assertFalse(self.spool.exists())
        self.run_desktop.assert_not_called()

    def test_inactive_missing_suffix_is_compared_through_a_bind_alias(self):
        alias = self.base / "alias"
        alias.mkdir()
        identity = host._identity

        def fixture_identity(path):
            return identity(self.base if Path(path) == alias else path)

        with patch.object(host, "_identity", side_effect=fixture_identity):
            self.assertTrue(
                host.ready(host_mounts=(host.InactiveMountSource(alias / "other"),))
            )
            self.assertEqual(
                host.readiness_reason(
                    host_mounts=(
                        host.InactiveMountSource(alias / self.spool.name / "missing"),
                    )
                ),
                "runtime-staging-overlaps-mount",
            )
        self.assertFalse(self.spool.exists())

    def test_stale_inactive_source_cannot_mask_permission_error(self):
        denied = self.base / "uninspectable"
        realpath = os.path.realpath

        def resolve(path, **kwargs):
            if Path(path) == denied:
                raise PermissionError("synthetic private path")
            return realpath(path, **kwargs)

        with patch.object(host.os.path, "realpath", side_effect=resolve):
            self.assertEqual(
                host.readiness_reason(host_mounts=(host.InactiveMountSource(denied),)),
                "runtime-staging-unverified",
            )

    def test_runtime_directory_has_no_fallback_and_must_be_canonical_and_private(self):
        alias = self.base / "runtime-alias"
        alias.symlink_to(self.base, target_is_directory=True)
        for value in ("", "relative", str(alias), str(self.base) + "/"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": value}),
            ):
                self.assertFalse(host.ready())
                with self.assertRaisesRegex(host.PreviewError, "not trusted"):
                    host.open_artifact("png", self.canonical)
        self.base.chmod(0o755)
        self.assertFalse(host.ready())
        self.assertEqual(host.readiness_reason(), "runtime-staging-untrusted")
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_artifact("png", self.canonical)
        self.assertFalse(self.spool.exists())
        self.run_desktop.assert_not_called()

    def test_no_desktop_or_trusted_opener_never_stages(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(host.ready())
            self.assertEqual(host.readiness_reason(), "desktop-session-unavailable")
            with self.assertRaisesRegex(
                host.PreviewError, "requires a desktop session"
            ):
                host.open_artifact("png", self.canonical)
        self.available.side_effect = self.availability_check
        for missing in (OPENER, MIME):
            with (
                self.subTest(missing=missing),
                patch.object(
                    host, "_trusted_exec", side_effect=lambda path: path != missing
                ),
            ):
                self.assertFalse(host.ready())
                self.assertEqual(
                    host.readiness_reason(),
                    f"{Path(missing).name}-unavailable-or-untrusted",
                )
                with self.assertRaisesRegex(host.PreviewError, "unavailable"):
                    host.open_artifact("png", self.canonical)
        self.assertFalse(self.spool.exists())
        self.run_desktop.assert_not_called()

    def test_later_approved_open_reaps_only_expired_artifacts_and_caps_live_files(self):
        with patch.object(host, "_MAX_FILES", 2):
            host.open_artifact("png", self.canonical)
            (old,) = self.spool.iterdir()
            self.mime_result.stdout = b"text/plain\n"
            host.open_artifact("md", b"# retained\n")
            self.assertEqual(len(list(self.spool.iterdir())), 2)
            with self.assertRaisesRegex(host.PreviewError, "limit reached"):
                host.open_artifact("txt", b"cannot yet stage\n")
            self.assertEqual(self.run_desktop.call_count, 4)
            past = time.time() - host._RETENTION_SECONDS - 60
            os.utime(old, (past, past))
            host.open_artifact("txt", b"new file\n")
            self.assertFalse(old.exists())
            self.assertEqual(
                {path.suffix for path in self.spool.iterdir()}, {".md", ".txt"}
            )
            self.assertEqual(self.run_desktop.call_count, 6)

    def test_symlinked_or_nonprivate_spool_is_not_followed(self):
        target = self.base / "outside"
        target.mkdir()
        self.spool.symlink_to(target, target_is_directory=True)
        self.assertFalse(host.ready())
        with self.assertRaises(host.PreviewError):
            host.open_artifact("png", self.canonical)
        self.assertEqual(list(target.iterdir()), [])
        self.spool.unlink()
        self.spool.mkdir(mode=0o700)
        self.spool.chmod(0o755)
        self.assertFalse(host.ready())
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_artifact("png", self.canonical)
        self.run_desktop.assert_not_called()

    def test_cleanup_never_follows_or_deletes_a_symlink_or_hardlink(self):
        self.spool.mkdir(mode=0o700)
        target = self.base / "outside-file"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        past = time.time() - host._RETENTION_SECONDS - 60
        os.utime(target, (past, past))
        candidate = self.spool / ("artifact-" + "1" * 32 + ".png")
        for link in (
            lambda: candidate.symlink_to(target),
            lambda: os.link(target, candidate),
        ):
            link()
            with self.assertRaisesRegex(host.PreviewError, "not trusted"):
                host.open_artifact("png", self.canonical)
            self.assertTrue(candidate.exists())
            self.assertEqual(target.read_bytes(), b"outside")
            candidate.unlink()
        self.run_desktop.assert_not_called()

    def test_timeout_error_remains_private_and_never_unlinks_approved_file_early(self):
        self.timeout_program = OPENER
        with self.assertRaisesRegex(
            host.PreviewError, "did not report completion"
        ) as error:
            host.open_artifact("png", self.canonical)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(host.safe_open_failure(error.exception), "opener-timeout")
        self.command_pair()
        (path,) = self.spool.iterdir()
        self.assertEqual(path.read_bytes(), self.canonical)

    def test_mime_timeout_never_dispatches_and_error_remains_private(self):
        self.timeout_program = MIME
        with self.assertRaises(host.PreviewError) as error:
            host.open_artifact("png", self.canonical)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(host.safe_open_failure(error.exception), "mime-query-timeout")
        self.run_desktop.assert_called_once()
        (command,) = self.run_desktop.call_args.args
        self.assertEqual(command[:3], (MIME, "query", "filetype"))
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_rejected_copy_does_not_consume_last_slot_or_remove_existing_copy(self):
        with patch.object(host, "_MAX_FILES", 2):
            host.open_artifact("png", self.canonical)
            (retained,) = self.spool.iterdir()
            self.mime_result.stdout = b"application/x-desktop\n"
            for _ in range(8):
                with self.assertRaises(host.PreviewError) as error:
                    host.open_artifact("md", b"# attempted draft\n")
                self.assertEqual(
                    host.safe_open_failure(error.exception), "mime-type-rejected"
                )
                self.assertEqual(list(self.spool.iterdir()), [retained])
            self.mime_result.stdout = b"text/plain\n"
            host.open_artifact("md", b"# opened draft\n")
            self.assertTrue(retained.exists())
            self.assertEqual(len(list(self.spool.iterdir())), 2)

    def test_previous_spool_is_not_reused_or_cleaned(self):
        legacy = self.base / f"oc-box-open-{os.geteuid()}"
        legacy.mkdir(mode=0o700)
        for index in range(host._MAX_FILES):
            path = legacy / f"artifact-{index:032x}.png"
            path.write_bytes(b"older copy")
            path.chmod(0o600)
        host.open_artifact("png", self.canonical)
        self.assertEqual(len(list(legacy.iterdir())), host._MAX_FILES)
        self.assertTrue(
            all(path.read_bytes() == b"older copy" for path in legacy.iterdir())
        )
        self.assertEqual(len(list(self.spool.iterdir())), 1)

    def test_only_explicit_fixed_failure_labels_leave_the_worker(self):
        for error in (
            ValueError("synthetic private message"),
            host.PreviewError("private", reason="synthetic private reason"),
        ):
            self.assertEqual(host.safe_open_failure(error), "host-open-failed")

    def test_failed_partial_write_is_not_left_for_the_opener(self):
        write = os.write

        def short_then_fail(fd, payload):
            if len(payload) == len(self.canonical):
                return write(fd, payload[:5])
            raise OSError("synthetic private storage diagnostic")

        with (
            patch.object(host.os, "write", side_effect=short_then_fail),
            self.assertRaisesRegex(
                host.PreviewError, "storage is unavailable"
            ) as error,
        ):
            host.open_artifact("png", self.canonical)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(list(self.spool.iterdir()), [])
        self.run_desktop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
