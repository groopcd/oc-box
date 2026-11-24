"""Synthetic contract tests; no engine, project or native-history I/O."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from subprocess import TimeoutExpired
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import workspace_uninstall as review


class WorkspaceFixture:
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.root = Path(private.name)
        self.state = self.root / "registry"
        self.protected = (str(self.root / "native-data"),)
        self.ident = "a" * 32
        self.graph = review.StoragePath(str(self.root / "graph"), (6, 100, 8))
        self.scope = review.EngineScope(
            rootless=True,
            remote=False,
            graph_root=self.graph,
            run_root=review.StoragePath(str(self.root / "run"), (7, 100, 9)),
            volume_root=review.StoragePath(self.graph.path + "/volumes", (6, 101, 8)),
        )

    def register(self, ident=None, **changes):
        data = {
            "version": 2,
            "id": ident or self.ident,
            "project": str(self.root / "projects" / (ident or self.ident)),
            "project_identity": [1, 25],
            "home": "/home/demo",
            "container": "oc-box-" + (ident or self.ident),
            "home_volume": "oc-box-home-" + (ident or self.ident),
            # Unknown fields bind the record, but are never copied into the plan.
            "future": {"value": "SYNTHETIC_DO_NOT_RENDER"},
            **changes,
        }
        path = self.state / "workspaces" / (ident or self.ident) / "workspace.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def resources(self, data, index=0):
        name, ident = data["home_volume"], data["id"]
        volume = review.VolumeInspection(
            name,
            ident,
            "local",
            "local",
            False,
            True,
            False,
            "2026-01-02T03:04:05.123456789Z",
            review.StoragePath(
                self.scope.volume_root.path + "/" + name + "/_data",
                (6, 200 + index, 8),
            ),
            0,
        )
        container = review.ContainerInspection(
            data["container"],
            f"{index + 1:064x}",
            ident,
            "true",
            review.ContainerState("exited", False, False, False, 0, 0, False),
            (
                review.HostMount(
                    "bind",
                    data["project"],
                    data.get("container_project", data["project"]),
                ),
                review.HostMount("volume", volume.mountpoint.path, data["home"], name),
            ),
            (),
            False,
            (
                review.StoragePath(
                    self.graph.path + f"/overlay/{index}/diff", (6, 300 + index, 8)
                ),
            ),
        )
        return container, volume

    def empty(self, **changes):
        return replace(review.EngineSnapshot(self.scope, (), (), (), True), **changes)

    def plan(self, snapshot=None, *, source=None, selector=None, protected=None):
        return review.plan_workspaces(
            self.state if selector is None else selector,
            inspect_engine=source
            if source is not None
            else Mock(return_value=self.empty() if snapshot is None else snapshot),
            protected_paths=self.protected if protected is None else protected,
        )

    def refused(self, reason, snapshot=None, **options):
        with self.assertRaises(review.WorkspacePlanError) as caught:
            self.plan(snapshot, **options)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("SYNTHETIC_DO_NOT_RENDER", str(caught.exception))
        return caught.exception


class WorkspaceReviewTest(WorkspaceFixture, unittest.TestCase):
    def test_exact_engine_targets_and_legacy_registry_modes_without_project_io(self):
        path, data = self.register(container_project="/original/project")
        path.chmod(0o644)
        path.parent.chmod(0o755)
        self.state.chmod(0o755)
        container, volume = self.resources(data)
        supplied = self.empty(
            containers=(container,),
            volumes=(volume,),
            labeled_networks=("known_z", "known_a"),
        )
        with patch.object(Path, "resolve", side_effect=AssertionError("no resolve")):
            plan = self.plan(supplied)
        self.assertTrue(plan.registry_present)
        self.assertEqual(plan.engine, self.scope)
        self.assertEqual(plan.retained_networks, ("known_a", "known_z"))
        (selected,) = plan.workspaces
        self.assertEqual(
            (selected.container, selected.home_volume), (container, volume)
        )
        self.assertEqual(
            selected.registration.record_stamp[:2],
            (path.stat().st_dev, path.stat().st_ino),
        )
        self.assertEqual(
            selected.registration.fingerprint,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assertNotIn("SYNTHETIC_DO_NOT_RENDER", repr(plan))
        self.assertEqual(
            {entry.name for entry in path.parent.iterdir()}, {"workspace.json"}
        )
        self.assertFalse(Path(data["project"]).exists())
        self.assertFalse(Path(self.graph.path).exists())

    def test_absence_requires_evidence_and_never_creates_registry(self):
        source = Mock(return_value=self.empty())
        plan = self.plan(source=source)
        source.assert_called_once_with(())
        self.assertFalse(plan.registry_present)
        self.assertEqual(plan.workspaces, ())
        self.assertFalse(self.state.exists())
        _, data = self.register()
        plan = self.plan()
        self.assertTrue(plan.registry_present)
        self.assertIsNone(plan.workspaces[0].container)
        self.assertIsNone(plan.workspaces[0].home_volume)
        _, volume = self.resources(data)
        source = Mock(return_value=self.empty(volumes=(volume,)))
        (selected,) = self.plan(source=source).workspaces
        source.assert_called_once_with(((data["container"], volume.name),))
        self.assertIsNone(selected.container)
        self.assertIs(selected.home_volume, volume)

    def test_only_registry_metadata_and_record_are_opened(self):
        path, data = self.register()
        project = Path(data["project"])
        project.mkdir(parents=True)
        marker = project / ".oc-box-id"
        marker.write_text("synthetic untouched")
        for name in ("lock", "control", "history"):
            (path.parent / name).mkdir()
        original_open = os.open
        allowed = {*self.state.parts, "workspaces", self.ident, "workspace.json"}

        def guarded_open(name, flags, **options):
            self.assertIn(name, allowed)
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR))
            return original_open(name, flags, **options)

        with patch.object(review.os, "open", side_effect=guarded_open):
            self.plan()
        self.assertEqual(marker.read_text(), "synthetic untouched")

    def test_malformed_last_record_never_reaches_engine(self):
        self.register()
        wrong = "b" * 32
        source = Mock(side_effect=AssertionError("must not inspect"))
        for field in (
            {"id": self.ident},
            {"container": "other"},
            {"home_volume": "other"},
            {"version": 2.0},
            {"project": "relative"},
            {"project": "//project"},
            {"home": "/home/../target"},
            {"project_identity": [1, True]},
            {"project_filesystem_identity": {}},
        ):
            with self.subTest(field=field):
                self.register(wrong, **field)
                self.assertEqual(
                    self.refused("invalid-registration", source=source).workspace_id,
                    wrong,
                )
        source.assert_not_called()

    def test_duplicate_keys_oversized_and_unrecognized_registry_entries(self):
        path, data = self.register()
        source = Mock()
        text = json.dumps(data)
        path.write_text(text[:-1] + ', "version": 2}')
        self.refused("invalid-registration", source=source)
        path.write_text(text)
        with patch.object(review, "MAX_RECORD_BYTES", 64):
            self.refused("invalid-registration", source=source)
        with patch.object(review, "MAX_WORKSPACES", 0):
            self.refused("registry-limit", source=source)
        unknown = path.parent.parent / "unexpected"
        unknown.mkdir()
        self.refused("unexpected-registry-entry", source=source)
        unknown.rename(path.parent.parent / ("b" * 32))
        self.refused("missing-registration", source=source)
        source.assert_not_called()

    def test_restore_attempts_are_retained_even_if_engine_would_look_empty(self):
        source = Mock()
        for changes in (
            {"restore_incomplete": True},
            {"restore_incomplete": 0},
            {"restore_staging_project": "/external/staging"},
            {"restore_container_create_attempted": True},
            {"restore_volume_create_attempted": True},
        ):
            with self.subTest(changes=changes):
                self.register(**changes)
                self.refused("incomplete-restore", source=source)
        source.assert_not_called()

    def test_no_follow_for_registry_ancestors_directories_and_record(self):
        path, _ = self.register()
        source = Mock()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.refused("unsafe-registry", selector=alias / self.state.name, source=source)
        other_workspace = self.root / "elsewhere"
        path.parent.rename(other_workspace)
        path.parent.symlink_to(other_workspace, target_is_directory=True)
        self.refused("unsafe-registry", source=source)
        path.parent.unlink()
        other_workspace.rename(path.parent)
        target = self.root / "outside"
        path.rename(target)
        path.symlink_to(target)
        self.refused("unsafe-registry", source=source)
        path.unlink()
        os.link(target, path)
        self.refused("unsafe-registry", source=source)
        path.unlink()
        os.mkfifo(path)
        self.refused("unsafe-registry", source=source)
        source.assert_not_called()

    def test_permission_and_same_device_mount_boundary_vetoes(self):
        path, _ = self.register()
        source = Mock()
        path.chmod(0o662)
        self.refused("unsafe-registry", source=source)
        path.chmod(0o600)
        self.state.chmod(0o777)
        self.refused("unsafe-registry", source=source)
        self.state.chmod(0o700)
        original_mount = review._mount_id
        inode = path.parent.stat().st_ino

        def inject_boundary(fd):
            return original_mount(fd) + (os.fstat(fd).st_ino == inode)

        with patch.object(review, "_mount_id", side_effect=inject_boundary):
            self.refused("registry-mount-boundary", source=source)
        source.assert_not_called()

    def test_registry_rechecked_after_callback_even_if_record_content_unchanged(self):
        path, _ = self.register()
        source = path.read_bytes()

        def replace_file(_):
            tmp = path.with_name("replace")
            tmp.write_bytes(source)
            tmp.replace(path)
            return self.empty()

        self.refused("registry-changed", source=replace_file)

        def register_more(_):
            self.register("b" * 32)
            return self.empty()

        self.refused("registry-changed", source=register_more)

    def test_root_scope_and_engine_overlap_checks(self):
        for selector in (
            "/",
            "/tmp",
            "unbound",
            str(self.state) + "/",
            str(self.state) + "/..",
        ):
            with self.subTest(selector=selector):
                self.refused("unsafe-state-root", selector=selector)
        self.refused("unsafe-state-root", protected=(str(self.state / "opencode"),))
        self.register(project=str(self.state.parent))
        self.refused("registry-project-overlap")
        self.register(project=self.graph.path + "/user-project")
        self.refused("engine-project-overlap")
        self.register()
        self.refused(
            "engine-storage-overlap", protected=(self.graph.path + "/external-db",)
        )
        self.refused(
            "engine-storage-overlap",
            self.empty(
                scope=replace(
                    self.scope,
                    run_root=replace(
                        self.scope.run_root, path=str(self.state / "engine")
                    ),
                )
            ),
        )
        self.refused(
            "unsupported-engine-storage",
            self.empty(
                scope=replace(
                    self.scope,
                    volume_root=replace(
                        self.scope.volume_root, path=str(self.root / "external")
                    ),
                )
            ),
        )
        path, _ = self.register()
        bound = path.parent.stat()
        self.refused(
            "engine-storage-overlap",
            self.empty(
                scope=replace(
                    self.scope,
                    run_root=replace(
                        self.scope.run_root,
                        identity=(bound.st_dev, bound.st_ino, 123456),
                    ),
                ),
            ),
        )

    def test_engine_must_be_local_bounded_complete_and_consistent(self):
        _, data = self.register()
        container, volume = self.resources(data)
        for scope in (
            replace(self.scope, remote=True),
            replace(self.scope, rootless=False),
        ):
            self.refused("engine-not-local-rootless", self.empty(scope=scope))
        for snapshot in (
            self.empty(complete=False),
            self.empty(complete=1),
            self.empty(
                containers=(container,)
            ),  # mount names a volume not in the inventory
            self.empty(containers=(container, container), volumes=(volume,)),
            self.empty(
                containers=(replace(container, ident="12345"),), volumes=(volume,)
            ),
            self.empty(
                containers=(replace(container, dependencies=("2" * 64,)),),
                volumes=(volume,),
            ),
            self.empty(labeled_networks=("duplicate", "duplicate")),
        ):
            with self.subTest(snapshot=snapshot):
                self.refused("engine-inventory-unverified", snapshot)
        with patch.object(review, "MAX_ENGINE_OBJECTS", 0):
            self.refused("engine-inventory-unverified", self.empty(volumes=(volume,)))
        with patch.object(review, "MAX_ENGINE_REFERENCES", 1):
            self.refused(
                "engine-inventory-unverified",
                self.empty(containers=(container,), volumes=(volume,)),
            )
        self.refused(
            "engine-inventory-unverified",
            source=Mock(side_effect=ValueError("SYNTHETIC_DO_NOT_RENDER")),
        )

    def test_owner_labels_orphans_and_foreign_objects(self):
        _, data = self.register()
        container, volume = self.resources(data)
        inventory = self.empty(containers=(container,), volumes=(volume,))
        self.refused(
            "container-ownership",
            replace(inventory, containers=(replace(container, managed_label=None),)),
        )
        self.refused(
            "volume-ownership",
            replace(inventory, volumes=(replace(volume, workspace_label=None),)),
        )
        orphan = replace(
            container, name="other", ident="2" * 64, mounts=(), writable_paths=()
        )
        for item in (
            orphan,
            replace(
                orphan,
                name="oc-box-" + "c" * 32,
                workspace_label=None,
                managed_label=None,
            ),
        ):
            self.refused(
                "orphaned-or-unexpected-resource",
                replace(inventory, containers=(container, item)),
            )
        self.refused(
            "orphaned-or-unexpected-resource",
            replace(
                inventory,
                volumes=(volume, replace(volume, name="other", mountpoint=None)),
            ),
        )
        foreign = replace(orphan, workspace_label=None, managed_label=None)
        self.assertEqual(
            self.plan(inventory),
            self.plan(replace(inventory, containers=(foreign, container))),
        )

    def test_paused_live_timed_out_and_mounted_stop_states_are_not_candidates(self):
        _, data = self.register()
        container, volume = self.resources(data)
        state = container.state
        for active in (
            None,
            replace(state, running=True),
            replace(state, paused=True),
            replace(state, restarting=True),
            replace(state, mounted=True),
            replace(state, pid=42),
            replace(state, conmon_pid=42),
            replace(state, pid=False),
            replace(state, status="stopping"),
        ):
            self.refused(
                "container-state-unverified",
                self.empty(
                    containers=(replace(container, state=active),), volumes=(volume,)
                ),
            )
        for status in ("created", "configured", "stopped", "exited"):
            self.plan(
                self.empty(
                    containers=(
                        replace(container, state=replace(state, status=status)),
                    ),
                    volumes=(volume,),
                )
            )

    def test_extra_mount_external_volume_and_unproven_writable_storage_block(self):
        _, data = self.register()
        container, volume = self.resources(data)
        inventory = self.empty(containers=(container,), volumes=(volume,))
        for item in (
            replace(volume, driver="plugin"),
            replace(volume, scope="global"),
            replace(volume, anonymous=True),
            replace(volume, default_options=False),
            replace(volume, external_storage=True),
            replace(volume, created_at="unknown"),
            replace(volume, created_at="2026-02-31T12:34:56Z"),
            replace(
                volume, mountpoint=replace(volume.mountpoint, path="/outside/data")
            ),
            replace(
                volume, mountpoint=replace(volume.mountpoint, identity=(6, 200, 19))
            ),
        ):
            self.refused(
                "unsupported-volume-storage", replace(inventory, volumes=(item,))
            )
        for count in (1, None, False):
            self.refused(
                "volume-in-use",
                replace(inventory, volumes=(replace(volume, mount_count=count),)),
            )
        for added in (
            container.mounts
            + (review.HostMount("bind", "/outside/data", "/external"),),
            (
                replace(container.mounts[0], aliases=(volume.mountpoint.path,)),
                container.mounts[1],
            ),
            (replace(container.mounts[0], destination="/wrong"), container.mounts[1]),
        ):
            self.refused(
                "unsupported-container-mounts",
                replace(inventory, containers=(replace(container, mounts=added),)),
            )
        self.refused(
            "unsupported-container-dependencies",
            replace(inventory, containers=(replace(container, in_pod=True),)),
        )
        self.refused(
            "unsupported-writable-storage",
            replace(inventory, containers=(replace(container, writable_paths=()),)),
        )

    def test_stopped_foreign_consumers_and_storage_aliases_block(self):
        _, data = self.register()
        owner, volume = self.resources(data)
        foreign = review.ContainerInspection(
            "outside", "2" * 64, None, None, owner.state, (), (), False
        )
        inventory = self.empty(containers=(owner,), volumes=(volume,))
        for mount in (
            review.HostMount("volume", volume.mountpoint.path, "/outside", volume.name),
            review.HostMount("bind", volume.mountpoint.path + "/subpath", "/outside"),
            review.HostMount(
                "bind", "/alias", "/outside", aliases=(volume.mountpoint.path,)
            ),
            review.HostMount("bind", owner.writable_paths[0].path, "/outside"),
        ):
            self.refused(
                "workspace-storage-in-use",
                replace(
                    inventory, containers=(owner, replace(foreign, mounts=(mount,)))
                ),
            )
        self.refused(
            "container-in-use",
            replace(
                inventory,
                containers=(owner, replace(foreign, dependencies=(owner.ident,))),
            ),
        )
        alias = replace(
            volume,
            name="outside",
            workspace_label=None,
            mountpoint=replace(volume.mountpoint, path="/outside/alias"),
        )
        self.refused(
            "workspace-storage-in-use", replace(inventory, volumes=(volume, alias))
        )

    def test_multiworkspace_plan_sorted_and_bad_last_target_refuses_whole_review(self):
        b, a = self.register("b" * 32)[1], self.register()[1]
        second, second_home = self.resources(b, index=1)
        first, first_home = self.resources(a)
        supplied = self.empty(
            containers=(second, first), volumes=(second_home, first_home)
        )
        plan = self.plan(supplied)
        self.assertEqual(
            tuple(target.container.ident for target in plan.workspaces),
            (first.ident, second.ident),
        )
        self.assertEqual(
            plan,
            self.plan(
                replace(
                    supplied,
                    containers=(first, second),
                    volumes=(first_home, second_home),
                )
            ),
        )
        self.assertEqual(
            self.refused(
                "container-state-unverified",
                replace(supplied, containers=(first, replace(second, state=None))),
            ).workspace_id,
            b["id"],
        )


class WorkspaceExecutorTest(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path, first = self.register()
        self.other_path, last = self.register("b" * 32)
        self.container, self.volume = self.resources(first)
        self.last_container, self.last_volume = self.resources(last, index=1)
        self.inventory = self.empty(
            containers=(self.container, self.last_container),
            volumes=(self.volume, self.last_volume),
        )
        self.reviewed = self.plan(self.inventory)
        self.inside_lock = False
        self.lock_entries = 0
        self.fence = Mock(return_value=True)
        self.inspect = Mock(side_effect=self.observe)
        self.remove = Mock(side_effect=self.remove_synthetic)
        self.absent = Mock(side_effect=self.observe_absence)
        self.engine = review.EngineActions(self.inspect, self.remove, self.absent)

    @contextmanager
    def locks(self, reviewed):
        self.assertIs(reviewed, self.reviewed)
        self.lock_entries += 1
        self.assertFalse(self.inside_lock)
        self.inside_lock = True
        try:
            yield self.fence
        finally:
            self.inside_lock = False

    def observe(self, names):
        self.assertTrue(self.inside_lock)
        self.assertEqual(
            names,
            (
                (self.container.name, self.volume.name),
                (self.last_container.name, self.last_volume.name),
            ),
        )
        return self.inventory

    def remove_synthetic(self, scope, resource):
        self.assertTrue(self.inside_lock)
        self.assertEqual(scope, self.scope)
        if resource.kind == "container":
            kept = tuple(
                item
                for item in self.inventory.containers
                if item.ident != resource.container_id
            )
            self.assertEqual(len(kept), len(self.inventory.containers) - 1)
            self.inventory = replace(self.inventory, containers=kept)
        else:
            self.assertEqual(resource.kind, "volume")
            kept = tuple(
                item for item in self.inventory.volumes if item.name != resource.name
            )
            self.assertEqual(len(kept), len(self.inventory.volumes) - 1)
            self.inventory = replace(self.inventory, volumes=kept)
        return review.RemovalAck.ACKNOWLEDGED

    def observe_absence(self, scope, resource):
        self.assertTrue(self.inside_lock)
        self.assertEqual(scope, self.scope)
        if resource.kind == "container":
            return not any(
                item.name == resource.name or item.ident == resource.container_id
                for item in self.inventory.containers
            )
        return not any(item.name == resource.name for item in self.inventory.volumes)

    def execute(self, **overrides):
        return review.execute_workspace_plan(
            self.reviewed,
            **{
                "state_root": self.state,
                "protected_paths": self.protected,
                "engine": self.engine,
                "hold_locks": self.locks,
                **overrides,
            },
        )

    def states(self, result, *states):
        self.assertEqual(
            tuple(item.state for item in result.resources),
            tuple(review.ResourceState(state) for state in states),
        )

    def foreign_use(self):
        volume = self.volume
        return review.ContainerInspection(
            "foreign",
            "f" * 64,
            None,
            None,
            self.container.state,
            (
                review.HostMount(
                    "volume", volume.mountpoint.path, "/outside", volume.name
                ),
            ),
            (),
            False,
        )

    def test_exact_locked_order_and_postchecks_leave_registry_bytes_unchanged(self):
        saved = self.path.read_bytes(), self.other_path.read_bytes()
        result = self.execute()
        self.assertTrue(result.complete)
        self.assertFalse(self.inside_lock)
        self.assertEqual(self.lock_entries, 1)
        self.states(result, *("confirmed",) * 4)
        actions = tuple(call.args[1] for call in self.remove.call_args_list)
        self.assertEqual(
            tuple(
                (action.kind, action.container_id, action.name) for action in actions
            ),
            (
                ("container", self.container.ident, self.container.name),
                ("volume", None, self.volume.name),
                ("container", self.last_container.ident, self.last_container.name),
                ("volume", None, self.last_volume.name),
            ),
        )
        self.assertEqual(self.absent.call_args_list, self.remove.call_args_list)
        self.assertEqual(self.inspect.call_count, len(actions) + 1)
        self.assertEqual((self.path.read_bytes(), self.other_path.read_bytes()), saved)
        self.assertEqual(
            {entry.name for entry in self.path.parent.iterdir()}, {"workspace.json"}
        )

    def test_wrong_state_or_protected_paths_never_enters_locks_or_engine(self):
        for options in (
            {"state_root": self.state.parent / "other"},
            {"protected_paths": ()},
            {"hold_locks": None},
        ):
            result = self.execute(**options)
            self.assertEqual(result.reason, "invalid-review-scope")
            self.states(result, *("retained",) * 4)
        self.assertEqual(self.lock_entries, 0)
        self.inspect.assert_not_called()
        self.remove.assert_not_called()

    def test_reviewed_absences_are_checked_but_never_removed(self):
        self.inventory = replace(self.inventory, containers=(), volumes=(self.volume,))
        self.reviewed = self.plan(self.inventory)
        result = self.execute()
        self.assertTrue(result.complete)
        self.states(result, "confirmed")
        self.remove.assert_called_once_with(
            self.scope, review.ResourceRef(self.ident, "volume", self.volume.name)
        )
        self.reviewed = self.plan(self.inventory)
        self.remove.reset_mock()
        result = self.execute()
        self.assertTrue(result.complete)
        self.assertEqual(result.resources, ())
        self.remove.assert_not_called()

    def test_changes_to_final_workspace_or_registry_block_first_removal(self):
        original = self.inventory
        self.inventory = replace(
            original,
            volumes=(
                self.volume,
                replace(self.last_volume, created_at="2026-04-04T01:01:01Z"),
            ),
        )
        self.assertEqual(self.execute().reason, "plan-changed")
        self.inventory = replace(
            original,
            containers=(
                self.container,
                replace(
                    self.last_container,
                    state=replace(self.last_container.state, running=True),
                ),
            ),
        )
        self.assertEqual(self.execute().reason, "plan-unverified")
        self.inventory = original
        data = json.loads(self.other_path.read_text())
        data["future"] = {"changed": True}
        self.other_path.write_text(json.dumps(data))
        result = self.execute()
        self.assertEqual(result.reason, "plan-changed")
        self.states(result, *("retained",) * 4)
        self.remove.assert_not_called()
        self.absent.assert_not_called()

    def test_scope_and_explicitly_absent_resources_cannot_change(self):
        self.inventory = replace(
            self.inventory,
            scope=replace(
                self.scope,
                run_root=replace(self.scope.run_root, identity=(7, 101, 9)),
            ),
        )
        self.assertEqual(self.execute().reason, "plan-changed")
        previous = self.inventory
        self.reviewed = self.plan(replace(previous, containers=(self.last_container,)))
        self.assertEqual(self.execute().reason, "plan-changed")
        self.remove.assert_not_called()

    def test_foreign_stopped_consumer_prevents_any_removal(self):
        self.inventory = replace(
            self.inventory, containers=(*self.inventory.containers, self.foreign_use())
        )
        result = self.execute()
        self.assertEqual(result.reason, "plan-unverified")
        self.states(result, *("retained",) * 4)
        self.remove.assert_not_called()

    def test_foreign_name_must_not_alias_a_full_container_id(self):
        alias = replace(self.foreign_use(), name=self.container.ident, mounts=())
        self.inventory = replace(
            self.inventory, containers=(*self.inventory.containers, alias)
        )
        self.refused("container-id-ambiguous", self.inventory)
        self.assertEqual(self.execute().reason, "plan-unverified")
        self.remove.assert_not_called()

    def test_new_stopped_consumer_or_replacement_prevents_home_removal(self):
        original = self.inventory
        for change in (
            lambda: replace(
                self.inventory,
                containers=(*self.inventory.containers, self.foreign_use()),
            ),
            lambda: replace(
                self.inventory,
                volumes=(
                    replace(self.volume, created_at="2026-04-04T01:01:01Z"),
                    self.last_volume,
                ),
            ),
        ):

            def watched_remove(scope, resource):
                ack = self.remove_synthetic(scope, resource)
                self.inventory = change()
                return ack

            self.inventory = original
            self.remove.reset_mock(side_effect=True)
            self.remove.side_effect = watched_remove
            result = self.execute()
            self.assertIn(result.reason, ("plan-unverified", "plan-changed"))
            self.states(result, "unknown", "retained", "retained", "retained")
            self.remove.assert_called_once()

    def test_full_id_still_present_under_other_name_defeats_apparent_absence(self):
        def renamed(scope, resource):
            ack = self.remove_synthetic(scope, resource)
            alias = replace(
                self.container,
                name="foreign",
                workspace_label=None,
                managed_label=None,
                mounts=(),
                writable_paths=(),
            )
            self.inventory = replace(
                self.inventory, containers=(*self.inventory.containers, alias)
            )
            return ack

        self.remove.side_effect = renamed
        self.absent.side_effect = None
        self.absent.return_value = True
        result = self.execute()
        self.assertEqual(result.reason, "plan-changed")
        self.states(result, "unknown", "retained", "retained", "retained")
        self.remove.assert_called_once()

    def test_timeout_or_interrupt_is_not_replayed_or_treated_as_absence(self):
        for error, expected in (
            (TimeoutExpired(["SYNTHETIC_DO_NOT_RENDER"], 1), "removal-unconfirmed"),
            (KeyboardInterrupt(), "interrupted"),
        ):
            self.remove.reset_mock()
            self.remove.side_effect = error
            result = self.execute()
            self.assertEqual(result.reason, expected)
            self.states(result, "unknown", "retained", "retained", "retained")
            self.remove.assert_called_once()
            self.absent.assert_not_called()
            self.assertNotIn("SYNTHETIC_DO_NOT_RENDER", repr(result))

    def test_second_dispatch_unconfirmed_keeps_first_confirmed_and_registry(self):
        saved = self.path.read_bytes()

        def partial(scope, resource):
            if resource.kind == "volume":
                # Synthetic model: the operation applied but acknowledgement did not arrive.
                self.remove_synthetic(scope, resource)
                return review.RemovalAck.UNCONFIRMED
            return self.remove_synthetic(scope, resource)

        self.remove.side_effect = partial
        result = self.execute()
        self.assertFalse(result.complete)
        self.states(result, "confirmed", "unknown", "retained", "retained")
        self.assertEqual(self.remove.call_count, 2)
        self.absent.assert_called_once()
        self.assertEqual(self.path.read_bytes(), saved)

    def test_only_exact_ack_and_absence_and_proven_not_dispatched(self):
        self.remove.side_effect = None
        for ack, reason, first in (
            (True, "removal-unconfirmed", "unknown"),
            (review.RemovalAck.NOT_DISPATCHED, "removal-not-dispatched", "retained"),
        ):
            self.remove.return_value = ack
            result = self.execute()
            self.assertEqual(result.reason, reason)
            self.states(result, first, "retained", "retained", "retained")
            self.absent.assert_not_called()
        self.remove.return_value = review.RemovalAck.ACKNOWLEDGED
        self.absent.side_effect = None
        self.absent.return_value = 1
        result = self.execute()
        self.assertEqual(result.reason, "absence-unverified")
        self.states(result, "unknown", "retained", "retained", "retained")

    def test_lock_refusal_loss_and_unverified_exit_not_success(self):
        @contextmanager
        def never_acquired(_):
            raise RuntimeError("SYNTHETIC_DO_NOT_RENDER")
            yield

        self.assertEqual(
            self.execute(hold_locks=never_acquired).reason, "lock-unavailable"
        )
        self.fence.return_value = 1
        self.assertEqual(self.execute().reason, "lock-unverified")
        self.inspect.assert_not_called()
        self.fence.return_value = True
        original = self.inventory

        def lost(*args):
            ack = self.remove_synthetic(*args)
            self.fence.return_value = False
            return ack

        self.remove.side_effect = lost
        result = self.execute()
        self.assertEqual(result.reason, "lock-unverified")
        self.states(result, "unknown", "retained", "retained", "retained")
        self.absent.assert_not_called()

        @contextmanager
        def bad_exit(plan):
            with self.locks(plan) as fence:
                yield fence
            raise RuntimeError("SYNTHETIC_DO_NOT_RENDER")

        self.inventory = original
        self.fence.return_value = True
        self.remove.side_effect = self.remove_synthetic
        result = self.execute(hold_locks=bad_exit)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "lock-exit-unverified")
        self.assertTrue(result.lock_cleanup_unverified)
        self.states(result, *("confirmed",) * 4)

    def test_deadline_after_dispatch_leaves_unknown_and_does_not_probe(self):
        clock = [0]

        def delayed(*args):
            ack = self.remove_synthetic(*args)
            clock[0] = review.EXECUTION_TIMEOUT
            return ack

        self.remove.side_effect = delayed
        with patch.object(review.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.execute()
        self.assertEqual(result.reason, "execution-deadline")
        self.states(result, "unknown", "retained", "retained", "retained")
        self.remove.assert_called_once()
        self.absent.assert_not_called()


class PodmanWorkspaceCommandsTest(WorkspaceFixture, unittest.TestCase):
    def test_only_scoped_nonforce_commands_and_exact_absence_codes(self):
        _, registration = self.register()
        container, volume = self.resources(registration)
        runtime = Mock(side_effect=(0, 1, 1, 0, 1))
        inspect = Mock()
        adapter = review.podman_actions(inspect, runtime)
        container_ref = review.ResourceRef(
            self.ident, "container", container.name, container.ident
        )
        volume_ref = review.ResourceRef(self.ident, "volume", volume.name)
        for ref in (container_ref, volume_ref):
            self.assertIs(
                adapter.remove(self.scope, ref), review.RemovalAck.ACKNOWLEDGED
            )
            self.assertIs(adapter.absent(self.scope, ref), True)
        self.assertEqual(
            [call.args for call in runtime.call_args_list],
            [
                (
                    self.scope,
                    ("--remote=false", "container", "rm", "--", container.ident),
                ),
                (
                    self.scope,
                    ("--remote=false", "container", "exists", "--", container.ident),
                ),
                (
                    self.scope,
                    ("--remote=false", "container", "exists", "--", container.name),
                ),
                (self.scope, ("--remote=false", "volume", "rm", "--", volume.name)),
                (self.scope, ("--remote=false", "volume", "exists", "--", volume.name)),
            ],
        )
        self.assertTrue(
            all(
                call.kwargs == {"timeout": review.PODMAN_COMMAND_TIMEOUT}
                for call in runtime.call_args_list
            )
        )
        inspect.assert_not_called()

    def test_bad_scope_selectors_result_codes_never_adopt_or_retry(self):
        _, registration = self.register()
        container, _ = self.resources(registration)
        runtime = Mock()
        adapter = review.podman_actions(Mock(), runtime)
        valid = review.ResourceRef(
            self.ident, "container", container.name, container.ident
        )
        for scope, ref in (
            (replace(self.scope, remote=True), valid),
            (replace(self.scope, rootless=False), valid),
            (replace(self.scope, graph_root=self.scope.run_root), valid),
            (self.scope, replace(valid, container_id=container.ident[:12])),
            (self.scope, replace(valid, name="--force")),
            (self.scope, replace(valid, kind="network")),
        ):
            self.assertIs(adapter.remove(scope, ref), review.RemovalAck.NOT_DISPATCHED)
            self.assertIs(adapter.absent(scope, ref), False)
        runtime.assert_not_called()
        for status in (0, 125, True, None):
            runtime.reset_mock()
            runtime.return_value = status
            self.assertIs(adapter.absent(self.scope, valid), False)
            runtime.assert_called_once()
        runtime.reset_mock()
        runtime.return_value = False
        self.assertIs(adapter.remove(self.scope, valid), review.RemovalAck.UNCONFIRMED)
        runtime.assert_called_once()


if __name__ == "__main__":
    unittest.main()
