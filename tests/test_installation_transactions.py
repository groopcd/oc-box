"""Activation failures use private synthetic installations, never the real HOME."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import installation, updater
from oc_box.version_info import get_version_info

APP = Path(__file__).resolve().parents[1]


class InstallationTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-install-transaction-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        home = patch.object(Path, "home", side_effect=lambda: self.home)
        home.start()
        self.addCleanup(home.stop)
        self.old = self.identity("a", 100)
        self.new = self.identity("b", 101)
        self.reset_home(self.home)

    def reset_home(self, home):
        self.home = home
        self.target = self.home / ".local/share/oc-box"
        self.first = self.install(self.old)
        self.first_files = {
            path.relative_to(self.first): path.read_bytes()
            for path in self.first.rglob("*")
            if path.is_file()
        }
        self.sentinel = (
            self.home / ".local/state/oc-box/workspaces/private/workspace.json"
        )
        self.sentinel.parent.mkdir(parents=True)
        self.sentinel.write_bytes(b"synthetic workspace state")
        self.paths = [
            self.home / ".local/share/man/man1/oc-box.1",
            self.home / ".local/share/info/oc-box.info",
            self.home / ".local/share/bash-completion/completions/oc-box",
            self.target / "current",
            *(
                self.target / name
                for name in (
                    *installation.PROGRAM_FILES,
                    "build-info.json",
                    "docs",
                    "completions",
                )
            ),
            self.home / ".local/bin/oc-box",
        ]
        self.before = self.snapshot()

    @staticmethod
    def identity(letter, count):
        return {
            "version": f"1.6.{count - 29}+g{letter * 7}",
            "commit": letter * 40,
            "commit_count": count,
            "dirty": False,
        }

    def install(self, build):
        with (
            patch.object(
                installation,
                "get_version_info",
                side_effect=lambda root: (
                    build if Path(root) == APP else get_version_info(root)
                ),
            ),
            patch.object(installation.shutil, "which", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return installation.install(APP, self.home)

    def snapshot(self):
        result = {}
        for path in self.paths:
            if path.is_symlink():
                result[path] = ("link", os.readlink(path))
            elif path.is_file():
                result[path] = ("file", path.read_bytes(), path.stat().st_mode)
            else:
                result[path] = ("absent",)
        return result

    def assert_old_coherent(self):
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual((self.target / "current").resolve(), self.first)
        self.assertEqual(updater.current_install(APP), self.old)
        self.assertEqual(get_version_info(self.first), self.old)
        self.assertEqual(
            {
                path.relative_to(self.first): path.read_bytes()
                for path in self.first.rglob("*")
                if path.is_file()
            },
            self.first_files,
        )
        self.assertEqual(self.sentinel.read_bytes(), b"synthetic workspace state")
        self.assertFalse((self.target / ".activation-pending.json").exists())
        self.assertEqual(list((self.target / "releases").iterdir()), [self.first])

    def test_every_late_publication_failure_restores_old_activation(self):
        replace = os.replace
        relatives = [path.relative_to(self.home) for path in self.paths]
        for number, relative in enumerate(relatives):
            for after in (False, True):
                self.reset_home(self.root / f"case-{number}-{after}")
                destination = self.home / relative
                failed = False

                def fault(source, target, destination=destination, after=after):
                    nonlocal failed
                    if Path(target) == destination and not failed:
                        failed = True
                        if after:
                            replace(source, target)
                        raise OSError("synthetic publication failure")
                    return replace(source, target)

                with self.subTest(path=destination.relative_to(self.home), after=after):
                    with (
                        patch.object(installation.os, "replace", side_effect=fault),
                        self.assertRaisesRegex(
                            OSError, "synthetic publication failure"
                        ),
                    ):
                        self.install(self.new)
                    self.assertTrue(failed)
                    self.assert_old_coherent()

    def test_failed_rollback_retains_explicit_recovery_and_never_claims_current(self):
        replace = os.replace
        failed = False

        def fault(source, target):
            nonlocal failed
            target = Path(target)
            if target == self.target / "current" and not failed:
                replace(source, target)
                failed = True
                raise OSError("synthetic publication failure")
            if target == self.target / "current" and failed:
                raise OSError("synthetic rollback failure")
            return replace(source, target)

        with (
            patch.object(installation.os, "replace", side_effect=fault),
            self.assertRaisesRegex(ValueError, "recovery"),
        ):
            self.install(self.new)
        marker = self.target / ".activation-pending.json"
        self.assertTrue(marker.is_file())
        self.assertTrue(list(self.target.glob(".activation-*/state.json")))
        self.assertTrue(self.first.is_dir())
        self.assertIn(
            str((self.target / "current").resolve() / "oc_box.py"),
            (self.home / ".local/bin/oc-box").read_text(),
        )
        with self.assertRaisesRegex(updater.UpdateError, "recovery"):
            updater.current_install(APP)
        with self.assertRaisesRegex(ValueError, "recovery"):
            self.install(self.new)
        self.assertEqual(self.sentinel.read_bytes(), b"synthetic workspace state")

    def test_metadata_cannot_advance_ahead_of_selected_pinned_launcher(self):
        replace = os.replace
        observed = []

        def observe(source, target):
            if (
                Path(target) == self.target / "current"
                and Path(source).name == ".current-next"
            ):
                candidate = Path(source).resolve(strict=True)
                self.assertEqual(
                    (self.home / ".local/bin/oc-box").read_text(),
                    "#!/bin/sh\nexec python3 "
                    + str(candidate / "oc_box.py")
                    + ' "$@"\n',
                )
                observed.append(candidate)
            return replace(source, target)

        with patch.object(installation.os, "replace", side_effect=observe):
            release = self.install(self.new)
        self.assertEqual(observed, [release])

    def test_killed_activation_is_blocked_until_explicit_recovery(self):
        script = """import os,sys
