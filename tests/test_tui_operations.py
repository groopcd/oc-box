"""Explicit action reviews and secret-free foreground handoff, without lifecycle I/O."""

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    oc_box as box,
    tui_operations as operations,
    tui_storage,
    workspace_service,
)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        environment = patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.record, self.data = box.create_workspace(self.project, "debian")
        self.ident = self.data["id"]
        self.output = self.root / "backup.ocbox"
        self.controller = operations.OperationController(box)
        self.forbidden = {}
        for name in ("main", "podman", "run", "launch_credentials"):
            replacement = patch.object(
                box, name, side_effect=AssertionError("unexpected execution")
            )
            self.forbidden[name] = replacement.start()
            self.addCleanup(replacement.stop)

    def archive(self, path=None, names=None):
        path = self.output if path is None else path
        names = names or (
            "key.age",
            "manifest.age",
            "project.age",
            "home.age",
            "registry.age",
        )
        with tarfile.open(path, "w") as archive:
            for name in names:
                value = b"SYNTHETIC_CIPHERTEXT"
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        path.chmod(0o600)
        return path

    def plan(self, action="workspace-backup", **kwargs):
        return self.controller.prepare(action, backup=self.output, **kwargs)

    def refusal(self, reason, operation, *args, **kwargs):
        with self.assertRaises(operations.OperationError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)

    def test_read_only_and_demo_write_refusal_precedes_file_access(self):
        for demo, readonly, reason in (
            (True, False, "demo"),
            (False, True, "read-only"),
        ):
            controller = operations.OperationController(
                box, demo=demo, read_only=readonly
            )
            for action in operations.ACTIONS - operations.READ_ACTIONS:
                with (
                    self.subTest(demo=demo, action=action),
                    patch.object(operations, "real_path") as access,
                    patch.object(box, "state_root") as state,
                    patch.object(tui_storage, "read_workspace_action") as registry,
                ):
                    self.refusal(reason, controller.prepare, action)
                access.assert_not_called()
                state.assert_not_called()
                registry.assert_not_called()

    def test_confirm_read_only_and_demo_refusals_precede_registration_access(self):
        for flag, reason in (("read_only", "read-only"), ("demo", "demo")):
            with self.subTest(flag=flag):
                controller = operations.OperationController(box)
                plan = controller.prepare(
                    "workspace-backup", backup=self.output, workspace=self.ident
                )
                setattr(controller, flag, True)
                with (
                    patch.object(box, "state_root") as state,
                    patch.object(tui_storage, "read_workspace_action") as registry,
                ):
                    self.refusal(reason, controller.confirm, plan, "CONFIRM")
                state.assert_not_called()
                registry.assert_not_called()

    def test_false_duplicate_id_cannot_change_review_or_canonical_cli_dispatch(self):
        other_project = self.root / "misleading-project"
        other_project.mkdir()
        false_record = self.state / "workspaces" / ("0" * 32) / "workspace.json"
        box.atomic_json(false_record, {**self.data, "project": str(other_project)})
        self.data["private_fixture"] = "PRIVATE_REGISTRATION_VALUE"
        box.atomic_json(self.record, self.data)
        with (
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("scan")
            ) as scan,
            patch.object(
                box, "read_json", side_effect=AssertionError("raw read")
            ) as raw,
        ):
            plan = self.plan(workspace=self.ident)
            self.assertEqual(plan.request["project"], str(self.project))
            self.assertNotIn(str(other_project), plan.review)
            self.assertNotIn("PRIVATE_REGISTRATION_VALUE", repr(plan))
            request = self.controller.confirm(plan, "CONFIRM")
            self.assertNotIn("PRIVATE_REGISTRATION_VALUE", json.dumps(request))

            def production_selector(argv):
                self.assertEqual(
                    argv,
                    ["backup", "--workspace", self.ident, "--output", str(self.output)],
                )
                selected = self.state / "workspaces" / argv[2] / "workspace.json"
                self.assertEqual(
                    json.loads(selected.read_bytes())["project"], request["project"]
                )
                return 0

            self.forbidden["main"].side_effect = production_selector
            self.assertEqual(operations.execute(box, request), 0)
        scan.assert_not_called()
        raw.assert_not_called()
        self.forbidden["main"].assert_called_once()
        for name in ("podman", "run", "launch_credentials"):
            self.forbidden[name].assert_not_called()

    def test_selected_directory_with_another_json_id_refuses_before_execution(self):
        box.atomic_json(self.record, {**self.data, "id": "f" * 32})
        self.refusal("workspace-validation", self.plan, workspace=self.ident)
        self.refusal(
            "workspace-validation",
            operations._OperationBackend(box).selected,
            self.ident,
        )
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_targeted_actions_ignore_unrelated_fifo_without_scanning_registry(self):
        unrelated = self.state / "workspaces" / ("0" * 32) / "workspace.json"
        unrelated.parent.mkdir(mode=0o700)
        os.mkfifo(unrelated, 0o600)
        with (
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("scan")
            ) as scan,
            patch.object(
                box, "read_json", side_effect=AssertionError("raw read")
            ) as raw,
        ):
            request = self.controller.confirm(
                self.plan(workspace=self.ident), "CONFIRM"
            )
            backend = operations._OperationBackend(box, request=request)
            path, data = backend.selected(self.ident)
        scan.assert_not_called()
        raw.assert_not_called()
        self.assertEqual(path, self.record)
        self.assertEqual(data["project"], str(self.project))
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_selected_fifo_is_opened_nonblocking_then_refused_as_nonregular(self):
        self.record.unlink()
        os.mkfifo(self.record, 0o600)
        original_open = os.open
        flags_seen = []

        def guarded_open(path, flags, *args, **kwargs):
            if path == "workspace.json":
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                flags_seen.append(flags)
            return original_open(path, flags, *args, **kwargs)

        with (
            patch.object(operations.os, "open", side_effect=guarded_open),
            patch.object(
                box, "read_json", side_effect=AssertionError("raw read")
            ) as raw,
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("scan")
            ) as scan,
        ):
            self.refusal("workspace-validation", self.plan, workspace=self.ident)
        self.assertTrue(flags_seen)
        raw.assert_not_called()
        scan.assert_not_called()
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_selected_unsafe_metadata_matrix_refuses_and_inventory_skips(self):
        original = self.record.read_bytes()
        for kind in (
            "oversized",
            "symlink",
            "hardlink",
            "group-writable",
            "wrong-container",
            "wrong-volume",
        ):
            with self.subTest(kind=kind):
                self.record.unlink()
                self.record.write_bytes(original)
                self.record.chmod(0o600)
                alias = self.root / (kind + ".json")
                if kind == "oversized":
                    self.record.write_bytes(b" " * (tui_storage.MAX_RECORD_BYTES + 1))
                elif kind == "symlink":
                    alias.write_bytes(original)
                    self.record.unlink()
                    self.record.symlink_to(alias)
                elif kind == "hardlink":
                    os.link(self.record, alias)
                elif kind == "group-writable":
                    self.record.chmod(0o620)
                elif kind == "wrong-container":
                    box.atomic_json(self.record, {**self.data, "container": "foreign"})
                else:
                    box.atomic_json(
                        self.record, {**self.data, "home_volume": "foreign"}
                    )
                with patch.object(
                    box, "read_json", side_effect=AssertionError("raw read")
                ) as raw:
                    self.refusal(
                        "workspace-validation", self.plan, workspace=self.ident
                    )
                    backend = operations._OperationBackend(box)
                    self.refusal("workspace-validation", backend.selected, self.ident)
                    self.assertEqual(backend.inventory(), [])
                    self.assertEqual(backend.inventory_skipped, 1)
                raw.assert_not_called()
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_host_association_inventory_skips_unrelated_unsafe_metadata_matrix(self):
        original_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            if path == "workspace.json":
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
            return original_open(path, flags, *args, **kwargs)

        other_id = "0" * 32
        path = self.state / "workspaces" / other_id / "workspace.json"
        data = {
            **self.data,
            "id": other_id,
            "container": "oc-box-" + other_id,
            "home_volume": "oc-box-home-" + other_id,
        }
        self.data["private_fixture"] = "PRIVATE_REGISTRATION_VALUE"
        box.atomic_json(self.record, self.data)
        for kind in (
            "fifo",
            "oversized",
            "symlink",
            "hardlink",
            "group-writable",
            "false-duplicate",
            "wrong-resource",
        ):
            with self.subTest(kind=kind):
                path.unlink(missing_ok=True)
                box.atomic_json(path, data)
                alias = self.root / ("unrelated-" + kind)
                if kind == "fifo":
                    path.unlink()
                    os.mkfifo(path, 0o600)
                elif kind == "oversized":
                    path.write_bytes(b" " * (tui_storage.MAX_RECORD_BYTES + 1))
                elif kind == "symlink":
                    alias.write_text(json.dumps(data))
                    path.unlink()
                    path.symlink_to(alias)
                elif kind == "hardlink":
                    os.link(path, alias)
                elif kind == "group-writable":
                    path.chmod(0o620)
                elif kind == "false-duplicate":
                    box.atomic_json(path, self.data)
                else:
                    box.atomic_json(path, {**data, "container": "foreign"})
                backend = operations._OperationBackend(box)
                with (
                    patch.object(operations.os, "open", side_effect=guarded_open),
                    patch.object(
                        box, "read_json", side_effect=AssertionError("raw read")
                    ) as raw,
                    patch.object(
                        box, "validate_project", side_effect=AssertionError("project")
                    ) as project,
                ):
                    self.assertEqual(
                        backend.inventory(),
                        [
                            (
                                self.record,
                                {"id": self.ident, "project": str(self.project)},
                            )
                        ],
                    )
                    self.assertEqual(backend.inventory_skipped, 1)
                    self.assertEqual(backend.associations("/synthetic/native.db"), [])
                raw.assert_not_called()
                project.assert_not_called()
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_operation_inventory_accepts_owned_legacy_0644_and_0755(self):
        self.record.chmod(0o644)
        for path in (self.record.parent, self.state / "workspaces", self.state):
            path.chmod(0o755)
        backend = operations._OperationBackend(box)
        self.assertEqual(
            backend.inventory(),
            [(self.record, {"id": self.ident, "project": str(self.project)})],
        )
        self.assertEqual(backend.inventory_skipped, 0)
        self.controller.confirm(self.plan(workspace=self.ident), "CONFIRM")

    def test_parent_confirmation_and_child_execution_bind_registry_directory_identity(
        self,
    ):
        plan = self.plan(workspace=self.ident)
        request = self.controller.confirm(plan, "CONFIRM")
        pending = self.plan(workspace=self.ident)
        before = tui_storage.read_workspace_action(box, self.ident)
        old = self.root / "old-state"
        self.state.rename(old)
        self.state.mkdir(mode=0o700)
        (old / "workspaces").rename(self.state / "workspaces")
        after = tui_storage.read_workspace_action(box, self.ident)
        self.assertEqual(before.identity, after.identity)
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertNotEqual(before.registry_identity, after.registry_identity)
        self.refusal("selection-changed", self.controller.confirm, pending, "CONFIRM")
        self.refusal("selection-changed", operations.execute, box, request)
        backend = operations._OperationBackend(box, request=request)
        self.refusal("selection-changed", backend.selected, self.ident)
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_child_workspace_connection_uses_canonical_target_and_review_bindings(self):
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.record, self.data)
        wrong = self.state / "workspaces" / ("0" * 32) / "workspace.json"
        box.atomic_json(wrong, {**self.data, "project": str(self.root / "wrong")})
        request = self.controller.confirm(self.plan(workspace=self.ident), "CONFIRM")
        backend = operations._OperationBackend(box, request=request)
        lease = Mock()
        lease.__enter__ = Mock(return_value={})
        lease.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                box, "read_json", side_effect=AssertionError("raw read")
            ) as raw,
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("scan")
            ) as scan,
            patch.object(workspace_service, "attach", return_value=lease) as attach,
            patch.object(
                workspace_service, "api", return_value={"version": "2.2.6", "pid": 42}
            ),
        ):
            connection = backend.session_connection(self.ident)
            self.assertEqual(connection.store, self.ident)
            connection.close()
        raw.assert_not_called()
        scan.assert_not_called()
        self.assertEqual(attach.call_args.args[1], self.record)
        self.assertEqual(attach.call_args.args[2]["project"], str(self.project))
        lease.__exit__.assert_called_once()
        box.atomic_json(self.record, {**self.data, "distro": "ubuntu"})
        with patch.object(workspace_service, "attach") as attach:
            self.refusal("selection-changed", backend.session_connection, self.ident)
        attach.assert_not_called()

    def test_exposure_refusal_remains_fixed_before_prepare_confirm_and_child(self):
        with patch.object(box, "APP", self.project):
            self.refusal("trusted-path-exposure", self.plan, workspace=self.ident)
        plan = self.plan(workspace=self.ident)
        with patch.object(box, "APP", self.project):
            self.refusal(
                "trusted-path-exposure", self.controller.confirm, plan, "CONFIRM"
            )
        plan = self.plan(workspace=self.ident)
        request = self.controller.confirm(plan, "CONFIRM")
        for operation, args in (
            (operations.execute, (box, request)),
            (
                operations._OperationBackend(box, request=request).selected,
                (self.ident,),
            ),
        ):
            with (
                self.subTest(operation=operation.__name__),
                patch.object(box, "APP", self.project),
            ):
                self.refusal("trusted-path-exposure", operation, *args)
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def test_workspace_backup_review_is_single_use_and_has_no_execution(self):
        plan = self.plan(workspace=self.ident)
        self.assertIn(self.ident, plan.review)
        self.assertIn("global vault", plan.review)
        request = self.controller.confirm(plan, "CONFIRM")
        self.assertEqual(request["workspace"], self.ident)
        self.assertEqual(request["action"], "workspace-backup")
        self.refusal("invalid-confirmation", self.controller.confirm, plan, "CONFIRM")
        for call in self.forbidden.values():
            call.assert_not_called()

    def test_wrong_phrase_and_forged_review_consume_tokens(self):
        plan = self.plan(workspace=self.ident)
        self.refusal("invalid-confirmation", self.controller.confirm, plan, "wrong")
        self.refusal("invalid-confirmation", self.controller.confirm, plan, "CONFIRM")
        plan = self.plan(workspace=self.ident)
        self.refusal(
            "invalid-confirmation",
            self.controller.confirm,
            replace(plan, review="different"),
            "CONFIRM",
        )

    def test_mutated_request_cannot_change_reviewed_target(self):
        plan = self.plan(workspace=self.ident)
        plan.request["backup"] = str(self.root / "other.ocbox")
        self.refusal("invalid-confirmation", self.controller.confirm, plan, "CONFIRM")

    def test_output_must_remain_absent_and_outside_project_and_state(self):
        for parent in (self.project, self.state):
            with self.subTest(parent=parent):
                self.refusal(
                    "protected-output-path",
                    self.controller.prepare,
                    "workspace-backup",
                    workspace=self.ident,
                    backup=parent / "out.ocbox",
                )
        plan = self.plan(workspace=self.ident)
        self.output.write_bytes(b"new file")
        self.refusal("output-exists", self.controller.confirm, plan, "CONFIRM")

    def test_registry_replacement_and_root_switch_refuse_confirmation(self):
        plan = self.plan(workspace=self.ident)
        box.atomic_json(self.record, {**self.data, "distro": "ubuntu"})
        self.refusal("selection-changed", self.controller.confirm, plan, "CONFIRM")
        plan = self.plan(workspace=self.ident)
        other = self.root / "other-state"
        with patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(other)}):
            self.refusal(
                "workspace-validation", self.controller.confirm, plan, "CONFIRM"
            )

    def test_restore_requires_new_directory_and_never_uses_force(self):
        self.archive()
        self.refusal("destination-required", self.plan, "workspace-restore")
        self.refusal(
            "destination-exists",
            self.plan,
            "workspace-restore",
            destination=self.project,
        )
        plan = self.plan("workspace-restore", destination=self.root / "restored")
        request = self.controller.confirm(plan, "RESTORE")
        self.forbidden["main"].side_effect = None
        self.forbidden["main"].return_value = 0
        self.assertEqual(operations.execute(box, request), 0)
        self.forbidden["main"].assert_called_once_with(
            ["restore", str(self.output), "--project", str(self.root / "restored")]
        )

    def test_input_replaced_after_review_is_refused_before_cli(self):
        self.archive()
        plan = self.plan("workspace-inspect")
        self.output.unlink()
        self.archive()
        self.refusal("selection-changed", self.controller.confirm, plan, "CONFIRM")
        self.forbidden["main"].assert_not_called()

    def test_readonly_can_inspect_but_cannot_be_weakened_after_prepare(self):
        self.archive()
        controller = operations.OperationController(box, read_only=True)
        plan = controller.prepare("workspace-inspect", backup=self.output)
        self.assertEqual(
            controller.confirm(plan, "CONFIRM")["action"], "workspace-inspect"
        )
        controller.read_only = False
        self.output.unlink()
        plan = controller.prepare(
            "workspace-backup", workspace=self.ident, backup=self.output
        )
        controller.read_only = True
        self.refusal("read-only", controller.confirm, plan, "CONFIRM")

    def test_session_source_and_native_selection_are_explicit(self):
        self.refusal("source-selection-required", self.plan, "session-backup")
        self.refusal(
            "source-changed",
            self.plan,
            "session-backup",
            origin="workspace",
            store="other",
            workspace=self.ident,
        )
        self.refusal(
            "session-selection-required",
            self.plan,
            "session-backup",
            origin="host",
            store="/synthetic/native.db",
        )
        session = {
            "id": "ses_synthetic",
            "projectID": "global",
            "location": {"directory": str(self.project)},
            "title": "SYNTHETIC",
            "time": {"created": 1, "updated": 2},
        }
        plan = self.plan(
            "session-backup",
            origin="host",
            store="/synthetic/native.db",
            session=session,
        )
        self.assertEqual(plan.request["session"], session)
        self.assertIn("including native children", plan.review)

    def test_backup_delete_accepts_only_private_regular_recognized_archives(self):
        self.archive()
        self.output.chmod(0o644)
        self.refusal("private-owned-file-required", self.plan, "backup-delete")
        self.output.chmod(0o600)
        plan = self.plan("backup-delete")
        self.refusal("invalid-confirmation", self.controller.confirm, plan, "CONFIRM")
        self.assertTrue(self.output.exists())
        self.output.write_bytes(b"not a backup")
        self.refusal("invalid-backup-file", self.plan, "backup-delete")

    def test_backup_delete_revalidates_in_foreground_child(self):
        self.archive()
        plan = self.plan("backup-delete")
        request = self.controller.confirm(plan, "DELETE")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(operations.execute(box, request), 0)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.project.exists())
        self.assertTrue(self.record.exists())
        self.forbidden["main"].assert_not_called()

    def test_archive_delete_rejects_duplicate_and_foreign_members(self):
        for names in (
            ("key.age", "manifest.age", "sessions.age", "sessions.age"),
            ("key.age", "manifest.age", "../foreign"),
        ):
            with self.subTest(names=names):
                self.archive(names=names)
                with self.assertRaises(operations.OperationError):
                    self.plan("backup-delete")

    def test_symlink_and_hardlink_inputs_are_refused(self):
        self.archive()
        alias = self.root / "alias"
        alias.symlink_to(self.output)
        self.refusal(
            "symlink-path", self.controller.prepare, "workspace-inspect", backup=alias
        )
        alias.unlink()
        os.link(self.output, alias)
        self.refusal("regular-file-required", self.plan, "workspace-inspect")

    def test_handoff_pins_runtime_state_and_keeps_request_out_of_argv_and_environment(
        self,
    ):
        self.archive()
        request = self.controller.confirm(self.plan("workspace-inspect"), "CONFIRM")
        seen = []

        def child(command, **kwargs):
            seen.append(
                json.loads(os.pread(kwargs["pass_fds"][0], operations.MAX_REQUEST, 0))
            )
            self.assertEqual(command[:3], [sys.executable, "-I", "-B"])
            self.assertEqual(command[3], str(box.PACKAGE / "tui_operations.py"))
            self.assertNotIn(str(self.output), command)
            self.assertNotIn("PRIVATE_SECRET_SENTINEL", json.dumps(kwargs["env"]))
            self.assertEqual(kwargs["env"]["OC_BOX_STATE_HOME"], str(self.state))
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            return 0

        with (
            patch.dict(
                os.environ,
                {
                    "ACCEPTANCE_API_KEY": "PRIVATE_SECRET_SENTINEL",
                    "PYTHONPATH": "PRIVATE_SECRET_SENTINEL",
                },
            ),
            patch.object(operations.subprocess, "call", side_effect=child),
        ):
            self.assertEqual(operations.handoff(box, request), 0)
        self.assertEqual(seen, [request])

    def test_purge_handoff_has_no_project_or_history_deletion_flags(self):
        plan = tui_storage.prepare_workspace_purge(box, self.ident)
        request = operations.purge_request(box, plan, plan.phrase)
        self.forbidden["main"].side_effect = None
        self.forbidden["main"].return_value = 0
        self.assertEqual(operations.execute(box, request), 0)
        self.forbidden["main"].assert_called_once_with(
            ["purge", "--workspace", self.ident, "--yes"]
        )

    def test_purge_handoff_checks_child_registry_binding(self):
        plan = tui_storage.prepare_workspace_purge(box, self.ident)
        request = operations.purge_request(box, plan, plan.phrase)
        box.atomic_json(self.record, {**self.data, "distro": "ubuntu"})
        self.refusal("selection-changed", operations.execute, box, request)
        self.forbidden["main"].assert_not_called()

    def test_invalid_operation_cannot_invoke_arbitrary_cli(self):
        for value in (None, {}, {"version": 1, "action": "reset"}):
            self.refusal("invalid-request", operations.execute, box, value)
        self.forbidden["main"].assert_not_called()

    def test_isolated_script_uses_its_own_flat_runtime_without_optional_ui_dependencies(
        self,
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(box.PACKAGE / "tui_operations.py"),
                "--invalid",
            ],
            env={"PATH": os.defpath},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"invalid-options", result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
