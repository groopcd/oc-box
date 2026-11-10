"""Synthetic diagnostic output contracts; no workspace or native commands."""

import contextlib
import copy
import io
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box
from tools import diagnose_import as diagnostic


class DiagnoseImportTests(unittest.TestCase):
    def setUp(self):
        self.workspace = "a" * 32
        self.session = "ses_explicit_selection"
        self.arguments = ["--workspace", self.workspace, "--session", self.session]
        self.private = (
            "SYNTHETIC_PRIVATE_KEY",
            "/synthetic/private/filename.txt",
            "synthetic-command --private-argument",
            "msg_implicit_private",
            "prt_implicit_private",
        )
        self.data = {
            "id": self.workspace,
            "project": "/synthetic/project",
            "container": "synthetic-container-never-executed",
            "opencode_version": "1.19.5",
        }
        self.source = {
            "info": {"id": self.session, "directory": self.data["project"]},
            "messages": [
                {
                    "info": {
                        "id": self.private[3],
                        "sessionID": self.session,
                        "role": "assistant",
                    },
                    "parts": [
                        {
                            "id": self.private[4],
                            "messageID": self.private[3],
                            "sessionID": self.session,
                            "type": "tool",
                            "state": {
                                "status": "completed",
                                "input": {key: key for key in self.private[:3]},
                            },
                        }
                    ],
                }
            ],
        }
        self.actual = copy.deepcopy(self.source)
        # All potentially live boundaries are replaced, including a final tripwire
        # on the real command runner. No synthetic path is opened or registered.
        self.commands = patch.object(
            box, "run", side_effect=AssertionError("unexpected command boundary")
        )
        runner = self.commands.start()
        self.addCleanup(self.commands.stop)
        self.addCleanup(runner.assert_not_called)
        self.fake = SimpleNamespace(
            BoxError=box.BoxError,
            valid_session_id=box.valid_session_id,
            opencode_compat=box.opencode_compat,
            check_backend=Mock(),
            find_workspace=Mock(
                return_value=(Path("/synthetic/registry/workspace.json"), self.data)
            ),
            read_json=Mock(side_effect=lambda path: copy.deepcopy(self.source)),
            validate_session_export=box.validate_session_export,
            workspace_lock=Mock(side_effect=lambda path: contextlib.nullcontext()),
            inspect_owned=Mock(return_value={"State": {"Running": True}}),
            export_container_session=Mock(
                side_effect=lambda data, session: copy.deepcopy(self.actual)
            ),
            verify_session_import=box.verify_session_import,
            container_project=box.container_project,
            podman=Mock(side_effect=AssertionError("unexpected lifecycle boundary")),
            stop_container=Mock(
                side_effect=AssertionError("unexpected lifecycle boundary")
            ),
        )
        patched = patch.object(diagnostic, "box", self.fake)
        patched.start()
        self.addCleanup(patched.stop)

    def invoke(self, arguments=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = diagnostic.cli(self.arguments if arguments is None else arguments)
        return code, output.getvalue(), errors.getvalue()

    def assert_private_absent(self, *values):
        # Do not dump captured diagnostic output when a fail-first assertion fires.
        encoded = "\n".join(values)
        self.assertFalse(
            any(value in encoded for value in self.private),
            "diagnostic output disclosed synthetic private data",
        )

    def assert_positional_paths(self, result):
        self.assertTrue(
            all(
                re.fullmatch(r"(?:/(?:field|item)/[0-9]+)*", item["path"])
                for item in result
            ),
            "difference paths must contain only fixed labels and positions",
        )
        self.assert_private_absent(json.dumps(result))

    def test_dictionary_keys_are_positional_for_added_removed_and_changed_fields(self):
        for case, left, right in (
            ("removed", {self.private[0]: 1}, {}),
            ("added", {}, {self.private[1]: 1}),
            ("changed", {self.private[2]: 1}, {self.private[2]: 2}),
            (
                "nested",
                {"parts": [{"state": {"input": {self.private[0]: 1}}}]},
                {"parts": [{"state": {"input": {self.private[0]: 2}}}]},
            ),
        ):
            with self.subTest(case=case):
                result = diagnostic.differences(left, right)
                self.assertTrue(result)
                self.assert_positional_paths(result)

    def test_scalar_values_types_and_list_counts_do_not_reveal_contents(self):
        for case, left, right in (
            ("value", self.private[0], self.private[1]),
            ("type", self.private[2], [self.private[0]]),
            ("length", [self.private[0]], []),
        ):
            with self.subTest(case=case):
                result = diagnostic.differences(left, right)
                self.assertTrue(result)
                self.assert_positional_paths(result)
        self.assertEqual(diagnostic.differences({"x": [1, None]}, {"x": [1, None]}), [])

    def test_difference_limits_and_paths_bound_serialized_output(self):
        left, right = [{"field": 1}] * 200, [{"field": 2}] * 200
        for limit, count in ((0, 0), (1, 1), (7, 7), (20, 20), (5000, 20)):
            with self.subTest(limit=limit):
                result = diagnostic.differences(left, right, limit=limit)
                self.assertEqual(len(result), count)
                self.assertLess(len(json.dumps(result)), 32768)
                self.assert_positional_paths(result)
        result = diagnostic.differences({self.private[0] + "x" * 100000: 1}, {})
        self.assertLess(len(json.dumps(result)), 256)
        self.assert_positional_paths(result)

    def test_malformed_comparison_inputs_and_untrusted_path_prefixes_are_refused(self):
        cycle = []
        cycle.append(cycle)
        deep_left, deep_right = 1, 2
        for _ in range(80):
            deep_left, deep_right = [deep_left], [deep_right]
        private_type = type(self.private[0], (), {})
        for case, left, right, options in (
            ("nonstring-key", {1: self.private[0]}, {}, {}),
            ("unsupported-type", private_type(), None, {}),
            ("cycle", cycle, [], {}),
            ("depth", deep_left, deep_right, {}),
            ("path-prefix", 1, 2, {"path": self.private[1]}),
            ("nonstring-path-prefix", 1, 2, {"path": private_type()}),
            ("negative-limit", 1, 2, {"limit": -1}),
            ("wrong-limit-type", 1, 2, {"limit": self.private[0]}),
        ):
            with self.subTest(case=case):
                with self.assertRaises(ValueError) as raised:
                    diagnostic.differences(left, right, **options)
                self.assert_private_absent(str(raised.exception))

    def test_v1_and_v2_reports_preserve_fields_without_payload_or_implicit_ids(self):
        fields = {
            "workspace",
            "session",
            "directory_matches",
            "source_messages",
            "container_messages",
            "source_parts",
            "container_parts",
            "message_ids_match",
            "transcript_exact_match",
            "transcript_verified",
            "obsolete_tool_raw_buffers_omitted",
            "first_differences",
        }
        for major in (1, 2):
            with self.subTest(major=major):
                if major == 2:
                    self.data["opencode_version"] = "2.2.6"
                    self.source = {
                        "info": {
                            "id": self.session,
                            "location": {"directory": self.data["project"]},
                        },
                        "messages": [
                            {
                                "id": self.private[3],
                                "type": "assistant",
                                "content": [
                                    {"input": {key: key for key in self.private[:3]}}
                                ],
                            }
                        ],
                    }
                self.actual = copy.deepcopy(self.source)
                message = self.actual["messages"][0]
                content = message["content"] if major == 2 else message["parts"]
                content[0]["input" if major == 2 else "state"] = {}
                code, output, errors = self.invoke()
                self.assertEqual(code, 0)  # I6 is deliberately left for Phase 3.
                self.assert_private_absent(output, errors)
                self.assertTrue(errors == "", "unexpected diagnostic error output")
                report = json.loads(output)
                self.assertEqual(set(report), fields)
                self.assertEqual(
                    (report["workspace"], report["session"]),
                    (self.workspace, self.session),
                )
                self.assertEqual(
                    (report["source_messages"], report["container_messages"]), (1, 1)
                )
                self.assertFalse(report["transcript_verified"])
                self.assert_positional_paths(report["first_differences"])
        self.fake.podman.assert_not_called()
        self.fake.stop_container.assert_not_called()

    def test_matching_transcript_retains_existing_count_and_verification_report(self):
        code, output, errors = self.invoke()
        self.assert_private_absent(output, errors)
        self.assertEqual(code, 0)
        self.assertTrue(errors == "", "unexpected diagnostic error output")
        report = json.loads(output)
        self.assertTrue(report["transcript_exact_match"])
        self.assertTrue(report["transcript_verified"])
        self.assertEqual(report["obsolete_tool_raw_buffers_omitted"], 0)
        self.assertEqual(report["first_differences"], [])

    def test_boundary_errors_emit_only_fixed_categories_on_stderr(self):
        class UnprintableError(box.BoxError):
            def __str__(self):
                raise AssertionError("exception formatting must not occur")

        private = " ".join(self.private)
        cases = (
            ("box", box.BoxError(private), "box-error"),
            ("unprintable", UnprintableError(), "box-error"),
            (
                "native",
                subprocess.CalledProcessError(
                    19, [private], output=private, stderr=private
                ),
                "subprocess-exit",
            ),
            (
                "timeout",
                subprocess.TimeoutExpired([private], 1, output=private, stderr=private),
                "timeout",
            ),
            ("os", OSError(private), "os-error"),
            ("value", ValueError(private), "invalid-data"),
            ("key", KeyError(private), "invalid-data"),
            ("type", TypeError(private), "invalid-data"),
            ("attribute", AttributeError(private), "invalid-data"),
            ("index", IndexError(private), "invalid-data"),
            ("recursion", RecursionError(private), "invalid-data"),
            ("unexpected", RuntimeError(private), "unexpected-error"),
            ("private-exit", SystemExit(private), "unexpected-error"),
        )
        for case, error, category in cases:
            with self.subTest(case=case):
                self.fake.export_container_session.side_effect = error
                code, output, errors = self.invoke()
                self.assert_private_absent(output, errors)
                self.assertEqual(code, 1)
                self.assertTrue(output == "", "failure must not emit a count report")
                self.assertTrue(
                    errors == f"diagnose-import: category={category}\n",
                    "error output must be a single fixed-category line",
                )

    def test_malformed_native_content_and_container_state_have_safe_errors(self):
        for case in ("content", "state", "source"):
            with self.subTest(case=case):
                self.fake.inspect_owned.return_value = {"State": {"Running": True}}
                self.data["opencode_version"] = "2.2.6"
                self.source = {
                    "info": {
                        "id": self.session,
                        "location": {"directory": self.data["project"]},
                    },
                    "messages": [
                        {"id": self.private[3], "type": "assistant", "content": None}
                    ],
                }
                self.actual = copy.deepcopy(self.source)
                if case == "state":
                    self.fake.inspect_owned.return_value = {"State": None}
                elif case == "source":
                    self.source["messages"] = None
                code, output, errors = self.invoke()
                self.assert_private_absent(output, errors)
                self.assertEqual(code, 1)
                self.assertTrue(output == "", "failure must not emit a count report")
                expected = "box-error" if case == "source" else "invalid-data"
                self.assertTrue(
                    errors == f"diagnose-import: category={expected}\n",
                    "malformed data requires a fixed category",
                )

    def test_invalid_cli_arguments_never_echo_private_input(self):
        cases = (
            ("unknown-option", [*self.arguments, "--" + self.private[0]], 2),
            ("missing-value", ["--workspace"], 2),
            (
                "invalid-workspace",
                ["--workspace", self.private[1], "--session", self.session],
                1,
            ),
            (
                "invalid-session",
                ["--workspace", self.workspace, "--session", self.private[2]],
                1,
            ),
        )
        for case, arguments, expected_code in cases:
            with self.subTest(case=case):
                code, output, errors = self.invoke(arguments)
                self.assert_private_absent(output, errors)
                self.assertEqual(code, expected_code)
                self.assertTrue(output == "", "invalid options must not emit a report")
                category = "invalid-options" if expected_code == 2 else "box-error"
                self.assertTrue(
                    errors == f"diagnose-import: category={category}\n",
                    "invalid options/selectors require a fixed category",
                )
        self.fake.check_backend.assert_not_called()

    def test_help_and_interruption_do_not_dump_private_data(self):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exited,
        ):
            diagnostic.cli(["--help"])
        self.assertEqual(exited.exception.code, 0)
        self.fake.check_backend.assert_not_called()
        self.assert_private_absent(output.getvalue())
        self.fake.export_container_session.side_effect = KeyboardInterrupt(
            " ".join(self.private)
        )
        code, output, errors = self.invoke()
        self.assert_private_absent(output, errors)
        self.assertEqual(code, 130)
        self.assertTrue(output == "", "interruption must not emit a count report")
        self.assertTrue(
            errors == "diagnose-import: category=interrupted\n",
            "interruption requires a fixed category",
        )


if __name__ == "__main__":
    unittest.main()
