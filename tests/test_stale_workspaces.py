import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_stale", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class StaleWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-stale-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environ = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        environ.start()
        self.addCleanup(environ.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.old_path, self.old = box.create_workspace(self.project, "tumbleweed")
        self.source = {
            "info": {
                "id": "ses_original",
                "directory": str(self.project),
                "title": "Saved work",
            },
            "messages": [
                {
                    "info": {
                        "id": "msg_original",
                        "sessionID": "ses_original",
                        "role": "user",
                    },
                    "parts": [
                        {
                            "id": "prt_original",
                            "sessionID": "ses_original",
                            "messageID": "msg_original",
                            "type": "text",
                            "text": "work to retain",
                        }
                    ],
                }
            ],
        }
        self.archive = box.archive_session(self.source, self.old)
        self.ident = self.archive["info"]["id"]
        box.atomic_json(
            self.old_path.parent / "history" / (self.ident + ".json"), self.archive
        )
        box.atomic_json(
            box.session_file(self.old_path),
            {
                "last": "ses_original",
                "sessions": ["ses_original"],
                "host_sessions": {"ses_original": self.ident},
            },
        )
        # Reproduce the original lost-marker incident, not merely two paths.
        (self.project / box.MARKER).unlink()
        self.new_path, self.new = box.create_workspace(self.project, "tumbleweed")
        box.atomic_json(
            box.session_file(self.new_path),
            {"last": self.ident, "sessions": [self.ident]},
        )
        self.work = self.project / "unfinished.txt"
        self.work.write_text("unfinished current work")
        self.args = argparse.Namespace(yes=True, delete_sessions=False, all=False)

    def test_plain_stale_purge_preserves_live_workspace_and_recoverable_host_archive(
        self,
    ):
        before = {
            path: path.read_bytes()
            for path in (
                self.new_path,
                box.session_file(self.new_path),
                self.project / box.MARKER,
                self.work,
            )
        }
        events = []
        info = {"State": {"Running": False}}
        with (
            patch.object(box, "inspect_owned", return_value=info),
            patch.object(box, "podman") as podman,
            patch.object(
                box,
                "sync_sessions",
                side_effect=lambda path, data: events.append(("sync", data["id"])),
            ),
            patch.object(
                box,
                "stop_container",
                side_effect=lambda data: events.append(("stop", data["id"])),
            ),
            patch.object(
                box,
                "remove_resources",
                side_effect=lambda data: events.append(("remove", data["id"])),
            ),
            patch.object(box, "run") as host_cli,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.purge(self.old_path, self.old, self.args)
        self.assertEqual(
            events, [(action, self.old["id"]) for action in ("sync", "stop", "remove")]
        )
        self.assertEqual(podman.call_args.args, ("start", self.old["container"]))
        host_cli.assert_not_called()  # Host archives are not deleted.
        self.assertFalse(self.old_path.parent.exists())
        self.assertEqual(box.workspace_paths(), [self.new_path])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        retired = box.state_root() / "retired" / self.old["id"]
        saved = retired / "history" / (self.ident + ".json")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(retired.stat().st_mode & 0o777, 0o700)
        self.assertEqual(box.read_json(saved), self.archive)
        # Mainline host archive still refers to its old controller directory.
        host = json.loads(json.dumps(self.archive))
        host["info"]["directory"] = str(self.old_path.parent / "controller")
        host["messages"][0]["parts"][0]["text"] = "host work after archive"
        self.assertTrue(box.restore_archive_directory(host, self.project))
        self.assertEqual(host["info"]["directory"], str(self.project))
        self.assertEqual(
            host["messages"][0]["parts"][0]["text"], "host work after archive"
        )

    def test_failed_final_sync_aborts_resource_and_metadata_deletion(self):
        with (
            patch.object(
                box, "inspect_owned", return_value={"State": {"Running": False}}
            ),
            patch.object(box, "podman"),
            patch.object(box, "sync_sessions", side_effect=box.BoxError("sync failed")),
            patch.object(box, "stop_container") as stop,
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(box.BoxError, "sync failed"):
                box.purge(self.old_path, self.old, self.args)
        stop.assert_called_once_with(self.old)
        remove.assert_not_called()
        self.assertTrue(self.old_path.exists())
        self.assertTrue(self.new_path.exists())

    def test_all_refuses_stale_target_before_deleting_any_history_or_resources(self):
        self.args.all = True
        with (
            patch.object(box, "inspect_owned") as inspect,
            patch.object(box, "run") as host_cli,
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(box.BoxError, "refusing --all before deleting"):
                box.purge(self.old_path, self.old, self.args)
        inspect.assert_not_called()
        host_cli.assert_not_called()
        remove.assert_not_called()
        self.assertTrue(self.old_path.exists())
        self.assertTrue(self.work.exists())

    def test_failed_provenance_copy_prevents_removing_resources(self):
        with (
            patch.object(box, "inspect_owned", return_value=None),
            patch.object(box, "stop_container"),
            patch.object(
                box, "retain_archive_provenance", side_effect=OSError("disk full")
            ),
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                box.purge(self.old_path, self.old, self.args)
        remove.assert_not_called()
        self.assertTrue(self.old_path.exists())

    def test_explicit_session_deletion_does_not_create_retired_history(self):
        self.args.delete_sessions = True
        before = (self.project / box.MARKER).read_bytes()
        with (
            patch.object(box, "stop_container"),
            patch.object(box, "remove_resources"),
            patch.object(box, "run") as host_cli,
            patch.object(box, "retain_archive_provenance") as keep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.purge(self.old_path, self.old, self.args)
        keep.assert_not_called()
        self.assertEqual(
            host_cli.call_args.args[0],
            ["opencode", "--pure", "session", "delete", self.ident],
        )
        self.assertEqual((self.project / box.MARKER).read_bytes(), before)
        self.assertFalse((box.state_root() / "retired" / self.old["id"]).exists())
        self.assertTrue(self.new_path.exists())

    def test_retired_provenance_is_validated_not_trusted_by_title(self):
        box.retain_archive_provenance(self.old_path, self.old)

        shutil.rmtree(self.old_path.parent)
        host = json.loads(json.dumps(self.archive))
        host["info"]["directory"] = str(self.old_path.parent / "controller")
        retired = box.state_root() / "retired" / self.old["id"]
        origin = box.read_json(retired / "workspace.json")
        origin["project"] = str(self.root / "unrelated")
        box.atomic_json(retired / "workspace.json", origin)
        self.assertFalse(box.restore_archive_directory(host, self.project))

    def test_list_identifies_stale_marker_and_issues_nondestructive_cleanup_hint(self):
        output, errors = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            patch.object(box, "podman") as podman,
        ):
            self.assertEqual(box.main(["list"]), 0)
        old_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith(self.old["id"])
        )
        current_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith(self.new["id"])
        )
        self.assertIn("marker mismatch", old_line)
        self.assertIn("present", current_line)
        self.assertIn("purge --workspace " + self.old["id"], errors.getvalue())
        podman.assert_not_called()


if __name__ == "__main__":
    unittest.main()
