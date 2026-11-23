"""Offline sharing-runner contracts, not external-host/Podman acceptance.

Donor fixture, engine and native boundaries are mocked. The only real child
processes are small Python stubs exercising generic PTY/pipe transport; none
executes this runner, OpenCode, Podman, FUSE, or a production share command.
"""

import contextlib
import copy
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import test_sharing_podman as runner


COMMIT = "b" * 40
PRIVATE = "SYNTHETIC_PRIVATE_ERROR_NOT_FOR_REPORTS"
NAMESPACE = "oc_box_share_" + "a" * 32


class OfflineCase(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="sharing-runner-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def enter(self, manager):
        # unittest.TestCase.enterContext is unavailable on supported Python 3.10.
        return self.stack.enter_context(manager)

    def refused(self, category):
        return self.assertRaisesRegex(runner.Refusal, "^" + category + "$")

    def arguments(self, *extra):
        return [
            "--source",
            "/synthetic/source",
            "--expect-commit",
            COMMIT,
            "--work-dir",
            "/synthetic/work",
            "--opencode-v2",
            "/synthetic/native",
            *extra,
        ]

    def child_arguments(self, mode="share", *extra):
        return [
            "--source",
            "/synthetic/source",
            "--expect-commit",
            COMMIT,
            "--child",
            mode,
            "--fixture",
            "/synthetic/fixture",
            "--project-name",
            "source-a",
            *(["--input-name", "share-input-a"] if mode == "share" else []),
            *extra,
        ]


class SharingOptionsAndGuardTests(OfflineCase):
    def test_parent_and_bounded_child_modes_have_distinct_valid_options(self):
        for arguments in (
            self.arguments(),
            self.arguments(
                "--mount-probe", "--require-selinux", "--temp-dir", "/synthetic/tmp"
            ),
            self.child_arguments(),
            self.child_arguments(
                "share", "--single-file", "--acknowledge-risk", "--protected"
            ),
            self.child_arguments("owner"),
            self.child_arguments("owner", "--reset"),
        ):
            with self.subTest(arguments=arguments):
                runner.validate_options(runner.parser().parse_args(arguments))

    def test_exact_lowercase_full_commit_is_required(self):
        args = runner.parser().parse_args(self.arguments())
        for commit in (
            None,
            "",
            "b" * 7,
            "b" * 39,
            "b" * 41,
            "B" * 40,
            "g" * 40,
            COMMIT + "\n",
            " " + COMMIT,
            "HEAD",
            "main",
        ):
            with self.subTest(commit=commit), self.refused("invalid-options"):
                args.expect_commit = commit
                runner.validate_options(args)

    def test_parent_rejects_all_child_only_switches_and_missing_paths(self):
        mutations = {
            "fixture": Path("/synthetic/fixture"),
            "project_name": "source-a",
            "input_name": "share-input-a",
            "single_file": True,
            "acknowledge_risk": True,
            "reset": True,
            "protected": True,
            "work_dir": None,
            "opencode_v2": None,
        }
        for name, value in mutations.items():
            args = runner.parser().parse_args(self.arguments())
            setattr(args, name, value)
            with self.subTest(name=name), self.refused("invalid-options"):
                runner.validate_options(args)

    def test_children_reject_parent_setup_switches_and_missing_selectors(self):
        mutations = {
            "fixture": None,
            "project_name": None,
            "work_dir": Path("/synthetic/work"),
            "temp_dir": Path("/synthetic/tmp"),
            "opencode_v2": Path("/synthetic/native"),
            "mount_probe": True,
            "require_selinux": True,
        }
        for mode in ("owner", "share"):
            for name, value in mutations.items():
                args = runner.parser().parse_args(self.child_arguments(mode))
                setattr(args, name, value)
                with (
                    self.subTest(mode=mode, name=name),
                    self.refused("invalid-options"),
                ):
                    runner.validate_options(args)

    def test_owner_cannot_share_and_share_cannot_reset(self):
        cases = [("share", "reset", True), ("share", "input_name", None)]
        cases.extend(
            ("owner", name, value)
            for name, value in (
                ("input_name", "share-input-a"),
                ("single_file", True),
                ("acknowledge_risk", True),
                ("protected", True),
            )
        )
        for mode, name, value in cases:
            args = runner.parser().parse_args(self.child_arguments(mode))
            setattr(args, name, value)
            with self.subTest(mode=mode, name=name), self.refused("invalid-options"):
                runner.validate_options(args)

    def test_parser_refuses_foreign_projects_inputs_and_path_injection(self):
        for option in ("--project-name", "--input-name"):
            for value in (
                "foreign",
                "../source-a",
                "/source-a",
                "source-a/child",
                "source-a\nprivate",
                "source-a\0private",
            ):
                with (
                    self.subTest(option=option, value=value),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    runner.parser().parse_args(
                        self.child_arguments("share", option, value)
                    )
                self.assertEqual(raised.exception.code, 2)

    def test_parser_error_is_fixed_category_not_private_argument_text(self):
        for arguments in (
            [],
            self.arguments("--" + PRIVATE),
            self.child_arguments("share", "--input-name", PRIVATE),
        ):
            errors = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(errors),
                self.assertRaises(SystemExit) as raised,
            ):
                runner.parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(
                errors.getvalue(), "sharing fixture: category=invalid-options\n"
            )
            self.assertNotIn(PRIVATE, errors.getvalue())

    def test_main_host_refusal_precedes_load_placement_native_engine_and_creation(self):
        history = SimpleNamespace(
            host_guard=Mock(side_effect=runner.Refusal("host-only")),
            fresh_parent=Mock(),
            load_source=Mock(),
        )
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(output, "close"))
            stack.enter_context(patch.object(runner.os, "dup", return_value=1))
            stack.enter_context(patch.object(runner.os, "fdopen", return_value=output))
            helpers = stack.enter_context(
                patch.object(
                    runner,
                    "helpers",
                    return_value=(history, Path("/synthetic/provider")),
                )
            )
            boundaries = [
                stack.enter_context(patch.object(target, name))
                for target, name in (
                    (runner, "validate_options"),
                    (runner, "load"),
                    (runner, "load_file"),
                    (runner, "Harness"),
                    (runner, "child"),
                    (runner, "owner_control"),
                    (runner.shutil, "which"),
                    (runner.resource, "setrlimit"),
                    (tempfile, "mkdtemp"),
                    (runner.subprocess, "run"),
                    (runner.subprocess, "Popen"),
                )
            ]
            # Even an owner control FD and invalid options must not move any
            # probe ahead of the fence. Neither row permits a host operation.
            for arguments in (self.arguments(), self.child_arguments("owner")):
                with self.subTest(child="--child" in arguments):
                    arguments[arguments.index(COMMIT)] = "not-a-commit"
                    with patch.dict(os.environ, {runner.CONTROL_FD_ENV: PRIVATE}):
                        self.assertEqual(runner.main(arguments), 1)
                    self.assertIn("category=host-only", output.getvalue())
                    self.assertNotIn(PRIVATE, output.getvalue())
        self.assertEqual(helpers.call_count, 2)
        helpers.assert_called_with()
        self.assertEqual(history.host_guard.call_count, 2)
        history.host_guard.assert_called_with()
        history.fresh_parent.assert_not_called()
        history.load_source.assert_not_called()
        for boundary in boundaries:
            boundary.assert_not_called()

    def test_load_cannot_import_source_or_provider_after_host_refusal(self):
        history = SimpleNamespace(
            host_guard=Mock(side_effect=runner.Refusal("host-only")),
            load_source=Mock(),
        )
        with (
            patch.object(
                runner, "helpers", return_value=(history, Path("/synthetic/provider"))
            ),
            patch.object(runner, "load_file") as imports,
            self.refused("host-only"),
        ):
            runner.load(runner.parser().parse_args(self.arguments()))
        history.load_source.assert_not_called()
        imports.assert_not_called()

    def test_real_donor_root_and_each_container_marker_remain_refusals(self):
        # Loading this stdlib-only donor is not loading candidate fixture helpers.
        # Every matrix row refuses: no test makes a guarded host operation run.
        history, _ = runner.helpers()
        for uid, container, marker in (
            (0, "0", None),
            (1000, "1", None),
            (1000, "0", "/.dockerenv"),
            (1000, "0", "/run/.containerenv"),
        ):
            with (
                self.subTest(uid=uid, container=container, marker=marker),
                patch.object(history.os, "geteuid", return_value=uid),
                patch.dict(os.environ, {"OC_BOX_CONTAINER": container}, clear=True),
                patch.object(
                    Path,
                    "exists",
                    autospec=True,
                    side_effect=lambda path: str(path) == marker,
                ),
                patch.object(history, "load_source") as source,
                patch.object(history.subprocess, "run") as engine,
                self.assertRaisesRegex(history.Refusal, "^host-only$"),
            ):
                history.host_guard()
            source.assert_not_called()
            engine.assert_not_called()

    def test_category_never_returns_private_exception_messages(self):
        cases = [
            (runner.Refusal("native-changed"), "native-changed"),
            (runner.Refusal(PRIVATE), "operation-failed"),
            (RuntimeError(PRIVATE), "operation-failed"),
            (OSError(errno.EACCES, PRIVATE, "/synthetic/private"), "io-error"),
            (
                subprocess.CalledProcessError(
                    2, [PRIVATE], output=PRIVATE, stderr=PRIVATE
                ),
                "operation-failed",
            ),
            (KeyboardInterrupt(PRIVATE), "interrupted"),
            (SystemExit(PRIVATE), "interrupted"),
        ]
        for error, expected in cases:
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(runner.category(error), expected)
                self.assertIn(runner.category(error), runner.CATEGORIES)
                self.assertNotIn(PRIVATE, runner.category(error))


class SharingRootValidationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = self.parent / "fixture"
        (self.root / "bin").mkdir(parents=True)
        self.binary = self.parent / "native"
        self.binary.write_bytes(b"SYNTHETIC_NATIVE_BYTES_NEVER_EXECUTED")
        self.wrapper = "# synthetic wrapper, never executed\n"
        (self.root / "bin/opencode").write_text(self.wrapper)
        self.config = {"plugins": [], "model": "fixture/share-model"}
        self.hashes = {"test_sharing_podman.py": "c" * 64}
        self.identity = {
            "commit": COMMIT,
            "commit_count": 100,
            "version": "1.7.71+g" + COMMIT[:7],
            "dirty": False,
            "sha256": {"src/oc_box/sharing.py": "d" * 64},
        }
        self.f = SimpleNamespace(
            validate_fixture=Mock(return_value=self.root),
            private_json=Mock(side_effect=lambda root, name: self.documents[name]),
            native_wrapper_source=Mock(return_value=self.wrapper),
            workspace_records=Mock(return_value={}),
            vault=SimpleNamespace(
                vault_path=Mock(return_value=self.root / "absent-vault")
            ),
            require_runtime_boot=Mock(),
            validate_store=Mock(),
            verify_owned_resources=Mock(
                side_effect=AssertionError("resource access forbidden")
            ),
        )
        self.loaded = SimpleNamespace(
            fixture=self.f,
            identity=self.identity,
            source=Path("/synthetic/source"),
            interactive=SimpleNamespace(native_binary=Mock(return_value=self.binary)),
            box=SimpleNamespace(
                run=Mock(side_effect=AssertionError("engine forbidden"))
            ),
            service=SimpleNamespace(
                attach=Mock(side_effect=AssertionError("attach forbidden"))
            ),
        )
        self.enter(patch.object(runner, "harness_hashes", return_value=self.hashes))
        self.marker = runner.purpose(
            self.root, self.loaded, self.hashes, self.binary, self.config
        )
        self.documents = {
            runner.MARKER: self.marker,
            "executables.json": {"2": str(self.binary)},
            "config/opencode/opencode.json": self.config,
        }
        self.history = SimpleNamespace(source_snapshot=Mock(return_value=self.identity))
        args = runner.parser().parse_args(self.arguments())
        args.opencode_v2 = self.binary
        self.harness = runner.Harness(
            args,
            self.history,
            self.loaded,
            SimpleNamespace(requests=0, errors=0),
            self.root,
        )
        self.harness.output = io.StringIO()
        # Any unplanned native/engine process is a test failure, never a probe.
        self.commands = self.enter(
            patch.object(
                runner.subprocess,
                "run",
                side_effect=AssertionError("process forbidden"),
            )
        )
        self.children = self.enter(
            patch.object(
                runner.subprocess,
                "Popen",
                side_effect=AssertionError("process forbidden"),
            )
        )

    def tearDown(self):
        self.f.verify_owned_resources.assert_not_called()
        self.loaded.box.run.assert_not_called()
        self.commands.assert_not_called()
        self.children.assert_not_called()

    def test_purpose_pins_source_harness_native_config_and_private_root(self):
        self.assertEqual(self.marker["format"], runner.FORMAT)
        self.assertEqual(self.marker["root"], str(self.root))
        self.assertEqual(self.marker["uid"], os.getuid())
        self.assertEqual(self.marker["source_identity"], self.identity)
        self.assertEqual(self.marker["harness_sha256"], self.hashes)
        self.assertEqual(
            self.marker["native"],
            {
                "path": str(self.binary),
                "version": "2.2.6",
                "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            },
        )
        self.assertEqual(
            self.marker["config_sha256"],
            hashlib.sha256(
                json.dumps(self.config, sort_keys=True).encode()
            ).hexdigest(),
        )
        self.assertNotIn(self.wrapper, json.dumps(self.marker))

    def test_valid_root_delegates_file_only_guards_and_returns_records(self):
        root, records = runner.validate_root(self.root, self.loaded, initial=True)
        self.assertEqual((root, records), (self.root, {}))
        self.f.validate_fixture.assert_called_once_with(
            self.root, allow_unrecorded=True
        )
        self.f.require_runtime_boot.assert_called_once_with(
            self.root, allow_uninitialized=True
        )
        self.f.validate_store.assert_not_called()

    def test_completed_failed_or_redirected_report_refuses_replay_before_native(self):
        for name in (runner.REPORT, "result.json"):
            path = self.root / name
            for kind in ("passed", "failed", "symlink"):
                with self.subTest(name=name, kind=kind):
                    if kind == "symlink":
                        path.symlink_to(self.parent / "missing-report")
                    else:
                        path.write_text(json.dumps({"status": kind}))
                    try:
                        with self.refused("purpose"):
                            runner.validate_root(self.root, self.loaded)
                    finally:
                        path.unlink()
        self.loaded.interactive.native_binary.assert_not_called()
        self.f.validate_store.assert_not_called()
        self.f.require_runtime_boot.assert_not_called()

    def test_purpose_schema_requires_exact_fields(self):
        valid = copy.deepcopy(self.marker)
        cases = [[], None, {**valid, "unexpected": "private"}]
        cases.extend(
            {key: value for key, value in valid.items() if key != missing}
            for missing in valid
        )
        for marker in cases:
            with self.subTest(
                kind=type(marker).__name__,
                keys=list(marker) if isinstance(marker, dict) else None,
            ):
                self.documents[runner.MARKER] = marker
                with self.refused("purpose"):
                    runner.validate_root(self.root, self.loaded)
        self.f.require_runtime_boot.assert_not_called()

    def test_wrong_root_uid_format_source_or_harness_refuses_before_native(self):
        valid = copy.deepcopy(self.marker)
        cases = {
            "root": str(self.parent / "foreign"),
            "uid": os.getuid() + 1,
            "format": "foreign-purpose",
            "source_identity": {"commit": "e" * 40},
            "harness_sha256": {"test_sharing_podman.py": "e" * 64},
        }
        for key, value in cases.items():
            self.documents[runner.MARKER] = {**valid, key: value}
            with self.subTest(key=key), self.refused("purpose"):
                runner.validate_root(self.root, self.loaded)
        self.loaded.interactive.native_binary.assert_not_called()
        self.f.require_runtime_boot.assert_not_called()

    def test_native_schema_version_digest_wrapper_and_mapping_are_pinned(self):
        valid = copy.deepcopy(self.marker["native"])
        for native in (
            {**valid, "version": "2.2.7"},
            {**valid, "sha256": "0" * 64},
            {**valid, "extra": "refuse"},
            {key: value for key, value in valid.items() if key != "sha256"},
        ):
            self.marker["native"] = native
            with self.subTest(native=native), self.refused("native-changed"):
                runner.validate_root(self.root, self.loaded)
        self.marker["native"] = valid
        self.documents["executables.json"] = {"2": "/synthetic/foreign"}
        with self.refused("native-changed"):
            runner.validate_root(self.root, self.loaded)
        self.documents["executables.json"] = {"2": str(self.binary)}
        (self.root / "bin/opencode").write_text("# changed wrapper\n")
        with self.refused("native-changed"):
            runner.validate_root(self.root, self.loaded)
        (self.root / "bin/opencode").write_text(self.wrapper)
        self.binary.write_bytes(b"REPLACED_NATIVE_NOT_EXECUTED")
        with self.refused("native-changed"):
            runner.validate_root(self.root, self.loaded)
        self.f.require_runtime_boot.assert_not_called()

    def test_config_hash_and_absent_vault_are_required_before_boot_or_engine(self):
        self.config["plugins"] = ["foreign"]
        with self.refused("purpose"):
            self.harness.validate()
        self.config["plugins"] = []
        (self.root / "absent-vault").write_bytes(b"SYNTHETIC_CIPHERTEXT")
        with self.refused("purpose"):
            self.harness.validate()
        self.f.require_runtime_boot.assert_not_called()
        self.f.validate_store.assert_not_called()

    def test_registry_rejects_foreign_names_and_credential_policy_escalation(self):
        for records in (
            {"foreign": (None, {})},
            {"source-a": (None, {"credentials": {}})},
            {"source-b": (None, {"credentials": {"mode": "env"}})},
            {"restored-full": (None, {})},
            {
                "restored-full": (
                    None,
                    {"credentials": {"mode": "vault", "env": ["TOKEN"], "vault": []}},
                )
            },
            {
                "restored-full": (
                    None,
                    {"credentials": {"mode": "selected", "env": [], "vault": []}},
                )
            },
        ):
            self.f.workspace_records.return_value = records
            with self.subTest(records=records), self.refused("workspace-allowlist"):
                self.harness.validate()
        self.f.require_runtime_boot.assert_not_called()
        self.f.validate_store.assert_not_called()

    def test_recorded_projects_allow_only_restored_empty_global_vault_policy(self):
        records = {
            "source-a": (self.root / "source-a", {"id": "a" * 32}),
            "source-b": (self.root / "source-b", {"id": "b" * 32}),
            "restored-full": (
                self.root / "restored-full",
                {
                    "id": "c" * 32,
                    "credentials": {"mode": "vault", "env": [], "vault": []},
                },
            ),
        }
        self.f.workspace_records.return_value = records
        self.assertEqual(
            runner.validate_root(self.root, self.loaded, initial=True)[1], records
        )
        self.f.require_runtime_boot.assert_called_once_with(
            self.root, allow_uninitialized=False
        )

    def test_source_and_harness_changes_refuse_before_all_donor_guards(self):
        for boundary in ("source", "harness"):
            self.history.source_snapshot.return_value = (
                self.identity if boundary == "harness" else {"commit": "f" * 40}
            )
            with (
                self.subTest(boundary=boundary),
                patch.object(
                    runner,
                    "harness_hashes",
                    return_value={} if boundary == "harness" else self.hashes,
                ),
                self.refused("source-changed"),
            ):
                self.harness.validate()
        self.f.validate_fixture.assert_not_called()
        self.f.validate_store.assert_not_called()

    def test_file_guard_refusals_never_continue_to_store_or_resources(self):
        for name in (
            "validate_fixture",
            "private_json",
            "workspace_records",
            "require_runtime_boot",
        ):
            target = getattr(self.f, name)
            old_effect = target.side_effect
            target.side_effect = runner.Refusal("purpose")
            try:
                with self.subTest(boundary=name), self.refused("purpose"):
                    self.harness.validate()
            finally:
                target.side_effect = old_effect
            self.f.validate_store.assert_not_called()
            self.loaded.service.attach.assert_not_called()

    def test_retained_boot_guard_precedes_store_and_refusal_prevents_engine(self):
        order = []
        self.f.require_runtime_boot.side_effect = lambda *a, **k: order.append(
            "file-boot-guard"
        )
        self.f.validate_store.side_effect = lambda *a, **k: order.append(
            "store-engine-boundary"
        )
        self.harness.validate()
        self.assertEqual(order, ["file-boot-guard", "store-engine-boundary"])
        self.f.validate_store.reset_mock()
        self.f.require_runtime_boot.side_effect = runner.Refusal("private-store")
        with self.refused("private-store"):
            self.harness.validate()
        self.f.validate_store.assert_not_called()

    def test_connection_boot_refusal_never_acquires_lease_or_inspects_resources(self):
        self.f.require_runtime_boot.side_effect = runner.Refusal("private-store")
        with self.refused("private-store"):
            with self.harness.connection():
                self.fail("connection after refused boot")
        self.loaded.service.attach.assert_not_called()
        self.f.validate_store.assert_not_called()

    def test_connection_rechecks_file_boot_guard_before_explicit_attach(self):
        order = []
        path, data = self.root / "source-a", {"id": "a" * 32}
        connection = {"url": "http://127.0.0.1:1", "password": PRIVATE}
        self.f.workspace_records.return_value = {"source-a": (path, data)}
        self.f.require_runtime_boot.side_effect = lambda *a, **k: order.append("boot")

        @contextlib.contextmanager
        def attach(*arguments):
            self.assertEqual(arguments, (self.loaded.box, path, data))
            order.append("attach")
            yield connection
            order.append("release")

        self.loaded.service.attach.side_effect = attach
        with self.harness.connection() as value:
            self.assertEqual(value, (data, connection))
            order.append("client")
        self.assertEqual(order, ["boot", "attach", "client", "release"])

    def test_progress_accepts_only_fixed_named_checks_without_private_output(self):
        self.harness.check("consent_cancel")
        with self.refused("protocol"):
            self.harness.check(PRIVATE)
        self.assertEqual(self.harness.checks, {"consent_cancel": True})
        self.assertNotIn(PRIVATE, self.harness.output.getvalue())

    def test_close_continues_all_owned_children_and_sanitizes_secondary_errors(self):
        order = []

        def fail_owner():
            order.append("owner")
            raise RuntimeError(PRIVATE)

        def fail_share():
            order.append("share")
            raise OSError(errno.EACCES, PRIVATE)

        owner = SimpleNamespace(close=Mock(side_effect=fail_owner))
        share = SimpleNamespace(close=Mock(side_effect=fail_share))
        self.harness.owners = [owner]
        self.harness.shares = [share]
        self.harness.close()
        self.assertEqual(order, ["share", "owner"])
        self.assertEqual(self.harness.secondary, ["io-error", "operation-failed"])
        owner.close.assert_called_once_with()
        share.close.assert_called_once_with()
        self.assertNotIn(PRIVATE, json.dumps(self.harness.secondary))


class SharingReportTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.loaded = SimpleNamespace(
            identity={
                "commit": COMMIT,
                "commit_count": 100,
                "version": "1.7.71+g" + COMMIT[:7],
                "dirty": False,
                "sha256": {"src/oc_box/sharing.py": "d" * 64},
            }
        )
        self.hashes = {"test_sharing_podman.py": "e" * 64}

    def harness(self):
        return SimpleNamespace(
            checks=dict.fromkeys((*runner.CHECKS, *runner.EXTRA_CHECKS), True),
            secondary=[],
            unavailable=[],
            stage="complete",
            provider=SimpleNamespace(requests=17, errors=0, private_output=PRIVATE),
            native={"version": "2.2.6", "sha256": "a" * 64},
            argv=[PRIVATE],
            env={"SYNTHETIC_TOKEN": PRIVATE},
            stderr=PRIVATE,
        )

    def report(self, harness, primary=None):
        return runner.report_value(harness, self.loaded, self.hashes, primary)

    def test_pass_requires_all_core_and_extra_checks_complete_stage_and_no_errors(self):
        value = self.report(self.harness())
        self.assertEqual(value["status"], "PASS")
        self.assertEqual(
            set(value["checks"]), set(runner.CHECKS) | set(runner.EXTRA_CHECKS)
        )
        self.assertTrue(all(result is True for result in value["checks"].values()))
        self.assertEqual(value["stage"], "complete")
        self.assertIsNone(value["primary_category"])
        self.assertEqual(value["secondary_categories"], [])
        self.assertEqual(value["not_run"], [])

    def test_each_missing_or_false_core_check_prevents_pass(self):
        for name in runner.CHECKS:
            for missing in (True, False):
                harness = self.harness()
                if missing:
                    del harness.checks[name]
                else:
                    harness.checks[name] = False
                with self.subTest(name=name, missing=missing):
                    value = self.report(harness)
                    self.assertEqual(value["status"], "FAIL")
                    self.assertEqual(value["primary_category"], "missing-checks")

    def test_missing_extra_check_cannot_silently_pass_without_not_run(self):
        # Strict intended contract: this catches the core-only false-PASS bug.
        for name in runner.EXTRA_CHECKS:
            harness = self.harness()
            del harness.checks[name]
            with self.subTest(name=name):
                self.assertNotEqual(self.report(harness)["status"], "PASS")

    def test_false_extra_check_prevents_pass(self):
        for name in runner.EXTRA_CHECKS:
            harness = self.harness()
            harness.checks[name] = False
            with self.subTest(name=name):
                self.assertEqual(self.report(harness)["status"], "FAIL")

    def test_unfinished_stage_cannot_pass_even_with_every_check_true(self):
        # A complete checklist is not proof the final phase actually finished.
        for stage in ("preparation", "live-sharing", "backup-restore", ""):
            harness = self.harness()
            harness.stage = stage
            with self.subTest(stage=stage):
                self.assertNotEqual(self.report(harness)["status"], "PASS")

    def test_explicitly_skipped_extra_scope_is_incomplete_not_pass(self):
        for skipped in ([runner.EXTRA_CHECKS[0]], list(runner.EXTRA_CHECKS)):
            harness = self.harness()
            harness.unavailable = skipped
            for name in skipped:
                del harness.checks[name]
            with self.subTest(skipped=skipped):
                value = self.report(harness)
                self.assertEqual(value["status"], "INCOMPLETE")
                self.assertEqual(set(value["not_run"]), set(skipped))

    def test_primary_or_secondary_exception_always_prevents_pass(self):
        for primary, secondary in (
            ("share-exit", []),
            (None, ["io-error"]),
            ("oracle", ["interrupted"]),
        ):
            harness = self.harness()
            harness.secondary = secondary
            with self.subTest(primary=primary, secondary=secondary):
                value = self.report(harness, primary)
                self.assertEqual(value["status"], "FAIL")
                self.assertEqual(value["primary_category"], primary)
                self.assertEqual(value["secondary_categories"], secondary)

    def test_preparation_without_harness_is_never_pass(self):
        value = self.report(None)
        self.assertEqual(value["status"], "FAIL")
        self.assertEqual(value["stage"], "preparation")
        self.assertEqual(value["checks"], {})
        self.assertEqual(value["not_run"], list(runner.EXTRA_CHECKS))
        self.assertIsNone(value["provider"])

    def test_report_contains_tested_identity_hashes_checks_and_fixed_errors_only(self):
        harness = self.harness()
        value = self.report(harness, runner.category(RuntimeError(PRIVATE)))
        self.assertEqual(value["source_identity"], self.loaded.identity)
        self.assertEqual(value["harness_sha256"], self.hashes)
        self.assertEqual(value["checks"], harness.checks)
        self.assertEqual(value["provider"], {"requests": 17, "errors": 0})
        self.assertEqual(value["primary_category"], "operation-failed")
        self.assertIn("external-host", value["scope"])
        self.assertEqual(value["manual_observations"], "NOT_AUTOMATICALLY_PASSED")
        encoded = json.dumps(value)
        for private in (PRIVATE, "SYNTHETIC_TOKEN", "argv", "stderr", "private_output"):
            self.assertNotIn(private, encoded)


class SharingSourceManifestTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = self.parent / "source"
        self.root.mkdir()
        self.labels = self.enter(
            patch.object(
                runner.os, "getxattr", return_value=b"synthetic_u:object_r:test_t:s0"
            )
        )

    def test_manifest_records_real_metadata_hashes_links_and_fifo_without_following(
        self,
    ):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "not-shared").write_text(PRIVATE)
        (self.root / "regular").write_text("SYNTHETIC_TEXT 中🙂")
        os.link(self.root / "regular", self.root / "hard-link")
        (self.root / "symlink").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.root / "fifo", 0o600)
        with patch.object(runner, "digest", wraps=runner.digest) as reads:
            manifest = runner.source_manifest(self.root)
        self.assertEqual(
            set(manifest), {".", "regular", "hard-link", "symlink", "fifo"}
        )
        self.assertEqual(
            {call.args[0] for call in reads.call_args_list},
            {self.root / "regular", self.root / "hard-link"},
        )
        for name, item in manifest.items():
            info = (self.root / name).lstat()
            self.assertEqual(
                item[:7],
                [
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ],
            )
            self.assertEqual(item[7], b"synthetic_u:object_r:test_t:s0".hex())
        self.assertEqual(manifest["regular"][3], 2)
        self.assertEqual(manifest["regular"][-1], manifest["hard-link"][-1])
        self.assertEqual(manifest["symlink"][-1], str(outside))
        self.assertEqual(len(manifest["fifo"]), 8)
        self.assertTrue(stat.S_ISFIFO(manifest["fifo"][0]))
        for call in self.labels.call_args_list:
            self.assertEqual(call.args[1], "security.selinux")
            self.assertEqual(call.kwargs, {"follow_symlinks": False})
        self.assertNotIn(PRIVATE, json.dumps(manifest))

    def test_manifest_is_stable_across_read_atime_effects_but_not_content_changes(self):
        path = self.root / "text"
        path.write_text("SYNTHETIC_BEFORE")
        info = path.stat()
        os.utime(path, ns=(1, info.st_mtime_ns))
        before = runner.source_manifest(self.root)
        path.read_bytes()  # May update atime; manifest deliberately makes no atime claim.
        self.assertEqual(runner.source_manifest(self.root), before)
        path.write_text("SYNTHETIC_AFTER")
        self.assertNotEqual(runner.source_manifest(self.root), before)

    def test_missing_or_unsupported_labels_are_none_but_denied_labels_refuse(self):
        for error in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            with (
                self.subTest(error=error),
                patch.object(
                    runner.os, "getxattr", side_effect=OSError(error, PRIVATE)
                ),
            ):
                self.assertIsNone(runner.source_manifest(self.root)["."][7])
        with (
            patch.object(
                runner.os, "getxattr", side_effect=OSError(errno.EACCES, PRIVATE)
            ),
            self.refused("io-error"),
        ):
            runner.source_manifest(self.root)

    def test_digest_streams_large_regular_content_without_printing_or_mutating(self):
        content = b"SYNTHETIC_CHUNK_CONTENT_" * 100000 + b"END"
        path = self.root / "large"
        path.write_bytes(content)
        before = path.stat()
        output, errors = io.StringIO(), io.StringIO()
        sizes = []

        @contextlib.contextmanager
        def tracked_open(candidate, mode):
            self.assertEqual((candidate, mode), (path, "rb"))
            with io.open(candidate, mode) as stream:

                def read(size):
                    sizes.append(size)
                    return stream.read(size)

                yield SimpleNamespace(read=read)

        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            patch.object(Path, "open", autospec=True, side_effect=tracked_open),
        ):
            self.assertEqual(runner.digest(path), hashlib.sha256(content).hexdigest())
        self.assertGreater(len(sizes), 2)
        self.assertEqual(set(sizes), {1024 * 1024})
        after = path.stat()
        self.assertEqual(
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns),
        )
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual(path.read_bytes(), content)


