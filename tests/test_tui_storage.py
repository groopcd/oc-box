from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, tui_storage as storage


class StorageFixture(unittest.TestCase):
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
        self.plans = []
        self.addCleanup(self.discard_plans)
        self.forbidden = {}
        for name in (
            "podman",
            "run",
            "main",
            "check_backend",
            "launch_credentials",
            "detect_opencode_release",
        ):
            replacement = patch.object(
                box, name, side_effect=AssertionError("unexpected lifecycle call")
            )
            self.forbidden[name] = replacement.start()
            self.addCleanup(replacement.stop)

    def tearDown(self):
        for name, operation in self.forbidden.items():
            with self.subTest(operation=name):
                operation.assert_not_called()

    def discard_plans(self):
        for plan in self.plans:
            storage.cancel_workspace_purge(plan)

    def plan(self, ident=None):
        plan = storage.prepare_workspace_purge(box, ident or self.ident)
        self.plans.append(plan)
        return plan

    def save(self, data=None):
        box.atomic_json(self.record, self.data if data is None else data)

    def expect_refusal(self, reason, operation, *args, **kwargs):
        with self.assertRaises(storage.StorageError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(
            str(caught.exception), f"Storage operation refused ({reason})."
        )
        return caught.exception


class StorageInventoryTests(StorageFixture):
    def test_selected_and_all_scopes_keep_source_store_and_root_identity(self):
        second = self.root / "second"
        second.mkdir()
        _, second_data = box.create_workspace(second, "ubuntu")
        chosen = storage.workspace_rows(box, selected=self.ident)
        self.assertEqual(chosen.scope, "workspace")
        self.assertEqual(chosen.skipped, 0)
        self.assertEqual(len(chosen.rows), 1)
        row = chosen.rows[0]
        self.assertEqual(row.source, "workspace")
        self.assertEqual(row.store, str(self.record.parent))
        self.assertEqual(row.state_root, self.state)
        self.assertEqual(row.record_path, self.record)
        self.assertEqual(row.project, str(self.project))
        self.assertEqual(row.opencode_version, "unknown")
        all_rows = storage.workspace_rows(box, all_workspaces=True)
        self.assertEqual(all_rows.scope, "all-workspaces")
        self.assertEqual(
            {item.workspace_id for item in all_rows.rows},
            {self.ident, second_data["id"]},
        )
        with self.assertRaises(FrozenInstanceError):
            row.project = "/changed"
        with self.assertRaises(FrozenInstanceError):
            all_rows.skipped = 1

    def test_no_selection_or_missing_selection_is_empty_not_all(self):
        for selected in (None, "f" * 32):
            with self.subTest(selected=selected):
                inventory = storage.workspace_rows(box, selected=selected)
                self.assertEqual(inventory.rows, ())
                self.assertEqual(inventory.skipped, 0)

    def test_missing_registry_is_empty_without_creating_state(self):
        absent = self.root / "absent"
        with patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(absent)}):
            inventory = storage.workspace_rows(box, all_workspaces=True)
        self.assertEqual(inventory.rows, ())
        self.assertFalse(absent.exists())

    def test_inventory_never_inspects_projects_or_reads_private_payloads(self):
        self.data.update(
            credentials={"secret": "PRIVATE_VALUE"},
            podman_args=["PRIVATE_ARG"],
            opencode_version="2.2.6",
        )
        self.save()
        with (
            patch.object(storage.status_report, "project_condition") as condition,
            patch.object(box, "read_json", side_effect=AssertionError("unsafe read")),
        ):
            result = storage.workspace_rows(box, all_workspaces=True)
        condition.assert_not_called()
        self.assertEqual(result.rows[0].opencode_version, "2.2.6")
        self.assertNotIn("PRIVATE", repr(result))

    def test_malformed_record_matrix_is_counted_without_hiding_valid_records(self):
        cases = {
            "invalid-json": "{PRIVATE_VALUE",
            "non-object": json.dumps([]),
            "invalid-id-type": json.dumps({**self.data, "id": 7}),
            "wrong-id": json.dumps(self.data),
            "relative-project": None,
            "parent-traversal": None,
            "control-project": None,
            "missing-image": None,
            "bad-version-type": None,
        }
        alterations = {
            "relative-project": {"project": "relative"},
            "parent-traversal": {"project": "/projects/../private"},
            "control-project": {"project": "/projects/line\nbreak"},
            "missing-image": {"image": ""},
            "bad-version-type": {"opencode_version": {"secret": "PRIVATE_VALUE"}},
        }
        for number, (name, content) in enumerate(cases.items(), 1):
            ident = f"{number:032x}"
            target = self.state / "workspaces" / ident / "workspace.json"
            target.parent.mkdir()
            if content is None:
                content = json.dumps({**self.data, "id": ident, **alterations[name]})
            target.write_text(content)
        result = storage.workspace_rows(box, all_workspaces=True)
        self.assertEqual([row.workspace_id for row in result.rows], [self.ident])
        self.assertEqual(result.skipped, len(cases))
        self.assertNotIn("PRIVATE", repr(result))

    def test_external_paths_and_symlinked_registry_components_are_never_followed(self):
        outside = self.root / "external"
        outside.mkdir()
        secret = outside / "workspace.json"
        secret.write_text("PRIVATE_VALUE")
        file_id, directory_id = "a" * 32, "b" * 32
        symlink_file = self.state / "workspaces" / file_id / "workspace.json"
        symlink_file.parent.mkdir()
        symlink_file.symlink_to(secret)
        symlink_directory = self.state / "workspaces" / directory_id
        symlink_directory.symlink_to(outside, target_is_directory=True)
        paths = [
            self.record,
            secret,
            symlink_file,
            symlink_directory / "workspace.json",
        ]
        with patch.object(box, "workspace_paths", return_value=paths):
            result = storage.workspace_rows(box, all_workspaces=True)
        self.assertEqual([item.workspace_id for item in result.rows], [self.ident])
        self.assertEqual(result.skipped, 3)
        self.assertEqual(secret.read_text(), "PRIVATE_VALUE")

    def test_disappeared_oversized_hardlinked_and_nonregular_records_are_skipped(self):
        paths = [self.record]
        for number, kind in enumerate(("missing", "oversized", "hardlink", "fifo"), 1):
            ident = f"{number:032x}"
            target = self.state / "workspaces" / ident / "workspace.json"
            target.parent.mkdir()
            if kind == "oversized":
                target.write_bytes(b" " * (storage.MAX_RECORD_BYTES + 1))
            elif kind == "hardlink":
                original = self.root / "original"
                original.write_text("PRIVATE_VALUE")
                os.link(original, target)
            elif kind == "fifo":
                os.mkfifo(target)
            paths.append(target)
        with patch.object(box, "workspace_paths", return_value=paths):
            result = storage.workspace_rows(box, all_workspaces=True)
        self.assertEqual([item.workspace_id for item in result.rows], [self.ident])
        self.assertEqual(result.skipped, 4)

    def test_demo_inventory_ignores_box_and_environment(self):
        fake = Mock()
        fake.state_root.side_effect = AssertionError("real state")
        fake.workspace_paths.side_effect = AssertionError("real files")
        with patch.dict(os.environ, {}, clear=True):
            result = storage.workspace_rows(fake, all_workspaces=True, demo=True)
        self.assertEqual(result.rows, ())
        self.assertTrue(result.synthetic)
        self.assertIsNone(result.state_root)
        self.assertEqual(fake.mock_calls, [])

    def test_invalid_scope_and_workspace_selector_matrix(self):
        for selected in ("", "../private", "--all", 42):
            with self.subTest(selected=selected):
                self.expect_refusal(
                    "invalid-workspace", storage.workspace_rows, box, selected=selected
                )
        for options in (
            {"all_workspaces": 1},
            {"all_workspaces": True, "selected": self.ident},
        ):
            with self.subTest(options=options):
                self.expect_refusal(
                    "invalid-scope", storage.workspace_rows, box, **options
                )

    def test_private_inventory_exception_is_replaced_with_fixed_reason(self):
        with patch.object(
            box, "workspace_paths", side_effect=box.BoxError("PRIVATE_VALUE")
        ):
            self.expect_refusal("inventory-unavailable", storage.workspace_rows, box)

    def test_host_scope_summary_offers_session_trees_not_whole_database_actions(self):
        self.assertIn("individual native session trees", storage.HOST_SCOPE_SUMMARY)
        self.assertIn(
            "No whole-host database wipe or restore", storage.HOST_SCOPE_SUMMARY
        )


