import contextlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import host_file_open_policy as policy, installation
from oc_box import install as installer


_BUILD = {
    "version": "1.7.99.dev0+gaaaaaaa",
    "commit": "a" * 40,
    "commit_count": 128,
    "dirty": False,
    "release_channel": "devel",
}


class HostFileOpenPolicyTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        # Set the same effective HOME and selectors in both the fake installer
        # and its client. No network, actual packages, or host files are needed.
        self.home = root / "synthetic home"
        self.home.mkdir()
        self.target = self.home / policy.INSTALL_PATH
        self.binary = self.home / policy.LAUNCHER
        self.default_registry = self.home / ".local/state/oc-box"
        self.receipt = self.target / policy.RECEIPT
        stack.enter_context(
            patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        )
        stack.enter_context(
            patch.object(installation, "get_version_info", return_value=_BUILD)
        )
        stack.enter_context(
            patch.object(installation.shutil, "which", return_value=None)
        )
        # Model a verified download's src layout and the actual staged runtime
        # copies; only the owned policy module needs its real contents.
        self.source = root / "download" / "source"
        for name in (
            *(f"src/oc_box/{name}" for name in installation.RUNTIME_FILES),
            *installation.RESOURCE_FILES,
            *(f"docs/{name}" for name in installation.MANUAL_FILES),
            installation.COMPLETION_FILE,
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# synthetic downloaded content\n")
        (self.source / "pyproject.toml").write_text("[build-system]\n")
        shutil.copyfile(
            policy.__file__, self.source / "src/oc_box/host_file_open_policy.py"
        )

    def install(self, *, home=None, verified=True, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            release = installation.install(
                self.source,
                self.home if home is None else home,
                verified_release=verified,
                **kwargs,
            )
        return release, output.getvalue()

    def enabled(self, release, registry=None):
        return policy.enabled(
            release,
            home=self.home,
            registry=self.default_registry if registry is None else registry,
        )

    def test_public_bootstrap_identity_only_enrolls_matching_clean_candidate(self):
        old = ["--devel", "--release-version", "1.7.99.dev0"]
        pinned = [*old, "--verified-release-commit", _BUILD["commit"]]
        for build, args, checkout, wanted in (
            (_BUILD, [], False, False),
            (_BUILD, ["--devel"], False, False),
            (_BUILD, ["--release-version", "1.7.99.dev0"], False, False),
            (_BUILD, old, True, False),
            (_BUILD, pinned, False, False),
            (_BUILD, pinned, True, True),
            (
                {**_BUILD, "dirty": True},
                pinned,
                True,
                False,
            ),
        ):
            with (
                self.subTest(args=args, dirty=build["dirty"]),
                patch.object(installer, "get_version_info", return_value=build),
                patch.object(
                    installer, "_verified_bootstrap_checkout", return_value=checkout
                ),
                patch.object(installer, "install") as install,
                patch("oc_box.update_channels.selected_channel"),
                patch("oc_box.update_channels.remember_channel"),
            ):
                self.assertEqual(installer.main(args), 0)
                self.assertIs(
                    install.call_args.kwargs.get("verified_release", False), wanted
                )

    def test_bootstrap_checkout_requires_exact_tag_and_verified_lineage(self):
        executable = "/usr/bin/git"
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        }

        def git(*args):
            return subprocess.run(
                [executable, *args],
                cwd=self.source,
                env=env,
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.strip()

        git("init", "--quiet", "--initial-branch=devel")
        git("add", ".")
        git("commit", "--quiet", "-m", "Synthetic installer source")
        commit = git("rev-parse", "HEAD")
        git("remote", "add", "origin", "https://github.com/groopcd/oc-box.git")
        with (
            patch.dict(os.environ, {"PATH": env["PATH"]}),
            patch.object(installer, "source_root", return_value=self.source),
        ):
            verify = installer._verified_bootstrap_checkout
            self.assertFalse(verify("1.7.99.dev0", commit))
            git("tag", "v1.7.99.dev0")
            self.assertFalse(verify("1.7.99.dev0", commit))
            git("update-ref", "refs/remotes/origin/devel", commit)
            self.assertTrue(verify("1.7.99.dev0", commit))
            (self.source / "untracked-synthetic.txt").touch()
            self.assertFalse(verify("1.7.99.dev0", commit))

    def test_verified_fresh_activates_and_explains_the_client_opt_out(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}):
            release, note = self.install()
        self.assertIn(
            "Ctrl-click host file opening is on for supported native TUIs", note
        )
        self.assertIn("--no-host-file-open", note)
        self.assertEqual(self.binary.read_bytes(), policy.launcher_bytes(release))
        self.assertEqual((self.target / "current").resolve(), release)
        self.assertEqual(
            (release / "host_file_open_policy.py").read_bytes(),
            Path(policy.__file__).read_bytes(),
        )
        info = self.receipt.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_nlink, 1)
        self.assertNotIn(str(self.home).encode(), self.receipt.read_bytes())
        before = set(self.home.rglob("*"))
        self.assertTrue(self.enabled(release))
        self.assertEqual(before, set(self.home.rglob("*")))
        self.assertFalse(self.default_registry.exists())
        self.assertFalse((self.home / ".config").exists())
        self.assertFalse((release / policy.RECEIPT).exists())
        # Later creation of the same selected registry/config is expected.
        self.default_registry.mkdir(parents=True)
        (self.home / ".config/oc-box").mkdir(parents=True)
        self.assertTrue(self.enabled(release))

    def test_source_install_and_its_verified_reinstall_do_not_enroll(self):
        first, note = self.install(verified=False)
        second, reinstall = self.install()
        for release in (first, second, self.source / "src/oc_box"):
            self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note + reinstall)
        self.assertFalse(self.receipt.exists())

    def test_existing_launcher_on_path_prevents_enrollment(self):
        with patch.object(
            installation.shutil,
            "which",
            side_effect=lambda name, **_: (
                "/existing/bin/oc-box" if name == "oc-box" else None
            ),
        ):
            release, note = self.install()
        self.assertNotIn("--no-host-file-open", note)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.enabled(release))

    def test_marked_install_survives_old_updater_without_rewriting_receipt_or_lock(
        self,
    ):
        first, _ = self.install()
        marker = self.receipt.stat()
        lock_path = self.target / policy.INSTALL_LOCK
        lock = lock_path.stat()
        self.assertEqual(lock_path.read_bytes(), b"")
        self.default_registry.mkdir(parents=True)
        second, note = self.install(verified=False)
        self.assertNotIn("--no-host-file-open", note)
        self.assertEqual(lock_path.read_bytes(), b"")
        self.assertEqual(lock_path.stat().st_ino, lock.st_ino)
        self.assertEqual(lock_path.stat().st_ctime_ns, lock.st_ctime_ns)
        self.assertEqual(self.receipt.stat().st_ino, marker.st_ino)
        self.assertEqual(self.receipt.stat().st_mtime_ns, marker.st_mtime_ns)
        self.assertFalse(self.enabled(first))
        self.assertTrue(self.enabled(second))

    def test_old_launcher_legacy_paths_and_partial_install_never_enroll(self):
        for sign, kind in (
            (".local/share/oc-box/oc_box.py", "file"),
            (".local/share/oc-box", "directory"),
            (".local/share/oc-box/.upgrade.lock", "file"),
            (".local/state/oc-box", "directory"),
            (".config/oc-box", "directory"),
            (".cache/oc-box", "directory"),
            (".local/share/uv/tools/oc-box", "directory"),
            (".oc-box", "directory"),
            (".local/share/man/man1/oc-box.1", "file"),
            (".local/bin/oc-box", "file"),
            (".local/bin/oc-box", "dangling"),
            (".config/oc-box", "dangling"),
            (".local/state", "dangling"),
        ):
            with (
                self.subTest(sign=sign, kind=kind),
                tempfile.TemporaryDirectory() as work,
            ):
                home = Path(work)
                path = home / sign
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "directory":
                    path.mkdir()
                elif kind == "file":
                    path.touch()
                else:
                    path.symlink_to(home / "missing")
                release, note = self.install(home=home)
                self.assertFalse(
                    policy.enabled(
                        release, home=home, registry=home / ".local/state/oc-box"
                    )
                )
                self.assertFalse((home / policy.INSTALL_PATH / policy.RECEIPT).exists())
                self.assertNotIn("--no-host-file-open", note)

    def test_selected_configuration_and_registry_are_both_checked(self):
        for index, key in enumerate(
            (
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
                "XDG_CACHE_HOME",
                "OC_BOX_STATE_HOME",
            )
        ):
            with self.subTest(key=key):
                home = self.home / f"home-{index}"
                home.mkdir()
                selector = self.home / f"selector-{index}"
                actual = selector if key == "OC_BOX_STATE_HOME" else selector / "oc-box"
                actual.mkdir(parents=True)
                with patch.dict(os.environ, {key: str(selector)}):
                    self.install(home=home)
                self.assertFalse((home / policy.INSTALL_PATH / policy.RECEIPT).exists())

    def test_custom_path_only_enables_under_the_same_inspected_selectors(self):
        config = self.home / "other-config"
        registry = self.home / "other-state"
        with patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(config),
                "OC_BOX_STATE_HOME": str(registry),
            },
        ):
            release, _ = self.install()
            self.assertTrue(self.enabled(release, registry))
            self.assertFalse(self.enabled(release))
            with patch.dict(
                os.environ, {"OC_BOX_STATE_HOME": str(registry / "elsewhere")}
            ):
                self.assertFalse(self.enabled(release, registry / "elsewhere"))
        self.assertFalse(self.enabled(release))

    def test_override_stripped_by_bootstrap_is_never_implicitly_authorized(self):
        release, _ = self.install()
        alternate = self.home / "alternate"
        alternate.mkdir()
        for key in ("XDG_STATE_HOME", "OC_BOX_STATE_HOME", "XDG_DATA_HOME"):
            with self.subTest(key=key), patch.dict(os.environ, {key: str(alternate)}):
                registry = {
                    "XDG_STATE_HOME": alternate / "oc-box",
                    "OC_BOX_STATE_HOME": alternate,
                    "XDG_DATA_HOME": self.default_registry,
                }[key]
                self.assertFalse(self.enabled(release, registry))
        self.default_registry.parent.mkdir()
        self.default_registry.symlink_to(alternate, target_is_directory=True)
        self.assertFalse(self.enabled(release, alternate))
        self.assertFalse(self.enabled(release))

    def test_non_active_source_wheel_and_release_symlink_are_refused(self):
        release, _ = self.install()
        for path in (
            self.source / "src/oc_box",
            release / "site-packages/oc_box",
            self.target,
        ):
            with self.subTest(path=path):
                self.assertFalse(self.enabled(path))
        wheel_metadata = release / "distribution.json"
        wheel_metadata.write_text("{}")
        self.assertFalse(self.enabled(release))
        wheel_metadata.unlink()
        actual = release.rename(self.home / "relocated")
        release.symlink_to(actual, target_is_directory=True)
        self.assertFalse(self.enabled(release))
        self.assertFalse(self.enabled(release.resolve()))

    def test_receipt_is_not_transferable_or_reusable_with_a_recreated_install_root(
        self,
    ):
        self.install()
        contents = self.receipt.read_bytes()
        other_home = self.home / "other-home"
        other_release, _ = self.install(home=other_home, verified=False)
        other_receipt = other_home / policy.INSTALL_PATH / policy.RECEIPT
        other_receipt.write_bytes(contents)
        other_receipt.chmod(0o600)
        self.assertFalse(
            policy.enabled(
                other_release,
                home=other_home,
                registry=other_home / ".local/state/oc-box",
            )
        )
        self.target.rename(self.target.with_name("prior"))
        second, _ = self.install()
        self.receipt.write_bytes(contents)
        self.receipt.chmod(0o600)
        self.assertFalse(self.enabled(second))

    def test_receipt_content_type_mode_and_link_count_are_strict(self):
        release, _ = self.install()
        original = self.receipt.read_bytes()
        witness = self.target / "witness"
        witness.write_bytes(original)
        witness.chmod(0o600)
        for invalid in (
            "empty",
            "large",
            "changed",
            "directory",
            "fifo",
            "mode",
            "symlink",
            "hardlink",
        ):
            with self.subTest(invalid=invalid):
                self.receipt.unlink()
                if invalid == "directory":
                    self.receipt.mkdir()
                elif invalid == "fifo":
                    os.mkfifo(self.receipt, 0o600)
                elif invalid == "symlink":
                    self.receipt.symlink_to(witness)
                elif invalid == "hardlink":
                    os.link(witness, self.receipt)
                else:
                    self.receipt.write_bytes(
                        {
                            "empty": b"",
                            "large": original * 256,
                            "changed": b"x" + original[1:],
                            "mode": original,
                        }[invalid]
                    )
                    self.receipt.chmod(0o644 if invalid == "mode" else 0o600)
                self.assertFalse(self.enabled(release))
                if invalid == "directory":
                    self.receipt.rmdir()
                else:
                    self.receipt.unlink()
                self.receipt.write_bytes(original)
                self.receipt.chmod(0o600)
        self.assertTrue(self.enabled(release))
        self.target.chmod(0o777)
        self.assertFalse(self.enabled(release))

    def test_inspection_errors_fail_off_without_interrupting_a_normal_install(self):
        open_file = os.open

        def unreadable(path, *args, **kwargs):
            if path == ".config":
                raise PermissionError("synthetic read refusal")
            return open_file(path, *args, **kwargs)

        with patch.object(policy.os, "open", side_effect=unreadable):
            release, note = self.install()
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)

    def test_state_appearing_during_slow_staging_is_rechecked_under_lock(self):
        def create_registry(_release):
            self.default_registry.mkdir(parents=True)

        with patch.object(
            installation, "optional_crypto_runtime", side_effect=create_registry
        ):
            release, note = self.install(with_vault=True)
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)
        self.assertFalse(self.receipt.exists())

    def test_updater_creating_root_after_preflight_still_disqualifies_install(self):
        mkdir = Path.mkdir

        def updater_first(path, *args, **kwargs):
            if path == self.target and not path.exists():
                mkdir(path)
                (path / ".upgrade.lock").touch()
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", updater_first):
            release, note = self.install()
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)
        self.assertFalse(self.receipt.exists())

    def test_legacy_launcher_appearing_immediately_at_activation_does_not_enroll(self):
        write = installation.atomic_file

        def legacy_arrives(path, *args, **kwargs):
            if path == self.binary and not kwargs.get("replace", True):
                write(path, b"legacy package installed concurrently\n", 0o755)
            return write(path, *args, **kwargs)

        with patch.object(installation, "atomic_file", side_effect=legacy_arrives):
            release, note = self.install()
        self.assertEqual(self.binary.read_bytes(), policy.launcher_bytes(release))
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)

    def test_interruption_before_launcher_activation_never_creates_receipt(self):
        write = installation.atomic_file

        def interrupted(path, *args, **kwargs):
            if path == self.binary:
                raise OSError("synthetic launcher write failure")
            return write(path, *args, **kwargs)

        with (
            patch.object(installation, "atomic_file", side_effect=interrupted),
            self.assertRaises(OSError),
        ):
            self.install()
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.receipt.exists())
        release, note = self.install()
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)

    def test_receipt_write_failure_does_not_claim_the_default_or_fail_install(self):
        link_file = os.link

        def receipt_link_failed(source, destination, *args, **kwargs):
            if destination == policy.RECEIPT:
                raise PermissionError("synthetic receipt write refusal")
            return link_file(source, destination, *args, **kwargs)

        with patch.object(policy.os, "link", side_effect=receipt_link_failed):
            release, note = self.install()
        self.assertEqual(self.binary.read_bytes(), policy.launcher_bytes(release))
        self.assertFalse(self.enabled(release))
        self.assertNotIn("--no-host-file-open", note)
        self.assertEqual(list(self.target.glob(policy.RECEIPT + "*")), [])


if __name__ == "__main__":
    unittest.main()
