"""Upgrade CLI ownership and channel dispatch without downloads or installs."""

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
        self.package = stack.enter_context(
            patch.object(updater, "package_managed", return_value=False)
        )
        self.message = stack.enter_context(
            patch.object(
                updater,
                "package_upgrade_message",
                return_value="Package manager guidance.",
            )
        )

    def assert_no_updates(self):
        self.upgrade.assert_not_called()
        self.settings.assert_not_called()

    def test_selectors_default_to_none_and_are_mutually_exclusive(self):
        self.assertIsNone(box.parser().parse_args(["upgrade"]).update_channel)
        for channel in ("stable", "devel"):
            with self.subTest(channel=channel):
                args = box.parser().parse_args(["upgrade", "--" + channel])
                self.assertEqual(args.update_channel, channel)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            box.main(["upgrade", "--stable", "--devel"])
        self.assert_no_updates()

    def test_upgrade_dispatches_preview_and_selected_channel_for_each_owner(self):
        for packaged in (False, True):
            for channel in (None, "stable", "devel"):
                for check in (False, True):
                    with self.subTest(packaged=packaged, channel=channel, check=check):
                        self.package.return_value = packaged
                        arguments = ["upgrade"]
                        if channel:
                            arguments.append("--" + channel)
                        if check:
                            arguments.append("--check")
                        self.assertEqual(box.main(arguments), 0)
                        self.upgrade.assert_called_once_with(
                            box.APP, check=check, channel=channel
                        )
                        self.upgrade.reset_mock()
        self.message.assert_not_called()
        self.settings.assert_not_called()

    def test_channel_selectors_are_rejected_outside_upgrade(self):
        for channel in ("stable", "devel"):
            for arguments in (
                [],
                ["start"],
                ["stop"],
                ["reset"],
                ["status"],
                ["--status"],
                ["--version"],
                ["--about"],
            ):
                with self.subTest(channel=channel, arguments=arguments):
                    with self.assertRaisesRegex(box.BoxError, "only to upgrade"):
                        box.main([*arguments, "--" + channel])
            with self.assertRaisesRegex(box.BoxError, "Credentials accepts only"):
                box.main(["credentials", "--" + channel])
            for command in (
                "completion",
                "doctor",
                "backup",
                "backup-info",
                "restore",
                "secrets",
                "web",
            ):
                with self.subTest(channel=channel, command=command):
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        box.main([command, "--" + channel])
        self.assert_no_updates()

    def test_automatic_update_settings_cannot_be_combined_with_selectors_or_check(self):
        for value in ("on", "off"):
            for extra in ("--stable", "--devel", "--check"):
                with self.subTest(value=value, extra=extra):
                    with self.assertRaisesRegex(box.BoxError, "cannot be combined"):
                        box.main(["upgrade", "--auto-update", value, extra])
        self.assert_no_updates()
        self.package.assert_not_called()

    def test_selectors_do_not_allow_unrelated_upgrade_options(self):
        for options in (
            ["--project", "/tmp/example-project"],
            ["--workspace", "example-id"],
            ["--new"],
            ["--status"],
            ["--no-vault"],
            ["--maintenance"],
        ):
            for channel in ("stable", "devel"):
                with self.subTest(options=options, channel=channel):
                    with self.assertRaises(box.BoxError):
                        box.main(["upgrade", "--" + channel, *options])
        self.assert_no_updates()
        self.package.assert_not_called()

    def test_github_automatic_update_settings_keep_existing_dispatch(self):
        for value in ("on", "off"):
            with self.subTest(value=value):
                self.assertEqual(box.main(["upgrade", "--auto-update", value]), 0)
                self.settings.assert_called_once_with(value == "on")
                self.settings.reset_mock()
        self.upgrade.assert_not_called()

    def test_package_automatic_update_settings_never_change_github_config(self):
        self.package.return_value = True
        with self.assertRaisesRegex(box.BoxError, "cannot manage a Python-package"):
            box.main(["upgrade", "--auto-update", "on"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(box.main(["upgrade", "--auto-update", "off"]), 0)
        self.assertEqual(output.getvalue(), "Package manager guidance.\n")
        self.assert_no_updates()

    def test_container_and_root_refuse_before_owner_detection_or_side_effects(self):
        for container, uid, message in (
            ("1", 1000, "on the host"),
            ("0", 0, "without sudo"),
        ):
            for arguments in (
                ["upgrade"],
                ["upgrade", "--devel"],
                ["upgrade", "--check", "--stable"],
                ["upgrade", "--auto-update", "on"],
                ["upgrade", "--auto-update", "off"],
            ):
                with self.subTest(container=container, uid=uid, arguments=arguments):
                    with (
                        patch.dict(os.environ, {"OC_BOX_CONTAINER": container}),
                        patch.object(os, "geteuid", return_value=uid),
                        self.assertRaisesRegex(box.BoxError, message),
                    ):
                        box.main(arguments)
        self.package.assert_not_called()
        self.assert_no_updates()

    def test_updater_refusals_including_editable_sources_are_not_bypassed(self):
        self.package.return_value = True
        for message in (
            "Editable source installation: update the checkout.",
            "Package owner is ambiguous; use your installation's manager.",
        ):
            with self.subTest(message=message):
                self.upgrade.side_effect = updater.UpdateError(message)
                with self.assertRaisesRegex(box.BoxError, message):
                    box.main(["upgrade"])
        self.settings.assert_not_called()
        self.message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