class StorageActionReadTests(StorageFixture):
    def test_oversized_action_registration_never_requests_an_unbounded_read(self):
        self.record.write_bytes(b" " * (storage.MAX_RECORD_BYTES + 100))
        original_fdopen = os.fdopen
        violations = []

        @contextmanager
        def guarded_stream(descriptor, *args, **kwargs):
            with original_fdopen(descriptor, *args, **kwargs) as stream:

                def read(size=-1):
                    if not 0 < size <= storage.MAX_RECORD_BYTES + 1:
                        violations.append(size)
                        raise AssertionError("unbounded metadata read")
                    return stream.read(size)

                yield SimpleNamespace(read=read)

        with patch.object(storage.os, "fdopen", side_effect=guarded_stream):
            self.expect_refusal(
                "invalid-registration", storage.read_workspace_action, box, self.ident
            )
        self.assertEqual(violations, [])

    def test_action_read_binds_one_open_canonical_record_without_inventory(self):
        self.data["private_fixture"] = "PRIVATE_REGISTRATION_VALUE"
        self.save()
        raw = self.record.read_bytes()
        with (
            patch.object(
                box, "workspace_paths", side_effect=AssertionError("scan")
            ) as scan,
            patch.object(
                box, "read_json", side_effect=AssertionError("unbounded")
            ) as unbounded,
        ):
            record = storage.read_workspace_action(box, self.ident)
        scan.assert_not_called()
        unbounded.assert_not_called()
        self.assertEqual(record.row.workspace_id, self.ident)
        self.assertEqual(record.row.project, str(self.project))
        self.assertEqual(record.row.record_path, self.record)
        self.assertEqual(record.fingerprint, hashlib.sha256(raw).hexdigest())
        info = self.record.stat()
        self.assertEqual(
            record.identity,
            (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ),
        )
        self.assertEqual(
            record.registry_identity,
            tuple(
                (path.stat().st_dev, path.stat().st_ino)
                for path in (self.state, self.state / "workspaces", self.record.parent)
            ),
        )
        self.assertEqual(record.project_condition, "present")
        self.assertEqual(record.data["private_fixture"], "PRIVATE_REGISTRATION_VALUE")
        self.assertNotIn("PRIVATE_REGISTRATION_VALUE", repr(record))
        with self.assertRaises(FrozenInstanceError):
            record.fingerprint = "forged"

    def test_action_read_modes_refuse_before_all_io(self):
        for flag, reason in (("read_only", "read-only"), ("demo", "demo")):
            with self.subTest(flag=flag), patch.object(box, "state_root") as root:
                self.expect_refusal(
                    reason,
                    storage.read_workspace_action,
                    box,
                    self.ident,
                    **{flag: True},
                )
                root.assert_not_called()

    def test_action_read_rejects_directory_id_mismatch_and_wrong_resource_names(self):
        for changes in (
            {"id": "f" * 32},
            {"container": "other"},
            {"home_volume": "other"},
        ):
            with self.subTest(changes=changes):
                self.save({**self.data, **changes})
                self.expect_refusal(
                    "invalid-registration",
                    storage.read_workspace_action,
                    box,
                    self.ident,
                )

    def test_action_read_preserves_exposure_guard_and_markerless_cleanup(self):
        with patch.object(box, "APP", self.project):
            self.expect_refusal(
                "trusted-path-exposure", storage.read_workspace_action, box, self.ident
            )
        (self.project / box.MARKER).unlink()
        self.assertEqual(
            storage.read_workspace_action(box, self.ident).project_condition,
            "marker missing",
        )

    def test_inventory_skips_group_writable_metadata_but_retains_owned_legacy_modes(
        self,
    ):
        self.record.chmod(0o644)
        self.record.parent.chmod(0o755)
        self.state.chmod(0o755)
        self.assertEqual(len(storage.workspace_rows(box, all_workspaces=True).rows), 1)
        storage.read_workspace_action(box, self.ident)
        for path in (
            self.record,
            self.record.parent,
            self.state / "workspaces",
            self.state,
        ):
            with self.subTest(path=path.name):
                original = path.stat().st_mode & 0o777
                try:
                    path.chmod(original | 0o020)
                    result = storage.workspace_rows(box, all_workspaces=True)
                    self.assertEqual(result.rows, ())
                    self.assertEqual(result.skipped, 1)
                    self.expect_refusal(
                        "unsafe-registry",
                        storage.read_workspace_action,
                        box,
                        self.ident,
                    )
                finally:
                    path.chmod(original)


