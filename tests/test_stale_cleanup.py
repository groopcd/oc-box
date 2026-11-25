import contextlib
from copy import deepcopy
import fcntl
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from oc_box import (
    entrypoint_migration,
    guarded_discard,
    native_runtime_guard,
    oc_box as box,
    stale_cleanup,
    workspace_control,
)


class FixtureEngine:
    def __init__(self, base):
        graph, runtime, volume = base / "graph", base / "run", base / "graph/volumes"
        for directory in (graph, runtime, volume):
            directory.mkdir()
        self.store = tuple(
            (str(path), guarded_discard._identity(path))
            for path in (graph, runtime, volume)
        )
        self.containers, self.volumes, self.networks = {}, {}, {}
        self.removals = []
        self.refusal = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def scope(self):
        return self.store

    def rows(self, kind):
        return self.containers if kind == "container" else self.volumes

    def catalog(self, kind):
        return tuple(sorted(self.rows(kind)))

    def inspect(self, kind, selectors):
        return tuple(deepcopy(self.rows(kind)[key]) for key in selectors)

    def exists(self, kind, selector, *, present):
        rows = self.rows(kind)
        actual = selector in rows or any(
            row["Name"] == selector for row in rows.values()
        )
        if actual != present:
            raise guarded_discard.Refused("Exact existence changed.")

    def labeled_networks(self, ident):
        return self.networks.get(ident, ())

    def check_namespace(self, targets):
        pass

    def mounted(self, rows):
        return {}

    def remove(self, kind, selector):
        self.removals.append((kind, selector))
        if self.refusal == (kind, selector):
            raise guarded_discard.Refused("Removal not confirmed.")
        row = self.rows(kind).pop(selector)
        leaf = Path(
            row["GraphDriver"]["Data"]["UpperDir"]
            if kind == "container"
            else row["Mountpoint"]
        )
        leaf.rmdir()
        leaf.parent.rmdir()

    def add(self, data, *, container=True, volume=True, running=False):
        ident = data["id"]
        full = ident * 2
        home = Path(self.store[2][0]) / data["home_volume"] / "_data"
        if volume:
            home.mkdir(parents=True)
            self.volumes[data["home_volume"]] = {
                "Name": data["home_volume"],
                "Driver": "local",
                "Scope": "local",
                "Options": {},
                "CreatedAt": "2026-01-01T00:00:00Z",
                "Labels": {box.LABEL: ident},
                "Mountpoint": str(home),
                "MountCount": 0,
            }
        if container:
            layer = Path(self.store[0][0]) / "overlay" / full / "diff"
            layer.mkdir(parents=True)
            self.containers[full] = {
                "Id": full,
                "Name": data["container"],
                "IsInfra": False,
                "IsService": False,
                "State": {
                    "Status": "running" if running else "exited",
                    "Running": running,
                    "Pid": 123 if running else 0,
                    "ConmonPid": 0,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                },
                "HostConfig": {"RestartPolicy": {"Name": "no"}},
                "Config": {"Labels": {box.LABEL: ident, "io.oc-box.managed": "true"}},
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": data["project"],
                        "Destination": data["project"],
                    },
                    {
                        "Type": "volume",
                        "Source": str(home),
                        "Destination": data["home"],
                        "Name": data["home_volume"],
                    },
                ],
                "GraphDriver": {"Name": "overlay", "Data": {"UpperDir": str(layer)}},
            }
        return full


class StaleCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ocbox-cleanup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.engine = FixtureEngine(self.root)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(self.state)})
        )
        stack.enter_context(patch.object(guarded_discard, "_require_host"))
        self.open_engine = stack.enter_context(
            patch.object(guarded_discard, "Engine", return_value=self.engine)
        )
        stack.enter_context(
            patch.object(
                stale_cleanup.workspace_service,
                "controller_running",
                return_value=False,
            )
        )
        self.confirm = stack.enter_context(
            patch.object(
                stale_cleanup.uninstall_tty, "confirm_review", return_value=True
            )
        )
        for name in (
            "podman",
            "check_backend",
            "detect_opencode_release",
            "launch_credentials",
            "purge",
            "sync_sessions",
            "start_container",
            "stop_container",
            "retain_archive_provenance",
        ):
            stack.enter_context(
                patch.object(box, name, side_effect=AssertionError(name))
            )
        self.output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.errors = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def workspace(self, name, *, missing=False):
        project = self.root / name
        project.mkdir()
        path, data = box.create_workspace(project, "tumbleweed")
        if missing:
            project.rename(project.with_name(name + "-elsewhere"))
        return path, data, project

    def archived(self, path):
        return self.state / "retired" / path.parent.name

    def run_cleanup(self):
        return box.main(["purge", "--cleanup"])

    def test_batch_removes_only_missing_projects_and_preserves_existing_history(self):
        first, one, project = self.workspace("one", missing=True)
        full = self.engine.add(one)
        second, two, _ = self.workspace("two", missing=True)
        other = self.engine.add(two)
        live, living, _ = self.workspace("runner")
        running = self.engine.add(living, running=True)
        missing_marker, _, with_project = self.workspace("marker")
        (with_project / box.MARKER).unlink()
        archive = box.archive_session(
            {"info": {"id": "ses_original", "directory": str(project)}, "messages": []},
            one,
        )
        archive_name = archive["info"]["id"] + ".json"
        export = first.parent / "history" / archive_name
        box.atomic_json(export, archive)
        digest = export.read_bytes()
        original_record = first.read_bytes()

        self.assertEqual(self.run_cleanup(), 0)
        self.assertCountEqual(
            self.engine.removals,
            [
                ("container", full),
                ("volume", one["home_volume"]),
                ("container", other),
                ("volume", two["home_volume"]),
            ],
        )
        self.assertEqual(tuple(self.engine.containers), (running,))
        self.assertTrue(live.is_file())
        self.assertTrue(missing_marker.is_file())
        self.assertTrue(project.with_name("one-elsewhere").is_dir())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(
            (self.archived(first) / "workspace.json").read_bytes(), original_record
        )
        self.assertEqual(
            (self.archived(first) / "history" / archive_name).read_bytes(), digest
        )
        archive["info"]["directory"] = str(first.parent / "controller")
        self.assertTrue(box.restore_archive_directory(archive, project))
        review, phrase = self.confirm.call_args.args
        self.assertEqual(phrase, "CLEANUP")
        self.assertIn(one["id"], review)
        self.assertIn(two["id"], review)
        self.assertNotIn(living["id"], review)
        self.confirm.assert_called_once()

    def test_volume_only_and_metadata_only_retire_without_sync(self):
        first, one, _ = self.workspace("volume-only", missing=True)
        self.engine.add(one, container=False)
        second, two, _ = self.workspace("gone", missing=True)
        guard = native_runtime_guard.Record(
            two["id"] * 2, two["home_volume"], "2.4.1", "f" * 64, False
        )
        native_runtime_guard.publish(second, None, guard)
        workspace_control.capture(second)
        with workspace_control.admission(second):
            workspace_control.mark_uncertain(second)
        observation = workspace_control.disposal_observation(second)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(self.engine.removals, [("volume", one["home_volume"])])
        self.assertFalse(first.exists())
        retired = self.archived(second) / "workspace.json"
        self.assertFalse(second.exists())
        self.assertEqual(native_runtime_guard.load(retired), guard)
        self.assertEqual(workspace_control.disposal_observation(retired), observation)

    def test_project_appearing_at_confirmation_keeps_exact_resources(self):
        path, data, project = self.workspace("returning", missing=True)
        self.engine.add(data)

        def reappear(*_):
            project.mkdir()
            return True

        self.confirm.side_effect = reappear
        self.assertEqual(self.run_cleanup(), 1)
        self.assertEqual(self.engine.removals, [])
        self.assertTrue(path.is_file())
        self.assertFalse(self.archived(path).exists())

    def test_dangling_links_missing_parent_and_different_filesystem_are_not_absence(
        self,
    ):
        path, data, project = self.workspace("dangling", missing=True)
        project.symlink_to(self.root / "not-present", target_is_directory=True)
        other, changed, _ = self.workspace("different-filesystem", missing=True)
        changed["project_identity"][0] += 1
        box.atomic_json(other, changed)
        parent = self.root / "nested"
        parent.mkdir()
        nested = parent / "project"
        nested.mkdir()
        third, _ = box.create_workspace(nested, "tumbleweed")
        parent.rename(self.root / "nested-elsewhere")
        self.assertEqual(self.run_cleanup(), 1)
        self.open_engine.assert_not_called()
        self.confirm.assert_not_called()
        self.assertTrue(path.is_file() and other.is_file() and third.is_file())

    def test_refused_confirmation_and_retired_collision_are_non_destructive(self):
        path, data, _ = self.workspace("stale", missing=True)
        self.engine.add(data)
        self.confirm.return_value = False
        with self.assertRaisesRegex(box.BoxError, "not confirmed"):
            self.run_cleanup()
        self.assertEqual(self.engine.removals, [])
        self.assertFalse(self.archived(path).exists())
        self.archived(path).mkdir(parents=True, mode=0o700)
        self.confirm.reset_mock()
        self.assertEqual(self.run_cleanup(), 1)
        self.confirm.assert_not_called()
        self.assertEqual(self.engine.removals, [])
        self.assertIn(
            "Retired evidence for this workspace already exists", self.errors.getvalue()
        )

    def test_retirement_reuses_legacy_retired_parent_without_exposing_registry(self):
        path, data, _ = self.workspace("stale", missing=True)
        # Equivalent to the intermediate directory left by plain purge.
        self.archived(path).parent.mkdir()
        self.archived(path).parent.chmod(0o755)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(self.archived(path).parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.archived(path).stat().st_mode & 0o777, 0o700)

    def test_metadata_only_retirement_does_not_inspect_bystander_storage_aliases(self):
        path, _, _ = self.workspace("metadata-only", missing=True)
        self.engine.volumes["custom-storage"] = {
            "Name": "custom-storage",
            "Driver": "external",
        }
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(self.archived(path).is_dir())
        self.assertEqual(self.engine.removals, [])
        self.assertIn("custom-storage", self.engine.volumes)

    def test_resource_appearing_during_retirement_preparation_keeps_registration(self):
        path, data, _ = self.workspace("metadata-only", missing=True)
        original = stale_cleanup._retirement

        @contextlib.contextmanager
        def late(root, registration, *, create=False):
            with original(root, registration, create=create) as result:
                if create:
                    self.engine.add(data, container=False)
                yield result

        with patch.object(stale_cleanup, "_retirement", side_effect=late):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertIn("Podman state changed before retirement", self.errors.getvalue())
        self.assertTrue(path.is_file())
        self.assertFalse(self.archived(path).exists())
        self.assertEqual(self.engine.removals, [])

    def test_request_stop_cannot_publish_into_a_detached_registry(self):
        for existing in (False, True):
            with self.subTest(existing_control=existing):
                path, _, _ = self.workspace(f"stop-race-{existing}", missing=True)
                observed = None
                if existing:
                    workspace_control.capture(path)
                    observed = workspace_control.disposal_observation(path)
                started = threading.Event()
                errors = []
                original = workspace_control._flock

                def blocked(fd, deadline):
                    started.set()
                    original(fd, deadline)

                def stop():
                    try:
                        workspace_control.request_stop(path)
                    except workspace_control.ControlError as exc:
                        errors.append(exc)

                moved = self.root / f"retired-race-{existing}"
                with workspace_control.retirement_gate(path):
                    with patch.object(workspace_control, "_flock", side_effect=blocked):
                        worker = threading.Thread(target=stop)
                        worker.start()
                        self.assertTrue(started.wait(2))
                        path.parent.rename(moved)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                if existing:
                    self.assertEqual(
                        workspace_control.disposal_observation(moved / path.name),
                        observed,
                    )
                else:
                    self.assertFalse((moved / "control").exists())

    def test_failed_ack_keeps_registration_and_stops_before_removing_volume(self):
        path, data, _ = self.workspace("stale", missing=True)
        full = self.engine.add(data)
        self.engine.refusal = ("container", full)
        with self.assertRaisesRegex(box.BoxError, "result may be partial"):
            self.run_cleanup()
        self.assertEqual(self.engine.removals, [("container", full)])
        self.assertTrue(path.is_file())
        self.assertFalse(self.archived(path).exists())
        self.assertIn(data["home_volume"], self.engine.volumes)

    def test_running_guarded_extra_network_and_pending_conversion_are_left_alone(self):
        running, one, _ = self.workspace("running", missing=True)
        self.engine.add(one, running=True)
        guarded, two, _ = self.workspace("guarded", missing=True)
        full = self.engine.add(two)
        native_runtime_guard.publish(
            guarded,
            None,
            native_runtime_guard.Record(
                full, two["home_volume"], "2.4.1", "f" * 64, False
            ),
        )
        network, three, _ = self.workspace("network", missing=True)
        self.engine.add(three)
        self.engine.networks[three["id"]] = ("external-network",)
        pending, four, _ = self.workspace("pending", missing=True)
        four[entrypoint_migration.FIELD] = {}
        box.atomic_json(pending, four)
        self.assertEqual(self.run_cleanup(), 1)
        self.assertEqual(self.engine.removals, [])
        self.confirm.assert_not_called()
        self.assertTrue(
            all(path.is_file() for path in (running, guarded, network, pending))
        )

    def test_existing_paths_are_not_candidates_even_in_an_empty_store(self):
        registration, data, project = self.workspace("existing")
        (project / box.MARKER).unlink()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertTrue(registration.is_file())
        self.open_engine.assert_not_called()
        self.confirm.assert_not_called()

    def test_guarded_surviving_pair_refuses_and_history_writer_lock_refuses(self):
        path, data, _ = self.workspace("guarded", missing=True)
        full = self.engine.add(data)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_cleanup(), 1)
            self.assertIn("A history writer still owns", self.errors.getvalue())
        finally:
            os.close(fd)
        native_runtime_guard.publish(
            path,
            None,
            native_runtime_guard.Record(
                full, data["home_volume"], "2.4.1", "f" * 64, False
            ),
        )
        self.assertEqual(self.run_cleanup(), 1)
        self.assertIn(
            "guarded or uncertain workspace still has Podman resources",
            self.errors.getvalue(),
        )
        self.assertEqual(self.engine.removals, [])
        self.confirm.assert_not_called()

    def test_cli_rejects_all_other_selectors_and_flags_before_backend(self):
        for extra in (
            ["--yes"],
            ["--all"],
            ["--delete-sessions"],
            ["--discard-guarded"],
            ["--workspace", "a" * 32],
            ["--project", "."],
            ["--status"],
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(box.BoxError, "no other options"),
            ):
                box.main(["purge", "--cleanup", *extra])
        with self.assertRaisesRegex(box.BoxError, "no other options"):
            box.main(["start", "--cleanup"])
        self.open_engine.assert_not_called()

    def test_namespace_object_is_safe_only_outside_the_removal_tree(self):
        mounts = (
            b"11 1 0:19 / / rw - btrfs /dev/synthetic rw\n"
            b"12 11 0:4 net:[4026532801] /run/synthetic-netns rw - nsfs nsfs rw\n"
        )
        seen = guarded_discard._parse_mounts(mounts)
        guarded_discard._check_mount_backing(seen, "/graph/old", 11)
        with self.assertRaisesRegex(
            guarded_discard.Refused, "namespace mount overlaps"
        ):
            guarded_discard._check_mount_backing(
                guarded_discard._parse_mounts(
                    mounts.replace(b"/run/synthetic-netns", b"/graph/old/ns")
                ),
                "/graph/old",
                11,
            )
        with self.assertRaisesRegex(guarded_discard.Refused, "mount path"):
            guarded_discard._parse_mounts(mounts.replace(b"- nsfs ", b"- overlay "))
