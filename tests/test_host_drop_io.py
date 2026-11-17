import base64
import json
import os
from pathlib import Path
import pty
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from oc_box import host_drop_io


TOKEN = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-private-image-content"
JPEG = b"\xff\xd8\xffsynthetic-jpeg"
GIF = b"GIF89asynthetic-gif"
WEBP = b"RIFF\x00\x00\x00\x00WEBPsynthetic-webp"
PDF = b"%PDF-1.7\nsynthetic-private-pdf-content"
SOURCE = Path(host_drop_io.__file__).read_text()


def name(number=0, extension="png"):
    return "drop_" + format(number, "032x") + "." + extension


def wire(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def put(number=0, content=PNG, extension="png"):
    return {
        "op": "put",
        "name": name(number, extension),
        "data": base64.b64encode(content).decode("ascii"),
    }


class DropStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="host-drop-io-test-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.store = host_drop_io.DropStore(TOKEN, parent=self.parent)
        self.addCleanup(self.store.close)
        self.root = Path(self.store.root)

    def refuse(self, category, operation, *args):
        with self.assertRaises(host_drop_io.Refused) as raised:
            operation(*args)
        self.assertEqual(str(raised.exception), category)

    def test_exclusive_private_root_file_bytes_and_idempotent_cleanup(self):
        self.assertEqual(self.root, self.parent / ("oc-box-drop-" + TOKEN))
        info = self.root.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(self.store.put(name(), PNG), str(self.root / name()))
        image = self.root / name()
        self.assertEqual(image.read_bytes(), PNG)
        self.assertEqual(stat.S_IMODE(image.stat().st_mode), 0o600)
        self.assertEqual(image.stat().st_uid, os.geteuid())
        self.assertEqual(image.stat().st_nlink, 1)
        self.assertTrue(self.store.close())
        self.assertTrue(self.store.close())
        self.assertFalse(self.root.exists())
        self.refuse("root-unavailable", self.store.put, name(1), PNG)

    def test_existing_directory_and_symlink_are_not_adopted_or_removed(self):
        marker = self.root / "unrelated-marker"
        marker.write_bytes(b"retain")
        self.refuse(
            "root-unavailable",
            lambda: host_drop_io.DropStore(TOKEN, parent=self.parent),
        )
        self.assertEqual(marker.read_bytes(), b"retain")
        other_token = "f" * 32
        link = self.parent / ("oc-box-drop-" + other_token)
        link.symlink_to(self.root, target_is_directory=True)
        self.refuse(
            "root-unavailable",
            lambda: host_drop_io.DropStore(other_token, parent=self.parent),
        )
        self.assertTrue(link.is_symlink())
        self.assertEqual(marker.read_bytes(), b"retain")

    def test_parent_symlink_is_not_followed(self):
        link = self.parent / "linked-parent"
        link.symlink_to(self.parent, target_is_directory=True)
        self.refuse(
            "root-unavailable",
            lambda: host_drop_io.DropStore("e" * 32, parent=link),
        )
        self.assertFalse((self.parent / ("oc-box-drop-" + "e" * 32)).exists())

    def test_generated_names_and_signature_extension_matrix(self):
        created = set()
        for index, (extension, content) in enumerate(
            (("png", PNG), ("jpeg", JPEG), ("gif", GIF), ("webp", WEBP), ("pdf", PDF))
        ):
            with self.subTest(extension=extension):
                self.store.put(name(index, extension), content)
                self.assertEqual(
                    (self.root / name(index, extension)).read_bytes(), content
                )
                created.add(name(index, extension))
        for index, (extension, content) in enumerate(
            (
                ("png", JPEG),
                ("jpeg", PNG),
                ("gif", WEBP),
                ("webp", GIF),
                ("pdf", PNG),
                ("png", PDF),
            ),
            10,
        ):
            with self.subTest(mismatch=extension):
                self.refuse(
                    "invalid-request", self.store.put, name(index, extension), content
                )
        for invalid in (
            "",
            "../" + name(),
            "/" + name(),
            "sub/" + name(),
            "drop_" + "A" * 32 + ".png",
            "drop_" + "0" * 31 + ".png",
            name() + "\n",
            name() + "\0",
            name(extension="jpg"),
            name(extension="svg"),
            name(extension="docx"),
            name(extension="PNG"),
            None,
            1,
        ):
            with self.subTest(name=repr(invalid)):
                self.refuse("invalid-request", self.store.put, invalid, PNG)
        for content in (
            b"",
            b"not-an-image",
            b"RIFFWEBP",
            b"RIFF0000NOTP",
            b"\x89PNG",
            b"%PD",
            b"%pdf-1.7",
            b"PK\x03\x04synthetic-not-supported-docx",
            bytearray(PNG),
            None,
            PNG.decode("latin1"),
        ):
            with self.subTest(content_type=type(content).__name__):
                self.refuse("invalid-request", self.store.put, name(20), content)
        self.assertEqual(
            {entry.name for entry in self.root.iterdir()},
            created,
        )

    def test_invalid_token_never_creates_a_directory(self):
        for token in (
            "",
            "0" * 31,
            "0" * 33,
            "A" * 32,
            "../" + TOKEN,
            TOKEN + "\n",
            1,
            None,
        ):
            with self.subTest(token=repr(token)):
                self.refuse(
                    "invalid-request",
                    lambda: host_drop_io.DropStore(token, parent=self.parent),
                )
        self.assertEqual(list(self.parent.iterdir()), [self.root])

    def test_existing_regular_symlink_fifo_and_directory_are_not_overwritten(self):
        outside = self.parent / "outside-image"
        outside.write_bytes(b"retain-original")
        entries = (
            (name(0), lambda path: path.write_bytes(b"existing")),
            (name(1), lambda path: path.symlink_to(outside)),
            (name(2), lambda path: os.mkfifo(path, 0o600)),
            (name(3), lambda path: path.mkdir()),
        )
        for filename, create in entries:
            with self.subTest(filename=filename):
                target = self.root / filename
                create(target)
                before = target.lstat()
                self.refuse("file-exists", self.store.put, filename, PNG)
                after = target.lstat()
                self.assertEqual(
                    (before.st_ino, before.st_mode), (after.st_ino, after.st_mode)
                )
        self.assertEqual(outside.read_bytes(), b"retain-original")
        self.assertFalse(self.store.close())
        self.assertEqual(len(list(self.root.iterdir())), 4)

    def test_deleted_files_do_not_release_names_or_lifetime_file_quota(self):
        for index in range(host_drop_io.MAX_FILES):
            self.store.put(name(index), PNG)
            (self.root / name(index)).unlink()
        self.refuse("file-exists", self.store.put, name(), PNG)
        self.refuse("quota-exceeded", self.store.put, name(host_drop_io.MAX_FILES), PNG)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.store.close())

    def test_per_file_and_lifetime_byte_limits_are_non_reusable(self):
        maximum = PNG + b"\0" * (host_drop_io.MAX_FILE - len(PNG))
        self.refuse("invalid-request", self.store.put, name(), maximum + b"\0")
        for index in range(host_drop_io.MAX_TOTAL // host_drop_io.MAX_FILE):
            self.store.put(name(index), maximum)
            (self.root / name(index)).unlink()
        self.refuse("quota-exceeded", self.store.put, name(20), PNG)
        self.assertTrue(self.store.close())

    def test_partial_write_reserves_quota_and_cleanup_knows_the_created_file(self):
        write = host_drop_io.os.write
        calls = 0

        def partial(fd, content):
            nonlocal calls
            calls += 1
            if calls == 1:
                return write(fd, content[:4])
            raise OSError("synthetic-private-error /private/image token=redacted")

        with patch.object(host_drop_io, "MAX_TOTAL", len(PNG)):
            with patch.object(host_drop_io.os, "write", side_effect=partial):
                self.refuse("io-failure", self.store.put, name(), PNG)
            self.refuse("quota-exceeded", self.store.put, name(1), PNG)
        self.assertEqual((self.root / name()).read_bytes(), PNG[:4])
        self.refuse("file-exists", self.store.put, name(), PNG)
        self.assertTrue(self.store.close())

    def test_short_writes_complete_without_repeating_or_truncating_data(self):
        write = host_drop_io.os.write
        with patch.object(
            host_drop_io.os,
            "write",
            side_effect=lambda fd, content: write(fd, content[:3]),
        ):
            self.store.put(name(), PNG)
        self.assertEqual((self.root / name()).read_bytes(), PNG)

    def test_root_rename_and_substitute_do_not_redirect_writes_or_cleanup(self):
        self.store.put(name(), PNG)
        retained = self.parent / "retained-root"
        self.root.rename(retained)
        self.root.mkdir(mode=0o700)
        marker = self.root / "unrelated-marker"
        marker.write_bytes(b"retain")
        self.refuse("root-changed", self.store.put, name(1), PNG)
        self.assertFalse(self.store.close())
        self.assertEqual((retained / name()).read_bytes(), PNG)
        self.assertEqual(marker.read_bytes(), b"retain")
        self.assertFalse((retained / name(1)).exists())
        self.assertFalse((self.root / name(1)).exists())

    def test_root_symlink_substitute_is_neither_followed_nor_removed(self):
        self.store.put(name(), PNG)
        retained = self.parent / "retained-root"
        self.root.rename(retained)
        self.root.symlink_to(retained, target_is_directory=True)
        self.refuse("root-changed", self.store.put, name(1), PNG)
        self.assertFalse(self.store.close())
        self.assertTrue(self.root.is_symlink())
        self.assertEqual((retained / name()).read_bytes(), PNG)

    def test_root_mode_change_refuses_mutation_including_cleanup(self):
        self.store.put(name(), PNG)
        self.root.chmod(0o750)
        self.refuse("root-changed", self.store.put, name(1), PNG)
        self.assertFalse(self.store.close())
        self.assertEqual((self.root / name()).read_bytes(), PNG)
        self.assertFalse((self.root / name(1)).exists())

    @unittest.skipUnless(
        os.geteuid() == 0, "changing a file owner requires container root"
    )
    def test_root_owner_change_refuses_mutation_including_cleanup(self):
        self.store.put(name(), PNG)
        os.chown(self.root, 1, -1)
        try:
            self.refuse("root-changed", self.store.put, name(1), PNG)
            self.assertFalse(self.store.close())
            self.assertEqual((self.root / name()).read_bytes(), PNG)
        finally:
            os.chown(self.root, 0, -1)

    @unittest.skipUnless(os.geteuid() == 0, "changing a group requires container root")
    def test_root_group_change_refuses_mutation_including_cleanup(self):
        self.store.put(name(), PNG)
        original_group = self.root.stat().st_gid
        os.chown(self.root, -1, 1 if original_group != 1 else 0)
        try:
            self.refuse("root-changed", self.store.put, name(1), PNG)
            self.assertFalse(self.store.close())
            self.assertEqual((self.root / name()).read_bytes(), PNG)
        finally:
            os.chown(self.root, -1, original_group)

    def test_parent_replacement_refuses_canonical_path_and_cleanup(self):
        self.store.put(name(), PNG)
        retained = self.parent.with_name(self.parent.name + "-retained")
        self.parent.rename(retained)
        self.addCleanup(retained.rmdir)
        moved_root = retained / self.root.name
        self.addCleanup(moved_root.rmdir)
        self.addCleanup((moved_root / name()).unlink)
        self.parent.mkdir(mode=0o700)
        self.refuse("root-changed", self.store.put, name(1), PNG)
        self.assertFalse(self.store.close())
        self.assertEqual((moved_root / name()).read_bytes(), PNG)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_cleanup_keeps_replaced_files_unknown_children_and_other_roots(self):
        self.store.put(name(), PNG)
        self.store.put(name(1), PNG)
        outside = self.parent / "outside"
        outside.write_bytes(b"retain-outside")
        (self.root / name()).unlink()
        (self.root / name()).symlink_to(outside)
        nested = self.root / "unrelated-dir"
        nested.mkdir()
        (nested / "child").write_bytes(b"retain-child")
        other = self.parent / ("oc-box-drop-" + "e" * 32)
        other.mkdir(mode=0o700)
        (other / name()).write_bytes(b"retain-other-owner")
        self.assertFalse(self.store.close())
        self.assertTrue((self.root / name()).is_symlink())
        self.assertFalse((self.root / name(1)).exists())
        self.assertEqual(outside.read_bytes(), b"retain-outside")
        self.assertEqual((nested / "child").read_bytes(), b"retain-child")
        self.assertEqual((other / name()).read_bytes(), b"retain-other-owner")

    def test_cleanup_does_not_unlink_a_changed_recorded_file(self):
        self.store.put(name(), PNG)
        (self.root / name()).chmod(0o640)
        self.assertFalse(self.store.close())
        self.assertEqual((self.root / name()).read_bytes(), PNG)


class DropProtocolTests(unittest.TestCase):
    def reject(self, raw):
        with self.assertRaises(host_drop_io.Refused) as raised:
            host_drop_io._request(raw)
        self.assertEqual(str(raised.exception), "invalid-request")

    def test_only_exact_put_and_close_schemas_are_accepted(self):
        self.assertEqual(host_drop_io._request(wire(put())), ("put", name(), PNG))
        self.assertEqual(
            host_drop_io._request(wire({"op": "close"})), ("close", None, None)
        )
        for value in (
            None,
            [],
            {},
            {"op": "unknown"},
            {"op": "close", "name": name()},
            {**put(), "path": "/private/host-image.png"},
            {**put(), "op": True},
            {**put(), "name": True},
            {**put(), "data": True},
            {**put(), "data": ""},
            {**put(), "data": "not base64\n"},
            {**put(), "data": "é"},
            {**put(), "data": "AA==\n"},
            {**put(), "name": "../" + name()},
        ):
            with self.subTest(kind=type(value).__name__):
                self.reject(wire(value))
        for key in ("op", "name", "data"):
            value = put()
            del value[key]
            self.reject(wire(value))

    def test_duplicate_keys_non_json_encoding_and_nonfinite_constants_refuse(self):
        for raw in (
            b"",
            b"\n",
            b"\xff\n",
            b"{",
            '{"op":"close"}'.encode("utf-16"),
            b'{"op":"close","op":"close"}\n',
            b'{"op":"close","nested":{"key":1,"key":2}}\n',
            b'{"op":NaN}\n',
            b'{"op":Infinity}\n',
            b'{"op":-Infinity}\n',
            b'{"op":"close"}\n{"op":"close"}\n',
        ):
            with self.subTest(raw=repr(raw)):
                self.reject(raw)

    def test_decoded_and_encoded_limits_are_checked_before_staging(self):
        maximum = PNG + b"\0" * (host_drop_io.MAX_FILE - len(PNG))
        self.assertEqual(host_drop_io._request(wire(put(content=maximum)))[2], maximum)
        self.reject(wire(put(content=maximum + b"\0")))
        self.reject(wire({**put(), "data": "A" * (host_drop_io.MAX_ENCODED + 1)}))

    def test_line_byte_limit_includes_whitespace_and_newline(self):
        close = b'{"op":"close"}'
        exactly = close + b" " * (host_drop_io.MAX_LINE - len(close) - 1) + b"\n"
        self.assertEqual(host_drop_io._request(exactly), ("close", None, None))
        self.reject(exactly + b" ")


class DropProcessTests(unittest.TestCase):
    def start(self, token=None):
        token = uuid.uuid4().hex if token is None else token
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", SOURCE, token],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.addCleanup(self.finish, process)
        return process, Path("/tmp/oc-box-drop-" + token)

    def finish(self, process):
        if process.poll() is None:
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def line(self, process):
        raw = bytearray()
        deadline = time.monotonic() + 10
        while len(raw) < 4096 and not raw.endswith(b"\n"):
            timeout = max(0, deadline - time.monotonic())
            self.assertGreater(timeout, 0, "helper did not finish its acknowledgement")
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            self.assertTrue(
                ready, "helper did not acknowledge a bounded protocol operation"
            )
            piece = os.read(process.stdout.fileno(), 1)
            if not piece:
                break
            raw.extend(piece)
        self.assertTrue(
            raw.endswith(b"\n"), "helper did not return one bounded JSON line"
        )
        return json.loads(raw)

    def startup(self):
        process, root = self.start()
        self.assertEqual(self.line(process), {"ok": True, "root": str(root)})
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        return process, root

    def send(self, process, value):
        self.send_bounded(process, wire(value))

    def send_bounded(self, process, content):
        fd = process.stdin.fileno()
        deadline = time.monotonic() + 10
        remaining = memoryview(content)
        os.set_blocking(fd, False)
        try:
            while remaining:
                timeout = max(0, deadline - time.monotonic())
                self.assertGreater(timeout, 0, "helper did not finish consuming stdin")
                _, writable, _ = select.select([], [fd], [], timeout)
                self.assertTrue(writable, "helper did not consume bounded stdin")
                try:
                    written = os.write(fd, remaining)
                except BlockingIOError:
                    continue
                self.assertGreater(written, 0)
                remaining = remaining[written:]
        finally:
            os.set_blocking(fd, True)

    def exited(self, process, expected):
        process.stdin.close()
        process.stdin = None
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, expected)
        self.assertEqual(error, b"")
        self.assertEqual(output, b"")

    def test_actual_isolated_source_process_stages_on_pipes_and_eof_cleans(self):
        process, root = self.startup()
        self.assertFalse(os.isatty(process.stdin.fileno()))
        self.assertFalse(os.isatty(process.stdout.fileno()))
        self.send(process, put())
        self.assertEqual(self.line(process), {"ok": True, "path": str(root / name())})
        self.assertEqual((root / name()).read_bytes(), PNG)
        self.assertEqual(stat.S_IMODE((root / name()).stat().st_mode), 0o600)
        self.send(process, put(number=1, content=JPEG, extension="jpeg"))
        self.assertEqual(
            self.line(process), {"ok": True, "path": str(root / name(1, "jpeg"))}
        )
        self.assertEqual((root / name(1, "jpeg")).read_bytes(), JPEG)
        self.send(process, put(number=2, content=PDF, extension="pdf"))
        self.assertEqual(
            self.line(process), {"ok": True, "path": str(root / name(2, "pdf"))}
        )
        self.assertEqual((root / name(2, "pdf")).read_bytes(), PDF)
        if Path("/proc").exists():
            arguments = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")
            self.assertEqual(arguments[1:5], [b"-I", b"-S", b"-B", b"-c"])
            for content in (PNG, JPEG, PDF):
                self.assertNotIn(base64.b64encode(content), b"\0".join(arguments))
                self.assertNotIn(content, b"\0".join(arguments))
            self.assertNotIn(b"/private/host-image.png", b"\0".join(arguments))
        process.stdin.close()
        process.stdin = None
        self.assertEqual(self.line(process), {"ok": True, "closed": True})
        output, error = process.communicate(timeout=10)
        self.assertEqual((process.returncode, output, error), (0, b"", b""))
        self.assertFalse(root.exists())

    def test_explicit_close_does_not_wait_for_pipe_eof_or_consume_later_requests(self):
        process, root = self.startup()
        self.send(process, put())
        self.assertEqual(self.line(process), {"ok": True, "path": str(root / name())})
        self.send(process, {"op": "close"})
        self.assertEqual(self.line(process), {"ok": True, "closed": True})
        process.wait(timeout=10)
        self.assertFalse(root.exists())
        self.exited(process, 0)

    def test_malformed_control_has_only_fixed_error_then_one_cleanup_ack(self):
        process, root = self.startup()
        self.send(process, put())
        self.line(process)
        marker = "/synthetic/private/image token=not-a-secret"
        self.send(process, {"op": "put", "path": marker, "data": marker})
        self.assertEqual(
            self.line(process), {"ok": False, "category": "invalid-request"}
        )
        self.assertEqual(self.line(process), {"ok": True, "closed": True})
        self.exited(process, 1)
        self.assertFalse(root.exists())

    def test_oversized_unterminated_stdin_is_refused_without_waiting_for_eof(self):
        process, root = self.startup()
        self.send_bounded(process, b" " * (host_drop_io.MAX_LINE + 1))
        self.assertEqual(
            self.line(process), {"ok": False, "category": "invalid-request"}
        )
        self.assertEqual(self.line(process), {"ok": True, "closed": True})
        process.wait(timeout=10)
        self.exited(process, 1)
        self.assertFalse(root.exists())

    def test_invalid_image_signature_cannot_create_a_file(self):
        process, root = self.startup()
        self.send(process, put(content=b"synthetic-not-an-image"))
        self.assertEqual(
            self.line(process), {"ok": False, "category": "invalid-request"}
        )
        self.assertEqual(self.line(process), {"ok": True, "closed": True})
        self.exited(process, 1)
        self.assertFalse(root.exists())

    def test_root_mode_change_reports_unfinished_cleanup_not_success(self):
        process, root = self.startup()
        self.send(process, put())
        self.line(process)
        root.chmod(0o750)
        self.addCleanup(root.rmdir)
        self.addCleanup((root / name()).unlink)
        self.send(process, put(number=1))
        self.assertEqual(self.line(process), {"ok": False, "category": "root-changed"})
        self.assertEqual(self.line(process), {"ok": True, "closed": False})
        self.exited(process, 1)
        self.assertEqual((root / name()).read_bytes(), PNG)
        self.assertFalse((root / name(1)).exists())

    def test_startup_rejects_bad_token_and_extra_authority_without_traceback(self):
        cases = (
            (),
            ("../synthetic-private-token",),
            ("A" * 32,),
            (TOKEN + "\n",),
            (TOKEN, "/synthetic/private/host-image.png"),
        )
        for arguments in cases:
            with self.subTest(argument_count=len(arguments)):
                # Exercise the literal argv, including the missing-token case.
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", "-c", SOURCE, *arguments],
                    input=b"",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(
                    result.stdout, wire({"ok": False, "category": "invalid-request"})
                )

    def test_interactive_terminal_input_refuses_before_creating_a_root(self):
        token = uuid.uuid4().hex
        root = Path("/tmp/oc-box-drop-" + token)
        master, slave = pty.openpty()
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", SOURCE, token],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(
            result.stdout, wire({"ok": False, "category": "invalid-request"})
        )
        self.assertFalse(root.exists())

    def test_native_filesystem_errors_never_print_paths_or_exception_details(self):
        token = uuid.uuid4().hex
        root = Path("/tmp/oc-box-drop-" + token)
        root.mkdir(mode=0o700)
        self.addCleanup(root.rmdir)
        marker = root / "synthetic-private-error-token"
        marker.write_bytes(b"retain")
        self.addCleanup(marker.unlink)
        process, _ = self.start(token)
        self.assertEqual(
            self.line(process), {"ok": False, "category": "root-unavailable"}
        )
        self.exited(process, 1)
        self.assertEqual(marker.read_bytes(), b"retain")


if __name__ == "__main__":
    unittest.main()
