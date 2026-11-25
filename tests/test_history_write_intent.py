import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from oc_box import history_write_intent as journal
from oc_box import oc_box as box


WORKSPACE = "a" * 32
PROFILE = "stock-2.4.1"
SOURCE = "ses_source"
OTHER_SOURCE = "ses_other"
A, B, C = ("a" * 64, "b" * 64, "c" * 64)


def archive(source=SOURCE, workspace=WORKSPACE):
    namespace = hashlib.sha256(f"{workspace}-v2".encode()).hexdigest()[:12]
    return f"ses_{namespace}_{source.split('_', 1)[-1]}"


def proof(intent, *, destination, checkpoint):
    return journal.VerifiedCheckpoint(
        intent.operation_id,
        intent.workspace_id,
        intent.profile,
        intent.source_id,
        intent.archive_id,
        destination,
        checkpoint,
    )


class HistoryWriteIntentTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.state = Path(private.name)
        self.registry = self.registration(WORKSPACE)
        self.folder = self.registry.parent
        self.pending = self.folder / journal.PENDING_NAME
        self.receipt = self.folder / journal.RECEIPT_NAME
        self.checkpoint = self.folder / "sessions-v2.json"
        self.checkpoint.write_text(json.dumps({SOURCE: A}), encoding="ascii")

    def registration(self, ident):
        folder = self.state / ident
        folder.mkdir(mode=0o755)
        path = folder / "workspace.json"
        path.write_text(
            json.dumps({"id": ident, "opencode_version": "2.4.1"}), encoding="ascii"
        )
        return path

    def open(self, profile=PROFILE):
        return journal.Journal(self.registry, WORKSPACE, profile=profile)

    def assertRefuses(self, category):
        return self.assertRaisesRegex(journal.JournalError, rf"\((?:{category})\)")

    def reserve(self, held):
        return held.reserve(SOURCE, archive(), A, B)

    def write_pending(self, raw):
        self.pending.write_bytes(raw)
        self.pending.chmod(0o600)

    def test_create_intent_does_not_claim_verified_prior_absence(self):
        with self.open() as held:
            intent = held.reserve_create(SOURCE, archive(), B)
            self.assertTrue(intent.atomic_create)
            self.assertIsNone(intent.expected_prior_digest)
            self.assertEqual(held.load(), intent)
        with self.open() as reopened:
            self.assertEqual(reopened.load(), intent)

    def test_serial_sync_never_bypasses_the_intent_fence(self):
        data = {"id": WORKSPACE, "opencode_version": "2.4.1"}
        start = box._overlap_journal_profile(data)
        with self.open(profile=start) as held:
            held.reserve_create(SOURCE, archive(), B)
        with (
            patch.object(box, "refresh_sessions") as refreshed,
            self.assertRaisesRegex(
                box.BoxError, "previous host history write is unresolved"
            ),
        ):
            box.sync_sessions(self.registry, data)
        refreshed.assert_not_called()
        self.assertTrue(self.pending.exists())

    def test_roundtrip_exact_retirement_and_next_serial_operation(self):
        original_checkpoint = self.checkpoint.read_bytes()
        held = self.open()
        with self.assertRefuses("closed"):
            held.load()
        with held:
            self.assertIsNone(held.load())
            self.assertFalse(self.pending.exists())
            intent = self.reserve(held)
            self.assertEqual(held.load(), intent)
            self.assertEqual(stat.S_IMODE(self.pending.stat().st_mode), 0o600)
            raw = self.pending.read_bytes()
            self.assertEqual(
                json.loads(raw),
                {
                    "version": 1,
                    "native_major": 2,
                    "kind": "pending",
                    "operation_id": intent.operation_id,
                    "workspace_id": WORKSPACE,
                    "profile": PROFILE,
                    "source_id": SOURCE,
                    "archive_id": archive(),
                    "expected_prior_digest": A,
                    "intended_digest": B,
                    "atomic_create": False,
                },
            )
        with self.open() as held:
            self.assertEqual(held.load(), intent)
            for new_source in (SOURCE, OTHER_SOURCE):
                with (
                    self.subTest(new_source=new_source),
                    self.assertRefuses("unresolved"),
                ):
                    held.reserve(new_source, archive(new_source), A, B)
            self.assertEqual(self.pending.read_bytes(), raw)
            accepted = proof(intent, destination=B, checkpoint=B)
            held.retire(intent, accepted)
            self.assertIsNone(held.load())
            self.assertEqual(self.pending.read_bytes(), raw)
            self.assertEqual(self.checkpoint.read_bytes(), original_checkpoint)
        with self.open() as held:
            self.assertIsNone(held.load())
            old_receipt = self.receipt.read_bytes()
            second = held.reserve(OTHER_SOURCE, archive(OTHER_SOURCE), None, C)
            self.assertNotEqual(second.operation_id, intent.operation_id)
            self.assertEqual(self.receipt.read_bytes(), old_receipt)
            with self.assertRefuses("retirement-unverified"):
                held.retire(intent, accepted)
            self.assertEqual(held.load(), second)
            held.retire(second, proof(second, destination=C, checkpoint=C))
            self.assertIsNone(held.load())
            self.assertNotEqual(self.receipt.read_bytes(), old_receipt)

    def test_unknown_stale_or_mismatched_results_never_retire(self):
        with self.open() as held:
            intent = self.reserve(held)
            raw = self.pending.read_bytes()
            verified = proof(intent, destination=B, checkpoint=B)
            incomplete = (
                None,
                True,
                replace(verified, destination_digest=None),
                replace(verified, destination_digest=A),
                replace(verified, checkpoint_digest=None),
                replace(verified, checkpoint_digest=A),
                replace(verified, workspace_id="b" * 32),
                replace(verified, profile="stock-2.4.0"),
                replace(verified, operation_id=C),
                replace(verified, source_id=OTHER_SOURCE),
                replace(verified, archive_id=archive(OTHER_SOURCE)),
            )
            for index, result in enumerate(incomplete):
                with (
                    self.subTest(result=index),
                    self.assertRefuses("checkpoint-unverified"),
                ):
                    held.retire(intent, result)
            # Even caller-side proof for matching destination/digest isn't
            # permission to retire a newly constructed/unreserved generation.
            with self.assertRefuses("retirement-unverified"):
                held.retire(
                    replace(intent, operation_id=C),
                    replace(verified, operation_id=C),
                )
            self.assertEqual(self.pending.read_bytes(), raw)
            self.assertFalse(self.receipt.exists())

    def test_native_error_keeps_pending_and_its_original_exception(self):
        unknown = TimeoutError("synthetic native timeout")
        with self.assertRaises(TimeoutError) as caught, self.open() as held:
            intent = self.reserve(held)
            raise unknown
        self.assertIs(caught.exception, unknown)
        with self.open() as held:
            self.assertEqual(held.load(), intent)

    def test_process_exit_then_source_returns_to_checkpoint_a(self):
        host = self.folder / "fixture-destination"
        host.write_text(A, encoding="ascii")
        prior_checkpoint = self.checkpoint.read_bytes()
        with self.open() as held:
            self.assertIsNone(held.load())

        child = """
import os, sys
from pathlib import Path
from oc_box.history_write_intent import Journal
file, workspace, profile, source, archive, a, b, host = sys.argv[1:]
with Journal(file, workspace, profile=profile) as journal:
    journal.reserve(source, archive, a, b)
    Path(host).write_text(b, encoding="ascii")
    os._exit(17)  # Destination mutation happened; its acknowledgement was lost.
"""
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                child,
                str(self.registry),
                WORKSPACE,
                PROFILE,
                SOURCE,
                archive(),
                A,
                B,
                str(host),
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 17, result.stderr.decode())
        self.assertEqual(host.read_text(encoding="ascii"), B)
        self.assertEqual(self.checkpoint.read_bytes(), prior_checkpoint)
        source_digest = A
        with self.open() as held:
            unresolved = held.load()
            self.assertIsNotNone(unresolved)
            self.assertEqual(unresolved.expected_prior_digest, source_digest)
            self.assertEqual(unresolved.intended_digest, B)
            with self.assertRefuses("unresolved"):
                held.reserve(SOURCE, archive(), B, source_digest)
            # A matching cached checkpoint or unknown acknowledge result may
            # not retire the attempt even if the current source returned to A.
            with self.assertRefuses("checkpoint-unverified"):
                held.retire(unresolved, proof(unresolved, destination=B, checkpoint=A))
        self.assertEqual(self.checkpoint.read_bytes(), prior_checkpoint)
        self.assertFalse(self.receipt.exists())

    def test_file_fsync_precedes_publication_and_directory_fsync_precedes_return(self):
        real_sync = os.fsync
        observed = []

        def verify(fd):
            is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            observed.append(is_directory)
            self.assertEqual(self.pending.exists(), is_directory)
            real_sync(fd)

        with self.open() as held, patch.object(journal.os, "fsync", side_effect=verify):
            self.reserve(held)
        self.assertEqual(observed, [False, True])

    def test_failed_publication_never_undoes_visible_intent_or_touches_checkpoint(self):
        real_sync = os.fsync
        original_checkpoint = self.checkpoint.read_bytes()

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "raw private diagnostic")
            real_sync(fd)

        with self.open() as held:
            with patch.object(journal.os, "fsync", side_effect=fail_directory):
                with self.assertRefuses("publication-unverified") as error:
                    self.reserve(held)
                self.assertNotIn("raw private diagnostic", str(error.exception))
            record = self.pending.read_bytes()
        with self.open() as held:
            self.assertEqual(held.load().intended_digest, B)
            with self.assertRefuses("unresolved"):
                self.reserve(held)
        self.assertEqual(self.pending.read_bytes(), record)
        self.assertEqual(self.checkpoint.read_bytes(), original_checkpoint)

    def test_failed_staging_file_sync_has_no_publication(self):
        with (
            self.open() as held,
            patch.object(
                journal.os, "fsync", side_effect=OSError(errno.EIO, "private")
            ),
        ):
            with self.assertRefuses("publication-unverified"):
                self.reserve(held)
            self.assertIsNone(held.load())
        self.assertFalse(list(self.folder.glob(".history-write-*.tmp")))

    def test_failed_retirement_preserves_pending_and_fences_unsynced_receipt(self):
        real_sync = os.fsync
        original_checkpoint = self.checkpoint.read_bytes()

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "raw private diagnostic")
            real_sync(fd)

        with self.open() as held:
            intent = self.reserve(held)
            record = self.pending.read_bytes()
            inode = self.pending.stat().st_ino
            with patch.object(journal.os, "fsync", side_effect=fail_directory):
                with self.assertRefuses("retirement-unverified"):
                    held.retire(intent, proof(intent, destination=B, checkpoint=B))
                with self.assertRefuses("retirement-unverified"):
                    held.load()
        self.assertEqual(self.pending.read_bytes(), record)
        self.assertEqual(self.pending.stat().st_ino, inode)
        self.assertEqual(self.checkpoint.read_bytes(), original_checkpoint)
        self.assertTrue(self.receipt.is_file())
        # A later reader can settle a complete receipt only after an actual
        # successful fsync. No native action or checkpoint rewrite happens.
        with self.open() as held:
            self.assertIsNone(held.load())
        self.assertEqual(self.pending.read_bytes(), record)

    def test_failed_next_publication_keeps_previous_receipt_and_new_unsynced_intent(
        self,
    ):
        real_sync = os.fsync
        directory_syncs = 0

        def fail_second_directory(fd):
            nonlocal directory_syncs
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                directory_syncs += 1
                if directory_syncs == 2:
                    raise OSError(errno.EIO, "private")
            real_sync(fd)

        with self.open() as held:
            intent = self.reserve(held)
            held.retire(intent, proof(intent, destination=B, checkpoint=B))
            receipt = self.receipt.read_bytes()
            with patch.object(journal.os, "fsync", side_effect=fail_second_directory):
                with self.assertRefuses("publication-unverified"):
                    held.reserve(SOURCE, archive(), B, C)
            self.assertEqual(held.load().intended_digest, C)
        self.assertEqual(self.receipt.read_bytes(), receipt)

    def test_racing_no_overwrite_target_does_not_unlink_target(self):
        external = self.folder / "untouched"
        external.write_bytes(b"untouched")
        os_link = os.link

        def insert_target(*args, **kwargs):
            self.pending.symlink_to(external)
            os_link(*args, **kwargs)

        with (
            self.open() as held,
            patch.object(journal.os, "link", side_effect=insert_target),
        ):
            with self.assertRefuses("publication-unverified"):
                self.reserve(held)
        self.assertTrue(self.pending.is_symlink())
        self.assertEqual(external.read_bytes(), b"untouched")

    def test_invalid_records_are_not_absence_and_remain_byte_identical(self):
        with self.open() as held:
            self.reserve(held)
            valid = self.pending.read_bytes()
            malformed = {
                "oversize": b"x" * (journal.MAX_BYTES + 1),
                "deep": b"[" * 1010 + b"]" * 1010,
                "duplicate": valid.replace(b'"version":1', b'"version":1,"version":1'),
                "version": valid.replace(b'"version":1', b'"version":2'),
                "native major": valid.replace(b'"native_major":2', b'"native_major":1'),
                "wrong archive": valid.replace(archive().encode(), b"ses_wrong"),
                "wrong workspace": valid.replace(WORKSPACE.encode(), b"f" * 32),
                "free form": valid[:-2] + b',"transcript":"private"}\n',
                "incomplete": valid[:-5],
            }
            for case, raw in malformed.items():
                with self.subTest(case=case):
                    self.write_pending(raw)
                    with self.assertRefuses("invalid-record|unsafe-storage"):
                        held.load()
                    with self.assertRefuses("invalid-record|unsafe-storage"):
                        self.reserve(held)
                    self.assertEqual(self.pending.read_bytes(), raw)

    def test_unsafe_metadata_is_not_opened_as_absent_or_repaired(self):
        external = self.folder / "elsewhere"
        external.write_bytes(b"untouched")
        with self.open() as held:
            self.pending.symlink_to(external)
            with self.assertRefuses("unsafe-storage"):
                held.load()
            with self.assertRefuses("unsafe-storage"):
                self.reserve(held)
            self.assertTrue(self.pending.is_symlink())
            self.assertEqual(external.read_bytes(), b"untouched")
            self.pending.unlink()
            os.mkfifo(self.pending, 0o600)
            with self.assertRefuses("unsafe-storage"):
                held.load()
            self.assertTrue(stat.S_ISFIFO(self.pending.stat().st_mode))
            self.pending.unlink()
            intent = self.reserve(held)
            raw = self.pending.read_bytes()
            for mode in (0o400, 0o700, 0o640, 0o1600):
                with self.subTest(mode=oct(mode)):
                    self.pending.chmod(mode)
                    with self.assertRefuses("unsafe-storage"):
                        held.load()
                    self.assertEqual(stat.S_IMODE(self.pending.stat().st_mode), mode)
            self.pending.chmod(0o600)
            alias = self.folder / "alias"
            os.link(self.pending, alias)
            with self.assertRefuses("unsafe-storage"):
                held.load()
            self.assertEqual(alias.read_bytes(), raw)
            alias.unlink()
            self.receipt.symlink_to(external)
            with self.assertRefuses("unsafe-storage"):
                held.retire(intent, proof(intent, destination=B, checkpoint=B))
            self.assertTrue(self.receipt.is_symlink())
            self.assertEqual(self.pending.read_bytes(), raw)

    def test_workspace_scope_and_lock(self):
        different = self.registration("b" * 32)
        with self.open() as held:
            self.reserve(held)
            with self.assertRefuses("busy"), self.open():
                self.fail("same workspace locked twice")
            with journal.Journal(different, "b" * 32, profile=PROFILE) as other:
                self.assertIsNone(other.load())
        with self.open("other-profile") as held, self.assertRefuses("profile-mismatch"):
            held.load()
        self.registry.unlink()
        self.registry.symlink_to(different)
        with self.assertRefuses("unsafe-storage"), self.open():
            self.fail("registry symlink accepted")
        self.registry.unlink()
        self.registry.write_text(
            json.dumps({"id": WORKSPACE, "opencode_version": "1.19.5"}),
            encoding="ascii",
        )
        with self.assertRefuses("invalid-scope"), self.open():
            self.fail("V1 workspace accepted")

    def test_matching_nonce_does_not_excuse_different_receipt(self):
        with self.open() as held:
            intent = self.reserve(held)
            held.retire(intent, proof(intent, destination=B, checkpoint=B))
            raw = self.receipt.read_bytes().replace(B.encode(), C.encode())
            self.receipt.write_bytes(raw)
            with self.assertRefuses("invalid-record"):
                held.load()
            with self.assertRefuses("invalid-record"):
                held.reserve(SOURCE, archive(), B, A)
            self.assertEqual(self.receipt.read_bytes(), raw)

    def test_registry_directory_replacement_cannot_redirect_a_held_journal(self):
        displaced = self.state / "displaced"
        with self.open() as held:
            self.folder.rename(displaced)
            replacement = self.registration(WORKSPACE)
            with self.assertRefuses("unsafe-storage"):
                self.reserve(held)
            self.assertFalse(self.pending.exists())
            self.assertFalse((displaced / journal.PENDING_NAME).exists())
            self.assertTrue(replacement.exists())
