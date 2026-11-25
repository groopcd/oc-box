"""Exercise immutable candidate bytes without an existing destination store."""

import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oc_box import history_overlap_archive as archive, oc_box, private_memory


class OverlapArchiveTests(unittest.TestCase):
    def test_candidate_path_and_publication_have_identical_sealed_bytes(self):
        payload = b'{"a":1}\n'
        with archive.sealed_candidate([payload[:3], payload[3:]]) as staged:
            self.assertEqual(staged.digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(staged.size, len(payload))
            self.assertFalse(os.get_inheritable(staged._fd))
            self.assertTrue(private_memory.is_sealed(staged._fd))
            with open(staged.native_path(), "rb") as file:
                self.assertEqual(file.read(), payload)
            with self.assertRaises(OSError):
                os.pwrite(staged._fd, b"changed", 0)
            output = io.BytesIO()
            staged.write_to(output)
            self.assertEqual(output.getvalue(), payload)
        with self.assertRaisesRegex(archive.ArchiveRefusal, "candidate-descriptor"):
            staged.native_path()

    def test_bound_and_representation_refusals_never_return_a_candidate(self):
        for value in ([], [b""], [b"1234", b"5"], ["1234"]):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(archive.ArchiveRefusal, "candidate-bound"),
            ):
                with archive.sealed_candidate(value, limit=4):
                    self.fail("An invalid candidate was admitted")

    def test_no_disk_fallback_for_failed_seals(self):
        with (
            patch.object(private_memory, "seal", side_effect=OSError),
            self.assertRaisesRegex(archive.ArchiveRefusal, "candidate-write"),
        ):
            with archive.sealed_candidate([b"candidate"]):
                self.fail("An unsealed candidate was admitted")

    def test_failed_publication_never_looks_like_a_full_copy(self):
        class ShortWriter:
            def write(self, data):
                return len(data) - 1

        with archive.sealed_candidate([b"candidate"]) as staged:
            with self.assertRaisesRegex(archive.ArchiveRefusal, "candidate-copy"):
                staged.write_to(ShortWriter())

    def test_publication_gate_runs_after_file_sync_and_cannot_replace_on_refusal(self):
        order = []
        sync = os.fsync

        def observed(fd):
            order.append("synced")
            return sync(fd)

        @contextlib.contextmanager
        def refuse():
            order.append("admitted")
            raise RuntimeError("synthetic stop intent")
            yield

        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "archive.json"
            target.write_bytes(b"old synthetic archive")
            original = target.stat().st_ino
            with (
                patch.object(oc_box.os, "fsync", side_effect=observed),
                self.assertRaisesRegex(RuntimeError, "synthetic stop intent"),
                oc_box._atomic_json_file(
                    target, binary=True, durable=True, publish_gate=refuse
                ) as output,
            ):
                output.write(b"new")
            self.assertEqual(order, ["synced", "admitted"])
            self.assertEqual(target.read_bytes(), b"old synthetic archive")
            self.assertEqual(target.stat().st_ino, original)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_unexpected_archive_after_staging_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "archive.json"

            @contextlib.contextmanager
            def gate():
                target.write_bytes(b"unrelated late file")
                yield

            with (
                self.assertRaises(FileExistsError),
                oc_box._atomic_json_file(
                    target, binary=True, durable=True, publish_gate=gate, replace=False
                ) as output,
            ):
                output.write(b"new")
            self.assertEqual(target.read_bytes(), b"unrelated late file")
            self.assertEqual(list(target.parent.iterdir()), [target])
