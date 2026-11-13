import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import installation

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "oc_box._version_test", APP / "src/oc_box/version_info.py"
)
assert spec is not None and spec.loader is not None
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-version-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        baseline = patch.object(version, "VERSION_BASE_COMMIT_COUNT", 1)
        baseline.start()
        self.addCleanup(baseline.stop)
        channel = patch.object(version, "RELEASE_CHANNEL", "stable")
        channel.start()
        self.addCleanup(channel.stop)
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.git_env,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    def test_each_commit_advances_version_without_modifying_a_version_file(self):
        self.git("init")
        source = self.root / "file.txt"
        source.write_text("first")
        self.git("add", "--", "file.txt")
        self.git("commit", "-m", "First")
        first = version.get_version_info(self.root)
        self.assertEqual(first["commit_count"], 1)
        self.assertEqual(first["release_channel"], "stable")
        self.assertTrue(first["version"].startswith(version.VERSION_SERIES + ".0+g"))
        self.assertEqual(version.public_version(first), version.VERSION_SERIES + ".0")
        source.write_text("second")
        self.assertTrue(
            version.get_version_info(self.root)["version"].endswith(".dirty")
        )
        self.git("add", "--", "file.txt")
        self.git("commit", "-m", "Second")
        second = version.get_version_info(self.root)
        self.assertEqual(second["commit_count"], 2)
        self.assertTrue(second["version"].startswith(version.VERSION_SERIES + ".1+g"))
        self.assertEqual(version.public_version(second), version.VERSION_SERIES + ".1")
        self.assertNotEqual(first["commit"], second["commit"])
        (self.root / "placeholder").touch()
        self.assertEqual(version.get_version_info(self.root), second)

    def test_incomplete_history_cannot_masquerade_as_a_stable_release(self):
        self.git("init")
        (self.root / "file.txt").write_text("fixture")
        self.git("add", "file.txt")
        self.git("commit", "-m", "First")
        with patch.object(version, "VERSION_BASE_COMMIT_COUNT", 2):
            self.assertIsNone(version.get_version_info(self.root)["commit"])
        shallow = self.root / "shallow"
        self.git("clone", "--depth=1", self.root.as_uri(), str(shallow))
        self.assertEqual(
            version.get_version_info(shallow)["version"],
            version.VERSION_SERIES + ".0+unknown",
        )
        self.assertIsNone(version.get_version_info(shallow)["commit_count"])

    def test_legacy_embedded_metadata_retains_its_original_identity(self):
        legacy = {
            "version": "0.1.27+g6165105",
            "commit": "616510567e9dc3f80945bcc0de87e27cd7361723",
            "commit_count": 27,
            "dirty": False,
        }
        (self.root / "build-info.json").write_text(json.dumps(legacy))
        self.assertEqual(version.get_version_info(self.root), legacy)
        self.assertEqual(version.public_version(legacy), "0.1.27")
        legacy["version"] = "1.0.7+gfixture"
        legacy["commit_count"] = 36
        (self.root / "build-info.json").write_text(json.dumps(legacy))
        self.assertEqual(version.get_version_info(self.root), legacy)
        self.assertEqual(version.public_version(legacy), "1.0.7")
        legacy["version"] = "1.5.39+gb9b9ec1"
        legacy["commit_count"] = 68
        (self.root / "build-info.json").write_text(json.dumps(legacy))
        self.assertEqual(version.get_version_info(self.root), legacy)
        self.assertEqual(version.public_version(legacy), "1.5.39")

    def test_minor_series_change_preserves_patch_count_and_source_provenance(self):
        self.git("init")
        (self.root / "source").write_text("fixture")
        self.git("add", "source")
        self.git("commit", "-m", "Fixture")
        with patch.object(version, "VERSION_SERIES", "1.5"):
            before = version.get_version_info(self.root)
        with patch.object(version, "VERSION_SERIES", "1.6"):
            after = version.get_version_info(self.root)
        self.assertEqual(before["version"].replace("1.5.", "1.6.", 1), after["version"])
        for name in ("commit", "commit_count", "dirty"):
            self.assertEqual(before[name], after[name])

    def test_devel_identity_uses_same_patch_count_and_source_commit(self):
        self.git("init")
        (self.root / "source").write_text("fixture")
        self.git("add", "source")
        self.git("commit", "-m", "Fixture")
        stable = version.get_version_info(self.root)
        with patch.object(version, "RELEASE_CHANNEL", "devel"):
            devel = version.get_version_info(self.root)
            self.assertEqual(
                version.public_version(devel), version.public_version(stable) + ".dev0"
            )
            (self.root / "source").write_text("modified")
            dirty = version.get_version_info(self.root)
            self.assertEqual(dirty["version"], devel["version"] + ".dirty")
        self.assertEqual(devel["release_channel"], "devel")
        self.assertEqual(devel["version"], stable["version"].replace("+g", ".dev0+g"))
        for field in ("commit", "commit_count", "dirty"):
            self.assertEqual(stable[field], devel[field])
        # A Git-less installed development release retains its source identity,
        # even when read by stable-series code.
        with patch.object(version, "source_root", return_value=self.root / "installed"):
            installed = self.root / "installed"
            installed.mkdir()
            (installed / "build-info.json").write_text(json.dumps(devel))
            self.assertEqual(version.get_version_info(installed), devel)

    def test_channel_is_not_an_environment_override_and_unknown_source_fails(self):
        with patch.dict(
            os.environ, {"RELEASE_CHANNEL": "devel", "OC_BOX_RELEASE_CHANNEL": "devel"}
        ):
            self.assertEqual(
                version.get_version_info(self.root)["release_channel"], "stable"
            )
        with patch.object(version, "RELEASE_CHANNEL", "candidate"):
            with self.assertRaisesRegex(ValueError, "Unknown source release channel"):
                version.get_version_info(self.root)
        with patch.object(version, "RELEASE_CHANNEL", "devel"):
            unknown = version.get_version_info(self.root)
            self.assertEqual(
                unknown["version"], version.VERSION_SERIES + ".0.dev0+unknown"
            )
            self.assertEqual(unknown["release_channel"], "devel")

    def test_installed_copy_keeps_source_version_without_git(self):
        home = self.root / "home"
        expected = version.get_version_info(APP)
        with (
            patch.object(Path, "home", return_value=home),
            patch.dict(sys.modules, {"version_info": version}),
            patch.object(installation, "get_version_info", version.get_version_info),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            from oc_box.install import main as install_main

            self.assertEqual(install_main([]), 0)
        installed = home / ".local/share/oc-box"
        self.assertEqual(version.get_version_info(installed), expected)
        self.assertEqual(
            (installed / "workspace_identity.py").read_bytes(),
            (APP / "src/oc_box/workspace_identity.py").read_bytes(),
        )
        self.assertEqual(
            (installed / "LICENSE").read_bytes(), (APP / "LICENSE").read_bytes()
        )
        launcher = home / ".local/bin/oc-box"
        result = subprocess.run(
            [str(launcher), "--version"], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "oc-box " + expected["version"])
        about = subprocess.run(
            [str(launcher), "--about"], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(about.returncode, 0, about.stderr)
        for text in (
            "Paulo Dias",
            "paulo.miguel.dias@gmail.com",
            "OpenAI",
            "GPL-3.0-or-later",
            expected["version"],
        ):
            self.assertIn(text, about.stdout)

    def test_about_and_version_need_no_podman(self):
        for flag in ("--version", "--about"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [sys.executable, "-B", str(APP / "src/oc_box/oc_box.py"), flag],
                    env={**os.environ, "PATH": "/not-installed"},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("oc-box " + version.VERSION_SERIES + ".", result.stdout)

    def test_missing_or_invalid_metadata_is_explicit_and_about_remains_readable(self):
        for content in (None, '{"partial":', '{"other":"value"}'):
            with self.subTest(content=content):
                if content is not None:
                    (self.root / "build-info.json").write_text(content)
                self.assertEqual(
                    version.get_version_info(self.root)["version"],
                    version.VERSION_SERIES + ".0+unknown",
                )
                self.assertIn(
                    version.VERSION_SERIES + ".0+unknown", version.about_text(self.root)
                )

    def test_license_is_complete_gpl3_with_project_later_version_notice(self):
        text = (APP / "LICENSE").read_text()
        self.assertIn("Version 3, 29 June 2007", text)
        self.assertIn("17. Interpretation of Sections 15 and 16.", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)
        notice = (APP / "NOTICE").read_text()
        self.assertIn("GPL-3.0-or-later", notice)
        self.assertIn("Copyright (C) 2026 Paulo Dias", notice)


if __name__ == "__main__":
    unittest.main()
