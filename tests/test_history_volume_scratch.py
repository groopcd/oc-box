"""Local descriptor tests; synthetic mount evidence is not Podman acceptance."""

import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import history_reader, history_volume_scratch as scratch


class ScratchTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.home = Path(private.name) / "volume"
        self.home.mkdir(mode=0o700)
        self.data = self.home / ".local/share/opencode"
        self.data.mkdir(parents=True)
        self.database = self.data / "oc-box-v2.db"
        self.database.write_bytes(b"synthetic database sentinel")
        fd = os.open(self.home, os.O_PATH | os.O_DIRECTORY)
        try:
            info = os.fstat(fd)
            mount = next(
                line.split()[1]
                for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
                if line.startswith("mnt_id:")
            )
        finally:
            os.close(fd)
        namespace = Path("/proc/self/ns/mnt").stat()
        self.record = {
            "scope": scratch.SCOPE,
            "version": "2.4.1",
            "home": str(self.home),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mount_id": int(mount),
            "namespace": [namespace.st_dev, namespace.st_ino],
        }

    def synthetic_mount(self):
        # A local test cannot add a volume mount. Only substitute the exact
        # mount row qualification; real procfs, no-follow lookups, stat, mount
        # IDs, identity rechecks and dir-fd cleanup still execute.
        return patch.object(scratch, "_verified_mount")

    def artifacts(self):
        return tuple(self.home.glob(".oc-box-quiet-history-*"))

    def test_real_unmounted_directory_does_not_count_as_volume_evidence(self):
        with self.assertRaisesRegex(scratch.ScratchRefusal, "volume-mount-unverified"):
            with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                self.fail("ordinary directory is not an admitted home mount")
        self.assertEqual(self.artifacts(), ())

    def test_canonical_paths_are_usable_by_child_and_leave_real_data_unchanged(self):
        retired = False

        def verify_retired():
            return retired

        with (
            self.synthetic_mount(),
            scratch.volume_scratch(self.record, verify_retired=verify_retired) as scope,
        ):
            root, selectors, version_data = scope.locations()
            self.assertEqual(root.parent, self.home)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(selectors["XDG_DATA_HOME"], str(self.data.parent))
            self.assertEqual(selectors["OPENCODE_DB"], self.database.name)
            self.assertEqual(Path(version_data).parent, root)
            self.assertEqual(selectors["HOME"], selectors["OPENCODE_TEST_HOME"])
            self.assertEqual(Path(selectors["TMPDIR"]), root / "tmp")
            self.assertFalse(any("/proc/" in value for value in selectors.values()))
            env = history_reader.fence_environment(selectors, version="2.4.1")
            child = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    "from pathlib import Path\n"
                    "import os\n"
                    "assert Path.cwd() == Path(os.environ['TMPDIR']).with_name('work')\n"
                    "assert 'OPENAI_API_KEY' not in os.environ\n"
                    "root = Path(os.environ['XDG_RUNTIME_DIR'])\n"
                    "(root / 'nested').mkdir(mode=0o700)\n"
                    "(root / 'nested' / 'log').write_text('synthetic')\n",
                ],
                cwd=root / "work",
                env=env,
                timeout=5,
                check=True,
                capture_output=True,
            )
            retired = child.returncode == 0
            scope.recheck()
        self.assertEqual(self.artifacts(), ())
        self.assertEqual(self.database.read_bytes(), b"synthetic database sentinel")
        with self.assertRaisesRegex(scratch.ScratchRefusal, "binding-ended"):
            scope.locations()

    def test_profile_and_kernel_binding_refusals_do_not_create(self):
        for field, value in (
            ("scope", "post-stop-reader"),
            ("version", "2.3.0"),
            ("version", "2.4.1-pre.1"),
            ("home", str(self.home) + "/../volume"),
            ("home", str(self.home) + "/"),
            ("home", "/proc/self/fd/17"),
            ("inode", self.record["inode"] + 1),
            ("device", self.record["device"] + 1),
            ("mount_id", self.record["mount_id"] + 1),
            (
                "namespace",
                [*self.record["namespace"][:1], self.record["namespace"][1] + 1],
            ),
        ):
            with self.subTest(field=field, value=value), self.synthetic_mount():
                with self.assertRaises(scratch.ScratchRefusal) as failure:
                    with scratch.volume_scratch(
                        {**self.record, field: value}, verify_retired=lambda: True
                    ):
                        self.fail("unverified binding accepted")
                self.assertNotIsInstance(
                    failure.exception, scratch.ScratchCleanupUncertain
                )
                self.assertEqual(self.artifacts(), ())

    def test_symlink_alias_and_nonprivate_volume_are_not_authority(self):
        alias = self.home.with_name("alias")
        alias.symlink_to(self.home, target_is_directory=True)
        with self.synthetic_mount():
            with self.assertRaisesRegex(scratch.ScratchRefusal, "path-unverified"):
                with scratch.volume_scratch(
                    {**self.record, "home": str(alias)}, verify_retired=lambda: True
                ):
                    self.fail("followed a volume symlink")
            self.home.chmod(0o772)
            with self.assertRaisesRegex(scratch.ScratchRefusal, "object-unverified"):
                with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                    self.fail("accepted a group-writable volume")
        self.assertEqual(self.artifacts(), ())

    def test_existing_name_is_not_reused_or_removed(self):
        occupied = self.home / (".oc-box-quiet-history-" + "0" * 32)
        occupied.mkdir(mode=0o700)
        marker = occupied / "unrelated"
        marker.write_text("untouched")
        with (
            self.synthetic_mount(),
            patch.object(scratch.secrets, "token_hex", return_value="0" * 32),
        ):
            with self.assertRaisesRegex(
                scratch.ScratchRefusal, "scratch-name-occupied"
            ):
                with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                    self.fail("adopted existing scratch")
        self.assertEqual(marker.read_text(), "untouched")

    def test_retirement_uncertainty_never_starts_cleanup(self):
        for verification in (lambda: False, lambda: 1):
            with self.subTest(verification=verification), self.synthetic_mount():
                with self.assertRaises(scratch.ScratchCleanupUncertain):
                    with scratch.volume_scratch(
                        self.record, verify_retired=verification
                    ) as scope:
                        root, _, _ = scope.locations()
                        marker = root / "still-in-use"
                        marker.write_text("retained")
                self.assertEqual(marker.read_text(), "retained")

    def test_dirty_child_errors_are_preserved_separately_from_cleanup(self):
        original = KeyboardInterrupt()
        with self.synthetic_mount(), self.assertRaises(KeyboardInterrupt) as caught:
            with scratch.volume_scratch(self.record, verify_retired=lambda: False):
                raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(original.history_source_cleanup_failed)
        self.assertEqual(len(self.artifacts()), 1)

    def test_primary_error_without_cleanup_uncertainty_keeps_identity(self):
        original = OSError(errno.EINVAL, "private caller diagnostic")
        with self.synthetic_mount(), self.assertRaises(OSError) as caught:
            with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                raise original
        self.assertIs(caught.exception, original)
        self.assertFalse(hasattr(original, "history_source_cleanup_failed"))
        self.assertEqual(self.artifacts(), ())

    def test_mount_or_directory_drift_refuses_without_scavenging(self):
        original_read = scratch._read
        drift = False

        def changed(proc, path, limit):
            data = original_read(proc, path, limit)
            return data + b"\n" if drift and path == b"mountinfo" else data

        with (
            self.synthetic_mount(),
            patch.object(scratch, "_read", side_effect=changed),
        ):
            with self.assertRaises(scratch.ScratchCleanupUncertain):
                with scratch.volume_scratch(
                    self.record, verify_retired=lambda: True
                ) as scope:
                    root, _, _ = scope.locations()
                    drift = True
                    with self.assertRaisesRegex(
                        scratch.ScratchRefusal, "mount-changed"
                    ):
                        scope.recheck()
                    drift = False
            self.assertTrue(root.is_dir(), "drift cannot establish a fresh baseline")
        with self.synthetic_mount(), self.assertRaises(scratch.ScratchCleanupUncertain):
            with scratch.volume_scratch(
                self.record, verify_retired=lambda: True
            ) as scope:
                root, _, _ = scope.locations()
                moved = self.home / "moved-by-fixture"
                root.rename(moved)
                root.mkdir(mode=0o700)
                marker = root / "replacement"
                marker.write_text("untouched")
        self.assertTrue(moved.is_dir())
        self.assertEqual(marker.read_text(), "untouched")

    def test_cleanup_refuses_aliases_and_specials_before_removing_any_entry(self):
        for create in (
            lambda path: path.symlink_to(self.database),
            lambda path: os.link(self.database, path),
            lambda path: os.mkfifo(path, 0o600),
        ):
            with self.subTest(create=create), self.synthetic_mount():
                with self.assertRaises(scratch.ScratchCleanupUncertain):
                    with scratch.volume_scratch(
                        self.record, verify_retired=lambda: True
                    ) as scope:
                        root, _, _ = scope.locations()
                        good = root / "should-retain"
                        good.write_text("retained")
                        unknown = root / "unknown"
                        create(unknown)
                self.assertEqual(good.read_text(), "retained")
                self.assertTrue(os.path.lexists(unknown))
                unknown.unlink()
                self.assertEqual(
                    self.database.read_bytes(), b"synthetic database sentinel"
                )

    def test_size_and_time_limits_refuse_without_attempting_to_clear(self):
        for constant, cap in (("_MAX_ENTRIES", 0), ("_CLEANUP_SECONDS", 0)):
            with (
                self.subTest(constant=constant),
                self.synthetic_mount(),
                patch.object(scratch, constant, cap),
            ):
                with self.assertRaises(scratch.ScratchCleanupUncertain):
                    with scratch.volume_scratch(
                        self.record, verify_retired=lambda: True
                    ) as scope:
                        root, _, _ = scope.locations()
                        marker = root / "preserve"
                        marker.write_text("bounded")
                self.assertEqual(marker.read_text(), "bounded")

    def test_partial_setup_cleans_only_confirmed_roots(self):
        mkdir = os.mkdir

        def unexpected_failure(path, mode=0o777, *, dir_fd=None):
            if path == b"cache":
                raise OSError(errno.EIO, "synthetic I/O failure")
            return mkdir(path, mode, dir_fd=dir_fd)

        with (
            self.synthetic_mount(),
            patch.object(scratch.os, "mkdir", side_effect=unexpected_failure),
        ):
            with self.assertRaisesRegex(scratch.ScratchRefusal, "scratch-setup-failed"):
                with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                    self.fail("failed initialization reached the caller")
        self.assertEqual(self.artifacts(), ())

    def test_setup_refuses_to_clean_up_an_unconfirmed_child_name(self):
        mkdir = os.mkdir

        def partial_failure(path, mode=0o777, *, dir_fd=None):
            mkdir(path, mode, dir_fd=dir_fd)
            if path == b"cache":
                raise OSError(errno.EIO, "synthetic uncertain mkdir")

        with (
            self.synthetic_mount(),
            patch.object(scratch.os, "mkdir", side_effect=partial_failure),
            self.assertRaises(scratch.ScratchCleanupUncertain),
        ):
            with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                self.fail("uncertain initialization reached the caller")
        (root,) = self.artifacts()
        self.assertTrue((root / "home").is_dir())
        self.assertTrue((root / "cache").is_dir())

    def test_failure_after_creation_but_before_identity_keeps_unproven_name(self):
        open_at = scratch._open

        def cannot_identify(parent, path, flags, *, cross=False):
            if path.startswith(b".oc-box-quiet-history-"):
                raise scratch.ScratchRefusal("scratch-path-unverified")
            return open_at(parent, path, flags, cross=cross)

        with (
            self.synthetic_mount(),
            patch.object(scratch, "_open", side_effect=cannot_identify),
        ):
            with self.assertRaises(scratch.ScratchCleanupUncertain):
                with scratch.volume_scratch(self.record, verify_retired=lambda: True):
                    self.fail("unproven initialization reached the caller")
        self.assertEqual(len(self.artifacts()), 1)


