"""Source layout/resource contracts, with no real workspace or credential state."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import installation, oc_box as box, paths, updater, version_info

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/oc_box"


class SourceLayoutTests(unittest.TestCase):
    def test_checkout_runtime_and_metadata_are_separate(self):
        self.assertEqual(box.PACKAGE, PACKAGE)
        self.assertEqual(box.APP, ROOT)
        self.assertEqual(paths.source_root(PACKAGE), ROOT)
        self.assertEqual(
            version_info.get_version_info(PACKAGE), version_info.get_version_info(ROOT)
        )
        self.assertEqual(
            version_info.about_text(PACKAGE), version_info.about_text(ROOT)
        )
        for name in installation.RUNTIME_FILES:
            self.assertEqual(installation.source_file(ROOT, name), PACKAGE / name)
            self.assertTrue((PACKAGE / name).is_file())
            if name != "install.py":
                self.assertFalse((ROOT / name).exists())
        for name in installation.RESOURCE_FILES:
            self.assertEqual(installation.source_file(ROOT, name), ROOT / name)

    def test_legacy_flat_layout_remains_supported(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-flat-layout-") as temporary:
            root = Path(temporary)
            (root / "oc_box.py").write_text("# synthetic runtime\n")
            self.assertEqual(paths.runtime_root(root), root)
            self.assertEqual(paths.source_root(root), root)
            self.assertEqual(
                installation.source_file(root, "oc_box.py"), root / "oc_box.py"
            )

    def test_source_directory_redirect_is_refused_before_installation(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-src-redirect-") as temporary:
            root = Path(temporary)
            source, foreign, home = root / "source", root / "foreign", root / "home"
            source.mkdir()
            foreign.mkdir()
            (source / "src").symlink_to(foreign, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                installation.install(source, home)
            self.assertFalse(home.exists())

    def test_installer_module_has_no_import_side_effects(self):
        spec = importlib.util.spec_from_file_location(
            "oc_box._installer_import_test", PACKAGE / "install.py"
        )
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.object(installation, "install") as install:
            spec.loader.exec_module(module)
        install.assert_not_called()

    def test_editable_source_install_disables_github_updater_only_for_its_checkout(
        self,
    ):
        from types import SimpleNamespace

        direct = json.dumps({"url": ROOT.as_uri(), "dir_info": {"editable": True}})
        with patch.object(
            updater.metadata,
            "distribution",
            return_value=SimpleNamespace(read_text=lambda name: direct),
        ):
            self.assertTrue(updater.package_managed(ROOT))
            self.assertFalse(updater.package_managed(ROOT / "unrelated"))
            self.assertIn(
                "editable source installation", updater.package_upgrade_message(ROOT)
            )
            with patch.object(updater, "git") as git:
                with self.assertRaisesRegex(
                    updater.UpdateError, "self-updater is disabled"
                ):
                    updater.upgrade(ROOT)
            git.assert_not_called()

    def test_root_compatibility_installer_creates_the_same_flat_release(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-src-installer-") as temporary:
            home = Path(temporary)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "PYTHONNOUSERSITE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "install.py")],
                env=env,
                cwd=home,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            root = home / ".local/share/oc-box"
            for name in installation.RUNTIME_FILES:
                self.assertEqual(
                    (root / name).read_bytes(), (PACKAGE / name).read_bytes()
                )
            self.assertFalse((root / "src").exists())
            binary = home / ".local/bin/oc-box"
            self.assertTrue(os.access(binary, os.X_OK))
            output = subprocess.run(
                [str(binary), "--version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertEqual(
                output.stdout.strip(),
                "oc-box " + version_info.get_version_info(ROOT)["version"],
            )
            output = subprocess.run(
                [str(binary), "distros"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertIn(
                "registry.opensuse.org/opensuse/tumbleweed:latest", output.stdout
            )
            # Exercise the separate flat-install import path with the older
            # standalone-Python limitation, not only the namespaced wheel.
            output = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import os,sys; from pathlib import Path; "
                    "sys.path.insert(0,sys.argv[1]); import oc_box as box; "
                    "import private_memory,backup_crypto; "
                    "assert Path(private_memory.__file__).parent==Path(sys.argv[1]).resolve(); "
                    "os.__dict__.pop('memfd_create',None); os.__dict__.pop('MFD_CLOEXEC',None); "
                    "box.check_private_output(); "
                    'exec("with box.captured_output(private=True) as stream:\\n'
                    " stream.write(b'synthetic-flat-capture'); stream.seek(0); "
                    "assert stream.read()==b'synthetic-flat-capture'\\n"
                    "with backup_crypto.memory_file(b'synthetic-flat-key') as stream:\\n"
                    " assert stream.read()==b'synthetic-flat-key'\"); "
                    "assert box.dependencies.probe('memfd')[0]",
                    str(root),
                ],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(output.returncode, 0, output.stderr)