class StorageRemovalTests(StorageFixture):
    def test_simultaneous_confirmation_returns_arguments_at_most_once(self):
        plan = self.plan()

        def confirm():
            try:
                return storage.confirm_workspace_purge(box, plan, plan.phrase)
            except storage.StorageError as error:
                return error.reason

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertCountEqual(results, [plan.cli_args, "invalid-plan"])

    def test_editable_self_checkout_refuses_prepare_without_private_path_detail(self):
        before = self.record.read_bytes()
        with patch.object(box, "APP", self.project):
            self.expect_refusal(
                "trusted-path-exposure",
                storage.prepare_workspace_purge,
                box,
                self.ident,
            )
        self.assertEqual(self.record.read_bytes(), before)

    def test_changed_application_exposure_refuses_confirm_and_consumes_review(self):
        plan = self.plan()
        with patch.object(box, "APP", self.project):
            self.expect_refusal(
                "trusted-path-exposure",
                storage.confirm_workspace_purge,
                box,
                plan,
                plan.phrase,
            )
        self.expect_refusal(
            "invalid-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )

    def test_missing_project_cleanup_does_not_invent_new_exposure_authority(self):
        (self.project / box.MARKER).unlink()
        self.project.rmdir()
        with patch.object(
            box, "validate_project", side_effect=AssertionError("missing")
        ) as validate:
            plan = self.plan()
            storage.confirm_workspace_purge(box, plan, plan.phrase)
        validate.assert_not_called()

    def test_review_and_confirmation_only_return_one_history_preserving_command(self):
        before = self.record.read_bytes()
        marker = (self.project / box.MARKER).read_bytes()
        plan = self.plan()
        self.assertEqual(plan.project, str(self.project))
        self.assertEqual(plan.record_path, self.record)
        self.assertEqual(plan.state_root, self.state)
        self.assertEqual(plan.project_condition, "present")
        self.assertEqual(plan.phrase, "DELETE WORKSPACE " + self.ident)
        self.assertEqual(len(plan.record_fingerprint), 64)
        self.assertNotIn(plan.nonce, repr(plan))
        with self.assertRaises(FrozenInstanceError):
            plan.cli_args = ("purge", "--all")
        self.assertEqual(
            storage.confirm_workspace_purge(box, plan, plan.phrase),
            ("purge", "--workspace", self.ident, "--yes"),
        )
        self.assertEqual(self.record.read_bytes(), before)
        self.assertEqual((self.project / box.MARKER).read_bytes(), marker)
        self.expect_refusal(
            "invalid-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )

    def test_fixed_review_explains_sync_locks_retention_and_race_limits(self):
        review = self.plan().review_text
        for warning in (
            "Host project files",
            "host OpenCode history",
            "archive recovery",
            "owning launcher and attached clients must exit",
            "no forced stop",
            "briefly start",
            "synchronize history",
            "does not unlock or refresh",
            "Another process can change state",
            "final CLI validation",
        ):
            with self.subTest(warning=warning):
                self.assertIn(warning, review)

    def test_read_only_and_demo_refuse_prepare_without_reading_state(self):
        for mode, reason in (("read_only", "read-only"), ("demo", "demo")):
            with self.subTest(mode=mode), patch.object(box, "state_root") as root:
                self.expect_refusal(
                    reason,
                    storage.prepare_workspace_purge,
                    box,
                    self.ident,
                    **{mode: True},
                )
                root.assert_not_called()

    def test_read_only_and_demo_refuse_confirm_and_consume_token_without_state_read(
        self,
    ):
        for mode, reason in (("read_only", "read-only"), ("demo", "demo")):
            with self.subTest(mode=mode):
                plan = self.plan()
                with patch.object(box, "state_root") as root:
                    self.expect_refusal(
                        reason,
                        storage.confirm_workspace_purge,
                        box,
                        plan,
                        plan.phrase,
                        **{mode: True},
                    )
                    root.assert_not_called()
                self.expect_refusal(
                    "invalid-plan",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    plan.phrase,
                )

    def test_inexact_phrase_matrix_consumes_review_before_retry(self):
        for phrase in ("", "DELETE WORKSPACE", self.ident, None, " ", "lowercase"):
            with self.subTest(phrase=phrase):
                plan = self.plan()
                self.expect_refusal(
                    "confirmation-mismatch",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    phrase,
                )
                self.expect_refusal(
                    "invalid-plan",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    plan.phrase,
                )

    def test_forged_and_copied_plan_matrix_never_authorizes_bulk_or_cross_workspace(
        self,
    ):
        for changes in (
            {},
            {"workspace_id": "a" * 32},
            {"cli_args": ("purge", "--all", "--yes")},
            {"cli_args": ("purge", "--delete-sessions", "--yes")},
            {"state_root": self.root / "other"},
            {"project": "/unrelated"},
            {"phrase": "YES"},
        ):
            with self.subTest(changes=changes):
                plan = self.plan()
                forged = replace(plan, **changes)
                self.expect_refusal(
                    "invalid-plan",
                    storage.confirm_workspace_purge,
                    box,
                    forged,
                    forged.phrase,
                )
                self.expect_refusal(
                    "invalid-plan",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    plan.phrase,
                )

    def test_frozen_dataclass_bypass_does_not_change_issued_command(self):
        plan = self.plan()
        object.__setattr__(plan, "cli_args", ("purge", "--all", "--yes"))
        self.expect_refusal(
            "invalid-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )

    def test_arbitrary_plan_and_unhashable_nonce_are_fixed_refusals(self):
        plan = self.plan()
        for value in (
            None,
            {},
            SimpleNamespace(nonce=plan.nonce),
            replace(plan, nonce=[]),
        ):
            with self.subTest(value=type(value).__name__):
                self.expect_refusal(
                    "invalid-plan", storage.confirm_workspace_purge, box, value, "YES"
                )
                storage.cancel_workspace_purge(value)
        storage.cancel_workspace_purge(plan)

    def test_cross_controller_use_consumes_plan_without_querying_other_box(self):
        plan = self.plan()
        other = Mock()
        self.expect_refusal(
            "invalid-plan", storage.confirm_workspace_purge, other, plan, plan.phrase
        )
        self.assertEqual(other.mock_calls, [])
        self.expect_refusal(
            "invalid-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )

    def test_changed_state_root_refuses_even_with_identical_copied_registration(self):
        plan = self.plan()
        other = self.root / "another-state"
        shutil.copytree(self.state, other)
        with patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(other)}):
            self.expect_refusal(
                "stale-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
            )

    def test_registry_directory_replacement_matrix_is_stale_even_with_identical_data(
        self,
    ):
        for component in (self.record.parent, self.state / "workspaces", self.state):
            with self.subTest(component=component.name):
                plan = self.plan()
                old = component.with_name(component.name + "-old")
                component.rename(old)
                shutil.copytree(old, component)
                self.expect_refusal(
                    "stale-plan",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    plan.phrase,
                )

    def test_modified_replaced_and_missing_record_matrix_is_stale(self):
        original = self.record.read_bytes()
        for kind in ("modified-in-place", "identical-replacement", "missing"):
            with self.subTest(kind=kind):
                self.record.write_bytes(original)
                plan = self.plan()
                if kind == "modified-in-place":
                    self.record.write_bytes(original + b"\n")
                elif kind == "identical-replacement":
                    replacement = self.record.with_name("replacement.json")
                    replacement.write_bytes(original)
                    replacement.chmod(0o600)
                    replacement.replace(self.record)
                else:
                    self.record.unlink()
                self.expect_refusal(
                    "stale-plan",
                    storage.confirm_workspace_purge,
                    box,
                    plan,
                    plan.phrase,
                )

    def test_original_project_and_host_history_remain_byte_exact(self):
        payload = self.project / "source.txt"
        payload.write_bytes(b"project source\x00fixture")
        history = self.state / "host-history-fixture.db"
        history.write_bytes(b"retained synthetic host history")
        provenance = self.record.parent / "history" / "saved.json"
        provenance.parent.mkdir()
        provenance.write_text("PRIVATE_TRANSCRIPT")
        before = {
            path: path.read_bytes()
            for path in (payload, history, provenance, self.record)
        }
        plan = self.plan()
        storage.confirm_workspace_purge(box, plan, plan.phrase)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertNotIn("PRIVATE_TRANSCRIPT", repr(plan))

    def test_markerless_mismatched_and_missing_project_remain_plain_purge_eligible(
        self,
    ):
        marker = self.project / box.MARKER
        for condition in ("marker missing", "marker mismatch", "missing directory"):
            with self.subTest(condition=condition):
                if condition == "marker missing":
                    marker.unlink()
                elif condition == "marker mismatch":
                    marker.write_text("f" * 32 + "\n")
                else:
                    marker.unlink()
                    self.project.rmdir()
                plan = self.plan()
                self.assertEqual(plan.project_condition, condition)
                self.assertEqual(
                    storage.confirm_workspace_purge(box, plan, plan.phrase),
                    ("purge", "--workspace", self.ident, "--yes"),
                )

    def test_changed_marker_after_review_requires_fresh_review(self):
        plan = self.plan()
        (self.project / box.MARKER).unlink()
        self.expect_refusal(
            "stale-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )
        fresh = self.plan()
        self.assertEqual(fresh.project_condition, "marker missing")

    def test_common_vfs_identity_mismatch_is_reviewed_without_repair(self):
        self.data["project_identity"] = [0, 0]
        self.save()
        before = self.record.read_bytes()
        plan = self.plan()
        self.assertEqual(plan.project_condition, "identity changed")
        storage.confirm_workspace_purge(box, plan, plan.phrase)
        self.assertEqual(self.record.read_bytes(), before)

    def test_unrelated_resource_names_are_refused_before_any_engine_call(self):
        for key in ("container", "home_volume"):
            with self.subTest(key=key):
                changed = {**self.data, key: "other-workspace"}
                self.save(changed)
                self.expect_refusal(
                    "invalid-registration",
                    storage.prepare_workspace_purge,
                    box,
                    self.ident,
                )

    def test_missing_or_invalid_registration_and_id_matrix(self):
        for ident in ("--all", "../private", [self.ident], None):
            with self.subTest(ident=ident):
                self.expect_refusal(
                    "invalid-workspace", storage.prepare_workspace_purge, box, ident
                )
        self.expect_refusal(
            "workspace-unavailable", storage.prepare_workspace_purge, box, "f" * 32
        )
        for changed in ({**self.data, "version": 1}, {**self.data, "id": "f" * 32}):
            with self.subTest(changed=changed["version"]):
                self.save(changed)
                self.expect_refusal(
                    "invalid-registration",
                    storage.prepare_workspace_purge,
                    box,
                    self.ident,
                )

    def test_record_and_directory_group_writes_refuse_but_legacy_0755_is_supported(
        self,
    ):
        for path in (
            self.record,
            self.record.parent,
            self.state / "workspaces",
            self.state,
        ):
            with self.subTest(path=path.name):
                original = path.stat().st_mode & 0o777
                path.chmod(original | 0o020)
                self.expect_refusal(
                    "unsafe-registry", storage.prepare_workspace_purge, box, self.ident
                )
                path.chmod(original)
        self.state.chmod(0o755)
        self.record.parent.chmod(0o755)
        plan = self.plan()
        storage.confirm_workspace_purge(box, plan, plan.phrase)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)

    def test_private_io_failures_never_appear_in_plan_or_errors(self):
        with patch.object(
            storage.status_report,
            "project_condition",
            side_effect=OSError("PRIVATE_VALUE"),
        ):
            self.expect_refusal(
                "invalid-registration", storage.prepare_workspace_purge, box, self.ident
            )
        with patch.object(box, "state_root", side_effect=box.BoxError("PRIVATE_VALUE")):
            self.expect_refusal(
                "inventory-unavailable",
                storage.prepare_workspace_purge,
                box,
                self.ident,
            )

    def test_foreign_registration_owner_is_not_a_removal_authority(self):
        with patch.object(storage.os, "getuid", return_value=os.getuid() + 1):
            self.expect_refusal(
                "unsafe-registry", storage.prepare_workspace_purge, box, self.ident
            )

    def test_symlink_replacement_never_becomes_a_registered_removal_target(self):
        plan = self.plan()
        external = self.root / "external.json"
        external.write_bytes(self.record.read_bytes())
        self.record.unlink()
        self.record.symlink_to(external)
        self.expect_refusal(
            "stale-plan", storage.confirm_workspace_purge, box, plan, plan.phrase
        )
        self.expect_refusal(
            "invalid-registration", storage.prepare_workspace_purge, box, self.ident
        )
        self.assertEqual(json.loads(external.read_bytes()), self.data)

    def test_cancelled_and_expired_plans_never_return_arguments(self):
        cancelled = self.plan()
        storage.cancel_workspace_purge(cancelled)
        self.expect_refusal(
            "invalid-plan",
            storage.confirm_workspace_purge,
            box,
            cancelled,
            cancelled.phrase,
        )
        with patch.object(storage.time, "monotonic", return_value=100):
            expired = self.plan()
        with patch.object(
            storage.time, "monotonic", return_value=100 + storage.PLAN_LIFETIME_SECONDS
        ):
            self.expect_refusal(
                "invalid-plan",
                storage.confirm_workspace_purge,
                box,
                expired,
                expired.phrase,
            )

    def test_pending_reviews_are_bounded_and_expired_capacity_is_reclaimed(self):
        with patch.object(storage, "MAX_PENDING_PLANS", 1):
            with patch.object(storage.time, "monotonic", return_value=100):
                self.plan()
                self.expect_refusal(
                    "plan-limit", storage.prepare_workspace_purge, box, self.ident
                )
            with patch.object(
                storage.time,
                "monotonic",
                return_value=100 + storage.PLAN_LIFETIME_SECONDS,
            ):
                renewed = self.plan()
                self.assertEqual(renewed.workspace_id, self.ident)


class StorageImportTests(unittest.TestCase):
    def test_isolated_flat_runtime_import_uses_only_synthetic_state(self):
        code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import tui_storage
import oc_box
assert not tui_storage.__package__
assert not hasattr(oc_box, '__path__')
oc_box.state_root = lambda: Path(sys.argv[2])
oc_box.workspace_paths = lambda: []
oc_box.main = lambda *a, **k: (_ for _ in ()).throw(AssertionError('execution'))
inventory = tui_storage.workspace_rows(oc_box, all_workspaces=True)
assert inventory.rows == () and inventory.skipped == 0
assert not Path(sys.argv[2]).exists()
assert not any(name.split('.')[0] in {'textual', 'rich', 'cryptography'} for name in sys.modules)
"""
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent-state"
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(box.PACKAGE), str(state)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertFalse(state.exists())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_package_import_needs_no_tui_or_crypto_dependencies(self):
        code = """
import sys
from oc_box import tui_storage
assert not any(name.split('.')[0] in {'textual', 'rich', 'cryptography'} for name in sys.modules)
assert tui_storage.HOST_SCOPE_SUMMARY
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
