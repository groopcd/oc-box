"""Synthetic disjoint Studio records; no launcher route or engine mutation."""

import json
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, studio_registry, web_named_stop_fence as fence
from oc_box import workspace_identity


_ID = "9" * 32


def _hold(scope, control):
    with control, box.workspace_lock(scope.path), fence.arm(scope):
        control.send(True)
        if not control.poll(10):
            raise RuntimeError("Synthetic owner was not retired.")


class StudioLifecycleFenceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        home, self.state, project = (
            root / part for part in ("home", "state", "project")
        )
        for path in (home, self.state, project):
            path.mkdir(mode=0o700)
        ambient = patch.dict(os.environ, {"HOME": str(home)}, clear=True)
        ambient.start()
        self.addCleanup(ambient.stop)
        self.marker = project / studio_registry.MARKER_NAME
        self.marker.write_text(f"studio-v1:{_ID}\n")
        runtime = self.state / studio_registry.NAMESPACE
        runtime.mkdir(mode=0o700)
        self.folder = runtime / _ID
        self.folder.mkdir(mode=0o700)
        self.record = self.folder / studio_registry.RECORD_NAME
        value = {"version": 1, "role": "studio", "id": _ID, "project": str(project)}
        workspace_identity.record(value, *workspace_identity.snapshot(project))
        self.record.write_text(json.dumps(value))
        self.record.chmod(0o600)
        self.scope = fence.studio_scope(self.state, _ID)
        self.gate = self.folder / "studio-stop.lock"

    def enroll_fixture(self):
        # This freshly provisioned fixture is the only enrollment authority.
        with box.workspace_lock(self.record):
            fence.enroll_new(self.scope)

    def test_selection_does_not_enroll_or_authorize_ordinary_stop(self):
        original = set(self.folder.iterdir())
        direct = Mock(return_value=True)
        self.assertEqual(self.scope.path, self.record)
        with self.assertRaisesRegex(fence.FenceError, "not-enrolled"):
            fence.route_stop(self.scope, direct_stop=direct)
        with self.assertRaisesRegex(fence.FenceError, "invalid-request"):
            fence.route_stop(self.record, direct_stop=direct)
        self.assertEqual(set(self.folder.iterdir()), original)
        direct.assert_not_called()

    def test_intent_does_not_modify_legacy_stop_or_reopen_without_verified_completion(
        self,
    ):
        before = self.record.read_bytes(), self.marker.read_bytes()
        direct = Mock(return_value=True)
        self.enroll_fixture()
        with box.workspace_lock(self.record), fence.arm(self.scope) as owner:
            self.assertFalse(owner.stop_requested())
            alternate = fence.studio_scope(self.state, _ID)
            self.assertIs(
                fence.route_stop(alternate, direct_stop=direct), fence.StopRoute.PENDING
            )
            self.assertTrue(owner.stop_requested())
            with self.assertRaisesRegex(fence.FenceError, "completion-unverified"):
                owner.complete(drained=1, stopped=True)
            self.assertTrue(owner.stop_requested())
            self.assertEqual(
                set(path.name for path in self.folder.iterdir()),
                {"registration.json", "lock", "studio-stop.lock"},
            )
            direct.assert_not_called()
            # Independent verified evidence is represented by explicit fixture
            # booleans. No actual stop or external ownership proof ran here.
            owner.complete(drained=True, stopped=True)
            fence.require_unarmed(self.scope)
        self.assertEqual((self.record.read_bytes(), self.marker.read_bytes()), before)
        self.assertTrue(self.gate.read_bytes().startswith(b"OCBSTU1\n"))

    def test_new_registration_inode_cannot_adopt_original_unarmed_fence(self):
        self.enroll_fixture()
        prior = self.gate.read_bytes()
        copy = self.folder / "fixture-copy"
        copy.write_bytes(self.record.read_bytes())
        copy.chmod(0o600)
        copy.replace(self.record)
        direct = Mock(return_value=True)
        with self.assertRaisesRegex(fence.FenceError, "wrong-lifetime"):
            fence.route_stop(self.scope, direct_stop=direct)
        rebound = fence.studio_scope(self.state, _ID)
        with self.assertRaisesRegex(fence.FenceError, "invalid-state"):
            fence.route_stop(rebound, direct_stop=direct)
        self.assertEqual(self.gate.read_bytes(), prior)
        direct.assert_not_called()

    def test_attach_observes_one_armed_lifetime_and_never_adopts_its_successor(self):
        self.enroll_fixture()
        with self.assertRaisesRegex(fence.FenceError, "wrong-lifetime"):
            fence.observe_attach(self.scope)
        direct = Mock(return_value=True)
        with box.workspace_lock(self.record), fence.arm(self.scope) as first:
            witness = fence.observe_attach(self.scope)
            self.assertTrue(fence.attach_current(witness))
            self.assertIs(
                fence.route_stop(self.scope, direct_stop=direct),
                fence.StopRoute.PENDING,
            )
            self.assertFalse(fence.attach_current(witness))
            with self.assertRaisesRegex(fence.FenceError, "wrong-lifetime"):
                fence.observe_attach(self.scope)
            first.complete(drained=True, stopped=True)
            with fence.arm(self.scope) as second:
                self.assertFalse(fence.attach_current(witness))
                new_witness = fence.observe_attach(self.scope)
                self.assertTrue(fence.attach_current(new_witness))
                second.complete(drained=True, stopped=True)
        self.assertFalse(fence.attach_current(new_witness))
        direct.assert_not_called()

    def test_attach_witness_stays_retired_after_observed_marker_loss(self):
        self.enroll_fixture()
        with box.workspace_lock(self.record), fence.arm(self.scope) as owner:
            witness = fence.observe_attach(self.scope)
            self.assertTrue(fence.attach_current(witness))
            marker = self.marker.read_bytes()
            self.marker.unlink()
            self.assertFalse(fence.attach_current(witness))
            self.marker.write_bytes(marker)
            self.assertFalse(fence.attach_current(witness))
            # A separate observation does not revive that object.
            self.assertTrue(fence.attach_current(fence.observe_attach(self.scope)))
            owner.complete(drained=True, stopped=True)

    def test_killed_original_owner_cannot_turn_pending_into_ordinary_stop(self):
        self.enroll_fixture()
        ctx = multiprocessing.get_context("fork")
        parent, worker = ctx.Pipe()
        child = ctx.Process(target=_hold, args=(self.scope, worker))
        with parent, worker:
            child.start()
            try:
                self.assertTrue(parent.poll(5) and parent.recv())
                direct = Mock(return_value=True)
                self.assertIs(
                    fence.route_stop(self.scope, direct_stop=direct),
                    fence.StopRoute.PENDING,
                )
                child.kill()
                child.join(5)
                self.assertEqual(child.exitcode, -9)
                self.assertIs(
                    fence.route_stop(self.scope, direct_stop=direct),
                    fence.StopRoute.PENDING,
                )
                with (
                    box.workspace_lock(self.record),
                    self.assertRaisesRegex(fence.FenceError, "armed"),
                ):
                    fence.require_unarmed(self.scope)
                direct.assert_not_called()
            finally:
                if child.is_alive():
                    child.kill()
                    child.join(3)
                child.close()
