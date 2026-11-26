import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, workspace_preferences, workspace_service


class AddonTests(unittest.TestCase):
    def command(self, *args):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            patch.object(
                box, "workspace_lock", side_effect=AssertionError("lifecycle lock")
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(box.main(["addon", *args]), 0)
        return output.getvalue()

    def test_catalogue_shows_defaults_without_a_workspace(self):
        with patch.object(box, "find_workspace", return_value=(None, None)):
            output = self.command()
            self.assertIn("DEFAULT", output)
            self.assertRegex(output, r"activity-timeline\s+disabled\s+disabled")
            self.assertIn("No registered workspace", output)
            with self.assertRaisesRegex(box.BoxError, "existing valid workspace"):
                self.command("activity-timeline", "on")

    def test_switch_persists_both_directions_without_touching_other_values(self):
        with tempfile.TemporaryDirectory() as directory:
            ident = "a" * 32
            workspace = Path(directory) / ident
            workspace.mkdir(mode=0o700)
            registration = workspace / "workspace.json"
            original = json.dumps({"id": ident})
            registration.write_text(original)
            saved = workspace / workspace_preferences.FILE_NAME
            with patch.object(
                box, "find_workspace", return_value=(registration, {"id": ident})
            ):
                self.assertRegex(
                    self.command(), r"activity-timeline\s+disabled\s+disabled"
                )
                self.assertFalse(saved.exists())
                workspace_preferences.set_preferences(
                    registration, ident, host_drop=False
                )
                previous = json.loads(saved.read_text())
                previous["addons"] = {"unrecognized-future-addon": True}
                saved.write_text(json.dumps(previous))
                self.command("activity-timeline", "on", "--workspace", ident)
                self.assertRegex(
                    self.command(), r"activity-timeline\s+enabled\s+disabled"
                )
                value = workspace_preferences.read(registration, ident)
                self.assertTrue(value["addons"]["activity-timeline"])
                self.assertTrue(value["addons"]["unrecognized-future-addon"])
                self.assertFalse(value["host_drop"])
                self.command("activity-timeline", "off")
                self.assertRegex(
                    self.command(), r"activity-timeline\s+disabled\s+disabled"
                )
                self.assertFalse(
                    workspace_preferences.read(registration, ident)["addons"][
                        "activity-timeline"
                    ]
                )
                self.assertEqual(registration.read_text(), original)

    def test_requested_owning_native_client_gets_a_single_notice_and_private_flag(self):
        data = {
            "home": "/synthetic/home",
            "container": "synthetic-container",
            "project": "/synthetic/project",
        }
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic",
            "ui_state": {
                "available": True,
                "generation": "a" * 32,
                "ownerClient": "b" * 32,
            },
        }
        arguments = [
            box.CONTAINER_OPENCODE,
            data["project"],
            "--server",
            connection["url"],
        ]
        for requested, owner, ready, flag, notice in (
            (False, True, True, None, None),
            (True, True, True, "1", "Activity timeline requested"),
            (True, True, False, None, "Activity timeline unavailable"),
            (True, False, True, None, None),
        ):
            with (
                self.subTest(requested=requested, owner=owner, ready=ready),
                patch.object(box, "maintenance_host_env", return_value={}),
                patch.object(box.startup_output, "line") as line,
            ):
                binding = (
                    connection
                    if ready
                    else {**connection, "ui_state": {"available": False}}
                )
                command = workspace_service.client_command(
                    box,
                    data,
                    binding,
                    arguments,
                    interactive=True,
                    ui_owner=owner,
                    activity_timeline=requested,
                )
                env = json.loads(command[command.index("-c") + 2])
                self.assertEqual(env.get("OC_BOX_ACTIVITY_TIMELINE"), flag)
                self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", env)
                if notice is None:
                    line.assert_not_called()
                else:
                    line.assert_called_once()
                    self.assertIn(notice, line.call_args.args[0])
