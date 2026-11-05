import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_repair", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class WorkspaceRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-repair-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.env = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "state")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")

    def change_identity(self, *, device=False):
        self.data["project_identity"][0 if device else 1] += 1
        box.atomic_json(self.path, self.data)

    def run_repair(self, *selector, info=None):
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "inspect_owned", return_value=info),
            patch.object(box, "launch") as launch,
            patch.object(box, "remove_resources") as remove,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["repair", *selector]), 0)
        launch.assert_not_called()
        remove.assert_not_called()
        return box.read_json(self.path)

    def test_deleting_host_cache_does_not_invalidate_workspace(self):
        home = self.root / "home"
        cache = home / ".cache"
        cache.mkdir(parents=True)
        (cache / "cached-file").write_text("disposable")
        with (
            patch.object(Path, "home", return_value=home),
            patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}),
        ):
            shutil.rmtree(cache)
            self.assertEqual(box.find_workspace(self.project), (self.path, self.data))

    def test_identity_mismatch_explains_recovery_without_replacing_marker(self):
        for device in (False, True):
            with self.subTest(device=device):
                self.change_identity(device=device)
                with self.assertRaises(box.BoxError) as raised:
                    box.find_workspace(self.project)
                message = str(raised.exception)
                self.assertIn("Recorded device/inode:", message)
                self.assertIn("current:", message)
                self.assertIn(f"oc-box repair --project '{self.project}'", message)
                self.assertNotIn("Remove .oc-box-id", message)
                self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_preserves_environment_metadata_sessions_and_project(self):
        self.data.update(
            ready=True,
            devices=["/dev/accel/accel0"],
            podman_args=["--group-add=keep-groups"],
        )
        self.change_identity(device=True)
        source = self.project / "unfinished.py"
        source.write_text("unfinished work")
        box.atomic_json(
            box.session_file(self.path),
            {"last": "ses_existing", "sessions": ["ses_existing"]},
        )
        session_bytes = box.session_file(self.path).read_bytes()
        marker_bytes = (self.project / box.MARKER).read_bytes()
        expected = dict(self.data)
        stat = self.project.stat()
        expected["project_identity"] = [stat.st_dev, stat.st_ino]
        repaired = self.run_repair(
            "--project", str(self.project), info={"State": {"Running": False}}
        )
        self.assertEqual(repaired, expected)
        self.assertEqual(box.find_workspace(self.project), (self.path, expected))
        self.assertEqual(box.session_file(self.path).read_bytes(), session_bytes)
        self.assertEqual((self.project / box.MARKER).read_bytes(), marker_bytes)
        self.assertEqual(source.read_text(), "unfinished work")
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "launch", return_value=0) as launch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["--project", str(self.project), "--resume"]), 0)
        self.assertEqual(launch.call_args.args[1], expected)
        self.assertEqual(box.session_state(self.path)["last"], "ses_existing")

    def test_repair_by_workspace_id_can_retain_a_missing_container(self):
        self.change_identity()
        repaired = self.run_repair("--workspace", self.data["id"])
        self.assertEqual(repaired["id"], self.data["id"])
        self.assertEqual(box.find_workspace(self.project), (self.path, repaired))

    def test_repair_unchanged_identity_is_read_only(self):
        before = self.path.read_bytes()
        with patch.object(box, "atomic_json") as write:
            self.run_repair("--project", str(self.project))
        write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_repair_cannot_rebind_a_copied_marker_to_another_path(self):
        other = self.root / "other"
        other.mkdir()
        (other / box.MARKER).write_text(self.data["id"])
        with self.assertRaisesRegex(box.BoxError, "another directory"):
            self.run_repair("--project", str(other))
        self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_by_id_requires_matching_regular_marker(self):
        self.change_identity()
        marker = self.project / box.MARKER
        for kind in ("missing", "symlink", "different workspace"):
            with self.subTest(kind=kind):
                marker.unlink(missing_ok=True)
                if kind == "symlink":
                    target = self.root / "marker-target"
                    target.write_text(self.data["id"])
                    marker.symlink_to(target)
                elif kind == "different workspace":
                    other = self.root / "other"
                    other.mkdir()
                    _, other_data = box.create_workspace(other, "ubuntu")
                    marker.write_text(other_data["id"])
                with self.assertRaises(box.BoxError):
                    self.run_repair("--workspace", self.data["id"])
                self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_requires_existing_registry(self):
        self.path.unlink()
        with self.assertRaisesRegex(box.BoxError, "Restore the registry"):
            self.run_repair("--project", str(self.project))

    def test_repair_does_not_create_workspace_without_marker(self):
        (self.project / box.MARKER).unlink()
        with self.assertRaisesRegex(box.BoxError, "No oc-box workspace"):
            self.run_repair("--project", str(self.project))

    def test_repair_refuses_running_container(self):
        self.change_identity()
        with self.assertRaisesRegex(box.BoxError, "Container is running"):
            self.run_repair(
                "--project", str(self.project), info={"State": {"Running": True}}
            )
        self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_checks_container_ownership(self):
        self.change_identity()
        inspected = subprocess.CompletedProcess(
            [], 0, '[{"Config": {"Labels": {}}}]', ""
        )
        with (
            patch.object(box, "podman", return_value=inspected),
            self.assertRaisesRegex(box.BoxError, "ownership label mismatch"),
        ):
            box.repair_workspace(self.path, self.data)
        self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_refuses_active_launcher(self):
        self.change_identity()
        with (
            box.workspace_lock(self.path),
            self.assertRaisesRegex(box.BoxError, "active oc-box process"),
        ):
            self.run_repair("--project", str(self.project))
        self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_validates_project_before_updating_identity(self):
        self.change_identity()
        with (
            patch.object(box, "validate_project", side_effect=box.BoxError("unsafe")),
            self.assertRaisesRegex(box.BoxError, "unsafe"),
        ):
            self.run_repair("--project", str(self.project))
        self.assertEqual(box.read_json(self.path), self.data)

    def test_repair_rejects_launch_and_deletion_options(self):
        for option in ("--resume", "--new", "--all", "--yes", "--memory=16g"):
            with (
                self.subTest(option=option),
                patch.object(box, "check_backend") as backend,
            ):
                with self.assertRaises(box.BoxError):
                    box.main(["repair", "--project", str(self.project), option])
                backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
