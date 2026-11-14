import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oc_box import install as installer, update_channels as channels


class ChannelInstallerTests(unittest.TestCase):
    def test_selectors_bind_source_and_persist_only_after_installation(self):
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": work}),
        ):
            build = {"version": "1.6.70+gabcdef0"}
            with (
                patch.object(installer, "get_version_info", return_value=build),
                patch.object(installer, "install") as install,
            ):
                for flags in (["--devel"], ["--stable", "--release-version", "1.6.71"]):
                    with (
                        self.subTest(flags=flags),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(installer.main(flags), 1)
                    install.assert_not_called()
                    self.assertFalse(channels.config_path().exists())
                install.side_effect = ValueError("synthetic staged failure")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(installer.main(["--stable"]), 1)
                self.assertFalse(channels.config_path().exists())
                install.side_effect = None
                self.assertEqual(
                    installer.main(["--stable", "--release-version", "1.6.70"]), 0
                )
                self.assertEqual(channels.selected_channel(), "stable")
                self.assertEqual(channels.config_path().stat().st_mode & 0o777, 0o600)

    def test_malformed_host_preferences_refuse_before_activation(self):
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": work}),
        ):
            target = Path(work) / "oc-box/config.json"
            target.parent.mkdir()
            target.write_text("{broken")
            with (
                patch.object(
                    installer,
                    "get_version_info",
                    return_value={"version": "1.6.70+gabcdef0"},
                ),
                patch.object(installer, "install") as install,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(installer.main(["--stable"]), 1)
            install.assert_not_called()
            self.assertEqual(target.read_text(), "{broken")

    def test_legacy_installer_call_does_not_change_channel_preference(self):
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": work}),
        ):
            channels.remember_channel("devel")
            before = channels.config_path().read_bytes()
            with patch.object(installer, "install") as install:
                self.assertEqual(installer.main([]), 0)
            install.assert_called_once()
            self.assertEqual(channels.config_path().read_bytes(), before)

    def test_bootstrap_downgrade_warns_that_state_is_not_rolled_back(self):
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(work) / "config")}),
        ):
            home = Path(work) / "home"
            previous = home / ".local/share/oc-box/build-info.json"
            previous.parent.mkdir(parents=True)
            previous.write_text('{"version":"1.7.80.dev0+gabcdef0"}')
            with (
                patch.object(Path, "home", return_value=home),
                patch.object(
                    installer,
                    "get_version_info",
                    return_value={"version": "1.6.70+gabcdef0"},
                ),
                patch.object(installer, "install") as install,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(installer.main(["--stable"]), 0)
            install.assert_called_once()
            self.assertIn("downgrade", output.getvalue())
            self.assertIn(
                "workspace/session state is not rolled back", output.getvalue()
            )


if __name__ == "__main__":
    unittest.main()
