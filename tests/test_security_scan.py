"""Offline fixtures for the scanner report and release-gate contracts."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import check_security_scan as scan


COMMIT = "0123456789abcdef" * 2 + "01234567"
PRIVATE = "SYNTHETIC-PRIVATE <details>::warning::"
RULE = "py/" + PRIVATE
RULE_GUID = "aeadd3dd-5741-4479-a5f3-5ac6a5e92cd0"


def sarif(rules=(), results=(), **run_fields):
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "CodeQL", "rules": list(rules)}},
                "results": list(results),
                **run_fields,
            }
        ],
    }


def finding(**overrides):
    return {
        "ruleId": RULE,
        "message": {"text": PRIVATE},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": PRIVATE}}}],
        **overrides,
    }


def rule(**properties):
    return {"id": RULE, "guid": RULE_GUID, "properties": properties}


def pip_audit(*vulns):
    return {
        "dependencies": [{"name": PRIVATE, "version": "1.0", "vulns": list(vulns)}],
        "fixes": [],
    }


class CodeQLParserTests(unittest.TestCase):
    def test_security_severity_is_from_rule_only(self):
        cases = (
            ({}, "unknown"),
            ({"security-severity": "not-a-score"}, "unknown"),
            ({"security-severity": True}, "unknown"),
            ({"security-severity": None}, "unknown"),
            ({"security-severity": "NaN"}, "unknown"),
            ({"security-severity": -1}, "unknown"),
            ({"security-severity": 10.1}, "unknown"),
            ({"security-severity": 0}, "lower"),
            ({"security-severity": "6.99999999999999999999"}, "lower"),
            ({"security-severity": "7.0"}, "high"),
            ({"security-severity": "7e0"}, "high"),
            ({"security-severity": 10}, "high"),
        )
        for properties, bucket in cases:
            with self.subTest(properties=properties):
                report = sarif(
                    [rule(**properties)],
                    [finding(level="note", properties={"security-severity": 1})],
                )
                self.assertEqual(
                    scan.parse_codeql(report), ({bucket: 1}, {"Unmapped": 1})
                )

    def test_extension_reference_and_multiple_cwe_associations(self):
        extension = {
            "name": "codeql/python-queries",
            "rules": [
                rule(
                    **{
                        "security-severity": "8.0",
                        "tags": [
                            "security",
                            "external/cwe/cwe-079",
                            "external/cwe/cwe-22",
                            "external/cwe/cwe-022",
                            "external/cwe/cwe-1024",
                        ],
                    }
                )
            ],
        }
        report = sarif(
            [rule(**{"security-severity": 0})],
            [
                finding(
                    ruleIndex=-1,
                    rule={
                        "id": RULE,
                        "guid": RULE_GUID,
                        "index": 0,
                        "toolComponent": {"index": 0, "name": "codeql/python-queries"},
                    },
                )
            ],
        )
        report["runs"][0]["tool"]["extensions"] = [extension]
        self.assertEqual(
            scan.parse_codeql(report),
            (
                {"high": 1},
                {"A01:2025 Broken Access Control": 1, "A05:2025 Injection": 1},
            ),
        )
        # A driver index may disambiguate; just an ID cannot when both have it.
        report["runs"][0]["results"] = [finding(ruleIndex=0)]
        self.assertEqual(scan.parse_codeql(report)[0], {"lower": 1})
        report["runs"][0]["results"] = [finding()]
        with self.assertRaisesRegex(scan.InvalidReport, "ambiguous"):
            scan.parse_codeql(report)
        # Unique ID lookup also works with no redundant component metadata.
        report["runs"][0]["tool"]["driver"]["rules"] = []
        self.assertEqual(scan.parse_codeql(report)[0], {"high": 1})

    def test_malformed_or_conflicting_rule_references_refuse(self):
        cases = (
            finding(ruleId=None),
            finding(ruleId="missing"),
            finding(rule={"id": "other"}),
            finding(ruleIndex=1),
            finding(ruleIndex=True),
            finding(ruleIndex=0, rule={"index": 1}),
            finding(rule={"guid": "other"}),
            finding(rule={"toolComponent": {"index": 0}}),
            finding(message={}),
        )
        for result in cases:
            with self.subTest(result=result):
                with self.assertRaises(scan.InvalidReport):
                    scan.parse_codeql(sarif([rule()], [result]))
        with self.assertRaisesRegex(scan.InvalidReport, "Ambiguous"):
            scan.parse_codeql(sarif([rule(), rule()], []))

    def test_empty_success_distinguished_from_absent_results_or_failed_run(self):
        self.assertEqual(scan.parse_codeql(sarif()), ({}, {}))
        self.assertEqual(
            scan.parse_codeql(
                sarif(invocations=[{"executionSuccessful": True}]),
            ),
            ({}, {}),
        )
        missing_results = sarif()
        del missing_results["runs"][0]["results"]
        for report in (
            {},
            {"version": "2.1.0", "runs": []},
            missing_results,
            sarif(results=[None]),
            sarif(invocations=[{"executionSuccessful": False}]),
            sarif(invocations=[{}]),
            sarif(
                invocations=[
                    {
                        "executionSuccessful": True,
                        "toolExecutionNotifications": [
                            {"level": "error", "message": {"text": PRIVATE}},
                        ],
                    }
                ]
            ),
        ):
            with self.subTest(report=report):
                with self.assertRaises(scan.InvalidReport):
                    scan.parse_codeql(report)


class PythonParserTests(unittest.TestCase):
    def test_counts_and_agreement(self):
        self.assertEqual(scan.parse_python(pip_audit(), 0), (1, 0))
        self.assertEqual(
            scan.parse_python(pip_audit({"id": PRIVATE}, {"id": "SYNTH-2"}), 1), (1, 2)
        )
        for report, status in (
            (pip_audit(), 1),
            (pip_audit({"id": PRIVATE}), 0),
            (pip_audit(), -9),
            (pip_audit(), 2),
            (pip_audit(), None),
            (pip_audit(), False),
        ):
            with self.subTest(status=status, report=report):
                with self.assertRaises(scan.InvalidReport):
                    scan.parse_python(report, status)

    def test_bad_backend_shape_skipped_rows_and_remediation_refuse(self):
        cases = (
            {},
            [],
            {"dependencies": [], "fixes": []},
            {"dependencies": [{"name": PRIVATE, "skip_reason": PRIVATE}]},
            {
                "dependencies": [
                    {"name": PRIVATE, "version": "1", "vulns": [], "error": PRIVATE}
                ]
            },
            {"dependencies": [{"name": PRIVATE, "version": "1"}]},
            {"dependencies": [{"name": PRIVATE, "version": "1", "vulns": [{}]}]},
            {"dependencies": [], "error": PRIVATE},
            {"dependencies": [], "fixes": [{"name": PRIVATE}]},
        )
        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(scan.InvalidReport):
                    scan.parse_python(report, 0)


class CheckerCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write_report(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_checker(self, kind, path, *options):
        output = StringIO()
        with redirect_stdout(output):
            status = scan.main([kind, str(path), "--commit", COMMIT, *options])
        text = output.getvalue()
        self.assertIn(COMMIT, text)
        self.assertNotIn(PRIVATE, text)
        self.assertNotIn(str(self.root), text)
        return status, text

    def test_gate_does_not_exempt_suppressed_findings(self):
        path = self.write_report(
            PRIVATE + ".sarif",
            sarif(
                [rule(**{"security-severity": 7})],
                [
                    finding(
                        suppressions=[
                            {
                                "kind": "inSource",
                                "status": "accepted",
                                "justification": PRIVATE,
                            }
                        ]
                    )
                ],
            ),
        )
        report_status, report = self.run_checker("codeql", path)
        gate_status, gate = self.run_checker("codeql", path, "--gate")
        self.assertEqual((report_status, gate_status), (0, 1))
        self.assertIn("not enforced", report)
        self.assertIn("Blocked by policy", gate)
        self.assertIn("Unmapped: 1", gate)

    def test_lower_and_unknown_codeql_gate_outcomes(self):
        for properties, expected in (({"security-severity": 6.9}, 0), ({}, 1)):
            with self.subTest(properties=properties):
                path = self.write_report(
                    "input.sarif", sarif([rule(**properties)], [finding()])
                )
                self.assertEqual(
                    self.run_checker("codeql", path, "--gate")[0], expected
                )
        path.write_text(
            json.dumps(sarif([rule()], [finding()])).replace(
                '"properties": {}',
                '"properties": {"security-severity": 10.0000000000000000001}',
                1,
            ),
            encoding="utf-8",
        )
        status, summary = self.run_checker("codeql", path, "--gate")
        self.assertEqual(status, 1)
        self.assertIn("unknown: 1", summary)

    def test_requires_one_regular_sarif(self):
        path = self.write_report("clean.sarif", sarif())
        self.assertEqual(self.run_checker("codeql", path, "--gate")[0], 0)
        alias = self.root / "alias.sarif"
        alias.symlink_to(path)
        self.assertEqual(self.run_checker("codeql", alias)[0], 2)
        self.assertEqual(self.run_checker("codeql", self.root)[0], 2)

    def test_input_errors_fail_even_report_only_without_leaking_data(self):
        path = self.root / (PRIVATE + ".sarif")
        cases = (
            b'{"version":',
            b'{"runs": [], "runs": []}',
            b'{"runs": NaN}',
            b"\xff",
        )
        for data in cases:
            with self.subTest(data=data):
                path.write_bytes(data)
                status, text = self.run_checker("codeql", path)
                self.assertEqual(status, 2)
                self.assertIn("Invalid scanner evidence", text)
        path.unlink()
        self.assertEqual(self.run_checker("codeql", path)[0], 2)

    def test_python_gate_blocks_all_advisories_and_requires_status(self):
        path = self.write_report(
            "audit.json", pip_audit({"id": PRIVATE, "fix_versions": []})
        )
        status, summary = self.run_checker(
            "python", path, "--scanner-exit", "1", "--gate"
        )
        self.assertEqual(status, 1)
        self.assertIn("A03:2025 Software Supply Chain Failures", summary)
        self.assertIn("unresolved advisory rows: 1", summary)
        self.assertEqual(self.run_checker("python", path, "--scanner-exit", "1")[0], 0)
        for options in ((), ("--scanner-exit", "0"), ("--scanner-exit", "2")):
            self.assertEqual(self.run_checker("python", path, *options)[0], 2)

    def test_public_python_ids_are_usable_without_echoing_raw_fields(self):
        report = pip_audit({"id": "CVE-2026-12345", "description": PRIVATE})
        report["dependencies"][0]["name"] = "example-package"
        path = self.write_report("audit.json", report)
        status, summary = self.run_checker("python", path, "--scanner-exit", "1")
        self.assertEqual(status, 0)
        self.assertIn("`example-package`: `CVE-2026-12345`", summary)

    def test_invalid_cli_identity_or_exit_does_not_echo_arguments(self):
        for option in ("--commit", "--scanner-exit"):
            with self.subTest(option=option):
                args = ["--commit", COMMIT] if option != "--commit" else []
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        scan.__file__,
                        "python",
                        PRIVATE,
                        *args,
                        option,
                        PRIVATE,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn(PRIVATE, completed.stderr)


if __name__ == "__main__":
    unittest.main()
