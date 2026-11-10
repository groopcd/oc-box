import contextlib
import io
import os
from pathlib import Path
import subprocess
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import backup_archive

APP = Path(__file__).resolve().parents[1]


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, kind, value in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.mode = 0o6755
            if kind == tarfile.REGTYPE:
                data = value.encode()
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
            else:
                entry.linkname = value
                output.addfile(entry)
    stream.seek(0)
    return stream


class BackupArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-archive-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "destination"
        self.destination.mkdir(mode=0o700)

    def test_real_create_restore_git_untracked_symlink_and_hardlink(self):
        source = self.root / "project"
        (source / ".git").mkdir(parents=True)
        (source / ".git/HEAD").write_text("ref: refs/heads/master\n")
        (source / "unfinished").write_text("unfinished edits")
        os.link(source / "unfinished", source / "hardlink")
        (source / "external-link").symlink_to("/unavailable/external")
        result = subprocess.run(
            [
                sys.executable,
                str(APP / "src/oc_box/backup_archive.py"),
                "create",
                str(source),
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        backup_archive.extract(
            self.destination, 1024**2, stream=io.BytesIO(result.stdout)
        )
        self.assertEqual(
            (self.destination / ".git/HEAD").read_text(), "ref: refs/heads/master\n"
        )
        self.assertEqual(
            (self.destination / "unfinished").read_text(), "unfinished edits"
        )
        self.assertEqual(
            (self.destination / "unfinished").stat().st_ino,
            (self.destination / "hardlink").stat().st_ino,
        )
        self.assertEqual(
            os.readlink(self.destination / "external-link"), "/unavailable/external"
        )

    def test_traversal_absolute_special_and_duplicate_entries_are_rejected(self):
        cases = (
            [("../escape", tarfile.REGTYPE, "bad")],
            [("/escape", tarfile.REGTYPE, "bad")],
            [("file", tarfile.REGTYPE, "one"), ("file", tarfile.REGTYPE, "two")],
            [("device", tarfile.CHRTYPE, "")],
            [("fifo", tarfile.FIFOTYPE, "")],
            [("hard", tarfile.LNKTYPE, "../escape")],
            [("hard", tarfile.LNKTYPE, "missing")],
            [("path\\escape", tarfile.REGTYPE, "bad")],
        )
        for entries in cases:
            with (
                self.subTest(entries=entries),
                tempfile.TemporaryDirectory(dir=self.root) as destination,
            ):
                with self.assertRaises(ValueError):
                    backup_archive.extract(
                        Path(destination), 1024**2, stream=archive(entries)
                    )
        self.assertFalse((self.root / "escape").exists())

    def test_symlink_ancestor_cannot_write_outside_destination(self):
        outside = self.root / "outside"
        outside.mkdir()
        data = archive(
            [
                ("link", tarfile.SYMTYPE, str(outside)),
                ("link/escape", tarfile.REGTYPE, "bad"),
            ]
        )
        with self.assertRaisesRegex(ValueError, "symlink"):
            backup_archive.extract(self.destination, 1024**2, stream=data)
        self.assertFalse((outside / "escape").exists())

    def test_expanded_size_limit_and_nonempty_destination_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "bound"):
            backup_archive.extract(
                self.destination,
                1,
                stream=archive([("large", tarfile.REGTYPE, "too large")]),
            )
        (self.destination / "existing").write_text("keep")
        with self.assertRaisesRegex(ValueError, "empty real"):
            backup_archive.extract(
                self.destination,
                100,
                stream=archive([("file", tarfile.REGTYPE, "value")]),
            )
        self.assertEqual((self.destination / "existing").read_text(), "keep")

    def test_setuid_and_setgid_bits_are_removed_and_plain_project_owner_is_current_user(
        self,
    ):
        backup_archive.extract(
            self.destination, 100, stream=archive([("file", tarfile.REGTYPE, "value")])
        )
        metadata = (self.destination / "file").stat()
        self.assertEqual(metadata.st_mode & 0o7777, 0o755)
        self.assertEqual(metadata.st_uid, os.getuid())

    def test_special_files_in_source_fail_creation_instead_of_silent_omission(self):
        source = self.root / "special"
        source.mkdir()
        os.mkfifo(source / "fifo")
        result = subprocess.run(
            [
                sys.executable,
                str(APP / "src/oc_box/backup_archive.py"),
                "create",
                str(source),
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_root_directory_is_kept_private_even_with_archived_public_permissions(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as output:
            root = tarfile.TarInfo(".")
            root.type, root.mode = tarfile.DIRTYPE, 0o777
            output.addfile(root)
        data.seek(0)
        backup_archive.extract(self.destination, 100, stream=data)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)

    def test_oversized_pax_or_longname_headers_are_rejected_before_reading_payload(
        self,
    ):
        for kind in (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME):
            with self.subTest(kind=kind):
                entry = tarfile.TarInfo("oversized-metadata")
                entry.type, entry.size = kind, 2 * 1024 * 1024
                data = io.BytesIO(entry.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024)
                with self.assertRaisesRegex(ValueError, "Oversized"):
                    backup_archive.extract(self.destination, 10 * 1024**2, stream=data)

    def test_mountinfo_rejects_nested_same_filesystem_bind_without_scanning_it(self):
        source = self.root / "nested-project"
        source.mkdir()
        nested = source / "mount"
        nested.mkdir()
        # It is specifically the mount table, not a changed st_dev, that detects
        # a bind of the same host filesystem nested under the project.
        value = f"1 0 0:1 / {source} rw - ext4 /dev/example rw\n2 1 0:1 /other {nested} rw - ext4 /dev/example rw\n"
        with patch.object(Path, "read_text", return_value=value):
            with self.assertRaisesRegex(ValueError, "Nested mounts"):
                backup_archive.create(source)

    def test_hardlink_cannot_name_an_archived_symlink_or_symlink_ancestor(self):
        for target in ("link", "link/file"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory(dir=self.root) as destination,
            ):
                data = archive(
                    [
                        ("link", tarfile.SYMTYPE, "/tmp/outside"),
                        ("hard", tarfile.LNKTYPE, target),
                    ]
                )
                with self.assertRaisesRegex(ValueError, "prior regular"):
                    backup_archive.extract(Path(destination), 100, stream=data)

    def test_extraction_syncs_final_metadata_hardlinks_and_implicit_directories(self):
        data = archive(
            [
                ("implicit/nested/file", tarfile.REGTYPE, "value"),
                ("hard", tarfile.LNKTYPE, "implicit/nested/file"),
                ("link", tarfile.SYMTYPE, "/unavailable/outside"),
            ]
        )
        synced = []
        original = os.fsync

        def sync(descriptor):
            info = os.fstat(descriptor)
            synced.append((Path(os.readlink(f"/proc/self/fd/{descriptor}")), info))
            original(descriptor)

        with patch.object(backup_archive.os, "fsync", side_effect=sync):
            backup_archive.extract(self.destination, 100, stream=data)
        paths = [path for path, _ in synced]
        self.assertIn(self.destination / "implicit/nested/file", paths)
        file_info = next(
            info
            for path, info in synced
            if path == self.destination / "implicit/nested/file"
        )
        self.assertEqual(file_info.st_nlink, 2)
        self.assertEqual(file_info.st_mode & 0o7777, 0o755)
        for directory in (
            self.destination / "implicit/nested",
            self.destination / "implicit",
            self.destination,
        ):
            self.assertIn(directory, paths)
        self.assertLess(
            paths.index(self.destination / "implicit/nested"),
            paths.index(self.destination / "implicit"),
        )
        self.assertLess(
            paths.index(self.destination / "implicit"), paths.index(self.destination)
        )
        self.assertNotIn(Path("/unavailable/outside"), paths)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)

    def test_extraction_file_and_directory_barrier_failures_are_fixed_refusals(self):
        for kind in ("file", "directory"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory(dir=self.root) as destination,
            ):
                original = os.fsync

                def sync(descriptor):
                    is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    if is_directory == (kind == "directory"):
                        raise OSError("PRIVATE_SYNC_ERROR")
                    original(descriptor)

                with (
                    patch.object(backup_archive.os, "fsync", side_effect=sync),
                    self.assertRaisesRegex(Exception, "durability") as error,
                ):
                    backup_archive.extract(
                        Path(destination),
                        100,
                        stream=archive([("file", tarfile.REGTYPE, "value")]),
                    )
                self.assertNotIn("PRIVATE", str(error.exception))

    def test_extract_cli_marks_barrier_failure_without_private_error_text(self):
        status = self.root / "extract.status"
        status.write_text("pending\n")
        status.chmod(0o600)
        errors = io.StringIO()
        with (
            patch.object(
                backup_archive.os, "fsync", side_effect=OSError("PRIVATE_SYNC_ERROR")
            ),
            patch.object(
                backup_archive.sys,
                "stdin",
                SimpleNamespace(buffer=archive([("file", tarfile.REGTYPE, "value")])),
            ),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(
                backup_archive.main(
                    [
                        "extract",
                        str(self.destination),
                        "--limit",
                        "100",
                        "--durability-status",
                        str(status),
                    ]
                ),
                1,
            )
        self.assertEqual(status.read_text(), "failed\n")
        self.assertNotIn("PRIVATE", errors.getvalue())

    def test_restrictive_metadata_syncs_in_an_unprivileged_child(self):
        uid, gid = (65534, 65534) if os.geteuid() == 0 else (os.getuid(), os.getgid())
        if os.geteuid() == 0:
            os.chown(self.destination, uid, gid)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as output:
            entry = tarfile.TarInfo("locked")
            entry.type, entry.mode = tarfile.DIRTYPE, 0
            output.addfile(entry)
            entry = tarfile.TarInfo("locked/file")
            entry.mode, entry.size = 0, 5
            output.addfile(entry, io.BytesIO(b"value"))
        code = (
            "import os,sys; from pathlib import Path; from oc_box import backup_archive as a; "
            "os.chdir(sys.argv[1]); uid,gid=map(int,sys.argv[2:4]); "
            "os.setgroups([]) if os.geteuid()==0 else None; "
            "os.setgid(gid) if os.geteuid()==0 else None; "
            "os.setuid(uid) if os.geteuid()==0 else None; assert os.geteuid()!=0; "
            "a.extract(Path('.'),100); assert Path('locked').stat().st_mode & 0o777==0; "
            "Path('locked').chmod(0o700); assert Path('locked/file').stat().st_mode & 0o777==0; "
            "Path('locked/file').chmod(0o600); assert Path('locked/file').read_bytes()==b'value'"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(self.destination),
                str(uid),
                str(gid),
            ],
            input=stream.getvalue(),
            capture_output=True,
            timeout=10,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(APP / "src")},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