# Generic synthetic protocol peer, not the production fixture's child mode.
SHARE_STUB = r"""
import fcntl, os, signal, sys, termios
if not os.isatty(0):
    print("NONINTERACTIVE_REFUSAL", flush=True)
    sys.exit(1)
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
def interrupt(*unused):
    print("CLEAN_SIGINT", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, interrupt)
print("SECURITY WARNING: cannot recall. Type 'share'", flush=True)
if sys.stdin.readline().strip() != "share":
    sys.exit(1)
print("Live read-only tools: oc_box_share_" + "a" * 32 + ". ", flush=True)
while True:
    signal.pause()
"""


class SharingChildTransportTests(OfflineCase):
    def command(self, script):
        return [sys.executable, "-I", "-B", "-u", "-c", script]

    def share(self, script=SHARE_STUB, *, tty=True):
        child = runner.ShareChild(
            self.command(script),
            {"PATH": os.defpath, "LANG": "C.UTF-8"},
            self.parent,
            tty=tty,
        )
        self.addCleanup(child.close)
        return child

    def pipe(self, script):
        child = runner.PipeChild(
            self.pipe_command(script),
            {"PATH": os.defpath, "LANG": "C.UTF-8"},
            self.parent,
        )
        self.addCleanup(child.close, 0.1)
        return child

    def pipe_command(self, script):
        # These generic protocol stubs historically print their machine frames.
        # Redirect only their Python stdout, never the production helper's stdout.
        return self.command(
            "import os, sys\n"
            "sys.stdout = os.fdopen(\n"
            "    int(os.environ.pop('OC_BOX_SHARING_CONTROL_FD')), 'w', buffering=1\n"
            ")\n" + script
        )

    def accepted(self):
        child = self.share()
        child.until(b"Type 'share'", timeout=5)
        self.assertIsNone(child.name)
        self.assertIn(b"SECURITY WARNING", child.content)
        child.send(b"share\n")
        self.assertEqual(child.until(ready=True, timeout=5), NAMESPACE)
        return child

    def test_real_pty_confirmation_namespace_and_ctrl_c_clean_sigint(self):
        child = self.accepted()
        child.send(b"\x03")
        child.finish(timeout=5)
        self.assertIn(b"CLEAN_SIGINT", child.content)
        self.assertFalse(child.worker.is_alive())
        self.assertIsNone(child.failure)

    def test_real_pty_cancel_never_announces_namespace(self):
        child = self.share()
        child.until(b"Type 'share'", timeout=5)
        child.send(b"no\n")
        child.finish(expected=1, timeout=5)
        self.assertIsNone(child.name)

    def test_noninteractive_stub_refuses_and_transport_cannot_send(self):
        child = self.share(tty=False)
        child.finish(expected=1, timeout=5)
        self.assertIsNone(child.name)
        self.assertIn(b"NONINTERACTIVE_REFUSAL", child.content)
        with self.refused("protocol"):
            child.send(b"share\n")

    def test_real_sigkill_is_observed_as_signal_exit_not_success(self):
        child = self.accepted()
        os.kill(child.process.pid, signal.SIGKILL)
        child.finish(expected=-signal.SIGKILL, timeout=5)
        self.assertEqual(child.process.returncode, -signal.SIGKILL)

    def test_share_exit_mismatch_is_a_fixed_refusal(self):
        child = self.share("import sys; sys.exit(3)", tty=False)
        with self.refused("share-exit"):
            child.finish(timeout=5)
        child.finish(expected=3, timeout=5)

    def test_share_output_bound_refuses_without_leaking_error_details(self):
        script = (
            "import os, signal, sys\nsignal.signal(signal.SIGINT, lambda *a: sys.exit(0))\nos.write(1, b'"
            + PRIVATE
            + "' * 100)\nsignal.pause()"
        )
        with patch.object(runner, "MAX_OUTPUT", 512):
            child = self.share(script, tty=False)
            self.assertTrue(child.done.wait(5))
            self.assertEqual(child.failure, "bounded-output")
            self.assertLessEqual(len(child.content), 512)
            with self.refused("share-start"):
                child.until(ready=True, timeout=1)
            self.assertNotIn(PRIVATE, child.failure)

    def test_ready_namespace_requires_exact_lowercase_32_hex_digits(self):
        for token in ("a" * 31, "a" * 33, "A" * 32, "g" * 32):
            with self.subTest(token=token):
                script = (
                    "print("
                    + repr("Live read-only tools: oc_box_share_" + token + ". ")
                    + ", flush=True)"
                )
                child = self.share(script, tty=False)
                self.assertTrue(child.done.wait(5))
                with self.refused("share-start"):
                    child.until(ready=True, timeout=1)
                self.assertIsNone(child.name)

    def test_share_send_is_byte_bounded_before_writing(self):
        child = self.share()
        child.until(b"Type 'share'", timeout=5)
        with self.refused("protocol"):
            child.send(b"x" * 1025)
        child.send(b"no\n")
        child.finish(expected=1, timeout=5)

    def test_share_close_stops_stub_joins_reader_and_closes_pty(self):
        child = runner.ShareChild(
            self.command(SHARE_STUB), {"PATH": os.defpath}, self.parent
        )
        try:
            child.until(b"Type 'share'", timeout=5)
        finally:
            child.close()
        self.assertEqual(child.process.returncode, 0)
        self.assertFalse(child.worker.is_alive())
        with self.assertRaises(OSError) as raised:
            os.fstat(child.fd)
        self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_pipe_readiness_is_small_json_stderr_stays_private_and_eof_releases(self):
        script = (
            "import sys\nprint("
            + repr(PRIVATE)
            + ", file=sys.stderr, flush=True)\nprint('{\"ready\":true}', flush=True)\nsys.stdin.buffer.read()"
        )
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.pipe(script)
            child.ready(timeout=5)
            child.finish(timeout=5)
        self.assertEqual(child.process.returncode, 0)
        self.assertTrue(child.process.stdin.closed)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        child.errors.seek(0)
        self.assertIn(PRIVATE.encode(), child.errors.read())

    def test_pipe_readiness_rejects_other_json_shapes(self):
        for payload, category in (
            ({"ready": False}, "owner-control-protocol"),
            ({"ready": 1}, "owner-control-protocol"),
            ({"ready": 1.0}, "owner-control-protocol"),
            ({"ready": True, "extra": PRIVATE}, "owner-control-protocol"),
            ([], "owner-control-protocol"),
            (True, "owner-control-protocol"),
        ):
            with self.subTest(payload=payload):
                script = (
                    "import signal, sys\nprint("
                    + repr(json.dumps(payload))
                    + ", flush=True)\nsignal.pause()"
                )
                child = self.pipe(script)
                with self.refused(category):
                    child.ready(timeout=5)

    def test_pipe_malformed_json_is_sanitized_without_exposing_private_stderr(self):
        script = (
            "import signal, sys\nprint("
            + repr(PRIVATE)
            + ", file=sys.stderr, flush=True)\nprint('not-json', flush=True)\nsignal.pause()"
        )
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.pipe(script)
            with self.refused("owner-control-protocol") as raised:
                child.ready(timeout=5)
        self.assertEqual(runner.category(raised.exception), "owner-control-protocol")
        self.assertNotIn(PRIVATE, runner.category(raised.exception))
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_pipe_readiness_is_bounded_even_without_newline(self):
        child = self.pipe(
            "import os, signal, sys\n"
            "os.write(sys.stdout.fileno(), b'x' * 9000)\nsignal.pause()"
        )
        with self.refused("bounded-output"):
            child.ready(timeout=5)

    def test_pipe_timeout_and_nonzero_exit_are_fixed_categories(self):
        child = self.pipe("import signal\nsignal.pause()")
        with self.refused("owner-control-timeout"):
            child.ready(timeout=0.1)
        failed = self.pipe(
            "import sys\nprint('{\"ready\":true}', flush=True)\nsys.stdin.buffer.read()\nsys.exit(3)"
        )
        failed.ready(timeout=5)
        with self.refused("owner-exit"):
            failed.finish(timeout=5)

    def test_pipe_close_terminates_waiting_stub_and_closes_private_channels(self):
        child = runner.PipeChild(
            self.pipe_command(
                "import signal\nprint('{\"ready\":true}', flush=True)\nsignal.pause()"
            ),
            {"PATH": os.defpath},
            self.parent,
        )
        try:
            child.ready(timeout=5)
        finally:
            child.close(0.1)
        self.assertIsNotNone(child.process.returncode)
        self.assertTrue(child.process.stdin.closed)
        self.assertIsNone(child.process.stdout)
        self.assertTrue(child.control.closed)
        self.assertTrue(child.errors.closed)


class SharingNestedMountTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.source, self.target = self.parent / "input", self.parent / "nested"
        self.source.mkdir()
        self.target.mkdir()
        (self.target / "mounted.txt").write_text("MOUNTED_NOT_EXPOSED")
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.spawn = self.enter(
            patch.object(runner.subprocess, "Popen", return_value=self.process)
        )
        self.command = self.enter(
            patch.object(
                runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            )
        )
        self.enter(
            patch.object(
                runner.shutil,
                "which",
                side_effect=lambda name: "/synthetic/bin/" + name,
            )
        )
        self.mounted = ("42", "fuse.bindfs")

    def test_mock_mount_is_readonly_private_and_unmounts_only_recorded_target(self):
        with patch.object(
            runner, "mount_at", side_effect=[None, self.mounted, self.mounted, None]
        ):
            with runner.NestedMount(self.source, self.target) as mount:
                self.assertEqual(mount.mounted, self.mounted)
        arguments = self.spawn.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                "/synthetic/bin/bindfs",
                "-f",
                "-r",
                "--no-allow-other",
                str(self.source),
                str(self.target),
            ],
        )
        self.assertEqual(
            self.spawn.call_args.kwargs["env"], {"PATH": os.defpath, "LANG": "C.UTF-8"}
        )
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(
            self.command.call_args.args[0],
            ["/synthetic/bin/fusermount3", "-u", str(self.target)],
        )
        self.assertEqual(self.command.call_count, 1)
        self.assertTrue(mount.errors.closed)
        self.process.wait.assert_called_once_with(timeout=5)

    def test_changed_mount_identity_refuses_without_unmount_or_global_cleanup(self):
        mount = runner.NestedMount(self.source, self.target)
        with patch.object(runner, "mount_at", side_effect=[None, self.mounted]):
            mount.__enter__()
        try:
            with (
                patch.object(runner, "mount_at", return_value=("99", "fuse.bindfs")),
                self.refused("mount-probe"),
            ):
                mount.close()
            self.command.assert_not_called()
        finally:
            # No real mount exists. Release only test-owned anonymous resources.
            mount.mounted = None
            mount.close()

    def test_existing_mount_refuses_before_process_creation(self):
        with (
            patch.object(runner, "mount_at", return_value=self.mounted),
            self.refused("mount-probe"),
        ):
            with runner.NestedMount(self.source, self.target):
                self.fail("preexisting mount was accepted")
        self.spawn.assert_not_called()
        self.command.assert_not_called()

    def test_symlink_source_or_target_refuses_without_mount_command(self):
        for name in ("source", "target"):
            alias = self.parent / (name + "-alias")
            alias.symlink_to(getattr(self, name), target_is_directory=True)
            source = alias if name == "source" else self.source
            target = alias if name == "target" else self.target
            with (
                self.subTest(name=name),
                patch.object(runner, "mount_at", return_value=None),
                self.refused("mount-probe"),
            ):
                with runner.NestedMount(source, target):
                    self.fail("symlink accepted")
        self.spawn.assert_not_called()
        self.command.assert_not_called()

    def test_missing_mount_tools_refuses_without_installing_or_spawning(self):
        with (
            patch.object(runner, "mount_at", return_value=None),
            patch.object(runner.shutil, "which", return_value=None),
            self.refused("mount-probe"),
        ):
            with runner.NestedMount(self.source, self.target):
                self.fail("missing tools accepted")
        self.spawn.assert_not_called()
        self.command.assert_not_called()

    def test_unexpected_mount_filesystem_refuses_without_unmounting_foreign_scope(self):
        with (
            patch.object(runner, "mount_at", side_effect=[None, ("43", "tmpfs")]),
            self.refused("mount-probe"),
        ):
            with runner.NestedMount(self.source, self.target):
                self.fail("foreign mount accepted")
        self.command.assert_not_called()
        self.process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(self.spawn.call_args.kwargs["stderr"].closed)

    def test_unmounted_child_timeout_terminates_only_its_process(self):
        mount = runner.NestedMount(self.source, self.target)
        mount.process = self.process
        self.process.wait.side_effect = [
            subprocess.TimeoutExpired("synthetic-stub", 5),
            0,
        ]
        mount.close()
        self.process.terminate.assert_called_once_with()
        self.assertEqual(self.process.wait.call_count, 2)
        self.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
