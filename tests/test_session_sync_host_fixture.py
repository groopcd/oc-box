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

    def source_files(self):
        self.ignored = b""
        self.files = {name: b"# audited helper\n" for name in runner.DONORS}
        self.files["src/oc_box/oc_box.py"] = b"# audited runtime\n"
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
        self.source_files()
        with patch.object(runner, "git", side_effect=self.git):
            identity = runner.source_snapshot(self.source, self.commit)
        self.assertEqual(identity["version"], "1.6.51+g" + self.commit[:7])
        self.assertFalse(identity["dirty"])
        self.assertEqual(
            identity["sha256"],
            {
                name: hashlib.sha256(value).hexdigest()
                for name, value in self.files.items()
            },
        )

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
            "tools.native_host_profile",
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
            exits=(0, 0, 0, 1, 0),
            stage="complete",
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["launcher_stages"], list(runner.LAUNCH_STAGES))
        self.assertEqual(report["expected_launcher_exit_codes"], [0, 0, 0, 1, 0])
        for exits, stage in (
            ((), "complete"),
            ((0, 0, 0, 1), "complete"),
            ((0, 0, 0, 0, 0), "complete"),
            ((0, 1, 0, 0, 0), "complete"),
            (runner.EXPECTED_EXITS, "recovery-sync"),
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
            ({}, None, (), (0,) * 6),
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
        with self.assertRaisesRegex(runner.Refusal, "report-schema"):
            runner.result(
                {},
                {**identity, "version": "1.7.51+g" + self.commit[:7]},
                digest,
                native,
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
            fixture=SimpleNamespace(
                host_guard=Mock(), NATIVE_VERSIONS=runner.NATIVE_VERSIONS
            ),
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

    def test_exact_profiles_thread_to_current_harness_without_changing_default_checks(
        self,
    ):
        old = self.harness()
        current = runner.build_harness(
            old.loaded, self.parent, old.binary, old.runner, native_version="2.3.0"
        )
        self.assertEqual(current.native["version"], "2.3.0")
        self.assertEqual(current.native["path"], str(old.binary))
        self.assertEqual(current.native["sha256"], runner.file_hash(old.binary))
        self.assertEqual(len(runner.CHECKS), 33)
        self.assertEqual(runner.EXPECTED_EXITS, (0, 0, 0, 1, 0))
        self.assertIn("tools/native_host_profile.py", runner.DONORS)
        for version in ("2.2.7", "2.3.1", "2"):
            with self.subTest(version=version), self.assertRaises(runner.Refusal):
                runner.Harness(
                    old.loaded,
                    self.parent,
                    old.binary,
                    old.runner,
                    native_version=version,
                )

    def test_current_recovery_setup_holds_real_job_without_reload_or_release(self):
        old = self.harness()
        h = runner.RecoveryHarness(
            old.loaded,
            self.parent,
            old.binary,
            old.runner,
            "snapshot",
            native_version="2.3.0",
        )
        h.baseline_ids = {"ses_selected", "ses_baseline"}
        h.box.session_state = Mock(return_value={"sessions": list(h.baseline_ids)})
        h.f.verify_owned_resources = Mock(return_value={"Id": "c" * 64})
        h.container_id = "c" * 64
        h.owner = SimpleNamespace(
            _history_source=SimpleNamespace(stop_token="synthetic")
        )
        h.provider = SimpleNamespace(requests=2, errors=0)
        h.data = {"project": "/synthetic/project"}
        h.box.container_project = lambda _data: "/synthetic/project"
        job = {
            "id": "job_held",
            "sessionID": "ses_selected",
            "kind": "shell",
            "status": "running",
        }
        h.request = Mock(
            side_effect=lambda method, route: {
                "/api/session/active": {"data": {}},
                "/api/experimental/job": {"data": [job]},
            }[route]
        )
        h.export = Mock(return_value={"messages": [{"text": "FOREGROUND_IDLE_MARKER"}]})
        h.metadata = lambda source=True: (
            {"jobs": [["durable-held-row"]], "starts": "x"} if source else {"jobs": []}
        )
        gate = SimpleNamespace(command=lambda: "synthetic held shell", release=Mock())
        with (
            patch.object(runner, "FileGate", return_value=gate),
            patch.object(runner, "Worker"),
        ):
            self.assertEqual(h.held_running("ses_selected"), 0)
        self.assertTrue(h.provider.background["background"])
        self.assertEqual(h.job, job)
        self.assertEqual(h.retained_status, "running")
        gate.release.assert_not_called()
        self.assertTrue(all(call.args[0] == "GET" for call in h.request.call_args_list))
        self.assertFalse(
            any("reload" in call.args[1] for call in h.request.call_args_list)
        )

    def test_current_reproduction_requires_running_pairs_and_complete_original_timeout(
        self,
    ):
        old = self.harness()
        for defect in (
            None,
            "cancelled diagnostic",
            "no pairs",
            "short wait",
            "owner alive",
        ):
            h = runner.RecoveryHarness(
                old.loaded,
                self.parent,
                old.binary,
                old.runner,
                "snapshot",
                native_version="2.3.0",
            )
            h.box.HistoryWaitTimeout = type("HistoryWaitTimeout", (Exception,), {})
            h.timeout = h.box.HistoryWaitTimeout()
            h.timeout.recovery_eligible = True
            h.timeout.diagnostic = "History wait timeout: phase=poll-delay; last complete observation: foreground=0, jobs-running=1, jobs-completed=0, jobs-error=0, jobs-cancelled=0."
            h.running_pairs, h.elapsed, h.owner_closed = 2, 30, True
            if defect == "cancelled diagnostic":
                h.timeout.diagnostic = h.timeout.diagnostic.replace(
                    "jobs-running=1", "jobs-running=0"
                )
            elif defect == "no pairs":
                h.running_pairs = 0
            elif defect == "short wait":
                h.elapsed = 1
            elif defect == "owner alive":
                h.owner_closed = False
            with self.subTest(defect=defect):
                if defect is None:
                    h.reproduction()
                else:
                    with self.assertRaises(runner.Refusal):
                        h.reproduction()

    def test_current_recovery_report_does_not_relabel_cancelled_job_evidence(self):
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "version": "1.6.51+g" + self.commit[:7],
            "dirty": False,
            "sha256": {"src/oc_box/history_reader.py": "c" * 64},
        }
        native = {"version": "2.3.0", "sha256": "d" * 64, "path": "/synthetic/native"}
        for case in runner.RECOVERY_CHECKS:
            observations = {
                "cancelled_pairs": 0,
                "running_pairs": 2,
                "native_client_exit": 1,
                "restarts": int(case == "snapshot"),
                "reader_leases": int(case == "snapshot"),
                "prepare_calls": 0,
                "auth_calls": 0,
                "provider_requests": 2,
                "shell_starts": 1,
                "explicit_stop_exit_code": 0 if case == "stop-cancel" else None,
                "recovery_calls": 1,
                "snapshot_completed": int(case == "snapshot"),
            }

            def report(observed):
                return runner.recovery_result(
                    case,
                    dict.fromkeys(runner.RECOVERY_CHECKS[case], True),
                    identity,
                    "e" * 64,
                    native,
                    exits=(0, 1),
                    stage="complete",
                    observations=observed,
                )

            current = report(observations)
            self.assertEqual(current["status"], "PASS")
            self.assertEqual(current["launcher_stages"], ["primer", "held_running"])
            self.assertIn("2.3.0", current["scope"])
            for missing in ("running_pairs", "native_client_exit"):
                self.assertEqual(
                    report({k: v for k, v in observations.items() if k != missing})[
                        "status"
                    ],
                    "FAIL",
                )
            for key, wrong in (
                ("running_pairs", 0),
                ("cancelled_pairs", 2),
                ("native_client_exit", 0),
                ("restarts", 2),
                ("reader_leases", 2),
                ("prepare_calls", 1),
                ("auth_calls", 1),
                ("provider_requests", 3),
            ):
                self.assertEqual(
                    report({**observations, key: wrong})["status"], "FAIL", key
                )

    def test_recovery_reports_keep_default_evidence_separate_and_require_real_counts(
        self,
    ):
        identity = {
            "commit": self.commit,
            "commit_count": 80,
            "version": "1.6.51+g" + self.commit[:7],
            "dirty": False,
            "sha256": {"src/oc_box/history_reader.py": "c" * 64},
        }
        native = {"version": "2.2.6", "sha256": "d" * 64}
        self.assertEqual(len(runner.CHECKS), 33)
        self.assertEqual(runner.EXPECTED_EXITS, (0, 0, 0, 1, 0))
        self.assertEqual(
            runner.LAUNCH_STAGES,
            ("graph", "readiness", "background", "shell", "recovery-sync"),
        )
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
                "recovery_calls": 1,
                "snapshot_completed": int(case == "snapshot"),
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
            self.assertEqual(passed["format"], "oc-box-history-recovery-host-v2")
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
                ("recovery_calls", 2),
                ("snapshot_completed", int(case != "snapshot")),
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

    def test_recovery_phase_excludes_budgeted_outer_cleanup_without_relaxing_trace(
        self,
    ):
        from oc_box import history_recovery

        h = self.recovery_harness()
        h.closed.set()
        h.source_fence = True
        h.box.history_recovery = history_recovery
        running, results = True, []

        def engine(command, **_options):
            nonlocal running
            action = command[1:]
            if action[:1] == ["stop"]:
                running = False
            elif action[:1] == ["start"]:
                running = True
            value = [
                {
                    "Id": h.container_id,
                    "State": {
                        "Running": running,
                        "Pid": 100 if running else 0,
                        "ConmonPid": 100 if running else 0,
                        "Status": "running" if running else "exited",
                    },
                }
            ]
            result = SimpleNamespace(returncode=0, stdout=json.dumps(value))
            results.append(result)
            return result

        h.raw_run = Mock(side_effect=engine)
        inspect = ["podman", "container", "inspect", h.container_id]

        def actual_recovery():
            self.assertTrue(h.recovering())
            with history_recovery.budget(30):
                for command in (
                    inspect,
                    ["podman", "stop", "--time", "10", h.container_id],
                    inspect,
                    ["podman", "start", h.container_id],
                    inspect,
                    ["podman", "stop", "--time", "10", h.container_id],
                    inspect,
                ):
                    self.assertIs(h.observe_run(command), results[-1])
            return True

        h.raw_recovery = Mock(side_effect=actual_recovery)
        self.assertIs(h.observe_recovery(), True)
        with history_recovery.budget(30):
            self.assertFalse(h.recovering())
            self.assertIs(h.observe_run(inspect), results[-1])
        self.assertEqual(h.raw_run.call_count, 8)
        self.assertEqual(h.recovery_calls, 1)
        self.assertEqual(h.recovery_results, [True])
        self.assertEqual(len(h.transitions), 7)
        self.assertEqual(h.stop_proofs, [True, True])
        h.verify_recovery_transitions()
        h.transitions.append(("inspect", True, False))
        with self.assertRaises(runner.Refusal):
            h.verify_recovery_transitions()

    def test_recovery_observer_preserves_failed_results_exceptions_and_phase_reset(
        self,
    ):
        h = self.recovery_harness()
        h.raw_recovery = Mock(return_value=False)
        self.assertIs(h.observe_recovery("public-argument"), False)
        h.raw_recovery.assert_called_once_with("public-argument")
        self.assertEqual(h.recovery_results, [False])
        self.assertFalse(h.recovering())
        failure = OSError("private-sentinel")
        h.raw_recovery.side_effect = failure
        with self.assertRaises(OSError) as raised:
            h.observe_recovery()
        self.assertIs(raised.exception, failure)
        self.assertEqual(h.recovery_calls, 2)
        self.assertEqual(h.recovery_results, [False])
        self.assertFalse(h.recovering())

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

    def test_current_owner_shutdown_finishes_native_client_before_recovery_admission(
        self,
    ):
        old = self.harness()
        for exit_code in (1, 0, None):
            h = runner.RecoveryHarness(
                old.loaded,
                self.parent,
                old.binary,
                old.runner,
                "snapshot",
                native_version="2.3.0",
            )
            h.stage, h.returned_at, h.data = "held_running", time.monotonic() - 30, {}
            h.service.live_connection = lambda: None
            h.service.socket_path = lambda *_: self.parent / "absent-socket"
            owner = SimpleNamespace(
                child=SimpleNamespace(returncode=0),
                clients=0,
                thread=SimpleNamespace(is_alive=lambda: False),
                listener=SimpleNamespace(fileno=lambda: -1),
                _history_timeout=object(),
            )
            calls = []

            def close(actual):
                self.assertIs(actual, owner)
                self.assertFalse(h.closed.is_set())
                calls.append("owner-closed")
                return "actual-shutdown-result"

            def join(timeout):
                self.assertEqual(timeout, 10)
                self.assertFalse(h.closed.is_set())
                calls.append("native-client-finished")
                return exit_code

            h.raw_shutdown = close
            h.client = SimpleNamespace(join=join)
            with self.subTest(exit_code=exit_code):
                if exit_code == 1:
                    self.assertEqual(
                        h.observe_shutdown(owner), "actual-shutdown-result"
                    )
                    self.assertTrue(h.closed.is_set())
                else:
                    with self.assertRaises(runner.Refusal):
                        h.observe_shutdown(owner)
                    self.assertFalse(h.closed.is_set())
                self.assertEqual(calls, ["owner-closed", "native-client-finished"])

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
