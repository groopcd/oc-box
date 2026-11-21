"""Offline preview reader tests: synthetic files, no Podman or real data."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

from oc_box import file_preview_io as preview


class RequestTests(unittest.TestCase):
    def test_normalized_utf8_paths_and_exact_frame_limit(self):
        body = (
            b'{"path":"/tmp/monde \xc3\xa9.txt","roots":["/mnt/work","/home/demo"],'
            b'"identities":[["1","2"],["3","4"]]}'
        )
        expected = (
            b"/tmp/monde \xc3\xa9.txt",
            (b"/mnt/work", b"/home/demo"),
            {b"/mnt/work": (1, 2), b"/home/demo": (3, 4)},
        )
        self.assertEqual(preview._request(body), expected)
        frame = body.ljust(preview.MAX_REQUEST, b" ")
        self.assertEqual(preview._request(frame), expected)
        with self.assertRaises(preview.Refused):
            preview._request(frame + b" ")

    def test_path_refusals(self):
        bad = (
            "",
            "/",
            "//tmp/file",
            "/tmp//file",
            "/tmp/.",
            "/tmp/./file",
            "/tmp/..",
            "/tmp/../file",
            "/tmp/file/",
            "tmp/file",
            "file:///tmp/a",
            "/tmp/a\0",
            "/tmp/\t",
            "/tmp/\n",
            "/tmp/\r",
            "/tmp/a\x1b",
            "/tmp/a\x7f",
            "/tmp/\u009b",
            "/tmp/a\u202e",
            "/tmp/\ud800",
            "/proc",
            "/proc/self/mountinfo",
            "/sys/synthetic",
            "/dev/zero",
            "/run/synthetic",
            "/" + "\u03bb" * 2048,
            [],
            123,
        )
        for path in bad:
            with self.subTest(path=path), self.assertRaises(preview.Refused):
                preview._path(path)
        self.assertEqual(preview._path("/proc-synthetic/file"), b"/proc-synthetic/file")

    def test_wrong_key_shape_or_roots_are_rejected(self):
        frames = (
            b"[]",
            b'{"path":"/file","roots":[],"extra":1}',
            b'{"path":"/file","path":"/other","roots":[]}',
            b'{"path":"/file","roots":[],"roots":[]}',
            b'{"path":"/file","roots":["/mnt/project"]}',
            b'{"path":"/file","roots":["/a","/a"]}',
            b'{"path":"/file","roots":["/a","/b","/c"]}',
            b'{"path":"/file","roots":["/proc","/ok"]}',
            b'{"path":"/file","roots":["/ok","/sys"]}',
            b'{"path":"/file","roots":{"project":"/project","home":"/home"}}',
            b'{"path":"/file","roots":["/a","/b"]}',
            b'{"path":"/file","roots":["/a","/b"],"identities":[[1,"2"],["3","4"]]}',
        )
        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(preview.Refused):
                preview._request(frame)


def mounts(project_line=b"30 20 0:9 /from /home/demo/project rw - ext4 /dev/f rw\n"):
    return (
        b"10 1 0:1 / / rw - overlay overlay rw\n"
        b"20 10 0:9 /vol /home/demo rw - ext4 /dev/f rw\n" + project_line
    )


class MountPolicyTests(unittest.TestCase):
    def test_mount_paths_unescape_once_and_only_exact_entries_are_approved(self):
        content = mounts() + (
            rb"40 10 0:7 /src /else\040where/part\134040 rw - ext4 /dev/f rw" + b"\n"
        )
        parsed = preview._mounts(content)
        self.assertEqual(preview._exact_mount(parsed, b"/else where/part\\040")[0], 40)
        for path in (b"/home", b"/else where", b"/home/demo/project/child"):
            with self.subTest(path=path), self.assertRaises(preview.Refused):
                preview._exact_mount(parsed, path)

    def test_pins_known_nested_roots_by_parent_even_with_same_filesystem(self):
        roots = (b"/home/demo/project", b"/home/demo")
        entries = preview._mounts(mounts())
        root = (100, preview._exact_mount(entries, b"/"))
        with (
            patch.object(preview, "_pin_mount", side_effect=[200, 300]) as pin,
            patch.object(preview, "_mount_id", side_effect=[20, 30]),
            patch.object(preview, "_keep", side_effect=lambda stack, fd: fd),
        ):
            result = preview._pin_roots(None, root, entries, 50, roots, 7)
        self.assertEqual([result[key][0] for key in roots], [300, 200])
        self.assertEqual(
            pin.call_args_list, [call(100, b"home/demo", 7), call(200, b"project", 7)]
        )

    def test_mount_stacks_shadowed_roots_and_wrong_ancestors_refused(self):
        project = b"/home/demo/project"
        for content, project_id in (
            (mounts(b""), 30),
            (mounts() + b"31 30 0:9 / /home/demo/project ro - ext4 /dev/f ro\n", 31),
            (mounts(b"30 10 0:9 /from /home/demo/project rw - ext4 /dev/f rw\n"), 30),
            (mounts(), 31),
        ):
            with (
                self.subTest(content=content, project_id=project_id),
                patch.object(preview, "_pin_mount", side_effect=[200, 300]),
                patch.object(preview, "_mount_id", side_effect=[20, project_id]),
                patch.object(preview, "_keep", side_effect=lambda stack, fd: fd),
                self.assertRaises(preview.Refused),
            ):
                entries = preview._mounts(content)
                root = (100, preview._exact_mount(entries, b"/"))
                preview._pin_roots(None, root, entries, 50, (project, b"/home/demo"), 7)

    def test_only_final_mount_component_may_cross(self):
        fd = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        with patch.object(preview, "_openat2", side_effect=[fd, 101]) as opened:
            self.assertEqual(preview._pin_mount(100, b"a/b/c", 7), 101)
        self.assertEqual(
            opened.call_args_list, [call(100, b"a/b", 7), call(fd, b"c", 7, cross=True)]
        )
        with self.assertRaises(OSError):
            os.fstat(fd)


@unittest.skipUnless(sys.platform == "linux", "Linux preview reader")
class ReaderTests(unittest.TestCase):
    def setUp(self):
        # Determine this independently of the helper. A test runner with a
        # separately mounted /tmp must not teach the helper to cross that mount.
        def mount_id(path):
            fd = os.open(path, os.O_PATH)
            try:
                return next(
                    line
                    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
                    if line.startswith("mnt_id:")
                )
            finally:
                os.close(fd)

        root_mnt = mount_id("/")
        for parent in ("/tmp", "/var/tmp"):
            if os.path.isdir(parent) and mount_id(parent) == root_mnt:
                fixture = tempfile.TemporaryDirectory(
                    prefix="oc-box-preview-test-", dir=parent
                )
                self.addCleanup(fixture.cleanup)
                self.directory = Path(fixture.name)
                self.path = self.directory / "test.txt"
                return
        self.skipTest("no root-filesystem temporary directory for root-only fixture")

    def run_reader(self, *, path=None, roots=(), frame=None):
        if frame is None:
            frame = json.dumps(
                {"path": str(path or self.path), "roots": roots}
            ).encode()
        return subprocess.run(
            [sys.executable, "-I", "-S", "-B", preview.__file__],
            input=frame,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=6,
        )

    def assert_reply(self, result, content=None):
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (preview.REFUSAL if content is None else 0, content or b"", b""),
        )

    def test_stdio_valid_unframed_text_empty_and_exact_size_limit(self):
        for path, content in (
            (self.directory / "no-extension", "hello é 😀\t\r\n".encode()),
            (self.directory / "misleading.pdf", b"strictly text .\n"),
            (self.path, b""),
            (self.path, b"x" * (preview.MAX_CONTENT - 2) + "\u03bb".encode()),
        ):
            with self.subTest(path=path, size=len(content)):
                path.write_bytes(content)
                self.assert_reply(self.run_reader(path=path), content)

    def test_bad_requests_are_silent(self):
        for frame in (
            b"",
            b'{"path":"/tmp/a","roots":[]}{}',
            b'{"path":"/tmp/a","roots":[]} '.ljust(preview.MAX_REQUEST + 1, b" "),
            '{"path":"/tmp/a","roots":[]}'.encode("utf-16"),
            b'{"path":"/tmp/\xff","roots":[]}',
            b"[[" * 2000 + b"]]" * 2000,
        ):
            with self.subTest(size=len(frame), prefix=frame[:30]):
                self.assert_reply(self.run_reader(frame=frame))

    def test_unclosed_private_input_times_out_silently(self):
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", preview.__file__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            child.wait(timeout=preview.READ_DEADLINE + 4)
            stdout, stderr = child.communicate()
            self.assertEqual(
                (child.returncode, stdout, stderr), (preview.REFUSAL, b"", b"")
            )
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_invalid_tail_and_oversize_never_emit_a_prefix(self):
        for ending in (
            b"\x00",
            b"\x1b",
            b"\x07",
            b"\x7f",
            b"\x0b",
            b"\x0c",
            b"\xff",
            b"\xe2\x82",
            "\u009b".encode(),
            "\u202e".encode(),
            "\u200d".encode(),
            "\ufeff".encode(),
            b"0123456789",
        ):
            with self.subTest(ending=ending):
                self.path.write_bytes(b"X" * (preview.MAX_CONTENT - 4) + ending)
                self.assert_reply(self.run_reader())

    def test_refuses_symlinks_both_final_and_in_ancestors(self):
        self.path.write_bytes(b"synthetic target\n")
        linked_file = self.directory / "file-link"
        linked_file.symlink_to(self.path)
        linked_parent = self.directory / "dir-link"
        linked_parent.symlink_to(self.directory, target_is_directory=True)
        for path in (linked_file, linked_parent / self.path.name):
            self.assert_reply(self.run_reader(path=path))

    def test_refuses_nonmount_approved_root_paths(self):
        project = self.directory / "project"
        project.mkdir()
        self.path.write_bytes(b"ordinary root file")
        self.assert_reply(self.run_reader(roots=(str(project), str(self.directory))))

    def test_root_only_kernel_lookup_cannot_cross_the_proc_mount(self):
        with ExitStack() as stack:
            _, pinned, _ = preview._namespace(stack, ())
            root = pinned[b"/"][0]
            with self.assertRaises(preview.Refused):
                preview._openat2(root, b"proc", os.O_PATH)
            path = preview._keep(
                stack, preview._openat2(root, b"proc", os.O_PATH, cross=True)
            )
            self.assertTrue(preview._procfs(path))

    def test_special_and_hardlinked_files_refused_before_data_open(self):
        fifo = self.directory / "fifo"
        os.mkfifo(fifo)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sockpath = self.directory / "socket"
        sock.bind(str(sockpath))
        self.path.write_bytes(b"synthetic hardlinked file")
        link = self.directory / "hardlink"
        os.link(self.path, link)
        for path in (fifo, sockpath, self.path, link, self.directory):
            with self.subTest(path=path):
                self.assert_reply(self.run_reader(path=path))
                fd = os.open(path, os.O_PATH)
                try:
                    with (
                        patch.object(
                            preview.os, "open", side_effect=AssertionError("data open")
                        ),
                        self.assertRaises(preview.Refused),
                    ):
                        preview._content(None, fd, None)
                finally:
                    os.close(fd)

    def test_changed_or_replaced_files_refused_after_read(self):
        for operation in ("grow", "truncate", "rewrite"):
            with self.subTest(operation=operation):
                self.path.write_bytes(b"synthetic original")
                read = os.read

                def change_after_first_read(fd, count):
                    chunk = read(fd, count)
                    if chunk:
                        if operation == "grow":
                            with self.path.open("ab") as stream:
                                stream.write(b"append")
                        elif operation == "truncate":
                            self.path.write_bytes(b"x")
                        else:
                            self.path.write_bytes(b"synthetic changed!")
                    return chunk

                with patch.object(
                    preview.os, "read", side_effect=change_after_first_read
                ):
                    with self.assertRaises(preview.Refused):
                        preview._snapshot(os.fsencode(self.path), ())

        self.path.write_bytes(b"synthetic original")
        content = preview._content

        def replace_after_content(pid, target, mount):
            result = content(pid, target, mount)
            self.path.rename(self.directory / "old")
            self.path.write_bytes(b"synthetic replacement")
            return result

        with patch.object(preview, "_content", side_effect=replace_after_content):
            with self.assertRaises(preview.Refused):
                preview._snapshot(os.fsencode(self.path), ())


if __name__ == "__main__":
    unittest.main()
