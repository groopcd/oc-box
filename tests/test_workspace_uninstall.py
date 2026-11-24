"""Synthetic contract tests; no engine, project or native-history I/O."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import workspace_uninstall as review


class WorkspaceReviewTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
