"""Metadata-backed scratch cleanup in fresh disposable directories."""

from pathlib import Path
import tempfile
import unittest

from oc_box.history_overlap_host_scratch import host_archive_scratch
from oc_box.history_volume_scratch import ScratchCleanupUncertain


class HostScratchTests(unittest.TestCase):
    def test_removes_the_exact_owned_tree_after_positive_retirement(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name)
            sentinel = parent / "synthetic-unrelated"
            sentinel.write_bytes(b"untouched")
            with host_archive_scratch(parent, verify_retired=lambda: True) as scratch:
                work = scratch / "temp"
                work.mkdir(mode=0o700)
                (work / "fixture-log").write_bytes(b"synthetic metadata")
            self.assertFalse(scratch.exists())
            self.assertEqual(list(parent.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_unverified_retirement_keeps_the_tree(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ScratchCleanupUncertain) as failure:
                with host_archive_scratch(
                    Path(name), verify_retired=lambda: False
                ) as scratch:
                    (scratch / "synthetic-log").touch()
            self.assertTrue(failure.exception.history_destination_cleanup_failed)
            self.assertTrue((scratch / "synthetic-log").exists())

    def test_unexplained_symlink_refuses_without_following_or_deleting_it(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name)
            sentinel = parent / "outside-dir"
            sentinel.mkdir()
            (sentinel / "kept").touch()
            with self.assertRaises(ScratchCleanupUncertain):
                with host_archive_scratch(
                    parent, verify_retired=lambda: True
                ) as scratch:
                    (scratch / "invalid-link").symlink_to(sentinel)
            self.assertTrue((scratch / "invalid-link").is_symlink())
            self.assertTrue((sentinel / "kept").is_file())
