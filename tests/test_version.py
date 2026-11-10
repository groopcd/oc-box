import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import ModuleType

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

    def test_unverifiable_git_never_adopts_embedded_clean_metadata(self):
        embedded = {
            "version": "1.5.39+gfixture",
            "commit": "a" * 40,
            "commit_count": 68,
            "dirty": False,
        }
        (self.root / "build-info.json").write_text(json.dumps(embedded))
        marker = self.root / ".git"
        for kind in ("directory", "worktree-file", "broken-link"):
            if kind == "directory":
                marker.mkdir()
            elif kind == "worktree-file":
                marker.write_text("gitdir: missing-private-fixture\n")
            else:
                marker.symlink_to(self.root / "missing-git-directory")
            for name, response in (
                ("shallow", subprocess.CompletedProcess([], 0, "true\n", "")),
                ("missing-git", FileNotFoundError()),
                ("timeout", subprocess.TimeoutExpired([], 5)),
                ("failed-inspection", subprocess.CalledProcessError(1, [])),
            ):
                with self.subTest(marker=kind, failure=name):
                    with patch.object(
                        version.subprocess,
                        "run",
                        **(
                            {"side_effect": response}
                            if isinstance(response, Exception)
                            else {"return_value": response}
                        ),
                    ):
                        actual = version.get_version_info(self.root)
                        self.assertEqual(
                            actual["version"], version.VERSION_SERIES + ".0+unknown"
                        )
                        self.assertIsNone(actual["commit"])
                        self.assertIsNone(actual["commit_count"])
                        self.assertIn(
                            "Source commit: unavailable", version.about_text(self.root)
                        )
            if marker.is_dir() and not marker.is_symlink():
                marker.rmdir()
            else:
                marker.unlink()

    def test_embedded_metadata_requires_typed_identity_fields(self):
        valid = {
            "version": "1.0.7+gfixture",
            "commit": "a" * 40,
            "commit_count": 36,
            "dirty": False,
            "distribution_version": "1.0.7",
        }
        cases = {
            "version-only": {"version": "1.0.7"},
            "version-type": {**valid, "version": 7},
            "version-control": {**valid, "version": "1.0.7\nunsafe"},
            "commit-type": {**valid, "commit": 7},
            "commit-format": {**valid, "commit": "not-a-commit"},
            "count-string": {**valid, "commit_count": "36"},
            "count-bool": {**valid, "commit_count": True},
            "count-zero": {**valid, "commit_count": 0},
            "dirty-string": {**valid, "dirty": "false"},
            "dirty-missing": {
                key: value for key, value in valid.items() if key != "dirty"
            },
            "distribution-type": {**valid, "distribution_version": []},
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                (self.root / "build-info.json").write_text(json.dumps(value))
                actual = version.get_version_info(self.root)
                self.assertIsNone(actual["commit"])
                self.assertIsNone(actual["commit_count"])
                self.assertIn("+unknown", version.about_text(self.root))
        (self.root / "build-info.json").write_text(json.dumps(valid))
        self.assertEqual(version.get_version_info(self.root), valid)

    def test_build_rejects_shallow_git_even_with_embedded_identity(self):
        from oc_box import version_info as runtime_version

        (self.root / ".git").mkdir()
        (self.root / "build-info.json").write_text(
            json.dumps(
                {
                    "version": "1.6.1+gfixture",
                    "commit": "a" * 40,
                    "commit_count": 30,
                    "dirty": False,
                }
            )
        )
        setuptools = ModuleType("setuptools")
        setuptools.setup = lambda **kwargs: None
        build_py = ModuleType("setuptools.command.build_py")
        build_py.build_py = type("build_py", (), {})
        sdist = ModuleType("setuptools.command.sdist")
        sdist.sdist = type("sdist", (), {})
        with (
            patch.dict(
                sys.modules,
                {
                    "setuptools": setuptools,
                    "setuptools.command.build_py": build_py,
                    "setuptools.command.sdist": sdist,
                },
            ),
            patch.object(
                version.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "true\n", ""),
            ),
            patch.object(
                runtime_version,
                "get_version_info",
                side_effect=lambda root: version.get_version_info(self.root),
            ),
            self.assertRaisesRegex(RuntimeError, "requires full Git history"),
        ):
            runpy.run_path(str(APP / "src/_oc_box_build.py"), run_name="__main__")

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