class MountRecordTests(unittest.TestCase):
    def test_exact_mount_edge_must_be_direct_writable_and_unambiguous(self):
        def rows(options, fs=b"btrfs", super_options=b"rw", extra=b""):
            return scratch._mount_rows(
                b"123 10 0:44 /data /home/quiet "
                + options
                + b" - "
                + fs
                + b" /dev/synthetic "
                + super_options
                + b"\n"
                + extra
            )

        scratch._verified_mount(rows(b"rw,nosuid"), b"/home/quiet", 123, 10)
        for observed, mount, parent in (
            (rows(b"ro"), 123, 10),
            (rows(b"rw", fs=b"overlay"), 123, 10),
            (rows(b"rw", super_options=b"ro"), 123, 10),
            (rows(b"rw"), 124, 10),
            (rows(b"rw"), 123, 11),
            (
                rows(
                    b"rw",
                    extra=b"124 123 0:44 /data /home/quiet rw - btrfs /device rw\n",
                ),
                123,
                10,
            ),
        ):
            with self.subTest(observed=observed, mount=mount, parent=parent):
                with self.assertRaisesRegex(
                    scratch.ScratchRefusal, "volume-mount-unverified"
                ):
                    scratch._verified_mount(observed, b"/home/quiet", mount, parent)


if __name__ == "__main__":
    unittest.main()
