import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_import", APP / "oc_box.py")
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class SessionImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-import-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.env = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.ident = "ses_host"
        self.exported = {
            "info": {
                "id": self.ident,
                "directory": str(self.project),
                "title": "Host work",
                "time": {"created": 1, "updated": 2},
            },
            "messages": [
                {
                    "info": {"id": "msg_user", "sessionID": self.ident, "role": "user"},
                    "parts": [
                        {
                            "id": "prt_text",
                            "messageID": "msg_user",
                            "sessionID": self.ident,
                            "type": "text",
                            "text": "history",
                        }
                    ],
                }
            ],
        }

    def test_import_argument_conflicts_and_invalid_ids_fail_before_backend(self):
        for args in (
            ["sync", "--import-session", self.ident],
            ["list", "--import-session", self.ident],
            ["--import-session", "bad-id"],
        ):
            with (
                self.subTest(args=args),
                patch.object(box, "check_backend") as backend,
                patch("sys.stderr", io.StringIO()),
            ):
                with self.assertRaises((box.BoxError, SystemExit)):
                    box.main(args)
                backend.assert_not_called()
        for mode in ("--new", "--resume"):
            with (
                self.subTest(mode=mode),
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                box.parser().parse_args([mode, "--import-session", self.ident])

    def test_host_export_is_read_only_uses_pure_profile_and_validates_project(self):
        with patch.object(
            box,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.exported), ""
            ),
        ) as run:
            self.assertEqual(
                box.export_host_session(self.path, self.data, self.ident), self.exported
            )
        self.assertEqual(
            run.call_args.args[0], ["opencode", "--pure", "export", self.ident]
        )
        self.assertTrue(run.call_args.kwargs["capture"])
        self.assertEqual(
            run.call_args.kwargs["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1"
        )
        self.exported["info"]["directory"] = str(self.root / "another-project")
        with patch.object(
            box,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.exported), ""
            ),
        ):
            with self.assertRaisesRegex(box.BoxError, "original project"):
                box.export_host_session(self.path, self.data, self.ident)

    def test_subagent_session_is_not_imported_as_a_root_conversation(self):
        self.exported["info"]["parentID"] = "ses_parent"
        with patch.object(
            box,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.exported), ""
            ),
        ):
            with self.assertRaisesRegex(box.BoxError, "parent conversation"):
                box.export_host_session(self.path, self.data, self.ident)

    def test_malformed_host_export_does_not_start_container(self):
        args = argparse.Namespace(
            command="start", new=False, model=None, import_session=self.ident
        )
        with (
            patch.object(
                box,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "1.19.5", ""),
                    subprocess.CompletedProcess([], 0, '{"partial":', ""),
                ],
            ),
            patch.object(box, "start_container") as start,
        ):
            with self.assertRaisesRegex(box.BoxError, "invalid/incomplete"):
                box.launch(self.path, self.data, args)
        start.assert_not_called()

    def test_import_preserves_history_backs_up_source_and_selects_exact_id(self):
        box.atomic_json(
            box.session_file(self.path),
            {"sessions": ["ses_previous"], "last": "ses_previous"},
        )
        results = [
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "imported", ""),
        ]
        with (
            patch.object(box, "run", side_effect=results) as run,
            patch.object(box, "export_container_session", return_value=self.exported),
        ):
            state = box.import_host_session(self.path, self.data, self.exported)
        self.assertEqual(state["last"], self.ident)
        self.assertEqual(state["sessions"], ["ses_previous", self.ident])
        backup = Path(state["imported_from_host"][self.ident]["backup"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(backup.read_text()), self.exported)
        self.assertFalse(backup.is_relative_to(self.project))
        payload = json.loads(run.call_args_list[1].kwargs["input"])
        self.assertEqual(payload["messages"], self.exported["messages"])
        self.assertGreater(payload["info"]["time"]["updated"], 2)
        self.assertEqual(self.exported["info"]["time"]["updated"], 2)

    def test_duplicate_import_does_not_overwrite_continued_container_work(self):
        existing = subprocess.CompletedProcess(
            [], 0, json.dumps([{"id": self.ident}]), ""
        )
        with patch.object(box, "run", return_value=existing) as run:
            with self.assertRaisesRegex(box.BoxError, "not overwritten"):
                box.import_host_session(self.path, self.data, self.exported)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(box.session_file(self.path).exists())

    def test_incomplete_transfer_never_selects_session_for_launch(self):
        partial = json.loads(json.dumps(self.exported))
        partial["messages"][0]["parts"][0]["text"] = "truncated or changed"
        with (
            patch.object(
                box,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "[]", ""),
                    subprocess.CompletedProcess([], 0, "imported", ""),
                ],
            ),
            patch.object(box, "export_container_session", return_value=partial),
        ):
            with self.assertRaisesRegex(box.BoxError, "verification failed"):
                box.import_host_session(self.path, self.data, self.exported)
        self.assertFalse(box.session_file(self.path).exists())
        self.assertTrue(
            (self.path.parent / "imports" / (self.ident + ".json")).exists()
        )

    def tool_transcript(self, status="completed"):
        value = json.loads(json.dumps(self.exported))
        value["messages"][0]["parts"].append(
            {
                "id": "prt_tool",
                "messageID": "msg_user",
                "sessionID": self.ident,
                "type": "tool",
                "tool": "read",
                "callID": "call_fixture",
                "state": {
                    "status": status,
                    "input": {"raw": "user data"},
                    "output": "saved output",
                    "raw": "obsolete buffer",
                },
            }
        )
        return value

    def test_only_obsolete_nonpending_tool_raw_may_be_omitted(self):
        for status in ("running", "completed", "error"):
            with self.subTest(status=status):
                original = self.tool_transcript(status)
                imported = json.loads(json.dumps(original))
                del imported["messages"][0]["parts"][1]["state"]["raw"]
                self.assertEqual(
                    box.verify_session_import(self.data, original, imported), 1
                )
                self.assertIn("raw", original["messages"][0]["parts"][1]["state"])

    def test_pending_raw_and_nested_raw_are_still_verified(self):
        for status, field in (
            ("pending", "raw"),
            ("completed", "input"),
            ("completed", "output"),
        ):
            with self.subTest(status=status, field=field):
                original = self.tool_transcript(status)
                imported = json.loads(json.dumps(original))
                if field == "input":
                    del imported["messages"][0]["parts"][1]["state"]["input"]["raw"]
                else:
                    del imported["messages"][0]["parts"][1]["state"][field]
                with self.assertRaisesRegex(box.BoxError, "verification failed"):
                    box.verify_session_import(self.data, original, imported)

    def test_changed_raw_is_not_ignored_when_present_in_both_copies(self):
        original = self.tool_transcript()
        imported = json.loads(json.dumps(original))
        imported["messages"][0]["parts"][1]["state"]["raw"] = "different buffer"
        with self.assertRaisesRegex(box.BoxError, "verification failed"):
            box.verify_session_import(self.data, original, imported)

    def test_recover_failed_verification_without_reimporting_or_replacing_backup(self):
        original = self.tool_transcript()
        backup = self.path.parent / "imports" / (self.ident + ".json")
        box.atomic_json(backup, original)
        before = backup.read_bytes()
        imported = json.loads(json.dumps(original))
        del imported["messages"][0]["parts"][1]["state"]["raw"]
        # Host may have changed since failure; recovery must use the backup.
        current_host = json.loads(json.dumps(original))
        current_host["messages"][0]["parts"][0]["text"] = "new host progress"
        existing = subprocess.CompletedProcess(
            [], 0, json.dumps([{"id": self.ident}]), ""
        )
        with (
            patch.object(box, "run", return_value=existing) as run,
            patch.object(box, "export_container_session", return_value=imported),
        ):
            state = box.import_host_session(self.path, self.data, current_host)
        self.assertEqual(run.call_count, 1)  # listing only, no import/write command
        self.assertEqual(state["last"], self.ident)
        self.assertEqual(backup.read_bytes(), before)
        self.assertIn(self.ident, state["imported_from_host"])
        # Once verified, even matching transcripts cannot be imported again.
        with patch.object(box, "run", return_value=existing):
            with self.assertRaisesRegex(box.BoxError, "not overwritten"):
                box.import_host_session(self.path, self.data, current_host)

    def test_recovery_refuses_missing_or_changed_container_content(self):
        original = self.tool_transcript()
        backup = self.path.parent / "imports" / (self.ident + ".json")
        box.atomic_json(backup, original)
        imported = json.loads(json.dumps(original))
        imported["messages"][0]["parts"][1]["state"]["output"] = (
            "continued container work"
        )
        existing = subprocess.CompletedProcess(
            [], 0, json.dumps([{"id": self.ident}]), ""
        )
        with (
            patch.object(box, "run", return_value=existing) as run,
            patch.object(box, "export_container_session", return_value=imported),
        ):
            with self.assertRaisesRegex(box.BoxError, "verification failed"):
                box.import_host_session(self.path, self.data, original)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(box.session_file(self.path).exists())

    def test_verification_rejects_missing_messages_or_parts(self):
        original = self.tool_transcript()
        for field in ("message", "part"):
            with self.subTest(field=field):
                imported = json.loads(json.dumps(original))
                if field == "message":
                    imported["messages"].clear()
                else:
                    imported["messages"][0]["parts"].pop()
                with self.assertRaisesRegex(box.BoxError, "verification failed"):
                    box.verify_session_import(self.data, original, imported)

    def test_imported_session_is_launched_even_if_another_is_newer(self):
        args = argparse.Namespace(
            command="start", new=False, model=None, import_session=self.ident
        )
        with (
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "1.19.5", ""),
            ),
            patch.object(box, "export_host_session", return_value=self.exported),
            patch.object(box, "start_container"),
            patch.object(box, "prepare_runtime"),
            patch.object(box, "import_host_session", return_value={"last": self.ident}),
            patch.object(box, "refresh_sessions") as refresh,
            patch.object(box, "sync_sessions"),
            patch.object(box, "run_tui", return_value=0) as tui,
            patch.object(box, "stop_container"),
        ):
            self.assertEqual(box.launch(self.path, self.data, args), 0)
        refresh.assert_not_called()
        self.assertEqual(tui.call_args.args[0][-2:], ["--session", self.ident])

    def test_import_failure_stops_container_without_launching(self):
        args = argparse.Namespace(
            command="start", new=False, model=None, import_session=self.ident
        )
        with (
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "1.19.5", ""),
            ),
            patch.object(box, "export_host_session", return_value=self.exported),
            patch.object(box, "start_container"),
            patch.object(box, "prepare_runtime"),
            patch.object(
                box, "import_host_session", side_effect=box.BoxError("failed import")
            ),
            patch.object(box, "run_tui") as tui,
            patch.object(box, "stop_container") as stop,
        ):
            with self.assertRaisesRegex(box.BoxError, "failed import"):
                box.launch(self.path, self.data, args)
        tui.assert_not_called()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
