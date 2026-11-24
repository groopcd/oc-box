"""Synthetic, offline fencing checks; no native commands or real registry."""

from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import stat
import unittest
from unittest.mock import Mock, call, patch

from oc_box import uninstall_recovery as recovery
from oc_box import workspace_uninstall as model
from oc_box.uninstall_review import UninstallRequest
from test_workspace_uninstall import WorkspaceFixture


class UninstallRecoveryTest(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _, registration = self.register()
        self.container, self.volume = self.resources(registration)
        self.reviewed = self.plan(
            self.empty(containers=(self.container,), volumes=(self.volume,))
        )
        self.container_ref = model.ResourceRef(
            self.ident, "container", self.container.name, self.container.ident
        )
        self.volume_ref = model.ResourceRef(self.ident, "volume", self.volume.name)
        self.proof = recovery.ReviewProof("a" * 64, "b" * 64, "c" * 64)
        self.gate = Mock(return_value=True)
        self.recheck = Mock(return_value=True)
        self.absent = Mock(return_value=True)
        self._ready()

    def _ready(self, name="recovery"):
        # Model independent provisioning, not a way to repair a spent anchor.
        self.directory = self.root / name
        self.directory.mkdir(mode=0o700)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(os.close, fd)
        self.fd = fd
        self.journal = self.directory / recovery.JOURNAL
        self.header = b'{"generation":"' + b"d" * 64 + b'","mode":"full","version":1}\n'
        self.journal.touch(mode=0o600)
        self.journal.write_bytes(self.header)
        self.anchor = recovery.ReadyAnchor(
            "d" * 64,
            model._file_stamp(self.directory.stat()),
            model._file_stamp(self.journal.stat()),
        )

    def _open(self, **overrides):
        return recovery.open_fence(
            **{
                "directory_fd": self.fd,
                "anchor": self.anchor,
                "request": UninstallRequest(True, False),
                "reviewed": self.reviewed,
                "proof": self.proof,
                "check_admission": self.gate,
                "revalidate": self.recheck,
                **overrides,
            }
        )

    def _refused(self, reason, **overrides):
        with self.assertRaises(recovery.RecoveryRefusal) as caught:
            with self._open(**overrides):
                self.fail("admitted a new attempt")
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)

    def _confirm(self, handle, ticket, ack=model.RemovalAck.ACKNOWLEDGED):
        return handle.confirm(ticket, ack=ack, absent=self.absent)

    def test_exact_order_synced_pending_and_postcheck_linked_confirmation(self):
        original_fsync = os.fsync
        synced = []

        def watch_fsync(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            original_fsync(fd)

        with self._open() as handle, patch.object(os, "fsync", side_effect=watch_fsync):
            for position, resource in enumerate((self.container_ref, self.volume_ref)):
                ticket = handle.reserve(resource)
                self.assertEqual(ticket.scope, self.scope)
                self.assertEqual(ticket.resource, resource)
                self.assertEqual(synced, [False, True] * (2 * position + 1))
                lines = self.journal.read_bytes().splitlines(keepends=True)
                self.assertEqual(len(lines), 2 * position + 2)
                pending = lines[-1]
                row = json.loads(pending)
                self.assertEqual(
                    (row["status"], row["step"], row["resource"]["name"]),
                    ("pending", position, resource.name),
                )
                self.assertEqual(row["resource"]["container_id"], resource.container_id)
                self.assertEqual(row["resource"]["workspace_id"], self.ident)
                self.assertEqual(row["installation"], self.proof.installation)
                self.assertEqual(row["store"], self.proof.store)
                self.assertEqual(row["approval"], self.proof.approval)
                self.assertEqual(
                    row["created"], self.volume.created_at if position else None
                )
                for field in ("scope", "backing", "registration"):
                    self.assertRegex(row[field], r"^[a-f0-9]{64}$")
                self.assertEqual(
                    self._confirm(handle, ticket),
                    model.ResourceOutcome(resource, model.ResourceState.CONFIRMED),
                )
                self.assertEqual(synced, [False, True] * (2 * position + 2))
                self.assertEqual(
                    json.loads(self.journal.read_bytes().splitlines()[-1]),
                    {
                        "status": "confirmed",
                        "step": position,
                        "pending": hashlib.sha256(pending).hexdigest(),
                    },
                )
        without_container = model._without_resource(self.reviewed, 0, "container")
        without_volume = model._without_resource(without_container, 0, "volume")
        retired = frozenset((self.container.ident,))
        self.assertEqual(
            self.recheck.call_args_list,
            [
                call(self.proof, self.reviewed, frozenset()),
                call(self.proof, without_container, retired),
                call(self.proof, without_container, retired),
                call(self.proof, without_volume, retired),
            ],
        )
        self.assertEqual(
            self.absent.call_args_list,
            [call(self.scope, self.container_ref), call(self.scope, self.volume_ref)],
        )
        self.assertNotIn(str(self.root).encode(), self.journal.read_bytes())
        self._refused("recovery-not-pristine")

    def test_only_full_non_dry_run_and_exact_scope_admission(self):
        for request in (
            UninstallRequest(False, False),
            UninstallRequest(True, True),
            UninstallRequest(1, False),
        ):
            self._refused("invalid-recovery-admission", request=request)
        self._refused(
            "invalid-recovery-admission", proof=replace(self.proof, store="unknown")
        )
        self._refused(
            "invalid-recovery-review",
            reviewed=replace(self.reviewed, engine=replace(self.scope, remote=True)),
        )
        self.gate.assert_not_called()
        self.gate.return_value = 1
        self._refused("recovery-admission-lost")
        self.gate.assert_called_once_with(self.anchor, self.proof)
        self.recheck.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), self.header)

    def test_pre_dispatch_changed_proof_and_competing_handle_refuse(self):
        self.recheck.return_value = False
        with self._open() as handle:
            self._refused("recovery-in-use")
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "plan-unverified"):
                handle.reserve(self.container_ref)
            self.recheck.return_value = True
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "attempt-closed"):
                handle.reserve(self.container_ref)
        self.assertEqual(self.journal.read_bytes(), self.header)
        with self._open() as handle:
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "step-unavailable"):
                handle.reserve(self.volume_ref)
        self.assertEqual(self.journal.read_bytes(), self.header)

    def test_unverified_results_keep_first_pending_and_block_next_step(self):
        for ix, (ack, absence, recheck, reason) in enumerate(
            (
                (True, True, True, "removal-unconfirmed"),
                (model.RemovalAck.NOT_DISPATCHED, True, True, "removal-unconfirmed"),
                (model.RemovalAck.UNCONFIRMED, True, True, "removal-unconfirmed"),
                (model.RemovalAck.ACKNOWLEDGED, 1, True, "absence-unverified"),
                (model.RemovalAck.ACKNOWLEDGED, True, False, "plan-unverified"),
            )
        ):
            with self.subTest(reason=reason, ack=ack):
                self._ready(f"case-{ix}")
                self.absent.reset_mock()
                self.recheck.reset_mock()
                self.absent.return_value = absence
                self.recheck.side_effect = (True, recheck)
                with self._open() as handle:
                    ticket = handle.reserve(self.container_ref)
                    pending = self.journal.read_bytes()
                    with self.assertRaisesRegex(recovery.RecoveryRefusal, reason):
                        self._confirm(handle, ticket, ack)
                    with self.assertRaisesRegex(recovery.RecoveryRefusal, "closed"):
                        handle.reserve(self.volume_ref)
                if ack is not model.RemovalAck.ACKNOWLEDGED:
                    self.absent.assert_not_called()
                    self.assertEqual(self.recheck.call_count, 1)
                self.assertEqual(self.journal.read_bytes(), pending)
                self._refused("recovery-not-pristine")

    def test_ticket_identity_and_lost_gate_never_probe_or_confirm(self):
        with self._open() as handle:
            ticket = handle.reserve(self.container_ref)
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "step-unavailable"):
                self._confirm(handle, replace(ticket))
        self._ready("lost-gate")
        with self._open() as handle:
            ticket = handle.reserve(self.container_ref)
            self.gate.side_effect = OSError("SYNTHETIC_DO_NOT_RENDER")
            with self.assertRaisesRegex(
                recovery.RecoveryRefusal, "admission-lost"
            ) as caught:
                self._confirm(handle, ticket)
            self.assertNotIn("SYNTHETIC_DO_NOT_RENDER", str(caught.exception))
        self.absent.assert_not_called()
        self.assertEqual(len(self.journal.read_bytes().splitlines()), 2)

    def test_append_failure_leaves_partial_or_unsynced_reservation_without_ticket(self):
        original_write, original_fsync = os.write, os.fsync
        for kind in ("short-write", "file-fsync", "directory-fsync"):
            with self.subTest(kind=kind):
                self._ready(kind)
                writes = 0

                def write(fd, raw):
                    nonlocal writes
                    if kind != "short-write":
                        return original_write(fd, raw)
                    writes += 1
                    if writes == 1:
                        return original_write(fd, raw[:10])
                    raise OSError("private")

                def sync(fd):
                    directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if kind == ("directory-fsync" if directory else "file-fsync"):
                        raise OSError("private")
                    original_fsync(fd)

                with self._open() as handle:
                    with (
                        patch.object(os, "write", side_effect=write),
                        patch.object(os, "fsync", side_effect=sync),
                        self.assertRaisesRegex(
                            recovery.RecoveryRefusal, "io-unverified"
                        ),
                    ):
                        handle.reserve(self.container_ref)
                    with self.assertRaisesRegex(recovery.RecoveryRefusal, "closed"):
                        handle.reserve(self.container_ref)
                self.assertGreater(self.journal.stat().st_size, len(self.header))
                self._refused("recovery-not-pristine")

    def test_failure_after_durable_reserve_still_never_returns_ticket(self):
        self.gate.side_effect = lambda *_: (
            self.journal.stat().st_size == len(self.header)
        )
        with self._open() as handle:
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "admission-lost"):
                handle.reserve(self.container_ref)
        self.assertEqual(len(self.journal.read_bytes().splitlines()), 2)
        self._refused("recovery-not-pristine")

    def test_confirm_write_failure_leaves_pending_without_follow_on_authority(self):
        with self._open() as handle:
            ticket = handle.reserve(self.container_ref)
            pending = self.journal.read_bytes()
            with (
                patch.object(os, "write", side_effect=OSError("private")),
                self.assertRaisesRegex(recovery.RecoveryRefusal, "io-unverified"),
            ):
                self._confirm(handle, ticket)
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "attempt-closed"):
                handle.reserve(self.volume_ref)
        self.absent.assert_called_once_with(self.scope, self.container_ref)
        self.assertEqual(self.journal.read_bytes(), pending)
        self._refused("recovery-not-pristine")

    def test_live_journal_replacement_never_probed_for_absence_or_adopted(self):
        with self._open() as handle:
            ticket = handle.reserve(self.container_ref)
            retained = self.directory / "original"
            self.journal.rename(retained)
            self.journal.touch(mode=0o600)
            self.journal.write_bytes(self.header)
            with self.assertRaisesRegex(recovery.RecoveryRefusal, "anchor-changed"):
                self._confirm(handle, ticket)
        self.absent.assert_not_called()
        self.assertEqual(len(retained.read_bytes().splitlines()), 2)
        self.assertEqual(self.journal.read_bytes(), self.header)
        self._refused("unsafe-recovery-anchor")

    def test_same_name_new_volume_generation_changes_persisted_binding(self):
        self.reviewed = self.plan(self.empty(volumes=(self.volume,)))
        with self._open() as handle:
            handle.reserve(self.volume_ref)
        previous = json.loads(self.journal.read_bytes().splitlines()[-1])
        changed = replace(
            self.volume,
            created_at="2026-01-03T03:04:05.123456789Z",
            mountpoint=replace(self.volume.mountpoint, identity=(6, 201, 8)),
        )
        self.reviewed = self.plan(self.empty(volumes=(changed,)))
        self._refused("recovery-not-pristine")
        # A separate synthetic authority can demonstrate the new projection,
        # but cannot resume or re-approve the preceding journal.
        self._ready("new-fixture")
        with self._open() as handle:
            handle.reserve(self.volume_ref)
        observed = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual(observed["resource"], previous["resource"])
        self.assertEqual(observed["created"], changed.created_at)
        self.assertNotEqual(observed["created"], previous["created"])
        self.assertNotEqual(observed["backing"], previous["backing"])

    def test_process_loss_after_reserve_not_reopened_by_new_approval_or_header_replay(
        self,
    ):
        def crash():
            with self._open() as handle:
                handle.reserve(self.container_ref)
                # Model process loss in the native-dispatch uncertainty window.
                os._exit(74)

        child = multiprocessing.get_context("fork").Process(target=crash)
        child.start()
        try:
            child.join(5)
        finally:
            if child.is_alive():
                child.kill()
                child.join(5)
        self.assertEqual(child.exitcode, 74)
        self.assertEqual(len(self.journal.read_bytes().splitlines()), 2)
        self._refused(
            "recovery-not-pristine", proof=replace(self.proof, approval="9" * 64)
        )
        self.journal.write_bytes(self.header)
        os.utime(
            self.journal,
            ns=(self.anchor.journal_stamp[-2], self.anchor.journal_stamp[-2]),
        )
        self._refused("recovery-not-pristine")
        self.journal.unlink()
        self._refused(None)
        self.assertFalse(self.journal.exists())

    def test_private_modes_mount_boundary_and_special_entries_refuse_before_io(self):
        for kind in ("public", "mount", "symlink", "fifo", "hardlink"):
            with self.subTest(kind=kind):
                self._ready(kind)
                original_mount = model._mount_id
                if kind == "public":
                    self.journal.chmod(0o644)
                elif kind in ("symlink", "fifo", "hardlink"):
                    target = self.root / f"{kind}-target"
                    self.journal.rename(target)
                    if kind == "symlink":
                        self.journal.symlink_to(target)
                    elif kind == "fifo":
                        os.mkfifo(self.journal, 0o600)
                    else:
                        os.link(target, self.journal)
                journal_info = self.journal.lstat()
                observed = replace(
                    self.anchor,
                    directory_stamp=model._file_stamp(self.directory.stat()),
                    journal_stamp=model._file_stamp(journal_info),
                )

                def mount(fd):
                    foreign = (
                        kind == "mount" and os.fstat(fd).st_ino == journal_info.st_ino
                    )
                    return original_mount(fd) + foreign

                with patch.object(model, "_mount_id", side_effect=mount):
                    self._refused("unsafe-recovery-anchor", anchor=observed)
        self.gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
