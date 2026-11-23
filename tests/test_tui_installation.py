from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oc_box import installation, install

APP = Path(__file__).resolve().parents[1]


class OptionalTuiInstallationTests(unittest.TestCase):
    def test_tui_flag_is_explicit_and_plain_install_never_resolves_gui_packages(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(installation, "optional_tui_runtime") as runtime,
            patch.object(installation.shutil, "which", return_value=None),
        ):
            release = installation.install(APP, Path(temporary) / "home")
            runtime.assert_not_called()
            self.assertFalse((release / "tui-support.json").exists())
            self.assertTrue((release / "tui_app.py").is_file())
            self.assertTrue((release / "docs/tui.md").is_file())

    def test_opt_in_marks_the_immutable_release_and_failure_retains_the_current_launcher(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(installation.shutil, "which", return_value=None),
        ):
            home = Path(temporary) / "home"
            with patch.object(installation, "optional_tui_runtime") as runtime:
                previous = installation.install(APP, home, with_tui=True)
            runtime.assert_called_once_with(previous)
            self.assertTrue((previous / "tui-support.json").is_file())
            launcher = (home / ".local/bin/oc-box").read_bytes()
            with patch.object(
                installation,
                "optional_tui_runtime",
                side_effect=ValueError("synthetic dependency failure"),
            ):
                with self.assertRaises(ValueError):
                    installation.install(APP, home, with_tui=True)
            self.assertEqual((home / ".local/share/oc-box/current").resolve(), previous)
            self.assertEqual((home / ".local/bin/oc-box").read_bytes(), launcher)

    def test_compatibility_installer_accepts_both_optional_features(self):
        with patch.object(install, "install") as installer:
            self.assertEqual(install.main(["--with-vault", "--with-tui"]), 0)
        self.assertEqual(
            installer.call_args.kwargs, {"with_vault": True, "with_tui": True}
        )


if __name__ == "__main__":
    unittest.main()
