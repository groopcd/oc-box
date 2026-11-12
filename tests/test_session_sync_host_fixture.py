"""Offline runner guards/oracles only; NEVER host/Podman acceptance.

No engine, native OpenCode, real user state, or external service is contacted.
"""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/test_session_sync_podman.py"
spec = importlib.util.spec_from_file_location("session_history_host_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SessionHistoryHostFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="history-runner-unit-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.source = self.parent / "source"
        self.source.mkdir()
        self.commit = "b" * 40

    def source_files(self, version_source=None):
        self.ignored = b""
        self.files = {name: b"# audited helper\n" for name in runner.DONORS}
        self.files["src/oc_box/oc_box.py"] = b"# audited runtime\n"
        self.files["src/oc_box/version_info.py"] = (
            b'VERSION_SERIES = "1.7"\nVERSION_BASE_COMMIT_COUNT = 29\n'
            if version_source is None
            else version_source
        )
        for name, value in self.files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.stage = (
            b"\0".join(
                b"100644 "
                + hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value)
                .hexdigest()
                .encode()
                + b" 0\t"
                + name.encode()
                for name, value in self.files.items()
            )
            + b"\0"
        )

    def git(self, source, *args):
        self.assertEqual(source, self.source)
        responses = {
            ("rev-parse", "--show-toplevel"): str(self.source).encode() + b"\n",
            ("rev-parse", "HEAD"): self.commit.encode() + b"\n",
            ("rev-parse", "--is-shallow-repository"): b"false\n",
            ("status", "--porcelain", "--untracked-files=all"): b"",
            ("rev-list", "--count", "HEAD"): b"80\n",
        }
        if args[0] == "ls-files":
            return self.ignored if "--ignored" in args else self.stage
        return responses[args]

    def test_host_guard_matrix(self):
        for uid, container, markers in (
            (0, "0", False),
            (1000, "1", False),
            (1000, "0", True),
        ):
            with (
                self.subTest(uid=uid, container=container, markers=markers),
                patch.object(os, "geteuid", return_value=uid),
                patch.dict(os.environ, {"OC_BOX_CONTAINER": container}, clear=True),
                patch.object(Path, "exists", return_value=markers),
                self.assertRaisesRegex(runner.Refusal, "host-only"),
            ):
                runner.host_guard()

    def test_main_host_refusal_precedes_every_probe_and_allocation(self):
        arguments = [
            "--source",
            str(self.source),
            "--expect-commit",
            self.commit,
            "--work-dir",
            str(self.parent),
            "--opencode-v2",
            "/synthetic/native",
        ]
        with (
            patch.object(runner, "host_guard", side_effect=runner.Refusal("host-only")),
            patch.object(runner, "fresh_parent") as placement,
            patch.object(runner, "load_source") as source,
            patch.object(runner.tempfile, "mkdtemp") as allocate,
            patch.object(runner.subprocess, "run") as command,
        ):
            self.assertEqual(runner.main(arguments), 1)
        placement.assert_not_called()
        source.assert_not_called()
        allocate.assert_not_called()
        command.assert_not_called()

    def test_invalid_options_are_fixed_and_help_has_no_host_effects(self):
        for arguments, expected in (([], 2), (["--help"], 0)):
            with (
                self.subTest(arguments=arguments),
                patch.object(runner, "host_guard") as host,
                patch.object(runner, "load_source") as source,
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()) as errors,
            ):
                with self.assertRaises(SystemExit) as raised:
                    runner.main(arguments)
                self.assertEqual(raised.exception.code, expected)
                if expected:
                    self.assertEqual(
                        errors.getvalue(),
                        "session-history fixture: category=invalid-options\n",
                    )
            host.assert_not_called()
            source.assert_not_called()

    def test_source_snapshot_hashes_git_owned_runtime_and_donors(self):
        for name, series in (("retained source", "1.6"), ("release source", "1.7")):
            self.source_files(
                f'VERSION_SERIES = "{series}"\nVERSION_BASE_COMMIT_COUNT = 29\n'.encode()
            )
            with (
                self.subTest(name=name),
                patch.object(runner, "git", side_effect=self.git),
                patch.object(runner.importlib, "import_module") as imports,
            ):
                identity = runner.source_snapshot(self.source, self.commit)
                self.assertEqual(
                    identity["version"], series + ".51+g" + self.commit[:7]
                )
                self.assertEqual(identity["commit_count"], 80)
                self.assertEqual(identity["commit"], self.commit)
                self.assertFalse(identity["dirty"])
                self.assertEqual(
                    identity["sha256"],
                    {
                        name: hashlib.sha256(value).hexdigest()
                        for name, value in self.files.items()
                    },
                )
            imports.assert_not_called()

    def test_source_version_declaration_matrix_refuses_before_import(self):
        series = b'VERSION_SERIES = "1.7"\n'
        baseline = b"VERSION_BASE_COMMIT_COUNT = 29\n"
        cases = {
            "missing series": baseline,
            "missing baseline": series,
            "unknown series": b'VERSION_SERIES = "1.8"\n' + baseline,
            "legacy series": b'VERSION_SERIES = "1.5"\n' + baseline,
            "patch in series": b'VERSION_SERIES = "1.7.0"\n' + baseline,
            "numeric series": b"VERSION_SERIES = 1.7\n" + baseline,
            "dynamic series": b'VERSION_SERIES = str("1.7")\n' + baseline,
            "duplicate series": series + series + baseline,
            "conditional series": series
            + b'if True:\n VERSION_SERIES = "1.6"\n'
            + baseline,
            "changed baseline": series + b"VERSION_BASE_COMMIT_COUNT = 30\n",
            "boolean baseline": series + b"VERSION_BASE_COMMIT_COUNT = True\n",
            "float baseline": series + b"VERSION_BASE_COMMIT_COUNT = 29.0\n",
            "dynamic baseline": series + b'VERSION_BASE_COMMIT_COUNT = int("29")\n',
            "duplicate baseline": series + baseline + baseline,
            "syntax error": b"VERSION_SERIES =\n",
        }
        for name, content in cases.items():
            self.source_files(content)
            with (
                self.subTest(name=name),
                patch.object(runner, "git", side_effect=self.git),
                patch.object(runner.importlib, "import_module") as imports,
                self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
            ):
                runner.load_source(self.source, self.commit)
            imports.assert_not_called()

    def test_source_version_declarations_are_parsed_without_executing_module(self):
        self.source_files(
            b'VERSION_SERIES = "1.7"\nVERSION_BASE_COMMIT_COUNT = 29\n'
            b'raise RuntimeError("must not execute source snapshot")\n'
        )
        with patch.object(runner, "git", side_effect=self.git):
            identity = runner.source_snapshot(self.source, self.commit)
        self.assertEqual(identity["version"], "1.7.51+g" + self.commit[:7])

    def test_source_version_file_must_be_git_verified_and_present(self):
        self.source_files()
        path = self.source / "src/oc_box/version_info.py"
        path.write_bytes(
            self.files["src/oc_box/version_info.py"].replace(b"1.7", b"1.6")
        )
        with (
            patch.object(runner, "git", side_effect=self.git),
            self.assertRaisesRegex(runner.Refusal, "source-changed"),
        ):
            runner.source_snapshot(self.source, self.commit)
        self.stage = b"\0".join(
            record
            for record in self.stage.split(b"\0")
            if not record.endswith(b"\tsrc/oc_box/version_info.py")
        )
        with (
            patch.object(runner, "git", side_effect=self.git),
            self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
        ):
            runner.source_snapshot(self.source, self.commit)

    def test_source_metadata_matrix_refuses_before_import(self):
        self.source_files()
        cases = {
            "wrong commit": (("rev-parse", "HEAD"), b"a" * 40),
            "shallow": (("rev-parse", "--is-shallow-repository"), b"true"),
            "dirty": (
                ("status", "--porcelain", "--untracked-files=all"),
                b" M runtime",
            ),
            "untracked": (
                ("status", "--porcelain", "--untracked-files=all"),
                b"?? helper",
            ),
            "incomplete history": (("rev-list", "--count", "HEAD"), b"29"),
            "empty count": (("rev-list", "--count", "HEAD"), b""),
            "negative count": (("rev-list", "--count", "HEAD"), b"-80"),
            "float count": (("rev-list", "--count", "HEAD"), b"80.0"),
            "non-count": (("rev-list", "--count", "HEAD"), b"private-sentinel"),
            "noncanonical count": (("rev-list", "--count", "HEAD"), b"080"),
            "nested source": (
                ("rev-parse", "--show-toplevel"),
                str(self.parent).encode(),
            ),
        }
        for name, (target, value) in cases.items():

            def git(source, *args, value=value, target=target):
                return value if args == target else self.git(source, *args)

            with (
                self.subTest(name=name),
                patch.object(runner, "git", side_effect=git),
                patch.object(runner.importlib, "import_module") as imports,
                self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
            ):
                runner.load_source(self.source, self.commit)
            imports.assert_not_called()

    def test_source_expected_commit_matrix_refuses_before_git_or_import(self):
        for name, commit in (
            ("missing", None),
            ("non-string", 80),
            ("short", "b" * 7),
            ("long", "b" * 41),
            ("uppercase", "B" * 40),
            ("non-hex", "z" * 40),
        ):
            with (
                self.subTest(name=name),
                patch.object(runner, "git") as git,
                patch.object(runner.importlib, "import_module") as imports,
                self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
            ):
                runner.load_source(self.source, commit)
            git.assert_not_called()
            imports.assert_not_called()

    def test_source_file_change_ignored_by_status_still_refuses(self):
        self.source_files()
        (self.source / "src/oc_box/oc_box.py").write_text("changed despite status\n")
        with (
            patch.object(runner, "git", side_effect=self.git),
            self.assertRaisesRegex(runner.Refusal, "source-changed"),
        ):
            runner.source_snapshot(self.source, self.commit)

    def test_source_symlink_and_missing_manifest_refuse(self):
        self.source_files()
        self.stage = b""
        with (
            patch.object(runner, "git", side_effect=self.git),
            self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
        ):
            runner.source_snapshot(self.source, self.commit)
        alias = self.parent / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(runner.Refusal, "unsafe-path"):
            runner.source_snapshot(alias, self.commit)

    def test_shadowed_runtime_and_namespace_matrix(self):
        for name in (
            "oc_box",
            "oc_box.workspace_service",
            "tools",
            "tools.test_global_vault_podman",
        ):
            for namespace in (False, True):
                module = ModuleType(name)
                if namespace:
                    module.__path__ = [str(self.parent / "foreign")]
                else:
                    module.__file__ = str(self.parent / "foreign.py")
                with (
                    self.subTest(name=name, namespace=namespace),
                    patch.object(sys, "modules", {name: module}),
                    self.assertRaisesRegex(runner.Refusal, "source-shadowing"),
                ):
                    runner.import_guard(self.source)

    def test_import_guard_accepts_only_source_locations(self):
        package = ModuleType("oc_box")
        package.__file__ = str(self.source / "src/oc_box/__init__.py")
        tools = ModuleType("tools")
        tools.__path__ = [str(self.source / "tools")]
        with patch.object(sys, "modules", {"oc_box": package, "tools": tools}):
            runner.import_guard(self.source)
            tools.__path__.append(str(self.parent / "foreign"))
            with self.assertRaisesRegex(runner.Refusal, "source-shadowing"):
                runner.import_guard(self.source)

    def test_runner_cannot_be_inside_source(self):
        with (
            patch.object(runner, "source_snapshot", return_value={}),
            patch.object(runner, "__file__", str(self.source / "tools/runner.py")),
            patch.object(runner.importlib, "import_module") as imports,
            self.assertRaisesRegex(runner.Refusal, "runner-placement"),
        ):
            runner.load_source(self.source, self.commit)
        imports.assert_not_called()

    def test_git_environment_does_not_inherit_credentials_or_selectors(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "private-sentinel",
                    "GIT_DIR": "foreign",
                    "GIT_CONFIG_COUNT": "1",
                },
            ),
            patch.object(runner.shutil, "which", return_value="/usr/bin/git"),
            patch.object(
                subprocess, "run", return_value=SimpleNamespace(stdout=b"ok")
            ) as command,
        ):
            self.assertEqual(runner.git(self.source, "status"), b"ok")
        self.assertEqual(command.call_args.args[0][0], "/usr/bin/git")
        for executable in (None, "./git", "git"):
            with (
                self.subTest(executable=executable),
                patch.object(runner.shutil, "which", return_value=executable),
                patch.object(subprocess, "run") as unused,
                self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
            ):
                runner.git(self.source, "status")
            unused.assert_not_called()
        env = command.call_args.kwargs["env"]
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(command.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_fresh_placement_rejects_all_retained_ancestors_and_symlinks(self):
        for marker in (
            "fixture.json",
            "fixture-ids.json",
            "interactive.json",
            "edges.json",
            runner.MARKER,
        ):
            root = self.parent / marker.replace(".", "-")
            child = root / "nested"
            child.mkdir(parents=True)
            (root / marker).write_text("retained")
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(runner.Refusal, "fresh-parent-required"),
            ):
                runner.fresh_parent(child)
        retained = self.parent / "oc-box-gv-host-renamed"
        retained.mkdir()
        with self.assertRaisesRegex(runner.Refusal, "fresh-parent-required"):
            runner.fresh_parent(retained)
        alias = self.parent / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(runner.Refusal, "unsafe-path"):
            runner.fresh_parent(alias)
        self.assertEqual(runner.fresh_parent(self.source), self.source)

    def harness(self):
        loaded = SimpleNamespace(
            fixture=SimpleNamespace(NATIVE_VERSION="2.2.6"),
            box=SimpleNamespace(),
            service=SimpleNamespace(),
            source=self.source,
            identity={"commit": self.commit, "sha256": {}},
        )
        native = self.parent / "native"
        native.write_bytes(b"synthetic-native")
        harness = runner.Harness(loaded, self.parent, native, runner.file_hash(SCRIPT))
        harness.validate_native_config = Mock()
        harness.resume_sources = set()
        return harness

    def test_validate_refusals_never_reach_engine_or_resource_inspection(self):
        for failing in (
            "same_identity",
            "validate_fixture",
            "private_json",
            "workspace_records",
            "require_runtime_boot",
            "validate_store",
        ):
            harness = self.harness()
            f = harness.f
            f.validate_fixture = Mock()
            f.private_json = Mock(return_value=harness.purpose())
            f.workspace_records = Mock(return_value={})
            f.require_runtime_boot = Mock()
            f.validate_store = Mock()
            f.verify_owned_resources = Mock()
            with (
                self.subTest(failing=failing),
                patch.object(harness, "same_identity") as same,
            ):
                target = same if failing == "same_identity" else getattr(f, failing)
                target.side_effect = runner.Refusal("fixture-purpose")
                with self.assertRaises(runner.Refusal):
                    harness.validate(initial=True)
            f.verify_owned_resources.assert_not_called()
            if failing != "validate_store":
                f.validate_store.assert_not_called()

    def test_registry_guard_is_source_a_only_and_credential_free(self):
        for records in (
            {"source-b": (None, {})},
            {"source-a": (None, {"credentials": {"mode": "env"}})},
        ):
            harness = self.harness()
            harness.f.validate_fixture = Mock()
            harness.f.private_json = Mock(return_value=harness.purpose())
            harness.f.workspace_records = Mock(return_value=records)
            harness.f.require_runtime_boot = Mock()
            harness.f.validate_store = Mock()
            with (
                self.subTest(records=records),
                patch.object(harness, "same_identity"),
                self.assertRaisesRegex(runner.Refusal, "fixture-only"),
            ):
                harness.validate()
            harness.f.require_runtime_boot.assert_not_called()
            harness.f.validate_store.assert_not_called()

    def test_source_runner_and_native_rechecked_independently(self):
        harness = self.harness()
        with (
            patch.object(
                runner, "source_snapshot", return_value=harness.loaded.identity
            ),
            patch.object(runner, "import_guard"),
        ):
            harness.same_identity()
            harness.binary.write_bytes(b"replaced")
            with self.assertRaisesRegex(runner.Refusal, "native-changed"):
                harness.same_identity()
            harness.native["sha256"] = runner.file_hash(harness.binary)
            harness.runner = "wrong"
            with self.assertRaisesRegex(runner.Refusal, "source-changed"):
                harness.same_identity()

    def test_reports_require_every_named_true_check_and_safe_categories(self):
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "version": "1.6.51+g" + self.commit[:7],
            "dirty": False,
            "sha256": {"src/oc_box/oc_box.py": "c" * 64},
        }
        digest = "d" * 64
        native = {"version": "2.2.6", "sha256": "e" * 64}
        for checks in (
            {},
            {name: True for name in runner.CHECKS - {"pending_exit"}},
            {name: True for name in runner.CHECKS - {"pending_later_progress"}},
            {**dict.fromkeys(runner.CHECKS, True), "host_clean": False},
        ):
            report = runner.result(checks, identity, digest, native)
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["primary_category"], "missing-checks")
        report = runner.result(
            dict.fromkeys(runner.CHECKS, True),
            identity,
            digest,
            native,
            exits=runner.EXPECTED_EXITS,
            stage="complete",
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["launcher_stages"], list(runner.LAUNCH_STAGES))
        self.assertEqual(
            report["expected_launcher_exit_codes"], list(runner.EXPECTED_EXITS)
        )
        self.assertEqual(len(report["checks"]), 42)
        for missing in runner.CHECKS:
            incomplete = runner.result(
                dict.fromkeys(runner.CHECKS - {missing}, True),
                identity,
                digest,
                native,
                exits=runner.EXPECTED_EXITS,
                stage="complete",
            )
            self.assertEqual(incomplete["status"], "FAIL", missing)
        for exits, stage in (
            ((0, 0, 0, 1, 0), "complete"),
            (runner.EXPECTED_EXITS[:-1], "complete"),
            ((0,) * 8, "complete"),
            (runner.EXPECTED_EXITS, "unarchived_restart"),
        ):
            with self.subTest(exits=exits, stage=stage):
                incomplete = runner.result(
                    dict.fromkeys(runner.CHECKS, True),
                    identity,
                    digest,
                    native,
                    exits=exits,
                    stage=stage,
                )
                self.assertEqual(incomplete["status"], "FAIL")
        self.assertIn(
            "transient completed-job row (not required or claimed)",
            report["not_covered"],
        )
        self.assertEqual(report["runner_sha256"], digest)
        for checks, primary, secondary, exits in (
            ({"unknown": True}, None, (), ()),
            ({"host_clean": 1}, None, (), ()),
            ({}, "private-sentinel", (), ()),
            ({}, None, ("private-sentinel",), ()),
            ({}, None, (), (True,)),
            ({}, None, (), (0,) * 9),
        ):
            with (
                self.subTest(checks=checks, primary=primary),
                self.assertRaisesRegex(runner.Refusal, "report-schema"),
            ):
                runner.result(
                    checks, identity, digest, native, primary, secondary, exits
                )
        for field in ("commit", "sha256", "version", "dirty", "commit_count"):
            bad = copy.deepcopy(identity)
            bad[field] = "private-sentinel"
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(runner.Refusal, "report-schema"),
            ):
                runner.result({}, bad, digest, native)

    def test_report_source_series_matrix_preserves_exact_count_and_commit(self):
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "dirty": False,
            "sha256": {"src/oc_box/version_info.py": "c" * 64},
        }
        for name, series in (("retained report", "1.6"), ("release report", "1.7")):
            identity["version"] = series + ".51+g" + self.commit[:7]
            with self.subTest(name=name):
                report = runner.result(
                    dict.fromkeys(runner.CHECKS, True),
                    identity,
                    "d" * 64,
                    {"version": "2.2.6", "sha256": "e" * 64},
                    exits=runner.EXPECTED_EXITS,
                    stage="complete",
                )
                self.assertEqual(report["status"], "PASS")
                self.assertEqual(report["source_identity"], identity)

    def test_report_source_identity_refusal_matrix_is_strict_for_both_series(self):
        for series in ("1.6", "1.7"):
            identity = {
                "commit": self.commit,
                "commit_count": 80,
                "version": series + ".51+g" + self.commit[:7],
                "dirty": False,
                "sha256": {"src/oc_box/version_info.py": "c" * 64},
            }
            cases = {
                "unknown series": {"version": "1.8.51+g" + self.commit[:7]},
                "legacy series": {"version": "1.5.51+g" + self.commit[:7]},
                "missing version": {"version": None},
                "wrong patch": {"version": series + ".50+g" + self.commit[:7]},
                "padded patch": {"version": series + ".051+g" + self.commit[:7]},
                "wrong short hash": {"version": series + ".51+g" + "a" * 7},
                "dirty suffix": {"version": identity["version"] + ".dirty"},
                "short commit": {"commit": self.commit[:7]},
                "non-hex commit": {"commit": "z" * 40},
                "uppercase commit": {"commit": "B" * 40},
                "wrong count": {"commit_count": 81},
                "baseline count": {"commit_count": 29},
                "boolean count": {"commit_count": True},
                "float count": {"commit_count": 80.0},
                "dirty source": {"dirty": True},
                "malformed audit hash": {
                    "sha256": {"src/oc_box/version_info.py": "z" * 64}
                },
            }
            for name, change in cases.items():
                with (
                    self.subTest(series=series, name=name),
                    self.assertRaisesRegex(runner.Refusal, "report-schema"),
                ):
                    runner.result(
                        {},
                        {**identity, **change},
                        "d" * 64,
                        {"version": "2.2.6", "sha256": "e" * 64},
                    )

    def test_error_categories_never_include_native_exception_contents(self):
        for error, expected in (
            (OSError("private-sentinel"), "io-error"),
            (
                subprocess.CalledProcessError(1, ["private-sentinel"]),
                "subprocess-error",
            ),
            (ValueError("private-sentinel"), "operation-failed"),
            (runner.Refusal("private-sentinel"), "fixture-refusal"),
            (
                type("FixtureError", (Exception,), {})("private-sentinel"),
                "fixture-refusal",
            ),
            (
                type("FixtureError", (Exception,), {})("unexpected-unlock"),
                "unexpected-unlock",
            ),
            (KeyboardInterrupt(), "interrupted"),
        ):
            self.assertEqual(runner.category(error), expected)

    def test_refusal_oracle_preserves_actual_exception_identity(self):
        error = ValueError("private-sentinel")
        callback = Mock(side_effect=error)
        self.assertIs(runner.expect_refusal(callback, ValueError), error)
        with self.assertRaisesRegex(runner.Refusal, "unexpected-success"):
            runner.expect_refusal(lambda: None, ValueError)

    def test_context_probe_closes_unexpected_successful_lease(self):
        closed = []

        @contextlib.contextmanager
        def lease():
            try:
                yield {"private": "synthetic"}
            finally:
                closed.append(True)

        with self.assertRaisesRegex(runner.Refusal, "unexpected-success"):
            runner.expect_context_refusal(lease(), ValueError)
        self.assertEqual(closed, [True])

    def test_refusal_snapshots_distinguish_private_archive_and_success_checkpoint(self):
        before = {
            "host": {"retained": True},
            "private": b"before",
            "synced": "old-digest",
        }
        changed_private = {**copy.deepcopy(before), "private": b"new-local-export"}
        runner.preserved(before, changed_private, private=False)
        with self.assertRaises(runner.Refusal):
            runner.preserved(before, changed_private)
        for key in ("host", "synced"):
            after = copy.deepcopy(before)
            after[key] = "changed"
            with self.subTest(key=key), self.assertRaises(runner.Refusal):
                runner.preserved(before, after, private=False)

    def test_sync_must_run_in_owner_context(self):
        harness = self.harness()
        harness.connection = {"workspace": "fixture", "password": "synthetic"}
        harness.service.live_connection = Mock(return_value=None)
        harness.box.sync_sessions = Mock()
        with self.assertRaises(runner.Refusal):
            harness.sync()
        harness.box.sync_sessions.assert_not_called()

    def resume_harness(self):
        """Private synthetic oracle inputs, not a native/runtime implementation."""
        harness = self.harness()
        harness.path, harness.data = self.parent / "workspace.json", {"id": "fixture"}
        project = "/synthetic/source-a"
        harness.box.container_project = Mock(return_value=project)
        harness.box.valid_session_id = lambda ident: (
            isinstance(ident, str) and ident.startswith("ses_") and ident.isascii()
        )
        harness.box.read_json = lambda path: json.loads(path.read_text())
        harness.box.verify_host_archive = Mock()
        harness.box.session_file = lambda _path: self.parent / "sessions.json"
        harness.archive_id = lambda ident, prefix="ses": prefix + "_copy_" + ident
        harness.provider = SimpleNamespace(requests=4, errors=0)
        harness.resume_requests = (4, 0)
        active, target = "ses_history_active", "ses_history_archived"
        child, archived_child = "ses_history_child", "ses_history_archived_child"
        deleted = "ses_history_deleted"
        sources = {
            ident: harness.seed_value(ident, project, parent=parent, archived=archived)
            for ident, parent, archived in (
                (active, None, False),
                (target, None, True),
                (child, active, False),
                (archived_child, target, True),
            )
        }
        # Native export omits the import envelope's location member.
        for value in sources.values():
            value.pop("location")
        harness.resume_sources = set(sources)
        harness.resume_missing = {deleted}
        state = {
            "sessions": [],
            "missing": [deleted],
            "synced": {},
            "host_sessions": {},
        }
        harness.box.session_state = Mock(return_value=state)
        history = self.parent / "history"
        history.mkdir(exist_ok=True)
        copies, calls = {}, []

        def copy_source(ident, source):
            value = copy.deepcopy(source)
            info = value["info"]
            info["id"] = harness.archive_id(ident)
            info["title"] = "[oc-box source-a] " + info.get("title", "Session")
            if info.get("parentID"):
                info["metadata"]["oc-box.archive"] = {
                    "version": 1,
                    "parentID": harness.archive_id(info["parentID"]),
                }
            for field in ("permissions", "parentID", "fork", "revert"):
                info.pop(field, None)
            for message in value["messages"]:
                message["id"] = harness.archive_id(message["id"], "msg")
            path = harness.archive_path(ident)
            path.write_text(json.dumps(value, sort_keys=True))
            copies[harness.archive_id(ident)] = value
            state["synced"][ident] = runner.file_hash(path)
            state["host_sessions"][ident] = harness.archive_id(ident)

        def inventory():
            return [copy.deepcopy(value["info"]) for value in sources.values()]

        harness.box.list_container_sessions = Mock(
            side_effect=lambda _data: inventory()
        )
        harness.export = Mock(
            side_effect=lambda ident, host=False: copy.deepcopy(
                copies[ident] if host else sources[ident]
            )
        )
        harness.host = Mock(
            side_effect=lambda method, route: {
                "data": [copy.deepcopy(value["info"]) for value in copies.values()]
            }
        )
        tick = 100

        def request(method, route, value=None):
            nonlocal tick
            self.assertEqual(method, "PATCH")
            ident = route.removeprefix("/api/session/")
            self.assertIn(ident, sources)
            calls.append((method, ident, copy.deepcopy(value)))
            info = sources[ident]["info"]
            tick += 1
            info["time"]["updated"] = tick
            if value["archived"]:
                info["time"]["archived"] = tick
            else:
                info["time"].pop("archived", None)
            if "title" in value:
                info["title"] = value["title"]
            return None

        harness.request = Mock(side_effect=request)
        harness.batch = Mock(
            side_effect=lambda operations: [
                request(*operation) for operation in operations
            ]
        )

        def sync():
            calls.append("sync")
            state["sessions"] = list(sources)
            roots = [
                info
                for info in inventory()
                if not info.get("parentID") and info["time"].get("archived") is None
            ]
            state["last"] = (
                max(roots, key=lambda info: info["time"]["updated"])["id"]
                if roots
                else None
            )
            for ident, source in sources.items():
                copy_source(ident, source)
            harness.box.session_file(harness.path).write_text(json.dumps(state))

        harness.sync = Mock(side_effect=sync)
        deleted_source = harness.seed_value(deleted, project)
        deleted_source.pop("location")
        copy_source(deleted, deleted_source)
        sync()
        calls.clear()
        return harness, sources, copies, state, calls

    def test_resume_stages_preserve_all_sources_children_deleted_and_host_copies(self):
        harness, sources, _copies, state, calls = self.resume_harness()
        original = copy.deepcopy(sources)
        old_ids = set(sources)
        self.assertEqual(harness.archive_roots("ses_history_active"), 0)
        self.assertIsNone(state["last"])
        self.assertEqual(set(state["sessions"]), old_ids)
        self.assertEqual(
            [item[1] for item in calls if isinstance(item, tuple)],
            ["ses_history_active", "ses_history_archived"],
        )
        for ident in ("ses_history_child", "ses_history_archived_child"):
            self.assertEqual(sources[ident], original[ident])
        self.assertTrue(
            all(
                source["messages"] == original[ident]["messages"]
                for ident, source in sources.items()
            )
        )
        new = "ses_history_new"
        sources[new] = harness.seed_value(new, "/synthetic/source-a")
        sources[new].pop("location")
        sources[new]["info"]["time"]["updated"] = 100
        self.assertEqual(harness.archived_restart(new), 0)
        self.assertEqual(state["last"], "ses_history_archived")
        self.assertEqual(set(state["sessions"]), old_ids | {new})
        self.assertEqual(harness.unarchived_restart("ses_history_archived"), 0)
        self.assertEqual(
            set(harness.checks),
            {
                "archived_roots_inventory",
                "archived_roots_retention",
                "all_archived_no_resume",
                "archived_next_start_new_root",
                "archived_restart_retention",
                "unarchived_root_eligible",
                "unarchived_next_start_resume",
                "unarchived_history_retention",
            },
        )
        self.assertEqual(harness.provider.requests, 4)
        harness.box.verify_host_archive.assert_any_call(
            harness.path, harness.data, harness.box.read_json(harness.archive_path(new))
        )

    def test_resume_inventory_refuses_incomplete_invalid_or_ineligible_selection(self):
        mutations = {
            "omitted archived root": lambda rows: rows.pop(1),
            "duplicate": lambda rows: rows.append(copy.deepcopy(rows[0])),
            "foreign": lambda rows: rows[0]["location"].update(directory="/foreign"),
            "nested": lambda rows: rows[0]["location"].update(
                directory="/synthetic/source-a/nested"
            ),
            "invalid id": lambda rows: rows[0].update(id=None),
            "invalid parent": lambda rows: rows[0].update(parentID=7),
            "missing time": lambda rows: rows[0].pop("time"),
            "invalid updated": lambda rows: rows[0]["time"].update(updated=True),
            "nan updated": lambda rows: rows[0]["time"].update(updated=float("nan")),
            "invalid archived": lambda rows: rows[1]["time"].update(archived="private"),
            "non-record": lambda rows: rows.append(None),
        }
        for name, mutate in mutations.items():
            harness, _sources, _copies, _state, _calls = self.resume_harness()
            rows = harness.box.list_container_sessions(harness.data)
            mutate(rows)
            harness.box.list_container_sessions.side_effect = None
            harness.box.list_container_sessions.return_value = rows
            with self.subTest(defect=name), self.assertRaises(runner.Refusal):
                harness.archive_roots("ses_history_active")
            harness.batch.assert_not_called()
            self.assertEqual(harness.checks, {})
        harness, _sources, _copies, _state, _calls = self.resume_harness()
        for ident in (
            "ses_history_archived",
            "ses_history_child",
            "ses_history_archived_child",
            "ses_foreign",
        ):
            with self.subTest(selected=ident), self.assertRaises(runner.Refusal):
                harness.resume_inventory(harness.resume_sources, selected=ident)

    def test_existing_graph_checkpoints_both_refuse_an_archived_root_as_last(self):
        # Execute the old graph's two selection assertions with synthetic
        # transports. This catches regression to root-only (not archive-aware)
        # selection without replacing any assertion in the host runner.
        for failing_sync in (1, 3):
            harness = self.harness()
            harness.path, harness.data = self.parent / "workspace.json", {}
            project = "/synthetic/source-a"
            harness.box.container_project = lambda _data: project
            harness.container_python = Mock()
            harness.archive_id = lambda ident: "ses_copy_" + ident
            harness.verify_archive = Mock()
            harness.snapshot = lambda _ident: {
                "private": b"old",
                "host": {},
                "synced": "old",
            }
            source = {"ses_initial": harness.seed_value("ses_initial", project)}
            archives = set()
            state = {}
            count = 0

            def inventory(_data):
                return [
                    value["info"]
                    for value in source.values()
                    if value["info"]["location"]["directory"] == project
                ]

            def request(method, route, value=None):
                if route == "/api/experimental/session/import":
                    source[value["info"]["id"]] = copy.deepcopy(value)
                elif route.endswith("/fork"):
                    source["ses_fork"] = harness.seed_value("ses_fork", project)
                    return {"data": {"id": "ses_fork"}}
                elif route.endswith("/revert/stage"):
                    source["ses_history_a"]["info"]["revert"] = value
                elif route.startswith("/api/session?"):
                    return {"data": inventory(None)[:100], "cursor": {"next": "next"}}
                elif method == "PATCH":
                    source[route.rsplit("/", 1)[-1]]["info"].update(value)
                elif method == "DELETE":
                    ident = route.rsplit("/", 1)[-1]
                    for removed in (
                        {ident, "ses_history_c", "ses_history_d"}
                        if ident == "ses_history_a"
                        else {ident}
                    ):
                        source.pop(removed)
                return None

            def sync():
                nonlocal count
                count += 1
                ids = {item["id"] for item in inventory(None)}
                state.update(
                    sessions=list(ids),
                    missing=["ses_history_a", "ses_history_c", "ses_history_d"]
                    if count >= 3
                    else [],
                )
                state["last"] = (
                    "ses_history_archived" if count == failing_sync else "ses_history_b"
                )
                state["host_sessions"] = {
                    ident: harness.archive_id(ident) for ident in ids
                }
                archives.update(harness.archive_id(ident) for ident in ids)

            harness.request = request
            harness.batch = lambda operations: [
                request(*operation) for operation in operations
            ]
            harness.export = lambda ident: {
                key: copy.deepcopy(source[ident][key]) for key in ("info", "messages")
            }
            harness.box.list_container_sessions = inventory
            harness.box.session_state = lambda _path: state
            harness.sync = sync
            harness.host = lambda *_args: {
                "data": [{"id": ident} for ident in archives]
            }

            # Only file names are observed before the second selection check.
            def history_glob(path, pattern):
                self.assertEqual(pattern, "*.json")
                return [path / (ident + ".json") for ident in archives]

            with (
                self.subTest(checkpoint=failing_sync),
                patch.object(Path, "glob", history_glob),
                self.assertRaises(runner.Refusal),
            ):
                harness.graph("ses_initial")
            self.assertEqual(count, failing_sync)
            self.assertNotIn("deleted_source_retention", harness.checks)
            self.assertEqual("scoped_graph" in harness.checks, failing_sync == 3)

    def test_next_start_cannot_reuse_old_archived_child_missing_or_foreign_session(
        self,
    ):
        for defect in (
            "old root",
            "child",
            "deleted",
            "foreign",
            "archived new",
            "new child",
            "extra new",
        ):
            harness, sources, _copies, _state, _calls = self.resume_harness()
            harness.archive_roots("ses_history_active")
            selected = {
                "old root": "ses_history_archived",
                "child": "ses_history_child",
                "deleted": "ses_history_deleted",
            }.get(defect, "ses_history_new")
            if selected not in harness.resume_sources | harness.resume_missing:
                sources[selected] = harness.seed_value(selected, "/synthetic/source-a")
                sources[selected].pop("location")
                info = sources[selected]["info"]
                if defect == "foreign":
                    info["location"]["directory"] = "/synthetic/foreign"
                elif defect == "archived new":
                    info["time"]["archived"] = 1
                elif defect == "new child":
                    info["parentID"] = "ses_history_archived"
                elif defect == "extra new":
                    sources["ses_extra"] = harness.seed_value(
                        "ses_extra", "/synthetic/source-a"
                    )
            with self.subTest(defect=defect), self.assertRaises(runner.Refusal):
                harness.archived_restart(selected)
            self.assertNotIn("archived_next_start_new_root", harness.checks)

    def test_resume_retention_refuses_lost_history_parent_links_archives_or_host_copy(
        self,
    ):
        for defect in (
            "source removed",
            "source messages",
            "source parent",
            "inventory parent",
            "state source",
            "state missing",
            "state association",
            "deleted association",
            "state checkpoint",
            "private copy",
            "host copy",
            "deleted copy",
        ):
            harness, sources, copies, state, _calls = self.resume_harness()
            harness.archive_roots("ses_history_active")
            child, active = "ses_history_child", "ses_history_active"
            if defect == "source removed":
                sources.pop(child)
            elif defect == "source messages":
                sources[child]["messages"] = []
            elif defect == "source parent":
                sources[child]["info"]["parentID"] = "ses_other"
            elif defect == "inventory parent":
                rows = harness.box.list_container_sessions(harness.data)
                next(row for row in rows if row["id"] == child)["parentID"] = (
                    "ses_other"
                )
                harness.box.list_container_sessions = Mock(return_value=rows)
            elif defect == "state source":
                state["sessions"].remove(child)
            elif defect == "state missing":
                state["missing"] = []
            elif defect == "state association":
                state["host_sessions"][child] = harness.archive_id(active)
            elif defect == "deleted association":
                state["host_sessions"]["ses_history_deleted"] = harness.archive_id(
                    active
                )
            elif defect == "state checkpoint":
                state["synced"][child] = "old"
            elif defect == "private copy":
                harness.archive_path(child).write_text("{}")
            elif defect == "host copy":
                copies[harness.archive_id(child)]["messages"] = []
            else:
                copies[harness.archive_id("ses_history_deleted")]["messages"] = []
            with self.subTest(defect=defect), self.assertRaises(runner.Refusal):
                harness.verify_resume_retention(harness.resume_sources)

    def test_unarchived_next_start_requires_actual_target_not_only_last_metadata(self):
        harness, sources, _copies, state, _calls = self.resume_harness()
        harness.archive_roots("ses_history_active")
        new = "ses_history_new"
        sources[new] = harness.seed_value(new, "/synthetic/source-a")
        sources[new].pop("location")
        harness.archived_restart(new)
        self.assertEqual(state["last"], harness.resume_target)
        for selected in (
            new,
            "ses_history_child",
            "ses_history_archived_child",
            "ses_other",
        ):
            with self.subTest(selected=selected), self.assertRaises(runner.Refusal):
                harness.unarchived_restart(selected)
            self.assertNotIn("unarchived_next_start_resume", harness.checks)
        harness.provider.requests += 1
        with self.assertRaisesRegex(runner.Refusal, "provider-contract"):
            harness.unarchived_restart(harness.resume_target)
        self.assertNotIn("resume_no_inference", harness.checks)

    def test_unarchive_requires_strictly_newest_native_metadata_with_bounded_wait(self):
        harness, sources, _copies, _state, _calls = self.resume_harness()
        harness.archive_roots("ses_history_active")
        new = "ses_history_new"
        sources[new] = harness.seed_value(new, "/synthetic/source-a")
        sources[new].pop("location")
        # A future-dated eligible root must not produce a metadata-only pass.
        sources[new]["info"]["time"]["updated"] = 10000
        with (
            patch.object(runner.time, "monotonic", side_effect=(0, 6)),
            self.assertRaisesRegex(runner.Refusal, "gate-timeout"),
        ):
            harness.archived_restart(new)
        harness.request.assert_called_once_with(
            "PATCH",
            "/api/session/ses_history_archived",
            {"archived": False, "title": "SYNTHETIC_UNARCHIVED_RESUME_ROOT"},
        )
        self.assertNotIn("unarchived_root_eligible", harness.checks)

    def test_archive_roots_requires_real_no_content_mutations_and_empty_resume_choice(
        self,
    ):
        for defect in (
            "json success",
            "root unchanged",
            "child changed",
            "source history",
            "last still set",
        ):
            harness, sources, _copies, state, _calls = self.resume_harness()
            batch, sync = harness.batch.side_effect, harness.sync.side_effect

            def mutate(operations):
                result = batch(operations)
                if defect == "json success":
                    return [{"success": True}] * len(operations)
                if defect == "root unchanged":
                    sources["ses_history_active"]["info"]["time"].pop("archived")
                elif defect == "child changed":
                    sources["ses_history_child"]["info"]["time"]["archived"] = 1
                elif defect == "source history":
                    sources["ses_history_active"]["messages"] = []
                return result

            def wrong_last():
                sync()
                state["last"] = "ses_history_archived"

            harness.batch.side_effect = mutate
            if defect == "last still set":
                harness.sync.side_effect = wrong_last
            with self.subTest(defect=defect), self.assertRaises(runner.Refusal):
                harness.archive_roots("ses_history_active")
            self.assertNotIn("all_archived_no_resume", harness.checks)

    def test_launch_uses_ordinary_no_vault_start_and_observes_its_selected_session(
        self,
    ):
        harness = self.harness()
        selected = "ses_native_selection"
        harness.validate = Mock()
        harness.stopped = Mock()
        harness.bind = Mock(return_value=selected)
        harness.f.no_vault_probe = contextlib.nullcontext
        harness.service.Owner = SimpleNamespace(publish=Mock(), wait_clients=Mock())
        harness.service.api = Mock(return_value={"native": True})

        @contextlib.contextmanager
        def attach(_box, _path, _data):
            yield {"session": selected}

        harness.service.attach = attach
        harness.box.run_tui = Mock()
        harness.box.main = Mock(
            side_effect=lambda arguments: harness.box.run_tui(["native-client"])
        )
        harness.path, harness.data = self.parent / "workspace.json", {}
        observed = []

        def archived_restart(ident):
            observed.append(ident)
            return 0

        harness.launch(archived_restart)
        harness.box.main.assert_called_once_with(
            [
                "start",
                "--project",
                str(self.parent / "source-a"),
                "--no-vault",
            ]
        )
        harness.bind.assert_called_once_with(["native-client"])
        self.assertEqual(observed, [selected])
        self.assertEqual(harness.exits, [0])
        harness.stopped.assert_called_once()

    def resume_run_harness(self):
        harness, _sources, _copies, _state, _calls = self.resume_harness()
        harness.validate = Mock()
        harness.stopped = Mock()
        harness.same_identity = Mock()
        harness.f.prepare_native_cli = Mock()
        harness.box.main = Mock(return_value=0)
        harness.box.sync_sessions = Mock()
        harness.lease = SimpleNamespace(join=Mock())
        harness.shell_id = "ses_history_active"
        harness.box.read_json = Mock(
            return_value={
                "messages": [{"type": "shell", "status": "exited", "exit": 0}]
            }
        )
        return harness

    def test_run_preserves_old_launch_sequence_then_three_final_ordinary_starts(self):
        harness = self.resume_run_harness()
        stages = []

        def launch(callback, *, expected=0, **_options):
            stages.append(callback.__name__)
            harness.exits.append(expected)

        harness.launch = launch
        harness.run()
        self.assertEqual(
            stages,
            [
                "graph",
                "readiness",
                "background",
                "shell",
                "archive_roots",
                "archived_restart",
                "unarchived_restart",
            ],
        )
        self.assertEqual(tuple(harness.exits), (0, 0, 0, 1, 0, 0, 0, 0))
        self.assertEqual(tuple(harness.exits), runner.EXPECTED_EXITS)
        self.assertEqual(
            runner.LAUNCH_STAGES, tuple(stages[:4] + ["recovery-sync"] + stages[4:])
        )
        harness.box.main.assert_called_once_with(["sync", "--workspace", "fixture"])
        harness.lease.join.assert_called_once_with(5)
        self.assertEqual(harness.stage, "complete")
        self.assertIn("resume_no_inference", harness.checks)

    def test_resume_no_inference_covers_startup_and_post_callback_shutdown(self):
        for boundary in ("startup", "shutdown"):
            for counter in ("requests", "errors"):
                with self.subTest(boundary=boundary, counter=counter):
                    harness = self.resume_run_harness()

                    def launch(callback, *, expected=0, **_options):
                        if (
                            boundary == "startup"
                            and callback.__name__ == "archive_roots"
                        ) or (
                            boundary == "shutdown"
                            and callback.__name__ == "unarchived_restart"
                        ):
                            setattr(
                                harness.provider,
                                counter,
                                getattr(harness.provider, counter) + 1,
                            )
                        harness.exits.append(expected)

                    harness.launch = launch
                    with self.assertRaises(runner.Refusal) as caught:
                        harness.run()
                    self.assertEqual(str(caught.exception), "provider-contract")
                    self.assertNotIn("resume_no_inference", harness.checks)
                    self.assertNotEqual(harness.stage, "complete")

    def pending_harness(self):
        """Synthetic observer inputs, never a production-runtime replacement."""
        harness = self.harness()
        harness.path, harness.data = self.parent / "workspace.json", {}
        harness.shell_id = "ses_history_shell"
        harness.box.BoxError = type("BoxError", (Exception,), {})
        harness.box.HistoryPending = type("HistoryPending", (harness.box.BoxError,), {})
        harness.box.opencode_v2_io = SimpleNamespace(
            UNSETTLED="OpenCode 2.x history is unsettled; retry sync after completion."
        )
        failure = harness.box.BoxError(harness.pending_message())
        calls = []
        harness.pending_earlier = "ses_history_earlier"
        harness.pending_later = "ses_history_after_pending"
        harness.pending_earlier_checkpoint = hashlib.sha256(
            b"earlier-before"
        ).hexdigest()
        harness.archive_id = lambda ident: "archive_" + ident
        harness.archive_path = lambda ident: self.parent / (ident + ".json")
        synced = {}
        for ident, content in (
            (harness.pending_earlier, b"earlier-advanced"),
            (harness.pending_later, b"later-new"),
        ):
            harness.archive_path(ident).write_bytes(content)
            synced[ident] = hashlib.sha256(content).hexdigest()
        harness.box.session_state = Mock(
            return_value={
                "sessions": [
                    harness.pending_earlier,
                    harness.shell_id,
                    harness.pending_later,
                ],
                "synced": synced,
                "host_sessions": {ident: harness.archive_id(ident) for ident in synced},
            }
        )
        harness.verify_archive = Mock(
            side_effect=lambda ident: calls.append("archive:" + ident)
        )
        harness.shell_before = {"host": {}, "private": b"old", "synced": "old"}
        harness.snapshot = Mock(
            side_effect=lambda _ident: (
                calls.append("snapshot") or copy.deepcopy(harness.shell_before)
            )
        )
        harness.gate = SimpleNamespace(release=lambda: calls.append("release"))
        harness.shell_worker = SimpleNamespace(
            join=lambda timeout: calls.append("join")
        )
        harness.export = Mock(
            return_value={
                "messages": [
                    {
                        "type": "shell",
                        "status": "exited",
                        "exit": 0,
                        "output": {"output": "SYNTHETIC_HISTORY_SHELL_DONE"},
                    }
                ]
            }
        )

        def original(path, data):
            calls.append("actual-sync")
            for ident in (
                harness.pending_earlier,
                harness.shell_id,
                harness.pending_later,
            ):
                try:
                    harness.box.export_container_session(data, ident)
                except harness.box.BoxError:
                    pass
            raise failure from None

        def refusing(data, ident):
            if ident == harness.shell_id:
                raise harness.box.HistoryPending(harness.box.opencode_v2_io.UNSETTLED)

        harness.box.export_container_session = refusing
        return harness, original, failure, calls

    def test_pending_observer_releases_after_actual_refusal_and_reraises_same_error(
        self,
    ):
        harness, original, failure, calls = self.pending_harness()
        with self.assertRaises(harness.box.BoxError) as raised:
            harness.pending_observer(original)(None, None)
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            calls,
            [
                "actual-sync",
                "archive:" + harness.pending_earlier,
                "archive:" + harness.pending_later,
                "snapshot",
                "release",
                "join",
            ],
        )
        for name in (
            "pending_aggregate_reason",
            "pending_session_order",
            "pending_earlier_progress",
            "pending_later_progress",
            "pending_archive_preservation",
            "pending_exit",
        ):
            self.assertTrue(harness.checks[name])
        calls.clear()
        self.assertEqual(harness.pending_observer(lambda *_: 7)(None, None), 7)
        self.assertEqual(calls, [])
        # A refusal from any other session (or an unrelated failure) never
        # authorizes release, pending-exit credit, or completion probing.
        harness.checks.clear()

        def other(path, data):
            try:
                harness.box.export_container_session(data, "ses_history_other")
            except harness.box.BoxError:
                raise failure from None

        harness.box.export_container_session = lambda data, ident: (
            _ for _ in ()
        ).throw(failure)
        with self.assertRaises(runner.Refusal):
            harness.pending_observer(other)(None, None)
        self.assertEqual(calls, [])
        self.assertNotIn("pending_exit", harness.checks)

    def test_pending_progress_oracles_refuse_before_gate_release(self):
        for defect in (
            "missing later checkpoint",
            "unchanged earlier checkpoint",
            "wrong later checkpoint",
            "wrong host association",
            "wrong loop order",
            "unverified native archive",
            "changed private archive",
            "changed native archive",
            "changed pending checkpoint",
        ):
            harness, original, _failure, calls = self.pending_harness()
            state = harness.box.session_state.return_value
            if defect == "missing later checkpoint":
                state["synced"].pop(harness.pending_later)
            elif defect == "unchanged earlier checkpoint":
                state["synced"][harness.pending_earlier] = (
                    harness.pending_earlier_checkpoint
                )
            elif defect == "wrong later checkpoint":
                state["synced"][harness.pending_later] = "wrong-digest"
            elif defect == "wrong host association":
                state["host_sessions"][harness.pending_later] = "unrelated-archive"
            elif defect == "wrong loop order":
                state["sessions"] = [
                    harness.pending_earlier,
                    harness.pending_later,
                    harness.shell_id,
                ]
            elif defect == "unverified native archive":
                harness.verify_archive.side_effect = runner.Refusal("oracle")
            else:
                key = {
                    "changed private archive": "private",
                    "changed native archive": "host",
                    "changed pending checkpoint": "synced",
                }[defect]
                after = copy.deepcopy(harness.shell_before)
                after[key] = "changed"
                harness.snapshot.side_effect = None
                harness.snapshot.return_value = after
            with self.subTest(defect=defect), self.assertRaises(runner.Refusal):
                harness.pending_observer(original)(None, None)
            self.assertNotIn("release", calls)
            self.assertNotIn("join", calls)
            self.assertNotIn("pending_archive_preservation", harness.checks)
            self.assertNotIn("pending_exit", harness.checks)
            harness.export.assert_not_called()

    def test_pending_reason_oracles_refuse_wrong_type_reason_or_aggregate(self):
        for defect in (
            "wrong export type",
            "wrong fixed reason",
            "wrong aggregate reason",
            "wrong aggregate count",
            "extra private diagnostic",
        ):
            harness, original, failure, calls = self.pending_harness()
            if defect in ("wrong export type", "wrong fixed reason"):
                error = (
                    harness.box.BoxError(harness.box.opencode_v2_io.UNSETTLED)
                    if defect == "wrong export type"
                    else harness.box.HistoryPending("private-sentinel")
                )
                harness.box.export_container_session = Mock(side_effect=error)
            elif defect == "wrong aggregate reason":
                failure.args = ("1 session(s) not settled yet: private-sentinel",)
            elif defect == "wrong aggregate count":
                failure.args = (
                    harness.pending_message().replace("1 session(s)", "2 session(s)"),
                )
            else:
                failure.args = (harness.pending_message() + " private-sentinel",)
            with self.subTest(defect=defect), self.assertRaises(runner.Refusal):
                harness.pending_observer(original)(None, None)
            self.assertEqual(calls, ["actual-sync"])
            harness.snapshot.assert_not_called()
            harness.verify_archive.assert_not_called()
            harness.export.assert_not_called()
            self.assertNotIn("pending_aggregate_reason", harness.checks)
            self.assertNotIn("pending_exit", harness.checks)

    def test_pending_order_requires_actual_forwarded_exports_not_only_state_order(self):
        harness, _original, failure, calls = self.pending_harness()

        def wrong_order(path, data):
            for ident in (
                harness.pending_later,
                harness.shell_id,
                harness.pending_earlier,
            ):
                try:
                    harness.box.export_container_session(data, ident)
                except harness.box.HistoryPending:
                    pass
            raise failure

        with self.assertRaises(runner.Refusal):
            harness.pending_observer(wrong_order)(None, None)
        self.assertEqual(calls, [])
        harness.verify_archive.assert_not_called()
        harness.snapshot.assert_not_called()
        self.assertNotIn("pending_session_order", harness.checks)
        self.assertNotIn("pending_exit", harness.checks)

    def test_direct_pending_export_requires_fixed_reason_before_check_credit(self):
        for reason, accepted in (
            ("OpenCode 2.x history is unsettled; retry sync after completion.", True),
            ("private-sentinel", False),
        ):
            harness, _original, _failure, calls = self.pending_harness()
            harness.box.export_container_session = Mock(
                side_effect=harness.box.HistoryPending(reason)
            )
            with self.subTest(accepted=accepted):
                if accepted:
                    harness.refuse_incomplete_export()
                    self.assertTrue(harness.checks["pending_fixed_reason"])
                    self.assertTrue(harness.checks["incomplete_export_refusal"])
                    self.assertEqual(calls, ["snapshot"])
                else:
                    with self.assertRaises(runner.Refusal):
                        harness.refuse_incomplete_export()
                    self.assertEqual(calls, [])
                    self.assertNotIn("pending_fixed_reason", harness.checks)

    def test_shell_prepares_earlier_update_and_new_later_session_without_sync(self):
        harness, _original, _failure, _calls = self.pending_harness()
        harness.connection = {}
        state = harness.box.session_state.return_value
        state["sessions"].remove(harness.pending_later)
        state["synced"].pop(harness.pending_later)
        state["host_sessions"].pop(harness.pending_later)
        harness.archive_path(harness.pending_later).unlink()

        def exported(_data, ident):
            if ident == "ses_history_background":
                return {"info": {"id": ident}}
            raise harness.box.HistoryPending(harness.box.opencode_v2_io.UNSETTLED)

        harness.box.export_container_session = exported
        harness.export = Mock(return_value={"messages": []})
        harness.request = Mock(
            return_value={"data": [{"type": "shell", "status": "running"}]}
        )
        harness.service.api = Mock(return_value={"data": {}})
        harness.sync = Mock()
        harness.seed = Mock()
        worker = SimpleNamespace(done=threading.Event())
        gate = SimpleNamespace(command=lambda: "synthetic-command")
        with (
            patch.object(runner, "ShellGate", return_value=gate),
            patch.object(runner, "Worker", return_value=worker),
        ):
            self.assertEqual(harness.shell("ses_selected"), 0)
        harness.request.assert_any_call(
            "PATCH",
            "/api/session/" + harness.pending_earlier,
            {"title": "SYNTHETIC_PROGRESS_BEFORE_PENDING"},
        )
        self.assertEqual(
            harness.pending_earlier_checkpoint,
            state["synced"][harness.pending_earlier],
        )
        harness.seed.assert_called_once_with(harness.pending_later)
        harness.sync.assert_not_called()
        self.assertNotIn(harness.pending_later, state["synced"])

    def test_workers_have_bounded_join_and_suppress_private_errors(self):
        worker = runner.Worker(lambda: 42)
        self.assertEqual(worker.join(), 42)
        worker = runner.Worker(
            lambda: (_ for _ in ()).throw(ValueError("private-sentinel"))
        )
        with self.assertRaisesRegex(runner.Refusal, "worker-failed"):
            worker.join()
        gate = threading.Event()
        worker = runner.Worker(lambda: gate.wait(2))
        try:
            with self.assertRaisesRegex(runner.Refusal, "worker-timeout"):
                worker.join(0)
        finally:
            gate.set()
            worker.join()

    def test_shell_gate_release_retries_failure_and_is_idempotent_after_success(self):
        harness = self.harness()
        harness.container_python = Mock()
        gate = runner.ShellGate(harness)
        self.assertIn("select.select", gate.command())
        harness.container_python.side_effect = runner.Refusal("worker-failed")
        with self.assertRaises(runner.Refusal):
            gate.release()
        self.assertFalse(gate.released)
        harness.container_python.side_effect = None
        gate.release()
        count = harness.container_python.call_count
        gate.release()
        self.assertEqual(harness.container_python.call_count, count)

    def test_cleanup_releases_all_gates_and_preserves_primary_exception(self):
        primary = ValueError("primary-private-sentinel")
        secondary = []
        release = Mock(side_effect=OSError("secondary-private-sentinel"))
        join = Mock(side_effect=runner.Refusal("worker-timeout"))
        with self.assertRaises(ValueError) as raised:
            try:
                raise primary
            finally:
                runner.finish_actions((release, join), secondary)
        self.assertIs(raised.exception, primary)
        self.assertEqual(secondary, ["io-error", "worker-timeout"])
        join.assert_called_once()
        secondary.clear()
        with self.assertRaises(OSError):
            runner.finish_actions((release, join), secondary)
        self.assertEqual(secondary, ["worker-timeout"])

    def test_pair_observer_preserves_results_and_records_only_waiter_booleans(self):
        ident = "ses_history_background"
        busy = {"data": {ident: {"type": "running"}}}
        idle = {"data": {}}
        job = {
            "data": [
                {
                    "sessionID": ident,
                    "kind": "shell",
                    "status": "running",
                    "command": "private-sentinel",
                }
            ]
        }
        empty = {"data": []}
        request = Mock()
        observer = runner.PairObserver(request, ident)
        observer.thread = threading.get_ident()
        for phase, active, jobs in (
            ("foreground", busy, empty),
            ("background", idle, job),
            ("wake", busy, empty),
            ("quiet", idle, empty),
        ):
            observer.phase = phase
            for _ in range(4):
                for target, response in (
                    ("/api/session/active", active),
                    ("/api/experimental/job", jobs),
                ):
                    request.return_value = response
                    self.assertIs(
                        observer(None, {}, {}, "GET", target, timeout=1), response
                    )
            self.assertEqual(observer.counts[phase], 4)
            self.assertIsNone(observer.active)
        # A cross-pair empty-active/empty-jobs observation during wake is NOT a
        # claimed running wakeup, nor is a request from a non-waiter thread.
        observer.phase = "wake"
        for target, response in (
            ("/api/session/active", idle),
            ("/api/experimental/job", empty),
        ):
            request.return_value = response
            observer(None, {}, {}, "GET", target)
        self.assertEqual(observer.counts["wake"], 4)
        observer.thread = -1
        request.return_value = busy
        observer(None, {}, {}, "GET", "/api/session/active")
        self.assertIsNone(observer.active)
        self.assertEqual(
            set(observer.counts), {"foreground", "background", "wake", "quiet"}
        )

    def test_pair_waiter_cannot_finish_early_or_skip_required_pairs(self):
        observer = runner.PairObserver(Mock(), "ses_fixture")
        worker = SimpleNamespace(done=threading.Event())
        with self.assertRaisesRegex(runner.Refusal, "worker-timeout"):
            observer.wait("background", 4, worker, timeout=0)
        observer.counts["background"] = 4
        observer.wait("background", 4, worker)
        worker.done.set()
        with self.assertRaisesRegex(runner.Refusal, "oracle"):
            observer.wait("background", 4, worker)

    def test_synthetic_provider_background_protocol_requires_real_tool_and_notice(self):
        provider = runner.Provider.__new__(runner.Provider)
        provider.background = {
            "command": "synthetic-command",
            "workdir": "/synthetic/project",
            "background": True,
        }
        provider.background_requests = 0
        provider.woken, provider.wake_release = threading.Event(), threading.Event()
        provider.wake_release.set()
        body = {
            "messages": [],
            "tools": [
                {
                    "function": {
                        "name": "shell",
                        "parameters": {
                            "properties": {
                                "command": {"type": "string"},
                                "workdir": {"type": "string"},
                                "background": {"type": "boolean"},
                            }
                        },
                    }
                }
            ],
        }
        delta, finish = provider.reply(body)
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(
            json.loads(delta["tool_calls"][0]["function"]["arguments"]),
            provider.background,
        )
        body["messages"].append({"role": "tool", "content": "SYNTHETIC_RUNNING_JOB"})
        self.assertEqual(
            provider.reply(body), ({"content": "FOREGROUND_IDLE_MARKER"}, "stop")
        )
        body["messages"].append(
            {
                "role": "user",
                "content": '<shell state="completed">SYNTHETIC_HISTORY_BG_DONE</shell>',
            }
        )
        self.assertEqual(
            provider.reply(body), ({"content": "REAL_WAKE_ACK_MARKER"}, "stop")
        )
        self.assertTrue(provider.woken.is_set())
        self.assertEqual(provider.background_requests, 3)
        with self.assertRaisesRegex(runner.Refusal, "provider-contract"):
            provider.reply(body)

    def test_native_config_selector_and_vault_guards_are_file_only(self):
        harness = self.harness()
        del harness.validate_native_config
        harness.distro = "tumbleweed"
        harness.provider = SimpleNamespace(config=lambda: {"synthetic": True})
        harness.f.PASS_ENV = "SYNTHETIC_UNUSED_NAME"
        harness.f.vault = SimpleNamespace(
            vault_path=lambda: self.parent / "absent-vault"
        )
        harness.f.native_wrapper_source = lambda: "# synthetic native selector\n"
        wrapper = self.parent / "bin/opencode"
        wrapper.parent.mkdir()
        wrapper.write_text(harness.f.native_wrapper_source())
        wrapper.chmod(0o700)
        harness.f.safe_path = lambda root, name: root / name
        mapping = {
            "config/opencode/opencode.json": harness.provider.config(),
            "config/oc-box/config.json": {
                "default_distro": harness.distro,
                "pass_env": [harness.f.PASS_ENV],
                "auto_update": False,
            },
            "executables.json": {"2": str(harness.binary)},
        }
        harness.f.private_json = lambda root, name: mapping[name]
        harness.validate_native_config()
        for key, before in mapping.items():
            mapping[key] = {"foreign": True}
            try:
                with self.subTest(key=key), self.assertRaises(runner.Refusal):
                    harness.validate_native_config()
            finally:
                mapping[key] = before
        wrapper.write_text("# foreign selector\n")
        with self.assertRaisesRegex(runner.Refusal, "native-changed"):
            harness.validate_native_config()
        wrapper.write_text(harness.f.native_wrapper_source())
        (self.parent / "absent-vault").write_text("synthetic unexpected vault")
        with self.assertRaisesRegex(runner.Refusal, "fixture-only"):
            harness.validate_native_config()

    def test_background_client_is_joined_after_wake_gate_not_initial_idle(self):
        # Offline scheduling oracle only. This models the separately probed
        # native CLI staying attached while background jobs remain; no transport
        # from this unit fixture is used by the HOST runner.
        harness = self.harness()
        ident = "ses_history_background"
        harness.data, harness.connection = {}, {}
        harness.seed = Mock(return_value=ident)
        harness.box.container_project = Mock(return_value="/synthetic/project")
        harness.provider = SimpleNamespace(
            background=None,
            woken=threading.Event(),
            wake_release=threading.Event(),
            background_requests=3,
            errors=0,
        )
        harness.provider.woken.set()
        job_released = threading.Event()
        gate = SimpleNamespace(
            command=lambda: "synthetic-command", release=job_released.set
        )
        notice = {
            "type": "synthetic",
            "metadata": {
                "source": "shell",
                "state": "completed",
                "exit": 0,
                "shellID": "sh_fixture",
                "jobID": "sh_fixture",
            },
            "text": "SYNTHETIC_HISTORY_BG_DONE",
        }
        harness.request = Mock(return_value={"data": [notice]})
        harness.export = Mock(
            return_value={
                "messages": [notice],
                "marker": "FOREGROUND_IDLE_MARKER REAL_WAKE_ACK_MARKER",
            }
        )
        harness.sync, harness.verify_archive = Mock(), Mock()

        def api(_box, _data, _connection, _method, target, _value=None, **_options):
            if target == "/api/session/active":
                return {
                    "data": {ident: {"type": "running"}}
                    if job_released.is_set()
                    and not harness.provider.wake_release.is_set()
                    else {}
                }
            return {
                "data": []
                if job_released.is_set()
                else [
                    {
                        "sessionID": ident,
                        "kind": "shell",
                        "status": "running",
                        "shellID": "sh_fixture",
                        "id": "sh_fixture",
                    }
                ]
            }

        harness.service.api = api

        def history_wait(*_args, **_kwargs):
            while not harness.provider.wake_release.is_set():
                for route in ("/api/session/active", "/api/experimental/job"):
                    harness.service.api(None, {}, {}, "GET", route)
                time.sleep(0.001)
            for _ in range(2):
                for route in ("/api/session/active", "/api/experimental/job"):
                    harness.service.api(None, {}, {}, "GET", route)

        harness.service.wait_history = history_wait
        harness.native_run = lambda *_: (
            0 if harness.provider.wake_release.wait(2) else 1
        )
        original_worker = runner.Worker
        test = self

        class ObservedWorker(original_worker):
            def __init__(self, callback):
                self.client = callback.__name__ == "<lambda>"
                super().__init__(callback)

            def join(self, timeout=20):
                if self.client:
                    test.assertTrue(harness.provider.wake_release.is_set())
                return super().join(min(timeout, 2))

        with (
            patch.object(runner, "ShellGate", return_value=gate),
            patch.object(runner, "Worker", ObservedWorker),
        ):
            self.assertEqual(harness.background("ses_parent"), 0)
        self.assertTrue(harness.checks["background_final_quiet"])
        harness.sync.assert_called_once()

    def test_ignored_source_files_refuse_except_inert_bytecode_caches(self):
        self.source_files()
        tolerated = (
            b"src/oc_box/__pycache__/oc_box.cpython-313.pyc\0"
            b"src/oc_box.egg-info/PKG-INFO\0"
        )
        self.ignored = tolerated
        with patch.object(runner, "git", side_effect=self.git):
            runner.source_snapshot(self.source, self.commit)
        for stray in (
            b"src/oc_box/injected.py",
            b"src/oc_box/sourceless.pyc",
            b"tools/__pycache__/helper.py",
            b"src/oc_box.egg-info.py",
            b"src/oc_box.egg-info/injected.py",
            b"src/oc_box/x__pycache__/helper.pyc",
        ):
            self.ignored = tolerated + stray + b"\0"
            with (
                self.subTest(stray=stray),
                patch.object(runner, "git", side_effect=self.git),
                self.assertRaisesRegex(runner.Refusal, "clean-source-required"),
            ):
                runner.source_snapshot(self.source, self.commit)

    def test_loaded_source_modules_must_be_hashed_tracked_files(self):
        self.source_files()
        tracked = {name: "0" * 64 for name in self.files}
        module = ModuleType("tools.helper")
        module.__file__ = str(self.source / "tools/test_global_vault_podman.py")
        with patch.object(sys, "modules", {"tools.helper": module}):
            runner.import_guard(self.source, tracked)
            module.__file__ = str(self.source / "tools/untracked_helper.py")
            with self.assertRaisesRegex(runner.Refusal, "source-shadowing"):
                runner.import_guard(self.source, tracked)
            # oc_box/tools names must still resolve inside the source.
            module.__file__ = str(self.parent / "elsewhere.py")
            with self.assertRaisesRegex(runner.Refusal, "source-shadowing"):
                runner.import_guard(self.source, tracked)
        other = ModuleType("unrelated_library")
        other.__file__ = str(self.parent / "elsewhere.py")
        with patch.object(sys, "modules", {"unrelated_library": other}):
            runner.import_guard(self.source, tracked)

    def run_main(self, command, **patches):
        console = io.StringIO()
        console.close = lambda: None
        with (
            patch.object(runner, "host_guard"),
            patch.object(runner.tempfile, "mkdtemp") as allocate,
            patch.object(runner.sys, "flags", SimpleNamespace(isolated=1)),
            patch.object(runner.sys, "dont_write_bytecode", True),
            patch.object(runner.os, "fdopen", return_value=console),
            patch.multiple(runner, **patches),
        ):
            code = runner.main(command)
        return code, console.getvalue(), allocate

    def test_work_temp_and_native_locations_cannot_be_inside_source(self):
        base = [
            "--source",
            str(self.source),
            "--expect-commit",
            self.commit,
            "--opencode-v2",
            "/synthetic/native",
        ]
        for option, value in (
            ("--work-dir", self.source / "work"),
            ("--temp-dir", self.source / "temporary"),
        ):
            value.mkdir()
            locations = {"--work-dir": self.parent, "--temp-dir": self.parent}
            locations[option] = value
            command = [*base]
            for name, location in locations.items():
                command += [name, str(location)]
            with self.subTest(option=option):
                source = Mock()
                code, console, allocate = self.run_main(command, load_source=source)
                self.assertEqual(code, 1)
                self.assertIn("FAIL category=placement", console)
                source.assert_not_called()
                allocate.assert_not_called()
        loaded = SimpleNamespace(
            fixture=SimpleNamespace(host_guard=Mock()),
            box=SimpleNamespace(DISTROS={"tumbleweed": None}),
            interactive=SimpleNamespace(
                native_binary=lambda _path: self.source / "node_modules/.bin/opencode"
            ),
            source=self.source,
            identity={},
        )
        code, console, allocate = self.run_main(
            [*base, "--work-dir", str(self.parent)],
            load_source=Mock(return_value=loaded),
        )
        self.assertEqual(code, 1)
        self.assertIn("FAIL category=placement", console)
        allocate.assert_not_called()

    def test_stale_bytecode_is_never_executed_in_the_isolated_runner(self):
        # Real interpreter semantics: a matching-mtime/size __pycache__ entry
        # normally replaces the hashed .py with different code.
        package = self.parent / "bytecode"
        package.mkdir()
        module = package / "target.py"
        module.write_text("VALUE = 'B' * 8\n")
        script = (
            "import importlib.util,importlib._bootstrap_external as b,marshal,os,sys\n"
            "spec=importlib.util.spec_from_file_location('r',sys.argv[1])\n"
            "r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)\n"
            "m=sys.argv[2]\n"
            "info=os.stat(m)\n"
            "cache=importlib.util.cache_from_source(m)\n"
            "os.makedirs(os.path.dirname(cache),exist_ok=True)\n"
            "code=compile(\"VALUE = 'A' * 8\\n\",m,'exec')\n"
            "open(cache,'wb').write(b._code_to_timestamp_pyc(code,int(info.st_mtime),info.st_size))\n"
            "sys.path.insert(0,os.path.dirname(m))\n"
            "if sys.argv[3]=='isolate': r.isolate_bytecode()\n"
            "import target\n"
            "print(target.VALUE)\n"
        )
        outputs = {}
        for mode in ("plain", "isolate"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    script,
                    str(SCRIPT),
                    str(module),
                    mode,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[mode] = result.stdout.strip()
        self.assertEqual(outputs["plain"], "AAAAAAAA")
        self.assertEqual(outputs["isolate"], "BBBBBBBB")

    def test_linked_host_child_refusal_must_name_the_linked_child_guard(self):
        harness = self.harness()
        harness.box.BoxError = type("BoxError", (Exception,), {})
        for text, accepted in (
            (
                "Host archive has linked child sessions; refusing recursive replacement.",
                True,
            ),
            ("OpenCode 2.x history sync failed.", False),
        ):
            harness.sync = Mock(side_effect=harness.box.BoxError(text))
            with self.subTest(text=text):
                if accepted:
                    harness.refuse_linked_children()
                else:
                    with self.assertRaises(runner.Refusal):
                        harness.refuse_linked_children()
        harness.sync = Mock(return_value=None)
        with self.assertRaisesRegex(runner.Refusal, "unexpected-success"):
            harness.refuse_linked_children()

    def test_shell_gate_release_waits_for_the_actual_fifo_reader(self):
        # Real FIFO semantics: the controller retries ENXIO until the gated
        # command has opened its reader, then releases exactly that command.
        import shlex

        fifo = self.parent / "gate"
        os.mkfifo(fifo, 0o600)
        outputs = []

        class Harness:
            def container_python(inner, code, payload=None, **_options):
                if "mkfifo" in code:
                    return ""
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    check=False,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                outputs.append(result.returncode)
                return result.stdout

        gate = runner.ShellGate(Harness())
        gate.path = str(fifo)
        reader = threading.Timer(
            0.4,
            lambda: outputs.append(
                subprocess.run(
                    shlex.split(gate.command()),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stdout.strip()
            ),
        )
        reader.start()
        gate.release()
        reader.join()
        self.assertTrue(gate.released)
        self.assertIn(0, outputs)
        self.assertIn("SYNTHETIC_HISTORY_SHELL_DONE", outputs)

    def test_foreground_waiter_must_span_observed_active_pairs_before_release(self):
        ident = "ses_history_foreground"
        busy = {"data": {ident: {"type": "running"}}}
        observer = runner.PairObserver(Mock(side_effect=lambda *_a, **_k: busy), ident)
        observer.phase = "foreground"
        observer.thread = threading.get_ident()
        # A deadline-only/idle waiter observes no foreground pairs.
        for target in ("/api/session/active", "/api/experimental/job"):
            observer.original.side_effect = None
            observer.original.return_value = {"data": {} if "active" in target else []}
            observer(None, {}, {}, "GET", target)
        self.assertEqual(observer.counts["foreground"], 0)
        worker = SimpleNamespace(done=threading.Event())
        with self.assertRaisesRegex(runner.Refusal, "worker-timeout"):
            observer.wait("foreground", 4, worker, timeout=0)
        for _ in range(4):
            for target, response in (
                ("/api/session/active", busy),
                ("/api/experimental/job", {"data": []}),
            ):
                observer.original.return_value = response
                observer(None, {}, {}, "GET", target)
        self.assertEqual(observer.counts["foreground"], 4)
        observer.wait("foreground", 4, worker)

    def test_foreground_provider_is_not_released_before_four_active_pairs(self):
        # Offline scheduling oracle only; no transport from this unit fixture is
        # used by the HOST runner.
        harness = self.harness()
        ident = "ses_history_foreground"
        harness.data, harness.connection = {}, {}
        harness.box.container_project = Mock(return_value="/synthetic/project")
        harness.seed = Mock(return_value=ident)
        harness.request = Mock()
        main = threading.get_ident()
        observed = []

        class Gate(threading.Event):
            def set(inner):
                observed.append(sum(1 for item in polls if item))
                super().set()

        polls = []
        harness.provider = SimpleNamespace(
            entered=threading.Event(), release=Gate(), requests=1, errors=0
        )
        harness.provider.entered.set()

        def api(_box, _data, _connection, _method, target, _value=None, **_options):
            if target == "/api/session/active":
                busy = not harness.provider.release.is_set()
                if threading.get_ident() != main:
                    polls.append(busy)
                return {"data": {ident: {"type": "running"}} if busy else {}}
            return {"data": []}

        calls = []

        def history_wait(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                return
            while not harness.provider.release.is_set():
                for route in ("/api/session/active", "/api/experimental/job"):
                    harness.service.api(None, {}, {}, "GET", route)
                time.sleep(0.001)
            for _ in range(2):
                for route in ("/api/session/active", "/api/experimental/job"):
                    harness.service.api(None, {}, {}, "GET", route)

        harness.service.api = api
        harness.service.wait_history = history_wait
        harness.native_run = lambda *_: 0 if harness.provider.release.wait(5) else 1
        harness.export = Mock(
            return_value={"marker": "SYNTHETIC_HISTORY_FOREGROUND_DONE"}
        )
        harness.sync = Mock(side_effect=runner.Refusal("stop-after-foreground"))
        with (
            patch.dict(
                sys.modules,
                {
                    "oc_box": SimpleNamespace(
                        session_terminal=SimpleNamespace(
                            choose_terminal=lambda *_: {"status": "running"}
                        )
                    ),
                    "oc_box.session_terminal": SimpleNamespace(
                        choose_terminal=lambda *_: {"status": "running"}
                    ),
                },
            ),
            self.assertRaisesRegex(runner.Refusal, "stop-after-foreground"),
        ):
            harness.readiness("ses_parent")
        # The first release is the oracle; the cleanup release is idempotent.
        self.assertGreaterEqual(observed[0], 4)
        self.assertTrue(harness.checks["foreground_wait_pending"])
        self.assertTrue(harness.checks["foreground_recovery"])
        self.assertTrue(harness.checks["idle_pty_readiness"])

    def test_bytecode_isolation_precedes_the_first_source_import(self):
        order = []
        fixture = SimpleNamespace(
            APP=self.source,
            box=SimpleNamespace(PACKAGE=self.source / "src/oc_box"),
            workspace_service=SimpleNamespace(),
        )

        def imported(name):
            order.append(("import", name))
            return fixture

        with (
            patch.object(runner, "source_snapshot", return_value={"sha256": {}}),
            patch.object(runner, "import_guard"),
            patch.object(
                runner, "isolate_bytecode", side_effect=lambda: order.append("isolate")
            ),
            patch.object(runner.importlib, "import_module", side_effect=imported),
            patch.object(sys, "path", list(sys.path)),
        ):
            runner.load_source(self.source, self.commit)
        self.assertEqual(order[0], "isolate")
        self.assertEqual(len(order), 3)

    def test_failure_trace_reports_only_code_locations_never_messages(self):
        def inner():
            raise KeyError("private-sentinel-key")

        def worker():
            try:
                inner()
            except KeyError as error:
                raise runner.Refusal("worker-failed") from error

        try:
            worker()
        except runner.Refusal as error:
            entries = runner.trace(error)
        self.assertEqual([entry["type"] for entry in entries], ["Refusal", "KeyError"])
        self.assertIn("inner", " ".join(entries[1]["frames"]))
        self.assertTrue(
            all(
                runner.FRAME.fullmatch(frame)
                for entry in entries
                for frame in entry["frames"]
            )
        )
        self.assertNotIn("private-sentinel", json.dumps(entries))
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "version": "1.6.51+g" + self.commit[:7],
            "dirty": False,
            "sha256": {"src/oc_box/oc_box.py": "c" * 64},
        }
        native = {"version": "2.2.6", "sha256": "e" * 64}
        report = runner.result(
            {}, identity, "d" * 64, native, "operation-failed", failure_trace=entries
        )
        self.assertEqual(report["failure_trace"], entries)
        for bad in (
            [{"type": "KeyError", "frames": ["/etc/passwd:read:1"]}],
            [{"type": "private sentinel", "frames": []}],
            [{"type": "KeyError", "frames": [], "message": "private-sentinel"}],
            [{"type": "KeyError", "frames": []}] * 5,
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(runner.Refusal, "report-schema"),
            ):
                runner.result({}, identity, "d" * 64, native, None, failure_trace=bad)

    def test_worker_failure_keeps_original_location_reachable(self):
        worker = runner.Worker(
            lambda: (_ for _ in ()).throw(ValueError("private-sentinel"))
        )
        with self.assertRaises(runner.Refusal) as raised:
            worker.join()
        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertEqual(str(raised.exception), "worker-failed")

    def test_archive_verification_accepts_untitled_launcher_session(self):
        # The launcher creates the selected session without a title, and native
        # 2.2.6 omits the field; production archives it as "Session".
        harness = self.harness()
        harness.data, harness.path = {}, self.parent / "workspace.json"
        harness.archive_id = lambda ident, prefix="ses": prefix + "_archive_" + ident
        harness.archive_path = lambda ident: self.parent / (ident + ".json")
        harness.box.verify_host_archive = Mock()
        for title, expected in ((None, "Session"), ("Named source", "Named source")):
            info = {"id": "ses_source", "time": {"created": 1}}
            if title is not None:
                info["title"] = title
            source = {
                "info": info,
                "messages": [{"id": "msg_source", "type": "user", "text": "x"}],
            }

            def archive(label):
                return {
                    "info": {
                        "id": "ses_archive_ses_source",
                        "title": label,
                        "time": {"created": 1},
                    },
                    "messages": [
                        {"id": "msg_archive_msg_source", "type": "user", "text": "x"}
                    ],
                }

            for label, accepted in (
                ("[oc-box source-a] " + expected, True),
                ("[oc-box source-a] Wrong", False),
            ):
                harness.export = Mock(
                    side_effect=lambda ident, host=False, label=label, source=source: (
                        archive(label) if host else source
                    )
                )
                harness.box.read_json = Mock(return_value=archive(label))
                with self.subTest(title=title, label=label):
                    if accepted:
                        harness.verify_archive("ses_source")
                    else:
                        with self.assertRaises(runner.Refusal):
                            harness.verify_archive("ses_source")

    def recovery_harness(self, case="snapshot"):
        old = self.harness()
        value = runner.RecoveryHarness(
            old.loaded, self.parent, old.binary, old.runner, case
        )
        value.box.PACKAGE = self.source
        (self.source / "history_reader.py").write_text("# pinned reader helper\n")
        value.container_id = "a" * 64
        value.provider = SimpleNamespace(requests=2, errors=0)
        value.box.HistoryWaitTimeout = type("HistoryWaitTimeout", (Exception,), {})
        value.timeout = value.box.HistoryWaitTimeout()
        value.timeout.recovery_eligible = True
        value.timeout.diagnostic = "History wait timeout: phase=poll-delay; last complete observation: foreground=0, jobs-running=0, jobs-completed=0, jobs-error=0, jobs-cancelled=1."
        value.cancelled_pairs, value.elapsed, value.owner_closed = 2, 30, True
        return value

    def test_recovery_reports_keep_default_evidence_separate_and_require_real_counts(
        self,
    ):
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "version": "1.7.51+g" + self.commit[:7],
            "dirty": False,
            "sha256": {"src/oc_box/history_reader.py": "c" * 64},
        }
        native = {"version": "2.2.6", "sha256": "d" * 64}
        self.assertEqual(len(runner.CHECKS), 42)
        self.assertEqual(runner.EXPECTED_EXITS, (0, 0, 0, 1, 0, 0, 0, 0))
        for case, count in (("snapshot", 12), ("stop-cancel", 6)):
            observations = {
                "cancelled_pairs": 2,
                "restarts": int(case == "snapshot"),
                "reader_leases": int(case == "snapshot"),
                "prepare_calls": 0,
                "auth_calls": 0,
                "provider_requests": 2,
                "shell_starts": 1,
                "explicit_stop_exit_code": 0 if case == "stop-cancel" else None,
            }

            def report(*, observations=observations, case=case, **changes):
                settings = {
                    "stage": "complete",
                    "exits": (0, 1),
                    "observations": observations,
                    "checks": dict.fromkeys(runner.RECOVERY_CHECKS[case], True),
                }
                settings.update(changes)
                return runner.recovery_result(
                    case, identity=identity, runner="e" * 64, native=native, **settings
                )

            passed = report()
            self.assertEqual(passed["status"], "PASS")
            self.assertEqual(passed["format"], "oc-box-history-recovery-host-v1")
            self.assertEqual(len(passed["checks"]), count)
            self.assertEqual(passed["launcher_exit_codes"], [0, 1])
            for name in runner.RECOVERY_CHECKS[case]:
                self.assertEqual(
                    report(
                        checks=dict.fromkeys(
                            runner.RECOVERY_CHECKS[case] - {name}, True
                        )
                    )["status"],
                    "FAIL",
                )
            for key, wrong in (
                ("cancelled_pairs", 0),
                ("restarts", 2),
                ("reader_leases", 2),
                ("prepare_calls", 1),
                ("auth_calls", 1),
                ("provider_requests", 3),
                ("shell_starts", 2),
            ):
                self.assertEqual(
                    report(observations={**observations, key: wrong})["status"],
                    "FAIL",
                    key,
                )
            for exits in ((), (0, 0), (1, 0)):
                self.assertEqual(report(exits=exits)["status"], "FAIL")
            for bad_native in ("2.2.7", "2.2.8"):
                with (
                    patch.dict(native, {"version": bad_native}),
                    self.assertRaises(runner.Refusal),
                ):
                    report()
            with self.assertRaises(runner.Refusal):
                report(observations={"private-sentinel": 0})

    def test_recovery_option_is_opt_in_and_all_cases_keep_the_early_host_guard(self):
        base = [
            "--source",
            str(self.source),
            "--expect-commit",
            self.commit,
            "--work-dir",
            str(self.parent),
            "--opencode-v2",
            "/synthetic/native",
        ]
        self.assertIsNone(runner.parser().parse_args(base).history_recovery_case)
        for case in (None, *runner.RECOVERY_CHECKS):
            with (
                patch.object(runner, "Harness") as default,
                patch.object(runner, "RecoveryHarness") as recovery,
            ):
                arguments = (object(), self.parent, Path("/synthetic/native"), "d" * 64)
                value = runner.build_harness(*arguments, recovery_case=case)
                if case is None:
                    default.assert_called_once_with(*arguments)
                    recovery.assert_not_called()
                    self.assertIs(value, default.return_value)
                else:
                    recovery.assert_called_once_with(*arguments, case)
                    default.assert_not_called()
                    self.assertIs(value, recovery.return_value)
        for case in runner.RECOVERY_CHECKS:
            arguments = [*base, "--history-recovery-case", case]
            self.assertEqual(
                runner.parser().parse_args(arguments).history_recovery_case, case
            )
            with (
                patch.object(
                    runner, "host_guard", side_effect=runner.Refusal("host-only")
                ),
                patch.object(runner, "load_source") as source,
                patch.object(runner.tempfile, "mkdtemp") as allocate,
                patch.object(runner, "RecoveryHarness") as harness,
            ):
                self.assertEqual(runner.main(arguments), 1)
            source.assert_not_called()
            allocate.assert_not_called()
            harness.assert_not_called()

    def test_recovery_reproduction_cannot_accept_a_drained_job_or_shortened_wait(self):
        # A genuine final tiny-budget query timeout may preserve the two prior
        # validated pairs. The typed production hint, not its final phase, rules.
        for phase in ("foreground-query", "background-query", "poll-delay"):
            h = self.recovery_harness()
            h.timeout.diagnostic = h.timeout.diagnostic.replace(
                "phase=poll-delay", "phase=" + phase
            )
            h.reproduction()
        for defect in (
            None,
            "not eligible",
            "drained",
            "no pairs",
            "short wait",
            "owner alive",
            "wrong error",
        ):
            h = self.recovery_harness()
            if defect == "not eligible":
                h.timeout.recovery_eligible = False
            elif defect == "drained":
                h.timeout.diagnostic = h.timeout.diagnostic.replace(
                    "jobs-cancelled=1", "jobs-cancelled=0"
                )
            elif defect == "no pairs":
                h.cancelled_pairs = 0
            elif defect == "short wait":
                h.elapsed = 1
            elif defect == "owner alive":
                h.owner_closed = False
            elif defect == "wrong error":
                h.timeout = ValueError("private-sentinel")
            with self.subTest(defect=defect):
                if defect is None:
                    h.reproduction()
                else:
                    with self.assertRaises(runner.Refusal):
                        h.reproduction()

    def test_reader_observer_guards_both_stores_before_forwarding_the_real_popen(self):
        for defect in (None, "source rows", "destination schedule", "actual failure"):
            h = self.recovery_harness()
            h.closed.set()
            h.job_rows = [["job_synthetic", "running"]]
            calls = []

            def metadata(*, source=True, calls=calls, defect=defect, h=h):
                calls.append("source" if source else "destination")
                if not source and defect == "destination schedule":
                    raise runner.Refusal("oracle")
                return {
                    "jobs": [] if not source or defect == "source rows" else h.job_rows
                }

            h.metadata = metadata
            child, failure = Mock(), OSError("private-sentinel")

            def popen(
                *args,
                calls=calls,
                defect=defect,
                failure=failure,
                child=child,
                **kwargs,
            ):
                calls.append("popen")
                if defect == "actual failure":
                    raise failure
                return child

            h.raw_popen = Mock(side_effect=popen)
            command = [
                "podman",
                "exec",
                "-i",
                h.container_id,
                "/usr/bin/env",
                "-i",
                (h.box.PACKAGE / "history_reader.py").read_text(),
                "lease",
            ]
            with self.subTest(defect=defect):
                if defect is None:
                    self.assertIs(
                        h.observe_popen(
                            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
                        ),
                        child,
                    )
                    self.assertEqual(calls, ["source", "destination", "popen"])
                    self.assertEqual(h.readers, [child])
                else:
                    with self.assertRaises(
                        OSError if defect == "actual failure" else runner.Refusal
                    ) as raised:
                        h.observe_popen(
                            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
                        )
                    if defect == "actual failure":
                        self.assertIs(raised.exception, failure)
                    else:
                        h.raw_popen.assert_not_called()
                    self.assertEqual(h.readers, [])

    def test_engine_observer_returns_actual_results_and_checks_env_before_selector(
        self,
    ):
        h = self.recovery_harness()
        h.closed.set()
        h.recovering = lambda: True
        h.source_fence = True
        h.box.history_reader = SimpleNamespace(current=lambda: None)
        response = SimpleNamespace(
            stdout=json.dumps([{"Id": h.container_id, "State": {"Running": False}}]),
            returncode=0,
        )
        h.raw_run = Mock(return_value=response)
        command = ["podman", "container", "inspect", h.container_id]
        self.assertIs(h.observe_run(command, private=True), response)
        self.assertEqual(h.transitions, [("inspect", True, False)])
        self.assertEqual(h.stop_proofs, [False])
        response.stdout = json.dumps(
            [
                {
                    "Id": h.container_id,
                    "State": {
                        "Running": False,
                        "Pid": 0,
                        "ConmonPid": 0,
                        "Status": "exited",
                    },
                }
            ]
        )
        self.assertIs(h.observe_run(command, private=True), response)
        self.assertEqual(h.stop_proofs, [False, True])
        env = runner.recovery_fence()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(env["OPENCODE_CONFIG_CONTENT"])
        self.assertIs(
            h.observe_run(["opencode", "debug", "paths", "db"], env=env), response
        )
        for key in runner.recovery_fence():
            bad = {name: value for name, value in env.items() if name != key}
            h.raw_run.reset_mock()
            with self.subTest(missing=key), self.assertRaises(runner.Refusal):
                h.observe_run(["opencode", "debug", "paths", "db"], env=bad)
            h.raw_run.assert_not_called()
        error = OSError("private-sentinel")
        h.raw_run.side_effect = error
        with self.assertRaises(OSError) as raised:
            h.observe_run(command)
        self.assertIs(raised.exception, error)

    def test_stop_worker_waits_for_real_close_then_publishes_intent_before_release(
        self,
    ):
        for defect in (None, "no timeout", "holder exited", "stop exited"):
            h = self.recovery_harness("stop-cancel")
            h.closed.set()
            h.path, h.stop_token = self.parent / "workspace.json", "b" * 64
            calls = []
            h.reproduction = Mock(
                side_effect=lambda calls=calls: calls.append("timeout")
            )
            if defect == "no timeout":
                h.reproduction.side_effect = runner.Refusal("oracle")
            h.holder = SimpleNamespace(
                poll=lambda defect=defect: 0 if defect == "holder exited" else None
            )
            stopper = SimpleNamespace(
                poll=lambda defect=defect: 0 if defect == "stop exited" else None,
                wait=lambda calls=calls, **kw: calls.append("stop return") or 0,
                stdin=SimpleNamespace(
                    write=lambda value, calls=calls: (
                        calls.append("signal")
                        if value == b"stop\n"
                        else self.fail("Wrong stop signal")
                    ),
                    flush=lambda: None,
                    close=lambda: None,
                ),
            )
            h.stopper = stopper
            h.control_process = Mock(
                side_effect=lambda action, calls=calls, stopper=stopper: (
                    calls.append(action) or stopper
                )
            )
            h.box.workspace_control = SimpleNamespace(
                unchanged=lambda *args, calls=calls: calls.append("intent") or False
            )
            h.release_admission = lambda calls=calls: calls.append("release")
            with self.subTest(defect=defect):
                if defect is None:
                    h.stop_after_close()
                    self.assertEqual(
                        calls, ["timeout", "signal", "intent", "release", "stop return"]
                    )
                    self.assertEqual(
                        set(h.checks),
                        {
                            "stop_after_real_timeout",
                            "stop_intent_before_admission",
                            "explicit_stop_success",
                        },
                    )
                    h.control_process.assert_not_called()
                else:
                    with self.assertRaises(runner.Refusal):
                        h.stop_after_close()
                    self.assertNotIn("explicit_stop_success", h.checks)
                    if defect == "no timeout":
                        h.control_process.assert_not_called()
                    else:
                        self.assertEqual(calls[-1], "release")

    def test_recovery_file_gate_release_is_safe_without_a_live_reader_and_counts_execution(
        self,
    ):
        import shlex

        h = SimpleNamespace(data={"home": str(self.parent)})
        (self.parent / ".local/share").mkdir(parents=True)

        def python(code, payload):
            with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                exec(compile(code, "fixture-gate", "exec"), {})

        h.container_python = python
        gate = runner.FileGate(h)
        gate.release()  # No reader exists, unlike a FIFO.
        gate.release()
        args = shlex.split(gate.command())
        with (
            patch.object(sys, "argv", args[2:]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(compile(args[2], "fixture-command", "exec"), {})
        self.assertEqual((Path(gate.path) / "starts").read_bytes(), b"x")
        self.assertTrue((Path(gate.path) / "release").is_file())

    def test_recovery_cleanup_bounds_and_reaps_children_even_after_timeout(self):
        child = Mock()
        child.wait.side_effect = [
            subprocess.TimeoutExpired("private-sentinel", 5),
            subprocess.TimeoutExpired("private-sentinel", 3),
            0,
        ]
        with self.assertRaisesRegex(runner.Refusal, "worker-timeout"):
            runner.RecoveryHarness.reap(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        child.stdin.close.assert_called_once()
        child.stdout.close.assert_called_once()

    def test_owner_close_observer_forwards_before_recording_and_never_waives_failure(
        self,
    ):
        h = self.recovery_harness()
        h.stage, h.returned_at, h.data = "cancelled", time.monotonic() - 30, {}
        h.service.live_connection = lambda: None
        h.service.socket_path = lambda *_: self.parent / "absent-socket"
        owner = SimpleNamespace(
            child=SimpleNamespace(returncode=0),
            clients=0,
            thread=SimpleNamespace(is_alive=lambda: False),
            listener=SimpleNamespace(fileno=lambda: -1),
            _history_timeout=h.timeout,
        )
        sentinel = object()

        def closed(actual):
            self.assertIs(actual, owner)
            self.assertFalse(h.closed.is_set())
            return sentinel

        h.raw_shutdown = closed
        self.assertIs(h.observe_shutdown(owner), sentinel)
        self.assertTrue(h.closed.is_set())
        h.reproduction()
        h.closed.clear()
        error = OSError("private-sentinel")
        h.raw_shutdown = Mock(side_effect=error)
        with self.assertRaises(OSError) as raised:
            h.observe_shutdown(owner)
        self.assertIs(raised.exception, error)
        self.assertFalse(h.closed.is_set())

    def test_scheduler_probe_is_read_only_and_refuses_every_row_not_only_due_work(self):
        import sqlite3

        database = self.parent / "native-fixture.db"
        for state in (None, "paused", "completed", "cancelled", "unknown"):
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS schedule(id TEXT PRIMARY KEY, session_id TEXT, status TEXT, last_message_id TEXT)"
                )
                db.execute("CREATE TABLE IF NOT EXISTS job(id TEXT PRIMARY KEY)")
                db.execute("DELETE FROM schedule")
                if state:
                    db.execute(
                        "INSERT INTO schedule VALUES('sch_synthetic','ses_synthetic',?,NULL)",
                        (state,),
                    )
            before = database.read_bytes()
            with (
                self.subTest(status=state),
                patch.object(sys, "argv", ["probe", str(database)]),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                if state is None:
                    exec(
                        compile(runner.METADATA_HELPER, "fixture-scheduler", "exec"), {}
                    )
                    self.assertEqual(json.loads(output.getvalue()), {"jobs": []})
                else:
                    with self.assertRaises(AssertionError):
                        exec(
                            compile(
                                runner.METADATA_HELPER, "fixture-scheduler", "exec"
                            ),
                            {},
                        )
            self.assertEqual(database.read_bytes(), before)

    def test_recovery_selector_preserves_only_public_fences_without_manufacturing_them(
        self,
    ):
        from tools import test_global_vault_podman as donor

        fence = runner.recovery_fence()
        fence["OPENCODE_CONFIG_CONTENT"] = json.dumps(fence["OPENCODE_CONFIG_CONTENT"])
        for opt_in, incoming, accepted in (
            (False, fence, True),
            (True, {}, True),
            (True, fence, True),
            (True, {**fence, "OPENCODE_CONFIG_CONTENT": "{}"}, False),
            (True, {**fence, "OPENCODE_FILEWATCHER_DISABLE": "0"}, False),
        ):
            script = donor.native_wrapper_source(
                root=self.parent, binary="/synthetic/native", history_recovery=opt_in
            )
            with (
                self.subTest(opt_in=opt_in, incoming=incoming),
                patch.dict(
                    os.environ,
                    {
                        **incoming,
                        "PRIVATE_API_KEY": "private-sentinel",
                        "OPENCODE_EXTRA": "private-sentinel",
                    },
                    clear=True,
                ),
                patch.object(sys, "argv", ["opencode", "debug", "paths", "db"]),
                patch.object(os, "execve") as execute,
            ):
                if accepted:
                    exec(compile(script, "fixture-selector", "exec"), {})
                else:
                    with self.assertRaises(AssertionError):
                        exec(compile(script, "fixture-selector", "exec"), {})
            if not accepted:
                execute.assert_not_called()
                continue
            actual = execute.call_args.args[2]
            self.assertEqual(
                execute.call_args.args[1], ["/synthetic/native", "debug", "paths", "db"]
            )
            self.assertNotIn("PRIVATE_API_KEY", actual)
            self.assertNotIn("OPENCODE_EXTRA", actual)
            self.assertEqual("OPENCODE_FLAGS" in actual, opt_in and bool(incoming))
            if opt_in and incoming:
                self.assertTrue(runner.fenced(actual))
        self.assertEqual(
            donor.native_wrapper_source(),
            donor.native_wrapper_source(history_recovery=False),
        )


if __name__ == "__main__":
    unittest.main()
