import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

from oc_box import activity_log, oc_box as box, tui


class TuiCommandTests(unittest.TestCase):
    def test_demo_readings_are_fixed_and_never_use_live_collectors(self):
        demo = tui.DemoBackend()
        with (
            patch.object(box, "podman", side_effect=AssertionError("real engine")),
            patch(
                "oc_box.tui_backend.HostSampler.sample",
                side_effect=AssertionError("real host"),
            ),
        ):
            first, second = demo.collect(), demo.collect()
        self.assertEqual(first["host"], second["host"])
        self.assertEqual(
            first["workspaces"][0]["metrics"], second["workspaces"][0]["metrics"]
        )
        self.assertEqual(first["host"]["cpu_percent"], 24)

    def test_help_requires_no_tty_optional_dependencies_or_backend(self):
        with (
            patch.object(box, "check_backend", side_effect=AssertionError("backend")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as caught:
                box.main(["tui", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--read-only", output.getvalue())
        self.assertIn("--demo", output.getvalue())

    def test_real_dashboard_refuses_container_without_looking_up_workspaces(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("inventory")
            ),
        ):
            with self.assertRaisesRegex(box.BoxError, "on the host"):
                tui.cli(box, [])

    def test_dashboard_requires_tty_before_dependency_import(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(sys.stdin, "isatty", return_value=False),
        ):
            with self.assertRaisesRegex(box.BoxError, "TTY"):
                tui.cli(box, ["--demo"])

    def test_selectors_are_mutually_exclusive(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            tui.parser().parse_args(["--workspace", "a" * 32, "--project", "/project"])
        self.assertEqual(caught.exception.code, 2)

    def test_command_journal_records_categories_not_private_arguments_or_errors(self):
        with (
            patch.object(
                sys, "argv", ["oc-box", "start", "--model", "PRIVATE_ARGUMENT"]
            ),
            patch.object(box, "main", side_effect=box.BoxError("PRIVATE_ERROR")),
            patch.object(activity_log, "record_event", return_value=True) as record,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.entrypoint(), 1)
        self.assertEqual(
            [call.args[0] for call in record.call_args_list],
            ["command.started", "command.failed"],
        )
        self.assertNotIn("PRIVATE_ARGUMENT", repr(record.call_args_list))
        self.assertNotIn("PRIVATE_ERROR", repr(record.call_args_list))

    def test_logging_failure_does_not_change_success_and_demo_is_not_journaled(self):
        with (
            patch.object(sys, "argv", ["oc-box", "stop"]),
            patch.object(box, "main", return_value=0),
            patch.object(
                activity_log, "record_event", side_effect=OSError("PRIVATE_ERROR")
            ),
        ):
            self.assertEqual(box.entrypoint(), 0)
        with (
            patch.object(sys, "argv", ["oc-box", "tui", "--demo"]),
            patch.object(box, "main", return_value=0),
            patch.object(activity_log, "record_event") as record,
        ):
            self.assertEqual(box.entrypoint(), 0)
            record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
