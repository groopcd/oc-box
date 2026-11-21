"""Synthetic PTY/PNG tests: never launch a desktop process."""

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

PREFIX = b"\x1b]778;oc-box-png:v1:"
A, B, C, D = (f"{n:032x}".encode() for n in range(4))


def frame(request, message, *, ending=b"\x07"):
    return PREFIX + request + b":" + message + ending


def begin(request, length):
    return frame(request, b"begin:" + str(length).encode())


def data(request, image):
    return frame(request, b"data:" + base64.b64encode(image))


def proposal(request, image):
    return (
        begin(request, len(image))
        + b"".join(
            data(request, image[i : i + 4096]) for i in range(0, len(image), 4096)
        )
        + frame(request, b"end")
    )


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
    def test_exact_frame_removed_at_every_split_and_lookalikes_preserved(self):
        before = b"\x1b]777;another protocol\x07\x1b]778;oc-box-png:v10:test\x07"
        after = b"\x1b[1mordinary \xe2\x95\x90 text\x1b[m"
        wire = before + proposal(A, b"snapshot bytes") + after
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                relay = host.Relay()
                received = (
                    relay.output_bytes(wire[:split])
                    + relay.output_bytes(wire[split:])
                    + relay.finish_output()
                )
                self.assertEqual(received, before + after)
                self.assertEqual(relay.take(), b"snapshot bytes")
                self.assertIsNone(relay.take())

    def test_complete_multichunk_proposal_has_no_host_effects_even_when_consumed(self):
        # Deliberately not a PNG: a PTY sequence can only propose immutable
        # bytes; host validation and approval are not part of the wire parser.
        image = b"x" * 4096 + b"second chunk"
        wire = proposal(A, image)
        relay = host.Relay()
        with (
            patch.object(host, "_validate_canonical") as validate,
            patch.object(host.os, "open") as open_file,
            patch.object(host.subprocess, "run") as run,
        ):
            for byte in wire[:-1]:
                self.assertEqual(relay.output_bytes(bytes((byte,))), b"")
                self.assertIsNone(relay.take())
            self.assertEqual(relay.output_bytes(wire[-1:]), b"")
            self.assertEqual(relay.take(), image)
            self.assertIsNone(relay.take())
            self.assertEqual(relay.finish_output(), b"")
            validate.assert_not_called()
            open_file.assert_not_called()
            run.assert_not_called()

    def test_pending_is_never_replaced_and_concurrent_or_seen_ids_do_not_revive(self):
        relay = host.Relay()
        wire = (
            begin(A, 1)
            + proposal(B, b"b")
            + data(A, b"a")
            + frame(A, b"end")
            + proposal(C, b"c")
        )
        self.assertEqual(relay.output_bytes(wire), b"")
        self.assertEqual(relay.take(), b"a")
        self.assertEqual(
            relay.output_bytes(b"".join(proposal(i, b"r") for i in (A, B, C))), b""
        )
        self.assertIsNone(relay.take())
        self.assertEqual(relay.output_bytes(proposal(D, b"d")), b"")
        self.assertEqual(relay.take(), b"d")

    def test_malformed_sequences_are_stripped_without_proposal(self):
        for label, malformed in (
            ("uppercase ID", proposal(b"A" * 32, b"x")),
            ("nonhex ID", proposal(b"a" * 31 + b"z", b"x")),
            ("short ID", proposal(b"f", b"x")),
            ("oversized total", begin(A, host.MAX_BYTES + 1)),
            ("leading zero", frame(A, b"begin:01") + data(A, b"f")),
            ("overlong decimal", frame(A, b"begin:" + b"1" * 20)),
            ("zero length", begin(A, 0)),
            ("non-ASCII", begin(A, 1) + frame(A, b"data:\xff")),
            ("noncanonical pad bits", begin(A, 1) + frame(A, b"data:Zh==")),
            ("missing padding", begin(A, 1) + frame(A, b"data:Zg")),
            ("extra padding", begin(A, 1) + frame(A, b"data:Zg===")),
            ("blank chunk", begin(A, 1) + frame(A, b"data:")),
            ("newline in Base64", begin(A, 1) + frame(A, b"data:Zg\n==")),
            ("too much data", begin(A, 1) + data(A, b"12")),
            ("oversized chunk", begin(A, 4097) + data(A, b"a" * 4097)),
            ("short early chunk", begin(A, 4097) + data(A, b"a")),
            ("premature end", begin(A, 4097) + data(A, b"a" * 4096)),
            ("end with parameter", begin(A, 1) + data(A, b"a") + frame(A, b"end:")),
            ("data before begin", data(A, b"a") + begin(A, 1)),
            ("duplicate begin", begin(A, 1) + begin(A, 1) + data(A, b"a")),
            (
                "extra data after exact match",
                begin(A, 1) + data(A, b"a") + data(A, b"b"),
            ),
        ):
            with self.subTest(label=label):
                relay = host.Relay()
                wire = b"left" + malformed + frame(A, b"end") + b"right"
                self.assertEqual(relay.output_bytes(wire), b"leftright")
                self.assertIsNone(relay.take())
                self.assertEqual(relay.output_bytes(proposal(B, b"fresh")), b"")
                self.assertEqual(relay.take(), b"fresh")

    def test_check_deadlines_over_inflight_and_pending_requests(self):
        now = 0
        relay = host.Relay(clock=lambda: now)
        self.assertEqual(relay.output_bytes(begin(A, 1)), b"")
        now += host.REQUEST_TIMEOUT
        self.assertEqual(relay.output_bytes(data(A, b"a") + frame(A, b"end")), b"")
        self.assertIsNone(relay.take())

        self.assertEqual(relay.output_bytes(proposal(B, b"b")), b"")
        now += host.PENDING_TIMEOUT - 1
        relay.check()
        self.assertEqual(relay.take(), b"b")

        self.assertEqual(relay.output_bytes(proposal(C, b"c")), b"")
        now += host.PENDING_TIMEOUT
        relay.check()
        self.assertIsNone(relay.take())
        self.assertEqual(relay.output_bytes(proposal(D, b"d")), b"")
        self.assertEqual(relay.take(), b"d")

    def test_only_inexact_prefix_is_flushed_on_eof(self):
        relay = host.Relay()
        self.assertEqual(relay.output_bytes(b"normal" + PREFIX[:-1]), b"normal")
        self.assertEqual(relay.finish_output(), PREFIX[:-1])  # not reserved

    def test_untrusted_oversized_timed_out_or_unclosed_frame_permanently_refused(self):
        for trigger in ("oversize", "timer", "eof"):
            with self.subTest(trigger=trigger):
                now = [0]
                relay = host.Relay(clock=lambda now=now: now[0])
                self.assertEqual(
                    relay.output_bytes(
                        proposal(A, b"now unsafe") + PREFIX + b"partial"
                    ),
                    b"",
                )
                self.assertIsNone(relay.take())
                with self.assertRaisesRegex(
                    host.RelayError, "lost the PTY frame boundary"
                ):
                    if trigger == "oversize":
                        relay.output_bytes(b"x" * 100_000 + b"\x07" + proposal(B, b"b"))
                    elif trigger == "timer":
                        now[0] += host.FRAME_TIMEOUT
                        relay.check()  # No incoming PTY bytes.
                    else:
                        relay.finish_output()
                for call in (
                    relay.take,
                    relay.finish_output,
                    lambda relay=relay: relay.output_bytes(b"\x07" + proposal(B, b"b")),
                ):
                    with self.assertRaises(host.RelayError):
                        call()

    def test_osc_string_terminator_is_only_a_discard_boundary(self):
        for split in (False, True):
            relay = host.Relay()
            first = begin(A, 1) + frame(A, b"data:YQ==", ending=b"\x1b\\")
            ending = frame(A, b"end") + b"normal"
            if split:
                self.assertEqual(relay.output_bytes(first[:-1]), b"")
                first = first[-1:]
            self.assertEqual(relay.output_bytes(first + ending), b"normal")
            self.assertIsNone(relay.take())

    def test_request_ids_cannot_be_recycled_when_retirement_budget_is_full(self):
        relay = host.Relay()
        with patch.object(host, "_MAX_IDS", 2):
            for request in (A, B):
                self.assertEqual(relay.output_bytes(proposal(request, b"allow")), b"")
                self.assertEqual(relay.take(), b"allow")
            for request in (A, C):
                self.assertEqual(relay.output_bytes(proposal(request, b"refuse")), b"")
                self.assertIsNone(relay.take())


class HostOpenerTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.spool = self.base / host._storage_name(os.geteuid())
        self.available = self.enterContext(
            patch.object(host, "_opener_available", return_value=True)
        )
        self.environment = {
            "HOME": "/home/test",
            "PATH": "/untrusted/bin",
            "XDG_CONFIG_HOME": "/host/desktop-config",
            "XDG_DATA_HOME": "/host/desktop-data",
            "XDG_CONFIG_DIRS": "/host/site-config:/etc/xdg",
            "XDG_DATA_DIRS": "/host/site-data:/usr/share",
            "WAYLAND_DISPLAY": "wayland-0",
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
        self.run_opener = self.enterContext(patch.object(host.subprocess, "run"))
        self.run_opener.return_value.returncode = 0
        self.canonical, _ = png.normalize(example_png())

    def test_fixed_opener_exact_approved_png_private_path_and_clean_environment(self):
        host.open_png(self.canonical)
        self.run_opener.assert_called_once()
        (command,), args = self.run_opener.call_args
        self.assertEqual(command[0], "/usr/bin/xdg-open")
        self.assertEqual(len(command), 2)
        path = Path(command[1])
        self.assertEqual(path.parent, self.spool)
        self.assertTrue(re.fullmatch(r"preview-[0-9a-f]{32}\.png", path.name))
        self.assertEqual(path.read_bytes(), self.canonical)
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(path.stat().st_uid, os.geteuid())
        self.assertEqual(
            args,
            {
                "env": {
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
                },
                "cwd": "/",
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "close_fds": True,
                "start_new_session": True,
                "timeout": host._OPEN_TIMEOUT,
                "check": False,
            },
        )

    def test_invalid_or_noncanonical_png_never_staged_or_launched(self):
        for candidate in (
            b"file:///home/synthetic/leak.png",
            b"",
            b"x" * (host.MAX_BYTES + 1),
            bytearray(self.canonical),
            self.canonical + b"trailing payload",
            example_png(metadata=True),
        ):
            with self.subTest(kind=type(candidate).__name__, size=len(candidate)):
                with self.assertRaisesRegex(host.PreviewError, "invalid canonical PNG"):
                    host.open_png(candidate)
                self.assertFalse(self.spool.exists())
        self.run_opener.assert_not_called()

    def test_preflight_never_creates_state_and_rejects_exposed_spool(self):
        project = self.base / "project"
        project.mkdir()
        self.assertTrue(host.ready(host_mounts=(project,)))
        self.assertFalse(host.ready(host_mounts=(self.base,)))
        self.assertFalse(host.ready(host_mounts=(self.spool,)))
        self.assertFalse(host.ready(host_mounts=(self.spool / "subdir",)))
        self.assertFalse(host.ready(host_mounts=("relative",)))
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_png(self.canonical, host_mounts=(self.base,))
        self.assertFalse(self.spool.exists())
        self.run_opener.assert_not_called()

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
                    host.open_png(self.canonical)
        self.base.chmod(0o755)
        self.assertFalse(host.ready())
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_png(self.canonical)
        self.assertFalse(self.spool.exists())
        self.run_opener.assert_not_called()

    def test_no_desktop_or_trusted_opener_never_stages(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(host.ready())
            with self.assertRaisesRegex(
                host.PreviewError, "requires a desktop session"
            ):
                host.open_png(self.canonical)
        self.available.return_value = False
        self.assertFalse(host.ready())
        with self.assertRaisesRegex(host.PreviewError, "opener is unavailable"):
            host.open_png(self.canonical)
        self.assertFalse(self.spool.exists())
        self.run_opener.assert_not_called()

    def test_later_approved_open_reaps_only_expired_previews_and_caps_live_files(self):
        with patch.object(host, "_MAX_FILES", 2):
            host.open_png(self.canonical)
            (old,) = self.spool.iterdir()
            host.open_png(self.canonical)
            self.assertEqual(len(list(self.spool.iterdir())), 2)
            with self.assertRaisesRegex(host.PreviewError, "limit reached"):
                host.open_png(self.canonical)
            self.assertEqual(self.run_opener.call_count, 2)
            past = time.time() - host._RETENTION_SECONDS - 60
            os.utime(old, (past, past))
            host.open_png(self.canonical)
            self.assertFalse(old.exists())
            self.assertEqual(len(list(self.spool.iterdir())), 2)
            self.assertEqual(self.run_opener.call_count, 3)

    def test_symlinked_or_nonprivate_spool_is_not_followed(self):
        target = self.base / "outside"
        target.mkdir()
        self.spool.symlink_to(target, target_is_directory=True)
        self.assertFalse(host.ready())
        with self.assertRaises(host.PreviewError):
            host.open_png(self.canonical)
        self.assertEqual(list(target.iterdir()), [])
        self.spool.unlink()
        self.spool.mkdir(mode=0o700)
        self.spool.chmod(0o755)
        self.assertFalse(host.ready())
        with self.assertRaisesRegex(host.PreviewError, "not trusted"):
            host.open_png(self.canonical)
        self.run_opener.assert_not_called()

    def test_cleanup_never_follows_or_deletes_a_symlink_or_hardlink(self):
        self.spool.mkdir(mode=0o700)
        target = self.base / "outside-file"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        past = time.time() - host._RETENTION_SECONDS - 60
        os.utime(target, (past, past))
        candidate = self.spool / ("preview-" + "1" * 32 + ".png")
        for link in (
            lambda: candidate.symlink_to(target),
            lambda: os.link(target, candidate),
        ):
            link()
            with self.assertRaisesRegex(host.PreviewError, "not trusted"):
                host.open_png(self.canonical)
            self.assertTrue(candidate.exists())
            self.assertEqual(target.read_bytes(), b"outside")
            candidate.unlink()
        self.run_opener.assert_not_called()

    def test_timeout_error_remains_private_and_never_unlinks_approved_file_early(self):
        self.run_opener.side_effect = subprocess.TimeoutExpired(
            "synthetic private command", 5, stderr=b"synthetic private stderr"
        )
        with self.assertRaisesRegex(
            host.PreviewError, "did not report completion"
        ) as error:
            host.open_png(self.canonical)
        self.assertNotIn("private", str(error.exception))
        (path,) = self.spool.iterdir()
        self.assertEqual(path.read_bytes(), self.canonical)

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
            host.open_png(self.canonical)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(list(self.spool.iterdir()), [])
        self.run_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
