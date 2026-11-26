import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, ui_state, workspace_preferences, workspace_service


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
            self.assertRegex(output, r"inline-timestamps\s+disabled\s+disabled")
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
                self.command("inline-timestamps", "on")
                self.command("activity-timeline", "off")
                self.assertRegex(
                    self.command(), r"activity-timeline\s+disabled\s+disabled"
                )
                self.assertFalse(
                    workspace_preferences.read(registration, ident)["addons"][
                        "activity-timeline"
                    ]
                )
                self.assertRegex(
                    self.command(), r"inline-timestamps\s+enabled\s+disabled"
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
        for requested, inline, owner, ready, notices in (
            (False, False, True, True, ()),
            (True, False, True, True, ("Activity timeline requested",)),
            (True, False, True, False, ("Activity timeline requested",)),
            (False, True, True, False, ("Inline timestamps requested",)),
            (True, True, False, True, ()),
            (
                True,
                True,
                True,
                True,
                ("Activity timeline requested", "Inline timestamps requested"),
            ),
        ):
            with (
                self.subTest(
                    requested=requested, inline=inline, owner=owner, ready=ready
                ),
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
                    inline_timestamps=inline,
                )
                env = json.loads(command[command.index("-c") + 2])
                self.assertEqual(
                    env.get("OC_BOX_ACTIVITY_TIMELINE"),
                    "1" if owner and requested else None,
                )
                self.assertEqual(
                    env.get("OC_BOX_INLINE_TIMESTAMPS"),
                    "1" if owner and inline else None,
                )
                self.assertEqual("OC_BOX_UI_STATE_CLIENT" in env, ready)
                self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", env)
                self.assertEqual(line.call_count, len(notices))
                for notice, call in zip(notices, line.call_args_list):
                    self.assertIn(notice, call.args[0])

    def test_incomplete_addon_does_not_block_preparing_the_base_config(self):
        source = box.PACKAGE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*ui_state.RESOURCES, ui_state.ACTIVITY_TIMELINE_RESOURCES[0]):
                (root / name).write_bytes((source / name).read_bytes())
            with (
                patch.object(box, "PACKAGE", root),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}),
            ):
                archive = box.config_archive(
                    {"opencode_version": "2.4.2", "home": "/synthetic/home"},
                    activity_timeline=True,
                )
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as config:
                prefix = "plugins/oc-box-ui-state/"
                self.assertIn(prefix + "ui_state_tui.js", config.getnames())
                for name in ui_state.ACTIVITY_TIMELINE_RESOURCES:
                    self.assertNotIn(prefix + name, config.getnames())

    def test_inline_assets_are_independently_optional(self):
        source = box.PACKAGE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in {
                *ui_state.RESOURCES,
                *ui_state.ACTIVITY_TIMELINE_RESOURCES,
                *ui_state.INLINE_TIMESTAMP_RESOURCES,
            }:
                (root / name).write_bytes((source / name).read_bytes())
            for inline, panel in (
                (False, False),
                (True, False),
                (False, True),
                (True, True),
            ):
                with self.subTest(inline=inline, panel=panel):
                    package = ui_state.package_sources(
                        root, include_addons=panel, inline_timestamps=inline
                    )
                    self.assertEqual("inline_timestamps_tui.mjs" in package, inline)
                    self.assertEqual("activity_timeline_tui.tsx" in package, panel)
                    self.assertEqual(
                        "activity_timeline_events.mjs" in package, inline or panel
                    )
            (root / "inline_timestamps_tui.mjs").unlink()
            package = ui_state.package_sources(
                root, include_addons=True, inline_timestamps=True
            )
            self.assertTrue(set(ui_state.ACTIVITY_TIMELINE_RESOURCES) <= package.keys())
            self.assertNotIn("inline_timestamps.mjs", package)
