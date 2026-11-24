import fcntl
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, web_named_stop_fence as fence, workspace_control


_WAIT = 10


def _receive(connection):
    if not connection.poll(_WAIT):
        raise AssertionError("Synthetic peer did not respond.")
    return connection.recv()


def _hold_direct_stop(path, connection):
    def direct():
        connection.send("in-direct-stop")
        if _receive(connection) != "finish":
            raise AssertionError("Invalid fixture message.")
        return True

    with connection:
        result = fence.route_stop(path, direct_stop=direct)
        connection.send(result)


def _hold_owner(path, connection):
    with connection, box.workspace_lock(path), fence.arm(path) as owner:
        connection.send("armed")
        if _receive(connection) == "observe":
            connection.send(owner.stop_requested())
            _receive(connection)


class NamedStopFenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-named-stop-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.workspace("fixture")
        self.entry = self.path.with_name("named-stop.lock")
        self.direct = Mock(return_value=True)

    def workspace(self, name):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / "workspace.json"
        path.write_text('{"synthetic":true}\n')
        path.chmod(0o600)
        return path

    def enroll(self):
        with box.workspace_lock(self.path):
            fence.enroll_new(self.path)

    def route(self, path=None, **options):
        return fence.route_stop(path or self.path, direct_stop=self.direct, **options)

    def assert_refused(self, category):
        return self.assertRaisesRegex(
            fence.FenceError,
            rf"^Named workspace stop routing refused \({category}\)\.$",
        )

    def start_peer(self, target):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=target, args=(self.path, child))
        process.start()
        child.close()

        def finish():
            try:
                if process.is_alive():
                    process.kill()
                process.join(_WAIT)
                self.assertFalse(process.is_alive())
                process.close()
            finally:
                parent.close()

        self.addCleanup(finish)
        return parent, process

    def test_explicit_enrollment_is_required_and_no_fence_is_adopted(self):
        with self.assert_refused("not-enrolled"):
            self.route()
        with self.assert_refused("not-enrolled"), box.workspace_lock(self.path):
            with fence.arm(self.path):
                self.fail("An unknown state must not arm.")
        self.assertFalse(self.entry.exists())
        self.direct.assert_not_called()
        with self.assert_refused("owner-lock"):
            fence.enroll_new(self.path)
        self.enroll()
        self.assertEqual(self.entry.stat().st_mode & 0o777, 0o600)
        with self.assert_refused("already-enrolled"), box.workspace_lock(self.path):
            fence.enroll_new(self.path)
        self.assertIs(self.route(), fence.StopRoute.DIRECT)
        self.direct.assert_called_once_with()
        self.assertFalse(self.path.with_name("control").exists())

    def test_torn_first_write_and_absent_entry_never_repair_or_run_direct_stop(self):
        write = os.pwrite

        def short_write(fd, value, offset):
            return write(fd, value[: len(value) // 2], offset)

        with (
            self.assert_refused("invalid-state"),
            box.workspace_lock(self.path),
            patch.object(fence.os, "pwrite", short_write),
        ):
            fence.enroll_new(self.path)
        torn = self.entry.read_bytes()
        with self.assert_refused("invalid-state"):
            self.route()
        with self.assert_refused("already-enrolled"), box.workspace_lock(self.path):
            fence.enroll_new(self.path)
        self.assertEqual(self.entry.read_bytes(), torn)
        self.entry.unlink()
        with self.assert_refused("not-enrolled"):
            self.route()
        self.assertFalse(self.entry.exists())
        self.direct.assert_not_called()

    def test_armed_stop_publishes_only_idempotent_intent_for_that_lifetime(self):
        legacy = workspace_control.capture(self.path)
        self.enroll()
        with box.workspace_lock(self.path), fence.arm(self.path) as first:
            identity = self.entry.stat().st_ino
            self.assertFalse(first.stop_requested())
            with self.assert_refused("armed"):
                fence.require_unarmed(self.path)
            # Named intent does not wait for, write, or reinterpret recovery control.
            with workspace_control.admission(self.path, timeout=0):
                self.assertIs(self.route(timeout=0), fence.StopRoute.PENDING)
            self.assertTrue(first.stop_requested())
            self.assertTrue(workspace_control.unchanged(self.path, legacy))
            requested = self.entry.read_bytes()
            with patch.object(fence.os, "pwrite", side_effect=AssertionError):
                self.assertIs(self.route(), fence.StopRoute.PENDING)
            self.assertEqual(self.entry.read_bytes(), requested)
            for drained, stopped in ((False, True), (True, False), (1, True)):
                with self.subTest(drained=drained, stopped=stopped):
                    with self.assert_refused("completion-unverified"):
                        first.complete(drained=drained, stopped=stopped)
                    self.assertEqual(self.entry.read_bytes(), requested)
            self.direct.assert_not_called()
            first.complete(drained=True, stopped=True)
            self.assertEqual(self.entry.stat().st_ino, identity)
            fence.require_unarmed(self.path)
        with box.workspace_lock(self.path), fence.arm(self.path) as second:
            self.assertFalse(second.stop_requested())
            self.assertNotEqual(second._generation, first._generation)
            with self.assert_refused("wrong-lifetime"):
                first.complete(drained=True, stopped=True)
            second.complete(drained=True, stopped=True)
        self.assertIs(self.route(), fence.StopRoute.DIRECT)
        self.direct.assert_called_once_with()

    def test_exception_on_uncompleted_owner_exit_leaves_named_fence(self):
        self.enroll()
        interrupted = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as raised:
            with box.workspace_lock(self.path), fence.arm(self.path):
                raise interrupted
        self.assertIs(raised.exception, interrupted)
        self.assertIs(self.route(), fence.StopRoute.PENDING)
        self.direct.assert_not_called()

    def test_interrupt_after_grant_does_not_strand_gate_on_the_pinned_descriptor(self):
        flock = workspace_control._flock

        def lock_then_interrupt(fd, deadline):
            flock(fd, deadline)
            raise KeyboardInterrupt

        self.enroll()
        with box.workspace_lock(self.path), fence.arm(self.path) as owner:
            with (
                patch.object(workspace_control, "_flock", lock_then_interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                owner.stop_requested()
            self.assertIs(self.route(timeout=0), fence.StopRoute.PENDING)
            self.assertTrue(owner.stop_requested(timeout=0))
            owner.complete(drained=True, stopped=True)
        self.direct.assert_not_called()

    def test_direct_stop_serializes_arming_through_the_entire_callback(self):
        self.enroll()
        peer, process = self.start_peer(_hold_direct_stop)
        self.assertEqual(_receive(peer), "in-direct-stop")
        with box.workspace_lock(self.path):
            with self.assert_refused("busy"), fence.arm(self.path, timeout=0.05):
                self.fail("Named owner was admitted during direct stop.")
            with self.assert_refused("busy"):
                self.route(timeout=0)
            peer.send("finish")
            self.assertIs(_receive(peer), fence.StopRoute.DIRECT)
            process.join(_WAIT)
            self.assertEqual(process.exitcode, 0)
            with fence.arm(self.path, timeout=0) as owner:
                self.assertFalse(owner.stop_requested())
                self.assertIs(self.route(timeout=0), fence.StopRoute.PENDING)
                owner.complete(drained=True, stopped=True)
        self.direct.assert_not_called()

    def test_owner_death_does_not_disarm_or_turn_late_intent_into_direct_stop(self):
        for requested_before_death in (False, True):
            with self.subTest(requested_before_death=requested_before_death):
                if requested_before_death:
                    self.path = self.workspace("second-fixture")
                    self.entry = self.path.with_name(self.entry.name)
                self.enroll()
                peer, process = self.start_peer(_hold_owner)
                self.assertEqual(_receive(peer), "armed")
                if requested_before_death:
                    self.assertIs(self.route(), fence.StopRoute.PENDING)
                    peer.send("observe")
                    self.assertIs(_receive(peer), True)
                saved = self.entry.read_bytes()
                process.kill()
                process.join(_WAIT)
                self.assertLess(process.exitcode, 0)
                self.assertEqual(self.entry.read_bytes(), saved)
                # The actual lifecycle flock is now free, but not the generation.
                with box.workspace_lock(self.path):
                    with self.assert_refused("armed"):
                        fence.require_unarmed(self.path)
                    with self.assert_refused("armed"), fence.arm(self.path):
                        self.fail("A dead owner's generation was replaced.")
                    self.assertIs(self.route(), fence.StopRoute.PENDING)
                self.direct.assert_not_called()

    def test_direct_stop_process_death_cannot_admit_another_stop_or_owner(self):
        self.enroll()
        peer, process = self.start_peer(_hold_direct_stop)
        self.assertEqual(_receive(peer), "in-direct-stop")
        process.kill()
        process.join(_WAIT)
        self.assertLess(process.exitcode, 0)
        with box.workspace_lock(self.path):
            with self.assert_refused("direct-unverified"), fence.arm(self.path):
                self.fail("An abandoned direct stop was treated as unarmed.")
            with self.assert_refused("direct-unverified"):
                fence.require_unarmed(self.path)
        with self.assert_refused("direct-unverified"):
            self.route()
        self.direct.assert_not_called()

    def test_context_metadata_does_not_substitute_for_original_exclusive_flock(self):
        self.enroll()
        with self.path.with_name("lock").open("a+") as stream:
            held = box.workspace_service.LIFECYCLE_LOCK.set((self.path, stream))
            try:
                for flags in (fcntl.LOCK_UN, fcntl.LOCK_SH):
                    fcntl.flock(stream, flags)
                    with self.assert_refused("owner-lock"), fence.arm(self.path):
                        self.fail("Fabricated owner context armed a generation.")
            finally:
                box.workspace_service.LIFECYCLE_LOCK.reset(held)
        with self.assert_refused("owner-lock"), box.workspace_lock(self.path):
            with fence.arm(self.path) as owner:
                _, stream = box.workspace_service.LIFECYCLE_LOCK.get()
                fcntl.flock(stream, fcntl.LOCK_UN)
                with self.assert_refused("owner-lock"):
                    owner.complete(drained=True, stopped=True)
        self.assertIs(self.route(), fence.StopRoute.PENDING)
        self.direct.assert_not_called()

    def test_same_bytes_at_a_substituted_gate_cannot_clear_or_admit_direct_stop(self):
        self.enroll()
        unarmed = self.entry.read_bytes()
        retired = self.entry.with_name("untouched-original")
        with self.assert_refused("unsafe-storage"), box.workspace_lock(self.path):
            with fence.arm(self.path) as owner:
                self.entry.rename(retired)
                armed = retired.read_bytes()
                self.entry.write_bytes(unarmed)
                self.entry.chmod(0o600)
                with self.assert_refused("invalid-state"):
                    self.route()
                with self.assert_refused("unsafe-storage"):
                    owner.complete(drained=True, stopped=True)
                self.assertEqual(self.entry.read_bytes(), unarmed)
                self.assertEqual(retired.read_bytes(), armed)
        self.direct.assert_not_called()

    def test_direct_stop_rechecks_parent_and_gate_before_releasing(self):
        self.enroll()
        original_dir = self.path.parent

        def replace_parent():
            original_dir.rename(self.root / "parked")
            self.workspace(original_dir.name)
            return True

        self.direct.side_effect = replace_parent
        with self.assert_refused("unsafe-storage"):
            self.route()
        self.direct.assert_called_once_with()
        self.assertFalse(self.entry.exists())
        with self.assert_refused("invalid-state"):
            self.route(self.root / "parked" / self.path.name)

    def test_unsafe_and_malformed_entries_never_call_the_stop_callback(self):
        self.enroll()
        valid = self.entry.read_bytes()
        for content in (b"", valid[:-1], valid + b"\n", bytes(4096)):
            self.entry.write_bytes(content)
            with self.assert_refused("invalid-state"):
                self.route()
        self.entry.unlink()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.entry))
            with self.assert_refused("unsafe-storage"):
                self.route()
        self.entry.unlink()
        os.mkfifo(self.entry, mode=0o600)
        with self.assert_refused("unsafe-storage"):
            self.route()
        self.entry.unlink()
        target = self.entry.with_name("other")
        target.write_bytes(valid)
        target.chmod(0o600)
        self.entry.symlink_to(target)
        with self.assert_refused("unsafe-storage"):
            self.route()
        self.entry.unlink()
        os.link(target, self.entry)
        with self.assert_refused("unsafe-storage"):
            self.route()
        target.unlink()
        self.entry.chmod(0o666)
        with self.assert_refused("unsafe-storage"):
            self.route()
        self.direct.assert_not_called()

    def test_io_failure_never_falls_through_to_direct_stop(self):
        self.enroll()
        with patch.object(fence.os, "fsync", side_effect=OSError("private-sentinel")):
            with self.assert_refused("unsafe-storage"):
                self.route()
        self.direct.assert_not_called()
        with self.assert_refused("direct-unverified"):
            self.route()
        self.path = self.workspace("armed-fixture")
        self.enroll()
        with box.workspace_lock(self.path), fence.arm(self.path) as owner:
            with patch.object(
                fence.os, "pwrite", side_effect=OSError("private-sentinel")
            ):
                with self.assert_refused("unsafe-storage"):
                    self.route()
            self.assertFalse(owner.stop_requested())
            self.assertIs(self.route(), fence.StopRoute.PENDING)
            owner.complete(drained=True, stopped=True)
        self.direct.assert_not_called()

    def test_direct_stop_preserves_caller_exception_without_clearing_uncertainty(self):
        self.enroll()
        original = RuntimeError("fixture caller error")
        with self.assertRaises(RuntimeError) as raised:
            fence.route_stop(self.path, direct_stop=Mock(side_effect=original))
        self.assertIs(raised.exception, original)
        with self.assert_refused("direct-unverified"):
            self.route(timeout=0)
        self.direct.assert_not_called()

    def test_direct_stop_requires_literal_confirmation_to_restore_unarmed(self):
        self.enroll()
        self.direct.return_value = 1
        with self.assert_refused("direct-unverified"):
            self.route()
        with self.assert_refused("direct-unverified"):
            self.route()
        self.direct.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
