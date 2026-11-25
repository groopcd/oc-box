"""Verify that the pass adapter uses one root/stop/lifecycle binding."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from oc_box import history_overlap_domains, workspace_control, workspace_service
from oc_box import history_overlap_writer
from oc_box import history_overlap_io, history_overlap_remote
from oc_box.oc_box import HistoryPending
from oc_box.history_overlap_host_env import private_host_archive_env
from oc_box.history_overlap_order import OverlapRefusal, Route
from oc_box.history_overlap_pass import (
    SourceOperations,
    bind_first_pass,
    quiet_source_operations,
)


class QuietSourceOperationsTests(unittest.TestCase):
    def setUp(self):
        self.data = {"container": "a" * 64}
        self.reply = b'E{"data":{"synthetic":true}}'
        self.route = Route("ses_input", "ses_archive_input")
        self.endpoint = {"url": "http://127.0.0.1:1234", "password": "synthetic-only"}
        self.lease = Mock(connection=self.endpoint)
        self.box = Mock(HistoryPending=HistoryPending)
        self.box.container_project.return_value = "/trial"
        self.events = []

        def hook(obj, name, **kwargs):
            part = patch.object(obj, name, **kwargs)
            self.addCleanup(part.stop)
            return part.start()

        hook(workspace_service, "quiet_history_source", return_value=self.lease)
        hook(workspace_service, "live_connection", return_value=self.endpoint)
        self.read = Mock()
        self.witness = Mock()
        self.start = hook(
            history_overlap_io.SourceRead, "start", side_effect=self.spawn
        )
        self.acquire = hook(
            history_overlap_remote.RemoteWitness, "acquire", side_effect=self.pin
        )
        self.read.wait_ready.return_value = b"ready"
        self.read.go.side_effect = self.go
        self.read.finish.side_effect = lambda: self.outcome("finish")
        self.read.cancel.side_effect = lambda: self.outcome("cancel")
        self.witness.observe.side_effect = self.observe
        self.read.response.side_effect = self.response
        self.adapter = quiet_source_operations(
            self.box,
            self.lease,
            self.data,
            generation="synthetic",
            probe=Mock(),
            command=lambda _: (["program"], {"PATH": "/synthetic"}),
            cancelled=lambda: False,
            deadline=time.monotonic() + 10,
        )

    @contextmanager
    def gate(self, end=None):
        self.events.append("gate")
        yield

    def spawn(self, argv, *, attempt, env, payload, admission, **bounds):
        self.assertNotIn(self.endpoint["password"], repr((argv, env)))
        self.assertEqual(json.loads(payload)["endpoint"], self.endpoint)
        self.assertTrue(bounds["two_phase"])
        self.attempt = attempt
        with admission(time.monotonic() + 1):
            self.events.append("spawn")
        return self.read

    def pin(self, attempt, ready, **_):
        self.assertIs(attempt, self.attempt)
        self.assertEqual(ready, b"ready")
        self.events.append("pin")
        return self.witness

    def go(self, witness, *, admission):
        self.assertIs(witness, self.witness)
        with admission(time.monotonic() + 1):
            self.events.append("go")

    def outcome(self, name):
        self.events.append(name)
        return history_overlap_io.SourceOutcome(
            "local-complete",
            True,
            0,
            True,
            True,
            True,
            time.monotonic_ns(),
            self.attempt,
        )

    def observe(self, **_):
        self.events.append("observe")
        return history_overlap_io.HelperAbsent(self.attempt, time.monotonic_ns())

    def response(self, proof):
        self.assertIs(proof.attempt, self.attempt)
        self.events.append("response")
        return self.reply

    def test_inventory_commits_only_after_verified_retirement_and_fresh_gate(self):
        self.reply = b'I[{"id":"ses_input"}]'
        self.adapter.inventory("path", self.data, gate=self.gate)
        self.assertEqual(
            self.events,
            [
                "gate",
                "spawn",
                "pin",
                "gate",
                "go",
                "finish",
                "observe",
                "response",
                "gate",
            ],
        )
        self.box.refresh_sessions.assert_called_once_with(
            "path",
            self.data,
            inventory=[{"id": "ses_input"}],
        )

    def test_local_success_without_remote_absence_refuses_read_and_replay(self):
        self.witness.observe.side_effect = None
        self.witness.observe.return_value = None
        handle = self.adapter.begin(self.route, gate=self.gate)
        with self.assertRaises(OverlapRefusal) as failure:
            self.adapter.receive(self.route, handle)
        self.assertTrue(failure.exception.history_source_cleanup_failed)
        self.assertFalse(self.adapter.cancel(self.route, handle))
        self.read.response.assert_not_called()
        self.start.assert_called_once()

    def test_pending_is_preserved_and_can_continue_after_exact_retirement(self):
        self.reply = b"U"
        with self.assertRaises(HistoryPending):
            self.adapter.read(self.route, gate=self.gate)
        self.reply = b'E{"data":{"synthetic":true}}'
        self.assertEqual(
            self.adapter.read(self.route, gate=self.gate), {"synthetic": True}
        )

    def test_failed_witness_never_sends_go_or_accepts_local_exit_as_cleanup(self):
        self.acquire.side_effect = history_overlap_remote.RemoteWitnessError(
            "synthetic"
        )
        with self.assertRaisesRegex(OverlapRefusal, "source-witness") as failure:
            self.adapter.begin(self.route, gate=self.gate)
        self.assertTrue(failure.exception.history_source_cleanup_failed)
        self.read.go.assert_not_called()
        self.read.cancel.assert_called_once()
        self.start.assert_called_once()


class FirstPassBindingTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        root = Path(private.name)
        self.path = root / "workspace.json"
        self.path.write_text('{"synthetic":true}\n')
        self.path.chmod(0o600)
        self.data = {"id": "a" * 32, "container": "b" * 64, "opencode_version": "2.4.1"}
        self.stop = workspace_control.capture(self.path)
        self.env = private_host_archive_env(
            "2.4.1",
            {"XDG_DATA_HOME": str(root / "data")},
            scratch=str(root / "scratch"),
        )
        self.domains = object.__new__(history_overlap_domains._Domains)
        self.domains.require_destination = Mock()
        self.actions = []
        token = workspace_service.LIFECYCLE_LOCK.set(object())
        self.addCleanup(workspace_service.LIFECYCLE_LOCK.reset, token)

    def action(self, name):
        def guarded(*_, gate):
            with gate():
                self.actions.append(name)
                return name

        return guarded

    def bound(self, **options):
        stub = patch.object(
            history_overlap_writer,
            "readback_once",
            side_effect=lambda *args, gate, deadline: self.action("readback")(
                gate=gate
            ),
        )
        stub.start()
        self.addCleanup(stub.stop)
        with patch.object(history_overlap_writer, "supports", return_value=True):
            return bind_first_pass(
                self.path,
                self.data,
                host=object(),
                env=self.env,
                domains=self.domains,
                source=SourceOperations(
                    self.action("inventory"),
                    self.action("read"),
                    self.action("begin"),
                    lambda *_: "received",
                    lambda *_: "retired",
                ),
                stop_token=self.stop,
                deadline=time.monotonic() + 10,
                **options,
            )

    def test_source_and_host_operations_share_stop_and_domain_checks(self):
        adapter = self.bound()
        route = Route("ses_input", "ses_archive_input")
        destination = adapter.destination(self.path, self.data, route, {}, None)
        self.assertEqual(adapter.inventory(self.path, self.data), "inventory")
        self.assertEqual(adapter.begin(route), "begin")
        self.assertEqual(destination.readback(route), "readback")
        with destination.publish_gate():
            self.actions.append("publish")
        workspace_control.request_stop(self.path)
        for operation in (adapter.begin, destination.readback):
            with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
                operation(route)
        self.assertEqual(self.actions, ["inventory", "begin", "readback", "publish"])
        self.assertEqual(self.domains.require_destination.call_count, 4)
        self.assertEqual(adapter.cancel(route, None), "retired")

    def test_changed_lifecycle_lock_is_not_inherited_by_the_next_phase(self):
        adapter = self.bound()
        token = workspace_service.LIFECYCLE_LOCK.set(object())
        try:
            with self.assertRaisesRegex(OverlapRefusal, "pass-lock"):
                adapter.read(Route("ses_input", "ses_archive_input"))
        finally:
            workspace_service.LIFECYCLE_LOCK.reset(token)
        self.assertEqual(self.actions, [])
        self.domains.require_destination.assert_not_called()

    def test_an_environment_guard_failure_cannot_start_a_child(self):
        adapter = self.bound()
        self.domains.require_destination.side_effect = (
            history_overlap_domains.DomainRefusal("synthetic drift")
        )
        with self.assertRaises(history_overlap_domains.DomainRefusal):
            adapter.begin(Route("ses_input", "ses_archive_input"))
        self.assertEqual(self.actions, [])

    def test_native_spawn_preflight_is_not_replayed_during_dispatch(self):
        preflight = Mock()
        uncertainty = Mock()
        adapter = self.bound(
            destination_preflight=preflight, destination_failed=uncertainty
        )
        route = Route("ses_input", "ses_archive_input")
        destination = adapter.destination(self.path, self.data, route, {}, None)

        def guarded(*_, gate, deadline):
            with gate():
                preflight.assert_called_once()
            with gate():
                preflight.assert_called_once()
            raise history_overlap_writer.WriterRefusal("cleanup", cleanup=True)

        with patch.object(history_overlap_writer, "create_once", side_effect=guarded):
            with self.assertRaises(history_overlap_writer.WriterRefusal):
                destination.create(None, route)
        uncertainty.assert_called_once()

    def test_stop_rotated_during_native_preflight_never_reaches_spawn(self):
        spawned = []
        adapter = self.bound(
            destination_preflight=lambda _deadline: workspace_control.request_stop(
                self.path
            )
        )
        route = Route("ses_input", "ses_archive_input")
        destination = adapter.destination(self.path, self.data, route, {}, None)

        def guarded(*_, gate, deadline):
            with gate():
                spawned.append("spawn")

        with patch.object(history_overlap_writer, "create_once", side_effect=guarded):
            with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
                destination.create(None, route)
        self.assertEqual(spawned, [])

    def test_source_generation_is_checked_before_worker_dispatch(self):
        source_generation = Mock(side_effect=OverlapRefusal("synthetic-generation"))
        adapter = self.bound(source_generation=source_generation)
        with self.assertRaisesRegex(OverlapRefusal, "synthetic-generation"):
            adapter.begin(Route("ses_input", "ses_archive_input"))
        source_generation.assert_called_once()
        self.assertEqual(self.actions, [])