from pathlib import Path
from oc_box import installation
home=Path(sys.argv[1])
replace=os.replace
def interrupt(source,target):
    replace(source,target)
    if Path(target)==home/'.local/share/oc-box/current':
        os._exit(73)
installation.os.replace=interrupt
installation.install(Path(sys.argv[2]),home)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.home), str(APP)],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.home),
                "PYTHONPATH": str(APP / "src"),
                "PYTHONNOUSERSITE": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertTrue((self.target / ".activation-pending.json").is_file())
        self.assertIn(
            str((self.target / "current").resolve() / "oc_box.py"),
            (self.home / ".local/bin/oc-box").read_text(),
        )
        self.assertTrue(self.first.is_dir())
        with self.assertRaisesRegex(updater.UpdateError, "recovery"):
            updater.current_install(APP)
        with self.assertRaisesRegex(ValueError, "recovery"):
            self.install(self.new)
        self.assertEqual(self.sentinel.read_bytes(), b"synthetic workspace state")

    def test_manual_install_can_intentionally_select_an_older_source(self):
        older = self.identity("c", 99)
        release = self.install(older)
        self.assertEqual((self.target / "current").resolve(), release)
        self.assertEqual(updater.current_install(APP), older)
        self.assertTrue(self.first.is_dir())

    def test_update_guard_rechecks_after_download_without_changing_manual_policy(self):
        older = self.identity("c", 99)
        with patch.dict(
            os.environ, {"OC_BOX_UPGRADE_EXPECTED_COMMIT": older["commit"]}
        ):
            self.assertEqual(self.install(older), self.first)
        self.assert_old_coherent()

    def test_update_guard_refuses_changed_or_unknown_download_identity(self):
        cases = {
            "changed-commit": {**self.new, "commit": "c" * 40},
            "dirty-source": {
                **self.new,
                "version": self.new["version"] + ".dirty",
                "dirty": True,
            },
            "unknown-source": {
                "version": "1.6.0+unknown",
                "commit": None,
                "commit_count": None,
                "dirty": False,
            },
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                with (
                    patch.dict(
                        os.environ,
                        {"OC_BOX_UPGRADE_EXPECTED_COMMIT": self.new["commit"]},
                    ),
                    self.assertRaisesRegex(
                        ValueError, "identity could not be verified"
                    ),
                ):
                    self.install(build)
                self.assert_old_coherent()

    def test_commit_marker_failure_before_or_after_unlink_rolls_back(self):
        unlink = Path.unlink
        for after in (False, True):
            self.reset_home(self.root / f"marker-{after}")
            marker = self.target / ".activation-pending.json"
            failed = False

            def fault(path, *args, marker=marker, after=after, **kwargs):
                nonlocal failed
                if path == marker and not failed:
                    failed = True
                    if after:
                        unlink(path, *args, **kwargs)
                    raise OSError("synthetic commit-marker failure")
                return unlink(path, *args, **kwargs)

            with self.subTest(after=after):
                with (
                    patch.object(Path, "unlink", fault),
                    self.assertRaisesRegex(OSError, "commit-marker failure"),
                ):
                    self.install(self.new)
                self.assertTrue(failed)
                self.assert_old_coherent()

    def test_first_install_late_failure_leaves_no_active_launcher_or_metadata(self):
        self.home = self.root / "fresh"
        self.target = self.home / ".local/share/oc-box"
        replace = os.replace

        def fault(source, target):
            result = replace(source, target)
            if Path(target) == self.home / ".local/bin/oc-box":
                raise OSError("synthetic first-publication failure")
            return result

        with (
            patch.object(installation.os, "replace", side_effect=fault),
            self.assertRaisesRegex(OSError, "first-publication failure"),
        ):
            self.install(self.new)
        self.assertFalse((self.home / ".local/bin/oc-box").exists())
        self.assertFalse((self.target / "build-info.json").exists())
        self.assertFalse((self.target / "current").exists())
        self.assertFalse(list(self.target.glob(".activation-*")))
        self.assertEqual(list((self.target / "releases").iterdir()), [])
        self.assertIsNone(installation.active_release(self.home))

    def test_legacy_flat_files_and_manual_directory_survive_late_failure(self):
        self.home = self.root / "legacy"
        self.target = self.home / ".local/share/oc-box"
        docs = self.target / "docs"
        docs.mkdir(parents=True)
        (docs / "user-note").write_bytes(b"retained legacy manual")
        inode = docs.stat().st_ino
        (self.target / "oc_box.py").write_bytes(b"legacy source sentinel")
        (self.target / "build-info.json").write_text(json.dumps(self.old))
        binary = self.home / ".local/bin/oc-box"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"legacy launcher sentinel")
        binary.chmod(0o755)
        replace = os.replace
        failed = False

        def fault(source, target):
            nonlocal failed
            result = replace(source, target)
            if Path(target) == binary and not failed:
                failed = True
                raise OSError("synthetic legacy publication failure")
            return result

        with (
            patch.object(installation.os, "replace", side_effect=fault),
            self.assertRaisesRegex(OSError, "legacy publication failure"),
        ):
            self.install(self.new)
        self.assertTrue(failed)
        self.assertEqual(binary.read_bytes(), b"legacy launcher sentinel")
        self.assertEqual(
            (self.target / "oc_box.py").read_bytes(), b"legacy source sentinel"
        )
        self.assertEqual(get_version_info(self.target), self.old)
        self.assertEqual(docs.stat().st_ino, inode)
        self.assertEqual((docs / "user-note").read_bytes(), b"retained legacy manual")
        self.assertFalse((self.target / "legacy-docs").exists())
        self.assertFalse((self.target / "current").exists())
        self.assertFalse(list(self.target.glob(".activation-*")))

    def test_post_commit_recovery_cleanup_failure_stays_coherent_and_blocked(self):
        remove = installation.shutil.rmtree

        def fault(path, *args, **kwargs):
            path = Path(path)
            if path.parent == self.target and path.name.startswith(".activation-"):
                raise OSError("synthetic post-commit cleanup failure")
            return remove(path, *args, **kwargs)

        with (
            patch.object(installation.shutil, "rmtree", side_effect=fault),
            self.assertRaisesRegex(OSError, "post-commit cleanup failure"),
        ):
            self.install(self.new)
        current = (self.target / "current").resolve()
        self.assertEqual(get_version_info(self.target), self.new)
        self.assertIn(
            str(current / "oc_box.py"), (self.home / ".local/bin/oc-box").read_text()
        )
        self.assertTrue(current.is_dir())
        self.assertTrue(self.first.is_dir())
        self.assertEqual(get_version_info(self.first), self.old)
        self.assertTrue(list(self.target.glob(".activation-*/state.json")))
        with self.assertRaisesRegex(updater.UpdateError, "recovery"):
            updater.current_install(APP)
        self.assertEqual(self.sentinel.read_bytes(), b"synthetic workspace state")


if __name__ == "__main__":
    unittest.main()
