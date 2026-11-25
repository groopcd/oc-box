"""Exercise the real sync loop with an injected, synthetic admitted adapter."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    history_write_intent,
    oc_box as box,
    workspace_control,
    workspace_service,
)
from oc_box.history_overlap_writer import WriterRefusal
from oc_box.history_overlap_order import OverlapRefusal
from oc_box.history_overlap_transaction import DestinationCreate, FirstPass


IDS = ("ses_a", "ses_b", "ses_c")


class FirstOverlapLoopTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.workspace = Path(scratch.name) / ("a" * 32) / "workspace.json"
        self.project = str(Path(scratch.name) / "synthetic-project")
        self.data = {
            "id": "a" * 32,
            "container": "b" * 64,
            "project": self.project,
            "opencode_version": "2.4.1",
        }
        box.atomic_json(self.workspace, self.data)
        self.state = {"sessions": list(IDS)}
        self.events = []

    def scope(self):
        binding = box._QuietSyncBinding(self.data["container"], (), {})
        binding.start_verified = True
        token = box._QUIET_SYNC_BINDING.set(binding)
        self.addCleanup(box._QUIET_SYNC_BINDING.reset, token)
        token = workspace_service.LIFECYCLE_LOCK.set(object())
        self.addCleanup(workspace_service.LIFECYCLE_LOCK.reset, token)

    def adapter(
        self, *, pending=None, conflict=None, publication=contextlib.nullcontext
    ):
        def read(route):
            self.events.append(("read", route.source))
            if route.source == pending:
                raise box.HistoryPending("synthetic pending")
            return {
                "info": {
                    "id": route.source,
                    "location": {"directory": self.project},
                    "title": "Synthetic history",
                },
                "messages": [],
            }

        def begin(route):
            self.events.append(("begin", route.source))
            return object()

        def cancel(route, _handle):
            self.events.append(("cancel", route.source))
            return True

        def destination(path, _data, route, archived, target):
            def create(sealed, selected):
                self.events.append(("import", route.source))
                self.assertIs(selected, route)
                self.assertFalse(target.exists())
                self.assertEqual(
                    json.loads(Path(sealed.native_path()).read_bytes()), archived
                )
                return (
                    "ses_conflict_unverified"
                    if route.source == conflict
                    else route.archive
                )

            def readback(selected):
                self.events.append(("readback", selected.source))
                self.assertIs(selected, route)
                return {
                    **archived,
                    "info": {
                        **archived["info"],
                        "location": {"directory": str(path.parent / "controller")},
                    },
                }

            return DestinationCreate(create, readback, publication)

        return FirstPass(
            lambda *_: self.state,
            read,
            begin,
            lambda route, _handle: read(route),
            cancel,
            destination,
        )

    def run_pass(self, adapter):
        with (
            patch.object(
                workspace_service,
                "live_connection",
                return_value={"workspace": self.data["id"]},
            ),
            patch.object(
                workspace_service, "quiet_history_source", return_value=object()
            ),
            patch.object(box, "export_container_session") as serial,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                box.sync_sessions(self.workspace, self.data, first_overlap=adapter)
            finally:
                serial.assert_not_called()

    def test_existing_serial_route_is_not_implied_by_a_test_adapter(self):
        inventory = Mock()
        empty = Mock()
        with self.assertRaisesRegex(OverlapRefusal, "pass-binding"):
            self.run_pass(FirstPass(inventory, empty, empty, empty, empty, empty))
        inventory.assert_not_called()

    def test_native_destination_cleanup_uncertainty_is_not_a_plain_sync_failure(self):
        error = WriterRefusal("cleanup", attempted=True, uncertain=True, cleanup=True)
        self.assertEqual(box._history_failure_reason(error), "cleanup-unverified")

    def test_missing_local_wait_evidence_is_not_a_plain_sync_failure(self):
        timeout = subprocess.TimeoutExpired(["synthetic"], 1)
        timeout.history_local_cleanup_failed = True
        error = box.BoxError("synthetic preparation failure")
        error.__cause__ = timeout
        self.assertEqual(box._history_failure_reason(error), "cleanup-unverified")
        self.assertFalse(
            box.history_recovery.exit_retry_eligible(
                box,
                {"opencode_version": "2.3.0"},
                error,
            )
        )
        error.history_source_read_failed = True
        self.assertFalse(
            box.history_recovery.exit_retry_eligible(box, self.data, error)
        )

    def test_stop_before_reservation_leaves_no_write_intent_or_native_attempt(self):
        self.scope()
        baseline = workspace_control.capture(self.workspace)
        workspace_control.request_stop(self.workspace)

        @contextlib.contextmanager
        def publication():
            with workspace_control.admission(self.workspace):
                if not workspace_control.unchanged(self.workspace, baseline):
                    raise OverlapRefusal("stop-intent")
                yield

        with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
            self.run_pass(self.adapter(publication=publication))
        self.assertEqual(self.events, [("read", "ses_a")])
        with history_write_intent.Journal(
            self.workspace,
            self.data["id"],
            profile=box._overlap_journal_profile(self.data),
        ) as journal:
            self.assertIsNone(journal.load())

    def test_pending_read_preserves_order_and_independent_verified_progress(self):
        self.scope()
        with self.assertRaisesRegex(box.HistoryPending, "1 session"):
            self.run_pass(self.adapter(pending="ses_b"))
        self.assertEqual(
            self.events,
            [
                ("read", "ses_a"),
                ("begin", "ses_b"),
                ("import", "ses_a"),
                ("readback", "ses_a"),
                ("read", "ses_b"),
                ("read", "ses_c"),
                ("import", "ses_c"),
                ("readback", "ses_c"),
            ],
        )
        self.assertEqual(
            set(box.session_state(self.workspace)["synced"]), {"ses_a", "ses_c"}
        )
        with history_write_intent.Journal(
            self.workspace,
            self.data["id"],
            profile=box._overlap_journal_profile(self.data),
        ) as journal:
            self.assertIsNone(journal.load())

    def test_unverified_native_ack_does_not_publish_and_aborts_the_next_worker(self):
        self.scope()
        with self.assertRaisesRegex(
            OverlapRefusal, "destination-acknowledgement"
        ) as failure:
            self.run_pass(self.adapter(conflict="ses_b"))
        self.assertTrue(failure.exception.history_write_intent_unresolved)
        self.assertEqual(
            self.events,
            [
                ("read", "ses_a"),
                ("begin", "ses_b"),
                ("import", "ses_a"),
                ("readback", "ses_a"),
                ("read", "ses_b"),
                ("begin", "ses_c"),
                ("import", "ses_b"),
                ("cancel", "ses_c"),
            ],
        )
        self.assertEqual(set(box.session_state(self.workspace)["synced"]), {"ses_a"})
        self.assertEqual(
            len(list((self.workspace.parent / "history").glob("*.json"))), 1
        )
        with history_write_intent.Journal(
            self.workspace,
            self.data["id"],
            profile=box._overlap_journal_profile(self.data),
        ) as journal:
            attempt = journal.load()
        self.assertEqual(attempt.source_id, "ses_b")
        self.assertTrue(attempt.atomic_create)
