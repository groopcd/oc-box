import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import studio_registry as registry
from oc_box import workspace_identity


ID = "a" * 32
OTHER = "b" * 32
STABLE = workspace_identity.STABLE_IDENTITY_KEY


class StudioRegistryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        app = patch.object(
            registry.paths, "source_root", return_value=self.root / "launcher"
        )
        app.start()
        self.addCleanup(app.stop)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.marker = self.project / registry.MARKER_NAME
        self.marker.write_text(f"studio-v1:{ID}\n")
        self.data = self.registration(ID, self.project)
        self.record = self.write_registration(ID, self.data)

    @staticmethod
    def registration(ident, project):
        data = {"version": 1, "role": "studio", "id": ident, "project": str(project)}
        workspace_identity.record(data, *workspace_identity.snapshot(project))
        return data

    def write_registration(self, ident, data, *, legacy=False):
        parent = self.state / ("workspaces" if legacy else registry.NAMESPACE)
        parent.mkdir(mode=0o755 if legacy else 0o700, exist_ok=True)
        folder = parent / ident
        folder.mkdir(mode=0o700, exist_ok=True)
        record = folder / ("workspace.json" if legacy else registry.RECORD_NAME)
        record.write_text(json.dumps(data))
        record.chmod(0o644 if legacy else 0o600)
        return record

    def select(self, **selector):
        return registry.select(self.state, **selector)

    def assert_both_refuse(self, message="could not be verified"):
        for selector in ({"project": self.project}, {"workspace_id": ID}):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(registry.RegistryError, message):
                    self.select(**selector)

    def test_selects_same_snapshot_without_writing(self):
        def contents():
            return {
                path.relative_to(self.root): (
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in self.root.rglob("*")
                if path.is_file()
            }

        before = contents()
        by_project = self.select(project=self.project)
        by_id = self.select(workspace_id=ID)
        self.assertEqual(by_project, by_id)
        self.assertEqual(
            (by_id.id, by_id.role, by_id.project), (ID, "studio", self.project)
        )
        self.assertEqual(by_id.project_identity, tuple(self.data["project_identity"]))
        self.assertEqual(before, contents())

    def test_selector_is_explicit_and_host_only(self):
        for selector in (
            {},
            {"project": self.project, "workspace_id": ID},
            {"workspace_id": ID.upper()},
            {"workspace_id": f"studio-v1:{ID}"},
            {"workspace_id": "../" + ID},
            {"project": "project"},
            {"project": str(self.project) + "/../project"},
        ):
            with self.subTest(selector=selector):
                with self.assertRaises(registry.RegistryError):
                    self.select(**selector)
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            self.assert_both_refuse("host-only")

    def test_marker_never_falls_back_to_legacy_or_unregistered(self):
        for marker in (
            f"{ID}\n",
            f"studio-v2:{ID}\n",
            f"studio-v1:{ID}\n\n",
            f"studio-v1:{OTHER}\n",
        ):
            with self.subTest(marker=marker):
                self.marker.write_text(marker)
                self.assert_both_refuse()
        self.marker.unlink()
        self.assert_both_refuse()
        self.marker.write_text(f"studio-v1:{ID}")
        self.record.unlink()
        self.write_registration(ID, self.data, legacy=True)
        self.assert_both_refuse()
        self.record.parent.rmdir()
        self.record.parent.parent.rmdir()
        self.assert_both_refuse()
        self.assertFalse((self.state / registry.NAMESPACE).exists())
        absent = self.root / "absent-state"
        with self.assertRaises(registry.RegistryError):
            registry.select(absent, workspace_id=ID)
        self.assertFalse(absent.exists())

    def test_invalid_record_role_version_id_or_shape_never_selects(self):
        for override in (
            {"role": "legacy"},
            {"role": "named"},
            {"id": OTHER},
            {"version": True},
            {"version": 2},
            {"project_identity": [False, 1]},
            {STABLE: None},
            {"project": str(self.project) + "/."},
            {"container": "untrusted"},
        ):
            with self.subTest(override=override):
                self.write_registration(ID, self.data | override)
                self.assert_both_refuse()
        for raw in (
            b'{"role":"studio","role":"legacy"}',
            json.dumps(self.data)
            .replace('"role": "studio"', '"role": "legacy", "role": "studio"')
            .encode(),
            b"\xff",
            b"x" * (registry._MAX_RECORD + 1),
        ):
            self.record.write_bytes(raw)
            self.assert_both_refuse()

    def test_copied_marker_or_replacement_project_is_not_identity(self):
        other = self.root / "other-project"
        other.mkdir()
        (other / self.marker.name).write_bytes(self.marker.read_bytes())
        with self.assertRaises(registry.RegistryError):
            self.select(project=other)
        self.project.rename(self.root / "retained-project")
        self.assert_both_refuse()
        other.rename(self.project)
        self.assert_both_refuse()

    def test_recorded_stable_identity_required_without_fallback(self):
        device, inode = self.data["project_identity"]
        stable = {
            "method": "linux-file-handle-v1",
            "fsid": "42",
            "handle": {"type": 1, "bytes": "aabb"},
        }
        self.write_registration(
            ID, self.data | {STABLE: stable, "project_identity": [device + 1, inode]}
        )
        with patch.object(
            workspace_identity, "snapshot_fd", return_value=([device, inode], stable)
        ):
            self.assertEqual(
                self.select(workspace_id=ID).project_identity, (device, inode)
            )
        with patch.object(
            workspace_identity, "snapshot_fd", return_value=([device, inode], None)
        ):
            self.assert_both_refuse()
        changed = stable | {"fsid": "43"}
        with patch.object(
            workspace_identity, "snapshot_fd", return_value=([device, inode], changed)
        ):
            self.assert_both_refuse()

    def test_refuses_linked_or_overpermissive_state(self):
        for path, mode in (
            (self.state, 0o777),
            (self.record.parent.parent, 0o755),
            (self.record.parent, 0o770),
            (self.record, 0o644),
            (self.marker, 0o666),
        ):
            with self.subTest(path=path):
                before = path.stat().st_mode & 0o777
                path.chmod(mode)
                self.assert_both_refuse()
                path.chmod(before)
        for path in (self.record, self.marker):
            original = path.with_suffix(".original")
            path.rename(original)
            for link in (os.link, os.symlink):
                with self.subTest(path=path, link=link):
                    link(original, path)
                    self.assert_both_refuse()
                    path.unlink()
            os.mkfifo(path, 0o600)
            self.assert_both_refuse()
            path.unlink()
            original.rename(path)
        for directory in (
            self.record.parent,
            self.record.parent.parent,
            self.state,
            self.project,
        ):
            with self.subTest(directory=directory):
                original = directory.with_name("linked-target")
                directory.rename(original)
                directory.symlink_to(original, target_is_directory=True)
                self.assert_both_refuse()
                directory.unlink()
                original.rename(directory)

    def test_refuses_intermediate_directory_links(self):
        parent = self.root / "alias"
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(registry.RegistryError):
            registry.select(parent / "state", workspace_id=ID)
        self.write_registration(ID, self.data | {"project": str(parent / "project")})
        with self.assertRaises(registry.RegistryError):
            self.select(workspace_id=ID)

    def test_refuses_protected_paths_in_either_direction(self):
        private = self.root / "private"
        private.mkdir()
        tls = registry.paths.web_tls_home()
        tls.parent.mkdir(parents=True)
        tls.symlink_to(private, target_is_directory=True)
        self.write_registration(ID, self.data | {"project": str(private / "child")})
        for project in (
            self.root,
            self.state / "child",
            self.home,
            self.root / "launcher",
            private,
            private / "child",
        ):
            with self.subTest(project=project):
                with self.assertRaisesRegex(
                    registry.RegistryError, "overlaps protected"
                ):
                    self.select(project=project)
        with self.assertRaisesRegex(registry.RegistryError, "overlaps protected"):
            self.select(workspace_id=ID)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.project)}):
            with self.assertRaisesRegex(registry.RegistryError, "overlaps protected"):
                self.select(project=self.project)

    def test_active_overlaps_and_id_collisions_refuse_even_with_stale_peers(self):
        for legacy in (False, True):
            for other_project in (
                self.project,
                self.project / "missing-child",
                self.project.parent,
            ):
                with self.subTest(legacy=legacy, other_project=other_project):
                    peer = self.write_registration(
                        OTHER,
                        self.data | {"id": OTHER, "project": str(other_project)},
                        legacy=legacy,
                    )
                    self.assert_both_refuse("overlaps protected")
                    peer.unlink()
                    peer.parent.rmdir()
        separate = self.root / "separate"
        separate.mkdir()
        actual = self.registration(OTHER, separate)
        self.write_registration(OTHER, actual, legacy=True)
        self.write_registration(OTHER, actual | {"id": "c" * 32}, legacy=True)
        self.assert_both_refuse()
        self.write_registration(OTHER, actual, legacy=True)
        self.assertEqual(self.select(workspace_id=ID).id, ID)
        self.write_registration(
            OTHER, self.data | {"id": OTHER, "project": str(separate)}, legacy=True
        )
        self.assert_both_refuse("overlaps protected")
        self.write_registration(OTHER, actual, legacy=True)
        self.write_registration(ID, actual | {"id": ID}, legacy=True)
        self.assert_both_refuse("overlaps protected")

    def test_malformed_or_over_capacity_peer_namespace_refuses(self):
        second = self.root / "second"
        second.mkdir()
        peer = self.write_registration(OTHER, self.registration(OTHER, second))
        with patch.object(registry, "_MAX_ENTRIES", 1):
            self.assert_both_refuse()
        peer.write_text("invalid")
        self.assert_both_refuse()

    def test_detects_replacement_during_binding(self):
        original = registry._scan
        for changed in (self.marker, self.record):
            with self.subTest(changed=changed):

                def replace(state, namespace, *, studio):
                    view = original(state, namespace, studio=studio)
                    if not studio and not moved.exists():
                        changed.rename(moved)
                        changed.write_bytes(moved.read_bytes())
                        changed.chmod(moved.stat().st_mode & 0o777)
                    return view

                moved = changed.with_suffix(".moved")
                with patch.object(registry, "_scan", side_effect=replace):
                    with self.assertRaises(registry.RegistryError):
                        self.select(workspace_id=ID)
                moved.replace(changed)
                self.assertEqual(self.select(workspace_id=ID).id, ID)

    def test_detects_detached_project_during_binding(self):
        snapshot = workspace_identity.snapshot_fd

        def detach(fd):
            retained = self.project.with_name("retained-project")
            self.project.rename(retained)
            self.project.mkdir()
            self.marker.write_bytes((retained / self.marker.name).read_bytes())
            return snapshot(fd)

        with patch.object(workspace_identity, "snapshot_fd", side_effect=detach):
            with self.assertRaises(registry.RegistryError):
                self.select(workspace_id=ID)


if __name__ == "__main__":
    unittest.main()
