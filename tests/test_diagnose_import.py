"""Synthetic diagnostic output contracts; no workspace or native commands."""

import contextlib
import copy
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import oc_box as box
from tools import diagnose_import as diagnostic

NONFINITE_FLOATS = (
    ("nan", float("nan")),
    ("positive-infinity", float("inf")),
    ("negative-infinity", float("-inf")),
    ("json-nan", json.loads("NaN")),
    ("json-positive-infinity", json.loads("Infinity")),
    ("json-negative-infinity", json.loads("-Infinity")),
    ("json-exponent-overflow", json.loads("1e309")),
    ("json-negative-exponent-overflow", json.loads("-1e309")),
)


class DiagnoseImportTests(unittest.TestCase):
    def setUp(self):
        self.workspace = "a" * 32
        self.session = "ses_explicit_selection"
        self.arguments = [
            "--live",
            "--workspace",
            self.workspace,
            "--session",
            self.session,
        ]
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
        if errors.getvalue():
            self.assert_safe_failure(errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def assert_safe_failure(self, encoded):
        self.assert_private_absent(encoded)
        report = json.loads(encoded)
        expected = {"primary_failure", "secondary_failures"}
        if report["primary_failure"]["category"] == "invalid-options":
            expected.add("guidance")
            self.assertTrue(report["guidance"] == diagnostic.OPTIONS_GUIDANCE)
        self.assertTrue(set(report) == expected, "failure JSON has unexpected fields")
        self.assertLessEqual(len(report["secondary_failures"]), 2)
        for failure in [report["primary_failure"], *report["secondary_failures"]]:
            self.assertTrue(set(failure) == {"stage", "category"})
            self.assertTrue(
                failure["stage"]
                in {
                    "options",
                    "backend",
                    "workspace",
                    "read-source",
                    "read-container",
                    "validate-source",
                    "workspace-lock",
                    "freshness",
                    "inspect",
                    "start",
                    "export",
                    "stop",
                    "workspace-unlock",
                    "compare",
                    "report",
                },
                "failure stage must be fixed",
            )
            self.assertTrue(
                failure["category"]
                in {
                    "invalid-options",
                    "interrupted",
                    "box-error",
                    "timeout",
                    "subprocess-exit",
                    "os-error",
                    "invalid-data",
                    "unexpected-error",
                },
                "failure category must be fixed",
            )

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

    def test_nonfinite_float_comparison_values_are_refused_in_both_operands(self):
        for case, value in NONFINITE_FLOATS:
            for side in ("source", "container"):
                with self.subTest(case=case, side=side):
                    invalid = [{"number": value}]
                    left, right = (invalid, []) if side == "source" else ([], invalid)
                    with self.assertRaises(ValueError) as raised:
                        diagnostic.differences(left, right)
                    self.assertTrue(
                        str(raised.exception) == "Invalid comparison data",
                        "nonfinite values require a fixed validation error",
                    )
        for value in (0.0, -0.0, 1.5, 1e308, -1e308):
            with self.subTest(finite=value):
                self.assertEqual(diagnostic.differences(value, value), [])

    def test_nonfinite_float_transcripts_have_fixed_invalid_data_cli_errors(self):
        baseline = copy.deepcopy(self.source)
        for case, value in NONFINITE_FLOATS:
            for side in ("source", "container"):
                with self.subTest(case=case, side=side):
                    self.source = copy.deepcopy(baseline)
                    self.actual = copy.deepcopy(baseline)
                    target = self.source if side == "source" else self.actual
                    target["messages"][0]["parts"][0]["state"]["input"]["number"] = (
                        value
                    )
                    code, output, errors = self.invoke()
                    self.assert_private_absent(output, errors)
                    self.assertEqual(code, 2)
                    self.assertTrue(
                        output == "", "invalid data must not emit a count report"
                    )
                    self.assertTrue(
                        json.loads(errors)["primary_failure"]["category"]
                        == "invalid-data",
                        "nonfinite values require the fixed invalid-data category",
                    )
        self.fake.podman.assert_not_called()
        self.fake.stop_container.assert_not_called()

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
                self.assertEqual(code, 1)
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
                self.assertEqual(code, 2)
                self.assertTrue(output == "", "failure must not emit a count report")
                self.assertTrue(
                    json.loads(errors)["primary_failure"]["category"] == category,
                    "error output must contain only fixed categories",
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
                    self.source["messages"][0]["content"] = []
                    self.actual = copy.deepcopy(self.source)
                elif case == "source":
                    self.source["messages"] = None
                code, output, errors = self.invoke()
                self.assert_private_absent(output, errors)
                self.assertEqual(code, 2)
                self.assertTrue(output == "", "failure must not emit a count report")
                expected = "box-error" if case == "source" else "invalid-data"
                self.assertTrue(
                    json.loads(errors)["primary_failure"]["category"] == expected,
                    "malformed data requires a fixed category",
                )

    def test_invalid_cli_arguments_never_echo_private_input(self):
        cases = (
            ("unknown-option", [*self.arguments, "--" + self.private[0]], 2),
            ("missing-value", ["--workspace"], 2),
            (
                "invalid-workspace",
                ["--workspace", self.private[1], "--session", self.session],
                2,
            ),
            (
                "invalid-session",
                ["--workspace", self.workspace, "--session", self.private[2]],
                2,
            ),
        )
        for case, arguments, expected_code in cases:
            with self.subTest(case=case):
                code, output, errors = self.invoke(arguments)
                self.assert_private_absent(output, errors)
                self.assertEqual(code, expected_code)
                self.assertTrue(output == "", "invalid options must not emit a report")
                category = "invalid-options"
                self.assertTrue(
                    json.loads(errors)["primary_failure"]["category"] == category,
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
            json.loads(errors)["primary_failure"]["category"] == "interrupted",
            "interruption requires a fixed category",
        )

    def offline_arguments(self, root):
        source, actual = root / "source.json", root / "actual.json"
        source.write_text(json.dumps(self.source), encoding="utf-8")
        actual.write_text(json.dumps(self.actual), encoding="utf-8")
        return [
            "--source-export",
            str(source),
            "--container-export",
            str(actual),
            "--session",
            self.session,
        ]

    def test_offline_actual_script_entrypoint_match_mismatch_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self.offline_arguments(root)
            originals = {path: path.read_bytes() for path in root.iterdir()}
            for case, expected in (("match", 0), ("mismatch", 1), ("malformed", 2)):
                with self.subTest(case=case):
                    if case == "mismatch":
                        self.actual["messages"][0]["parts"][0]["state"]["input"] = {}
                        arguments = self.offline_arguments(root)
                    elif case == "malformed":
                        (root / "actual.json").write_text(
                            self.private[0], encoding="utf-8"
                        )
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            str(Path(diagnostic.__file__)),
                            *arguments,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        env={
                            "PATH": os.defpath,
                            "HOME": directory,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                    )
                    self.assert_private_absent(result.stdout, result.stderr)
                    self.assertEqual(result.returncode, expected)
                    report = json.loads(
                        result.stderr if expected == 2 else result.stdout
                    )
                    if expected == 2:
                        self.assert_safe_failure(result.stderr)
                        self.assertEqual(
                            report["primary_failure"]["category"], "invalid-data"
                        )
                    else:
                        self.assertEqual(report["transcript_verified"], expected == 0)
                    if case == "match":
                        self.assertEqual(
                            {path: path.read_bytes() for path in root.iterdir()},
                            originals,
                        )
            self.fake.check_backend.assert_not_called()

    def test_offline_default_has_no_engine_lookup_lock_export_or_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = self.offline_arguments(Path(directory))
            code, output, errors = self.invoke(arguments)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["transcript_verified"])
        self.assertEqual(errors, "")
        for name in (
            "check_backend",
            "find_workspace",
            "read_json",
            "workspace_lock",
            "inspect_owned",
            "export_container_session",
            "podman",
            "stop_container",
        ):
            getattr(self.fake, name).assert_not_called()

    def test_old_invocation_requires_fixed_live_opt_in_guidance(self):
        code, output, errors = self.invoke(self.arguments[1:])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        report = json.loads(errors)
        self.assertEqual(report["primary_failure"]["category"], "invalid-options")
        self.assertIn("--live", report["guidance"])
        self.assert_private_absent(errors)
        self.fake.check_backend.assert_not_called()

    def test_live_primary_and_stop_secondary_errors_are_separate(self):
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        self.fake.podman.side_effect = None
        primary = subprocess.CalledProcessError(
            19, self.private, stderr=" ".join(self.private)
        )
        self.fake.export_container_session.side_effect = primary
        self.fake.stop_container.side_effect = OSError(" ".join(self.private))
        code, output, errors = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        report = json.loads(errors)
        self.assertEqual(
            report["primary_failure"],
            {"stage": "export", "category": "subprocess-exit"},
        )
        self.assertEqual(
            report["secondary_failures"], [{"stage": "stop", "category": "os-error"}]
        )
        self.assert_private_absent(output, errors)
        self.fake.stop_container.assert_called_once_with(self.data)

    def test_successful_live_body_stop_failure_even_inside_caller_except_is_error(self):
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        self.fake.podman.side_effect = None
        self.fake.stop_container.side_effect = OSError(" ".join(self.private))
        try:
            raise RuntimeError("handled caller exception")
        except RuntimeError:
            code, output, errors = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(
            json.loads(errors)["primary_failure"],
            {"stage": "stop", "category": "os-error"},
        )

    def test_live_partial_start_failure_attempts_restore_without_replacing_primary(
        self,
    ):
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        primary = box.BoxError(" ".join(self.private))
        self.fake.podman.side_effect = primary
        self.fake.stop_container.side_effect = None
        with self.assertRaises(box.BoxError) as raised:
            diagnostic.main(self.arguments)
        self.assertIs(raised.exception, primary)
        self.fake.stop_container.assert_called_once_with(self.data)
        self.fake.export_container_session.assert_not_called()

    def test_live_cancellation_identity_survives_stop_failure_in_actual_main(self):
        for primary in (KeyboardInterrupt(" ".join(self.private)), SystemExit(73)):
            with self.subTest(kind=type(primary).__name__):
                self.fake.inspect_owned.return_value = {"State": {"Running": False}}
                self.fake.podman.side_effect = None
                self.fake.export_container_session.side_effect = primary
                self.fake.stop_container.side_effect = OSError(" ".join(self.private))
                with self.assertRaises(type(primary)) as raised:
                    diagnostic.main(self.arguments)
                self.assertIs(raised.exception, primary)

    def native_export(self):
        return {
            "info": {
                "id": self.session,
                "location": {"directory": self.data["project"]},
            },
            "messages": [
                {
                    "id": self.private[3],
                    "type": "assistant",
                    "content": [
                        {
                            "type": "tool",
                            "input": {
                                self.private[0]: {self.private[1]: self.private[2]}
                            },
                        }
                    ],
                }
            ],
        }

    def test_actual_live_script_uses_private_production_v2_export_transport(self):
        self.data["opencode_version"] = "2.2.6"
        self.source = self.native_export()
        for case in ("match", "mismatch", "malformed", "native-refusal", "interrupted"):
            with self.subTest(case=case):
                actual = copy.deepcopy(self.source)
                if case == "mismatch":
                    actual["messages"][0]["content"][0]["input"] = {}
                response = SimpleNamespace(
                    returncode=19 if case == "native-refusal" else 0,
                    stdout=self.private[0]
                    if case == "malformed"
                    else json.dumps(actual),
                    stderr=" ".join(self.private),
                )
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.object(box, "check_backend"),
                    patch.object(
                        box,
                        "find_workspace",
                        return_value=(
                            Path("/synthetic/registry/workspace.json"),
                            self.data,
                        ),
                    ),
                    patch.object(
                        box, "read_json", return_value=copy.deepcopy(self.source)
                    ),
                    patch.object(
                        box, "workspace_lock", return_value=contextlib.nullcontext()
                    ),
                    patch.object(
                        box, "inspect_owned", return_value={"State": {"Running": True}}
                    ),
                    patch.object(
                        box.workspace_service, "live_connection", return_value=None
                    ),
                    patch.object(
                        box,
                        "exec_command",
                        return_value=["synthetic-private-exec-never-run"],
                    ),
                    patch.object(box, "run", return_value=response) as transport,
                    patch.object(box, "podman") as podman,
                    patch.object(box, "stop_container") as stop,
                    patch.object(
                        sys, "argv", [str(Path(diagnostic.__file__)), *self.arguments]
                    ),
                    patch.object(sys, "path", list(sys.path)),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit) as exited,
                ):
                    if case == "interrupted":
                        transport.side_effect = KeyboardInterrupt(
                            " ".join(self.private)
                        )
                    runpy.run_path(str(Path(diagnostic.__file__)), run_name="__main__")
                expected = {
                    "match": 0,
                    "mismatch": 1,
                    "malformed": 2,
                    "native-refusal": 2,
                    "interrupted": 130,
                }[case]
                self.assertEqual(exited.exception.code, expected)
                self.assert_private_absent(output.getvalue(), errors.getvalue())
                report = json.loads(
                    errors.getvalue() if expected in (2, 130) else output.getvalue()
                )
                if expected in (0, 1):
                    self.assertEqual(report["transcript_verified"], expected == 0)
                    self.assert_positional_paths(report["first_differences"])
                else:
                    self.assertEqual(output.getvalue(), "")
                    self.assert_safe_failure(errors.getvalue())
                    self.assertEqual(report["primary_failure"]["stage"], "export")
                    self.assertEqual(
                        report["primary_failure"]["category"],
                        {
                            "malformed": "invalid-data",
                            "native-refusal": "box-error",
                            "interrupted": "interrupted",
                        }[case],
                    )
                transport.assert_called_once()
                self.assertTrue(transport.call_args.kwargs["private"])
                self.assertTrue(transport.call_args.kwargs["capture"])
                self.assertFalse(transport.call_args.kwargs["check"])
                self.assertIsNone(
                    json.loads(transport.call_args.kwargs["input"])["endpoint"]
                )
                podman.assert_not_called()
                stop.assert_not_called()

    def test_offline_v2_pair_directory_identity_format_and_tolerated_omission(self):
        for case in ("native-match", "directory", "identity", "format", "obsolete-raw"):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as directory,
            ):
                self.actual = copy.deepcopy(self.source)
                expected = 1
                if case == "native-match":
                    self.source = self.native_export()
                    self.actual = copy.deepcopy(self.source)
                    expected = 0
                elif case == "directory":
                    self.actual["info"]["location"]["directory"] = self.private[1]
                elif case == "identity":
                    self.actual["messages"][0]["id"] = "msg_alternative"
                elif case == "format":
                    self.actual = {
                        "info": {"id": self.session, "directory": self.data["project"]},
                        "messages": [],
                    }
                else:
                    self.source = {
                        "info": {"id": self.session, "directory": self.data["project"]},
                        "messages": [
                            {
                                "info": {
                                    "id": self.private[3],
                                    "sessionID": self.session,
                                },
                                "parts": [
                                    {
                                        "id": self.private[4],
                                        "sessionID": self.session,
                                        "messageID": self.private[3],
                                        "type": "tool",
                                        "state": {
                                            "status": "completed",
                                            "raw": self.private[2],
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                    self.actual = copy.deepcopy(self.source)
                    del self.actual["messages"][0]["parts"][0]["state"]["raw"]
                    expected = 0
                code, output, errors = self.invoke(
                    self.offline_arguments(Path(directory))
                )
                self.assertEqual(code, expected)
                self.assertEqual(errors, "")
                self.assert_private_absent(output)
                report = json.loads(output)
                self.assertEqual(report["transcript_verified"], expected == 0)
                if case == "obsolete-raw":
                    self.assertFalse(report["transcript_exact_match"])
                    self.assertEqual(report["obsolete_tool_raw_buffers_omitted"], 1)

    def test_offline_readonly_inputs_and_read_failures_never_write_or_fall_back_live(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self.offline_arguments(root)
            originals = {path: path.read_bytes() for path in root.iterdir()}
            for path in root.iterdir():
                path.chmod(0o400)
            code, output, errors = self.invoke(arguments)
            self.assertEqual(code, 0)
            self.assertEqual(errors, "")
            self.assertEqual(
                {path: path.read_bytes() for path in root.iterdir()}, originals
            )
            self.assertEqual(len(list(root.iterdir())), 2)
            for case, error, category in (
                ("missing", FileNotFoundError(self.private[1]), "os-error"),
                ("denied", PermissionError(self.private[1]), "os-error"),
                (
                    "invalid-utf8",
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, self.private[0]),
                    "invalid-data",
                ),
            ):
                for failing_read in (1, 2):
                    with self.subTest(case=case, read=failing_read):
                        effects = (
                            [error]
                            if failing_read == 1
                            else [json.dumps(self.source), error]
                        )
                        with patch.object(Path, "read_text", side_effect=effects):
                            code, output, errors = self.invoke(arguments)
                        self.assertEqual(code, 2)
                        self.assertEqual(output, "")
                        self.assertEqual(
                            json.loads(errors)["primary_failure"]["category"], category
                        )
                        self.assert_private_absent(output, errors)
        self.fake.check_backend.assert_not_called()
        self.fake.export_container_session.assert_not_called()

    def test_live_stopped_success_restores_and_running_success_never_stops(self):
        for running in (False, True):
            with self.subTest(running=running):
                self.fake.inspect_owned.return_value = {"State": {"Running": running}}
                self.fake.podman.reset_mock(side_effect=True)
                self.fake.stop_container.reset_mock(side_effect=True)
                code, output, errors = self.invoke()
                self.assertEqual(code, 0)
                self.assertEqual(errors, "")
                self.assertTrue(json.loads(output)["transcript_verified"])
                if running:
                    self.fake.podman.assert_not_called()
                    self.fake.stop_container.assert_not_called()
                else:
                    self.fake.podman.assert_called_once_with(
                        "start", self.data["container"], capture=True, private=True
                    )
                    self.fake.stop_container.assert_called_once_with(self.data)

    def test_lock_finalizer_failure_preserves_primary_identity_and_fails_success(self):
        class FailingLock:
            def __enter__(self):
                return self

            def __exit__(lock, *arguments):
                raise OSError(" ".join(self.private))

        self.fake.workspace_lock.side_effect = lambda path: FailingLock()
        primary = box.BoxError(" ".join(self.private))
        self.fake.export_container_session.side_effect = primary
        state = {"stage": "options", "secondary_failures": []}
        with self.assertRaises(box.BoxError) as raised:
            diagnostic.main(self.arguments, state=state)
        self.assertIs(raised.exception, primary)
        self.assertEqual(state["stage"], "export")
        self.assertEqual(
            state["secondary_failures"],
            [{"stage": "workspace-unlock", "category": "os-error"}],
        )
        self.fake.export_container_session.side_effect = lambda data, session: (
            copy.deepcopy(self.actual)
        )
        try:
            raise RuntimeError("handled caller exception")
        except RuntimeError:
            code, output, errors = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(
            json.loads(errors)["primary_failure"],
            {"stage": "workspace-unlock", "category": "os-error"},
        )

    def test_live_interruption_is_primary_over_stop_and_unlock_errors(self):
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        self.fake.podman.side_effect = None
        self.fake.export_container_session.side_effect = KeyboardInterrupt(
            " ".join(self.private)
        )
        self.fake.stop_container.side_effect = OSError(" ".join(self.private))

        @contextlib.contextmanager
        def lock(path):
            try:
                yield
            finally:
                raise ValueError(" ".join(self.private))

        self.fake.workspace_lock.side_effect = lock
        code, output, errors = self.invoke()
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        report = json.loads(errors)
        self.assertEqual(
            report["primary_failure"], {"stage": "export", "category": "interrupted"}
        )
        self.assertEqual(
            report["secondary_failures"],
            [
                {"stage": "stop", "category": "os-error"},
                {"stage": "workspace-unlock", "category": "invalid-data"},
            ],
        )
        self.assert_private_absent(errors)

    def test_invalid_mode_combinations_and_unobserved_running_state_refuse_effects(
        self,
    ):
        for arguments in (
            ["--live", "--session", self.session],
            [*self.arguments, "--source-export", self.private[1]],
            ["--session", self.session, "--source-export", self.private[1]],
            ["--session", self.session, "--container-export", self.private[1]],
        ):
            with self.subTest(arguments_count=len(arguments)):
                code, output, errors = self.invoke(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(
                    json.loads(errors)["primary_failure"]["category"], "invalid-options"
                )
        self.fake.check_backend.assert_not_called()
        for state in (
            None,
            {},
            {"State": None},
            {"State": {"Running": 1}},
            {"State": {"Running": "false"}},
        ):
            with self.subTest(state_kind=type(state).__name__):
                self.fake.inspect_owned.return_value = state
                code, output, errors = self.invoke()
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(
                    json.loads(errors)["primary_failure"]["stage"], "inspect"
                )
        self.fake.podman.assert_not_called()
        self.fake.stop_container.assert_not_called()
        self.fake.export_container_session.assert_not_called()

    def test_repropagated_primary_identity_is_not_a_new_cleanup_failure(self):
        primary = box.BoxError(" ".join(self.private))

        class RepropagatingLock:
            def __enter__(self):
                return self

            def __exit__(self, kind, error, traceback):
                raise error

        self.fake.workspace_lock.side_effect = lambda path: RepropagatingLock()
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        self.fake.podman.side_effect = None
        self.fake.export_container_session.side_effect = primary
        self.fake.stop_container.side_effect = primary
        state = {"stage": "options", "secondary_failures": []}
        with self.assertRaises(box.BoxError) as raised:
            diagnostic.main(self.arguments, state=state)
        self.assertIs(raised.exception, primary)
        self.assertEqual(state["secondary_failures"], [])

    def test_locked_freshness_refuses_completed_intervening_changes_before_native_io(
        self,
    ):
        original_data, original_source = (
            copy.deepcopy(self.data),
            copy.deepcopy(self.source),
        )
        for case in (
            "unchanged",
            "version",
            "identity",
            "container",
            "registration-path",
            "source",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                registration = root / "workspace.json"
                import_path = root / "imports" / (self.session + ".json")
                import_path.parent.mkdir()
                registration.write_text(json.dumps(original_data), encoding="utf-8")
                import_path.write_text(json.dumps(original_source), encoding="utf-8")
                replacement = copy.deepcopy(original_data)
                replacement_source = copy.deepcopy(original_source)
                if case == "version":
                    replacement["opencode_version"] = "2.2.6"
                elif case == "identity":
                    replacement["project_identity"] = [991, 882]
                elif case == "container":
                    replacement["container"] = "replacement-never-executed"
                elif case == "source":
                    replacement_source["messages"][0]["parts"][0]["state"]["input"] = {}
                entered = False
                observations = []

                def discover(**options):
                    selected = (
                        root / "replacement.json"
                        if entered and case == "registration-path"
                        else registration
                    )
                    observations.append((entered, selected))
                    return selected, json.loads(
                        registration.read_text(encoding="utf-8")
                    )

                @contextlib.contextmanager
                def lock(path):
                    nonlocal entered
                    # A different owner completed its update before our lock was
                    # acquired: use independently decoded/replaced file contents.
                    registration.write_text(json.dumps(replacement), encoding="utf-8")
                    import_path.write_text(
                        json.dumps(replacement_source), encoding="utf-8"
                    )
                    entered = True
                    yield

                self.fake.find_workspace.side_effect = discover
                self.fake.read_json.side_effect = lambda path: json.loads(
                    path.read_text(encoding="utf-8")
                )
                self.fake.workspace_lock.side_effect = lock
                for name in (
                    "check_backend",
                    "inspect_owned",
                    "podman",
                    "export_container_session",
                    "stop_container",
                ):
                    getattr(self.fake, name).reset_mock()
                code, output, errors = self.invoke()
                if case == "unchanged":
                    self.assertEqual(code, 0)
                    self.assertTrue(json.loads(output)["transcript_verified"])
                    self.assertEqual(errors, "")
                    self.assertEqual(
                        observations, [(False, registration), (True, registration)]
                    )
                    self.fake.inspect_owned.assert_called_once()
                    self.fake.check_backend.assert_called_once()
                    self.fake.export_container_session.assert_called_once()
                else:
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertEqual(
                        json.loads(errors)["primary_failure"],
                        {"stage": "freshness", "category": "box-error"},
                    )
                    for name in (
                        "check_backend",
                        "inspect_owned",
                        "podman",
                        "export_container_session",
                        "stop_container",
                    ):
                        getattr(self.fake, name).assert_not_called()
                self.assertEqual(
                    json.loads(registration.read_text(encoding="utf-8")), replacement
                )
                self.assertEqual(
                    json.loads(import_path.read_text(encoding="utf-8")),
                    replacement_source,
                )
                self.assertEqual(
                    sorted(
                        path.relative_to(root).as_posix()
                        for path in root.rglob("*")
                        if path.is_file()
                    ),
                    ["imports/" + self.session + ".json", "workspace.json"],
                )
                self.assertEqual(self.data, original_data)
                self.assertEqual(self.source, original_source)

    def test_explicit_empty_export_option_is_mixed_mode_refusal_before_backend(self):
        for option in ("--source-export", "--container-export"):
            with self.subTest(option=option):
                code, output, errors = self.invoke([*self.arguments, option, ""])
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(
                    json.loads(errors)["primary_failure"],
                    {"stage": "options", "category": "invalid-options"},
                )
        self.fake.check_backend.assert_not_called()
        self.fake.find_workspace.assert_not_called()

    def test_malformed_live_export_remains_primary_over_stop_failure(self):
        self.fake.inspect_owned.return_value = {"State": {"Running": False}}
        self.fake.podman.side_effect = None
        self.fake.export_container_session.side_effect = None
        self.fake.export_container_session.return_value = {
            self.private[0]: self.private[1]
        }
        self.fake.stop_container.side_effect = OSError(" ".join(self.private))
        code, output, errors = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        report = json.loads(errors)
        self.assertEqual(
            report["primary_failure"], {"stage": "export", "category": "box-error"}
        )
        self.assertEqual(
            report["secondary_failures"], [{"stage": "stop", "category": "os-error"}]
        )

    def test_malformed_native_content_is_operational_failure_before_live_effects(self):
        self.source = self.native_export()
        for content in (None, self.private[0], {self.private[1]: self.private[2]}):
            with self.subTest(content_kind=type(content).__name__):
                self.source["messages"][0]["content"] = content
                self.actual = copy.deepcopy(self.source)
                code, output, errors = self.invoke()
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(
                    json.loads(errors)["primary_failure"],
                    {"stage": "validate-source", "category": "invalid-data"},
                )
        self.fake.inspect_owned.assert_not_called()
        self.fake.podman.assert_not_called()
        self.fake.stop_container.assert_not_called()
        self.fake.export_container_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
