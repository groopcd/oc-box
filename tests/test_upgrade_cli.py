"""Published-channel CLI routing; no real downloads, installs or workspaces."""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, updater


class UpgradeCliTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        stack.enter_context(patch.object(os, "geteuid", return_value=1000))
        self.upgrade = stack.enter_context(patch.object(updater, "upgrade"))
        self.settings = stack.enter_context(patch.object(updater, "update_settings"))
        self.packaged = stack.enter_context(
            patch.object(updater, "package_managed", return_value=False)
        )
        self.message = stack.enter_context(
            patch.object(
                updater, "package_upgrade_message", return_value="Owner guidance."
            )
        )

    def test_selectors_default_none_and_are_mutually_exclusive(self):
        self.assertIsNone(box.parser().parse_args(["upgrade"]).update_channel)
        for channel in ("stable", "devel"):
            self.assertEqual(
                box.parser().parse_args(["upgrade", "--" + channel]).update_channel,
                channel,
            )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            box.main(["upgrade", "--stable", "--devel"])
        self.upgrade.assert_not_called()

    def test_preview_and_channel_route_for_github_and_package_owners(self):
        for packaged in (False, True):
            for channel in (None, "stable", "devel"):
                for check in (False, True):
                    with self.subTest(packaged=packaged, channel=channel, check=check):
                        self.packaged.return_value = packaged
                        args = ["upgrade"]
                        if channel:
                            args.append("--" + channel)
                        if check:
                            args.append("--check")
                        self.assertEqual(box.main(args), 0)
                        self.upgrade.assert_called_once_with(
                            box.APP,
                            check=check,
                            channel=channel,
                            with_vault=False,
                            with_tui=False,
                        )
                        self.upgrade.reset_mock()
        self.settings.assert_not_called()
        self.message.assert_not_called()

    def test_channel_flags_cannot_change_workspace_or_informational_commands(self):
        for channel in ("--stable", "--devel"):
            for args in ([], ["start"], ["stop"], ["reset"], ["status"], ["--version"]):
                with self.subTest(args=args, channel=channel):
                    with self.assertRaisesRegex(box.BoxError, "only to upgrade"):
                        box.main([*args, channel])
            with self.assertRaisesRegex(box.BoxError, "Credentials accepts only"):
                box.main(["credentials", channel])
            for command in ("tui", "share", "web", "doctor", "backup", "restore"):
                with self.subTest(command=command, channel=channel):
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        box.main([command, channel])
        self.upgrade.assert_not_called()
        self.settings.assert_not_called()

    def test_autoupdate_options_cannot_be_combined_with_preview_or_channel(self):
        for value in ("on", "off"):
            for extra in ("--check", "--stable", "--devel", "--vault", "--tui"):
                with self.assertRaisesRegex(box.BoxError, "cannot be combined"):
                    box.main(["upgrade", "--auto-update", value, extra])
        self.packaged.assert_not_called()
        self.settings.assert_not_called()
        self.upgrade.assert_not_called()

    def test_root_and_container_refuse_before_owner_detection(self):
        for container, uid, message in (
            ("1", 1000, "on the host"),
            ("0", 0, "without sudo"),
        ):
            for args in (
                ["upgrade"],
                ["upgrade", "--devel"],
                ["upgrade", "--check", "--stable"],
                ["upgrade", "--devel", "--vault", "--tui"],
                ["upgrade", "--auto-update", "off"],
            ):
                with self.subTest(container=container, uid=uid, args=args):
                    with (
                        patch.dict(os.environ, {"OC_BOX_CONTAINER": container}),
                        patch.object(os, "geteuid", return_value=uid),
                        self.assertRaisesRegex(box.BoxError, message),
                    ):
                        box.main(args)
        self.packaged.assert_not_called()
        self.settings.assert_not_called()
        self.upgrade.assert_not_called()

    def test_package_autoupdate_does_not_change_github_preferences(self):
        self.packaged.return_value = True
        with self.assertRaisesRegex(box.BoxError, "cannot manage a Python-package"):
            box.main(["upgrade", "--auto-update", "on"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(box.main(["upgrade", "--auto-update", "off"]), 0)
        self.assertEqual(output.getvalue(), "Owner guidance.\n")
        self.settings.assert_not_called()
        self.upgrade.assert_not_called()

    def test_github_autoupdate_still_routes_and_owner_refusals_propagate(self):
        for value in ("on", "off"):
            self.assertEqual(box.main(["upgrade", "--auto-update", value]), 0)
            self.settings.assert_called_once_with(value == "on")
            self.settings.reset_mock()
        self.upgrade.side_effect = updater.UpdateError(
            "Editable source: update checkout."
        )
        with self.assertRaisesRegex(box.BoxError, "Editable source"):
            box.main(["upgrade", "--devel"])


if __name__ == "__main__":
    unittest.main()
