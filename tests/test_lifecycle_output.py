"""Verify CLI reporting integration without starting an actual workspace."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import activity_log, journal_output, lifecycle_output
from oc_box import oc_box as box

WORKSPACE = "a" * 32


class LifecycleOutputTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory(prefix="oc-box-lifecycle-output-")
        self.addCleanup(area.cleanup)
        self.root = Path(area.name) / "state"
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_CONTAINER": "0",
                "OC_BOX_STATE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(Path(area.name) / "config"),
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_scoped_report_context_resets_after_error_and_nested_invocation(self):
        self.assertFalse(lifecycle_output.concise())
        with lifecycle_output.invocation("start", self.root):
            self.assertTrue(lifecycle_output.concise())
            lifecycle_output.bind_workspace(WORKSPACE)
            with (
                self.assertRaises(RuntimeError),
                lifecycle_output.invocation("sync", self.root, verbose=True),
            ):
                self.assertFalse(lifecycle_output.concise())
                self.assertIsNone(lifecycle_output.workspace())
                raise RuntimeError("synthetic")
            self.assertEqual(lifecycle_output.workspace(), WORKSPACE)
        self.assertFalse(lifecycle_output.concise())
        self.assertIsNone(lifecycle_output.workspace())
        self.assertFalse(self.root.exists())

    def test_entrypoint_enables_concise_output_and_logs_only_allowlisted_metadata(self):
        for verbose in ("0", "1"):
            with self.subTest(verbose=verbose):

                def run_start(verbose=verbose):
                    self.assertIs(lifecycle_output.concise(), verbose == "0")
                    lifecycle_output.bind_workspace(WORKSPACE)
                    display = io.StringIO()
                    with box.loading_stage(
                        "PRIVATE_STAGE_LABEL", stage="configuration", stream=display
                    ):
                        pass
                    self.assertIs(bool(display.getvalue()), verbose == "1")
                    # The diagnostic input is not a free-form message sink.
                    lifecycle_output.record(
                        "history.result", level="warn", reason="DO_NOT_PERSIST"
                    )
                    return 0

                with (
                    patch.dict(os.environ, {"OC_BOX_LIFECYCLE_DIAGNOSTICS": verbose}),
                    patch.object(sys, "argv", ["oc-box", "start"]),
                    patch.object(box, "main", side_effect=run_start),
                ):
                    self.assertEqual(box.entrypoint(), 0)
                self.assertFalse(lifecycle_output.concise())

        # A real CLI read dispatches before any workspace/backend startup.
        output = io.StringIO()
        with (
            patch.object(
                box, "find_workspace", side_effect=AssertionError("workspace lookup")
            ),
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            patch.object(
                box, "launch_credentials", side_effect=AssertionError("vault")
            ),
            patch.object(sys, "argv", ["oc-box", "logs", "--json", "--level", "debug"]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(box.entrypoint(), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [row["event"] for row in records],
            ["command.started", "lifecycle.stage", "command.finished"] * 2,
        )
        self.assertEqual(len(activity_log.read_events(self.root)), 6)
        for event in records[1:3] + records[4:6]:
            self.assertEqual(event["workspace"], WORKSPACE)
        for secret in ("DO_NOT_PERSIST", "PRIVATE_STAGE_LABEL"):
            self.assertNotIn(secret, output.getvalue())

    def test_lifecycle_recording_defers_disk_writes_until_reporting_boundary(self):
        with (
            patch.object(activity_log, "record_event", return_value=True) as writer,
            lifecycle_output.invocation("sync", self.root),
        ):
            for count in range(65):
                lifecycle_output.record(
                    "history.pass", metrics={"sessions": count}, level="debug"
                )
            writer.assert_not_called()
            lifecycle_output.flush()
            self.assertEqual(writer.call_count, 64)
            self.assertEqual(
                writer.call_args_list[0].kwargs["metrics"], {"sessions": 1}
            )
            lifecycle_output.flush()
            self.assertEqual(writer.call_count, 64)

    def test_log_help_and_empty_read_never_create_state(self):
        with (
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as result:
                box.main(["logs", "--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertIn("--level", output.getvalue())
            output.seek(0)
            output.truncate()
            self.assertEqual(box.main(["logs", "--json"]), 0)
            self.assertEqual(output.getvalue(), "")
        self.assertFalse(self.root.exists())

    def test_journald_mirror_is_opt_in_and_cannot_fail_private_append(self):
        with patch.object(
            journal_output, "emit", side_effect=OSError("private")
        ) as emit:
            self.assertTrue(
                activity_log.record_event("command.started", command="sync")
            )
            emit.assert_not_called()
            with patch.dict(os.environ, {"OC_BOX_JOURNALD": "1"}):
                self.assertFalse(
                    activity_log.record_event("history.result", reason="DO_NOT_PERSIST")
                )
                emit.assert_not_called()
                self.assertTrue(
                    activity_log.record_event(
                        "history.result",
                        command="sync",
                        level="info",
                        outcome="success",
                    )
                )
                emit.assert_called_once()
                with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
                    self.assertTrue(activity_log.record_event("command.finished"))
                emit.assert_called_once()
        self.assertEqual(len(activity_log.read_events(self.root)), 3)

    def test_journald_cli_is_explicit_and_never_falls_back_to_private_records(self):
        with (
            patch.object(
                activity_log, "read_events", side_effect=AssertionError("file reader")
            ),
            patch.object(journal_output, "read_events", return_value=[]) as read,
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                box.main(
                    ["logs", "--journald", "--level", "warn", "--limit", "3", "--json"]
                ),
                0,
            )
            self.assertEqual(output.getvalue(), "")
            read.assert_called_once_with(level="warn", limit=3, workspace=None)
            failure = journal_output.JournalError("unavailable")
            failure.args = ("PRIVATE_SENTINEL",)
            read.side_effect = failure
            with self.assertRaisesRegex(
                box.BoxError, "system journal is unavailable"
            ) as raised:
                box.main(["logs", "--journald"])
            self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
        self.assertFalse(self.root.exists())

    def test_misordered_read_only_logs_invocation_does_not_append_or_mirror(self):
        with (
            patch.dict(os.environ, {"OC_BOX_JOURNALD": "1"}),
            patch.object(sys, "argv", ["oc-box", "--project", ".", "logs"]),
            patch.object(journal_output, "emit") as mirror,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(box.entrypoint(), 1)
            self.assertIn("Use 'oc-box logs' first", errors.getvalue())
        mirror.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_already_reported_history_failure_does_not_print_a_second_error(self):
        for error in (
            box.BoxError("PRIVATE_SENTINEL"),
            RuntimeError("PRIVATE_SENTINEL"),
        ):
            with self.subTest(error=type(error).__name__):
                error.history_outcome_reported = True
                with (
                    patch.object(sys, "argv", ["oc-box", "sync"]),
                    patch.object(box, "main", side_effect=error),
                    contextlib.redirect_stderr(io.StringIO()) as output,
                ):
                    self.assertEqual(box.entrypoint(), 1)
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
