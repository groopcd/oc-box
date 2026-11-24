import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import zipfile

from oc_box import installation, oc_box as box, updater
from oc_box.version_info import VERSION_SERIES, get_version_info

APP = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-update-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Most fixtures deliberately simulate the GitHub installation channel,
        # even when the test runner itself is an editable development install.
        editable = patch.object(updater, "editable_source_root", return_value=None)
        editable.start()
        self.addCleanup(editable.stop)
        self.home = self.root / "home"
        self.home.mkdir()
        self.identity = patch.object(Path, "home", return_value=self.home)
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.env = patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(self.home / ".config"), "OC_BOX_CONTAINER": ""},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        regular_user = patch.object(updater.os, "geteuid", return_value=1000)
        regular_user.start()
        self.addCleanup(regular_user.stop)
        releases = patch.object(
            updater,
            "available_releases",
            return_value={
                "stable": {
                    "version": "1.7.99999",
                    "tag": "v1.7.99999",
                    "channel": "stable",
                },
                "devel": None,
            },
        )
        self.releases = releases.start()
        self.addCleanup(releases.stop)

    def offer_newer_stable(self):
        release = f"{int(VERSION_SERIES.split('.')[0]) + 1}.0.99999"
        self.releases.return_value["stable"] = {
            "version": release,
            "tag": "v" + release,
            "channel": "stable",
        }

    def test_up_to_date_and_check_only_never_install_or_fetch_history(self):
        commit = "a" * 40
        for check, current in ((False, commit), (True, "b" * 40)):
            with (
                self.subTest(check=check),
                patch.object(updater.os, "geteuid", return_value=1000),
                patch.object(
                    updater,
                    "current_install",
                    return_value={
                        "version": "1.7.99999",
                        "commit": current,
                        "dirty": False,
                    },
                ),
                patch.object(
                    updater, "git", return_value=commit + "\trefs/tags/v1.7.99999"
                ) as git,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertIsNone(updater.upgrade(APP, check=check))
                self.assertEqual(git.call_count, 1)
                self.assertIn(
                    "up to date" if not check else "check only", out.getvalue()
                )

    def test_public_git_fetch_does_not_inherit_credentials_or_git_overrides(self):
        hostile = {
            "PERSONAL_GITHUB_TOKEN": "synthetic",
            "GITHUB_TOKEN": "copilot",
            "GH_TOKEN": "other",
            "CUSTOM_SECRET": "custom",
            "SSH_AUTH_SOCK": "/secret",
            "HTTP_PROXY": "http://secret.invalid",
            "GIT_CONFIG_COUNT": "1",
            "GIT_SSH_COMMAND": "bad command",
            "LD_PRELOAD": "/bad",
            "PYTHONPATH": "/bad",
        }
        with (
            patch.dict(os.environ, hostile),
            patch.object(
                updater.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "response", ""),
            ) as execute,
        ):
            self.assertEqual(
                updater.git(["ls-remote", updater.UPSTREAM], self.root), "response"
            )
        env = execute.call_args.kwargs["env"]
        for key in hostile:
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], str(self.root))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertIn("credential.helper=", execute.call_args.args[0])
        self.assertIn("http.followRedirects=false", execute.call_args.args[0])
        self.assertIn("protocol.https.allow=always", execute.call_args.args[0])
        self.assertNotIn("--depth", execute.call_args.args[0])

    def test_network_failure_does_not_change_existing_installation(self):
        # Exercise the fetch path even when the checkout has advanced past
        # the historical 1.7 sample releases used by the other tests.
        self.offer_newer_stable()
        with contextlib.redirect_stdout(io.StringIO()):
            release = installation.install(APP, self.home)
        wrapper = (self.home / ".local/bin/oc-box").read_bytes()
        with (
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(updater, "git", side_effect=updater.UpdateError("offline")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(updater.UpdateError, "offline"):
                updater.upgrade(APP)
        self.assertEqual((self.home / ".local/bin/oc-box").read_bytes(), wrapper)
        self.assertTrue(release.is_dir())

    def test_invalid_remote_head_and_root_are_rejected(self):
        self.offer_newer_stable()
        with (
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(updater, "git", return_value="bad response"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(updater.UpdateError, "Unexpected GitHub"):
                updater.upgrade(APP)
        with (
            patch.object(updater.os, "geteuid", return_value=0),
            patch.object(updater, "git") as network,
        ):
            with self.assertRaisesRegex(updater.UpdateError, "without sudo"):
                updater.upgrade(APP)
        network.assert_not_called()

    def test_downloaded_identity_mismatch_never_runs_the_installer(self):
        answers = ["a" * 40 + "\trefs/tags/v1.7.99999", "", "", "b" * 40, "2", ""]
        with (
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(
                updater,
                "current_install",
                return_value={"version": "0.1.1", "commit": "c" * 40},
            ),
            patch.object(updater, "git", side_effect=answers),
            patch.object(updater.subprocess, "run") as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "identity could not be verified"
            ):
                updater.upgrade(APP)
        install.assert_not_called()

    def test_self_upgrade_preserves_opt_in_vault_dependency_installation(self):
        current = self.home / ".local/share/oc-box/current"
        current.mkdir(parents=True)
        (current / "vault-support.json").write_text('{"optional_crypto":true}')
        commit = "a" * 40
        answers = [commit + "\trefs/tags/v1.7.99999", "", "", commit, "2", "", ""]
        with (
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(
                updater,
                "current_install",
                return_value={
                    "version": "0.1.1",
                    "commit": "b" * 40,
                    "commit_count": 1,
                },
            ),
            patch.object(updater, "git", side_effect=answers),
            patch.object(
                updater,
                "get_version_info",
                return_value={"commit": commit, "dirty": False, "version": "1.7.99999"},
            ),
            patch.object(updater, "verify_release_source"),
            patch.object(updater, "installer_options", return_value={"--with-vault"}),
            patch.object(
                updater.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as installer,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(updater.upgrade(APP), current.resolve())
        self.assertIn("--with-vault", installer.call_args.args[0])
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", installer.call_args.kwargs["env"])

    def test_installed_build_ahead_of_published_history_is_not_downgraded(self):
        answers = ["a" * 40 + "\trefs/tags/v1.7.99999", "", "", "a" * 40, "2", ""]
        with (
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(
                updater,
                "current_install",
                return_value={
                    "version": "0.1.3",
                    "commit": "c" * 40,
                    "commit_count": 3,
                },
            ),
            patch.object(updater, "git", side_effect=answers),
            patch.object(updater.subprocess, "run") as install,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertIsNone(updater.upgrade(APP))
        install.assert_not_called()
        self.assertIn("ahead of the published release", output.getvalue())

    def test_git_timeout_or_missing_tool_is_a_clear_bounded_failure(self):
        for error in (subprocess.TimeoutExpired([], 10), FileNotFoundError()):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(updater.subprocess, "run", side_effect=error),
            ):
                with self.assertRaises(updater.UpdateError):
                    updater.git(["ls-remote", updater.UPSTREAM], self.root)

    def test_auto_update_settings_preserve_pass_env_and_are_private(self):
        target = self.home / ".config/oc-box/config.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"pass_env":["MY_SETTING"]}')
        with contextlib.redirect_stdout(io.StringIO()):
            updater.update_settings(True)
        self.assertEqual(
            json.loads(target.read_text()),
            {"pass_env": ["MY_SETTING"], "auto_update": True},
        )
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        target.write_text("{broken")
        with self.assertRaises(updater.UpdateError):
            updater.update_settings(False)
        self.assertEqual(target.read_text(), "{broken")

    def test_auto_update_is_opt_in_skips_checkout_and_recursion(self):
        target = self.home / ".config/oc-box/config.json"
        with (
            patch.object(updater, "_github_upgrade") as upgrade,
            patch.object(updater.os, "geteuid", return_value=1000),
        ):
            self.assertIsNone(updater.automatic_upgrade(self.root))
            target.parent.mkdir(parents=True)
            target.write_text('{"auto_update":false}')
            self.assertIsNone(updater.automatic_upgrade(self.root))
            target.write_text('{"auto_update":true}')
            self.assertIsNone(updater.automatic_upgrade(APP))
            with patch.dict(os.environ, {"OC_BOX_UPDATE_CHECKED": "1"}):
                self.assertIsNone(updater.automatic_upgrade(self.root))
            upgrade.assert_not_called()
            self.assertIsNone(updater.automatic_upgrade(self.root))
            self.assertFalse((self.home / ".local").exists())

    def test_auto_check_lock_does_not_block_another_launcher(self):
        import fcntl

        target = self.home / ".local/share/oc-box"
        settings = self.home / ".config/oc-box/config.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"auto_update":true}')
        with (
            patch.object(updater.os, "geteuid", return_value=os.getuid()),
            patch.object(updater, "_regular_user", return_value=True),
            patch.object(installation.shutil, "which", return_value=None),
            patch.object(updater, "_github_upgrade") as upgrade,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            release = installation.install(APP, self.home)
            with (target / ".upgrade.lock").open("r+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(updater.automatic_upgrade(release))
        upgrade.assert_not_called()

    def test_automatic_upgrade_restarts_new_release_with_original_args(self):
        class Restarted(Exception):
            pass

        arguments = ["--project", str(self.root / "not-yet-opened"), "--new"]
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": ""}),
            patch.object(
                updater, "automatic_upgrade", side_effect=Restarted
            ) as restart,
            patch.object(box, "check_backend") as backend,
        ):
            with self.assertRaises(Restarted):
                box.main(arguments)
        restart.assert_called_once_with(box.APP, launch_args=arguments)
        backend.assert_not_called()

    def test_automatic_update_failure_does_not_block_existing_workspace_launch(self):
        project = self.root / "project"
        project.mkdir()
        with patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}):
            path, data = box.create_workspace(project, "tumbleweed")
            with (
                patch.dict(os.environ, {"OC_BOX_CONTAINER": ""}),
                # The updater's class-wide UID simulation must not impersonate
                # a different owner for these real workspace preference files.
                patch.object(box.os, "geteuid", return_value=path.stat().st_uid),
                patch.object(
                    updater,
                    "automatic_upgrade",
                    side_effect=updater.UpdateError("network offline"),
                ),
                patch.object(box, "check_backend"),
                patch.object(box, "detect_opencode_release", return_value="1.19.5"),
                patch.object(box, "launch", return_value=0) as launch,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(box.main(["--project", str(project)]), 0)
            self.assertIn("continuing with the installed version", errors.getvalue())
            self.assertEqual(launch.call_args.args[0], path)
            self.assertEqual(launch.call_args.args[1]["id"], data["id"])

    def test_upgrade_cli_rejects_workspace_actions_and_runs_without_podman(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": ""}),
            patch.object(updater, "upgrade") as upgrade,
            patch.object(box, "check_backend") as backend,
        ):
            self.assertEqual(box.main(["upgrade", "--check"]), 0)
        upgrade.assert_called_once_with(
            box.APP, check=True, channel=None, with_vault=False, with_tui=False
        )
        backend.assert_not_called()
        for args in (
            ["--check"],
            ["list", "--auto-update=on"],
            ["upgrade", "--project=/tmp/project"],
            ["upgrade", "--new"],
            ["upgrade", "--yes"],
            ["upgrade", "--check", "--auto-update=on"],
        ):
            with self.subTest(args=args), self.assertRaises(box.BoxError):
                box.main(args)
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            with self.assertRaisesRegex(box.BoxError, "on the host"):
                box.main(["upgrade"])

    def test_upgrade_cli_requests_vault_tui_or_both_without_workspace_actions(self):
        for flags, vault, tui in (
            (["--vault"], True, False),
            (["--tui"], False, True),
            (["--vault", "--tui"], True, True),
        ):
            for selector in ([], ["--devel"], ["--stable"]):
                with (
                    self.subTest(flags=flags, selector=selector),
                    patch.object(updater, "upgrade") as upgrade,
                    patch.object(box, "check_backend") as backend,
                ):
                    self.assertEqual(
                        box.main(["upgrade", *selector, *flags, "--check"]), 0
                    )
                    upgrade.assert_called_once_with(
                        box.APP,
                        check=True,
                        channel=selector[0][2:] if selector else None,
                        with_vault=vault,
                        with_tui=tui,
                    )
                    backend.assert_not_called()
        for flag in ("--vault", "--tui"):
            for arguments in (
                [flag],
                ["list", flag],
                ["credentials", flag],
                ["--version", flag],
                ["upgrade", flag, "--auto-update", "off"],
                ["upgrade", flag, "--auto-update", "on"],
            ):
                with (
                    self.subTest(arguments=arguments),
                    patch.object(updater, "upgrade") as upgrade,
                ):
                    with self.assertRaises(box.BoxError):
                        box.main(arguments)
                    upgrade.assert_not_called()

    def test_upgrade_delegates_requested_features_only_to_the_package_owner(self):
        from oc_box import package_updates

        with (
            patch.object(updater, "package_managed", return_value=True),
            patch.object(package_updates, "upgrade") as package,
            patch.object(updater, "git") as network,
        ):
            updater.upgrade(APP, channel="devel", with_vault=True, with_tui=True)
        package.assert_called_once_with(
            APP, check=False, channel="devel", with_vault=True, with_tui=True
        )
        network.assert_not_called()

    def test_package_managed_install_cannot_create_competing_github_launcher(self):
        from oc_box import package_updates

        source = self.root / "package"
        source.mkdir()
        (source / "distribution.json").write_text('{"installer":"python-package"}')
        with (
            patch.object(updater, "git") as network,
            patch.object(updater, "update_settings") as settings,
            patch.object(package_updates, "_inventory") as package_network,
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "No package update was attempted"
            ):
                updater.upgrade(source)
            self.assertIsNone(updater.automatic_upgrade(source))
            with (
                patch.object(box, "APP", source),
            ):
                with self.assertRaisesRegex(
                    box.BoxError, "No package update was attempted"
                ):
                    box.main(["upgrade"])
                with self.assertRaisesRegex(box.BoxError, "cannot manage"):
                    box.main(["upgrade", "--auto-update=on"])
            network.assert_not_called()
            settings.assert_not_called()
            package_network.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Python package installer"):
            installation.install(source, self.home)
        self.assertFalse((self.home / ".local/bin/oc-box").exists())

    def test_package_upgrade_message_selects_pipx_uv_or_current_interpreter(self):
        for marker, expected in (
            ("pipx_metadata.json", "pipx upgrade oc-box"),
            ("uv-receipt.toml", "uv tool upgrade oc-box"),
            (None, "-m pip install --upgrade oc-box"),
        ):
            with self.subTest(marker=marker):
                if marker:
                    (self.root / marker).touch()
                with patch.object(updater.sys, "prefix", str(self.root)):
                    self.assertIn(expected, updater.package_upgrade_message())
                if marker:
                    (self.root / marker).unlink()

    def test_versioned_install_keeps_old_release_and_preserves_workspace_data(self):
        registry = self.home / ".local/state/oc-box/workspaces/keep"
        registry.mkdir(parents=True)
        (registry / "workspace.json").write_text("private workspace sentinel")
        with contextlib.redirect_stdout(io.StringIO()):
            first = installation.install(APP, self.home)
        wrapper = (self.home / ".local/bin/oc-box").read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            second = installation.install(APP, self.home)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertIn(str(first / "oc_box.py"), wrapper)
        self.assertIn(
            str(second / "oc_box.py"), (self.home / ".local/bin/oc-box").read_text()
        )
        self.assertEqual((self.home / ".local/share/oc-box/current").resolve(), second)
        self.assertEqual(
            (registry / "workspace.json").read_text(), "private workspace sentinel"
        )

    def test_incomplete_download_never_switches_existing_launcher(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installation.install(APP, self.home)
        binary = self.home / ".local/bin/oc-box"
        before = binary.read_bytes()
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        (incomplete / "oc_box.py").write_text("incomplete source")
        with self.assertRaisesRegex(ValueError, "Missing or nonregular"):
            installation.install(incomplete, self.home)
        self.assertEqual(binary.read_bytes(), before)

    def test_staging_failure_never_changes_existing_launcher_or_active_release(self):
        with contextlib.redirect_stdout(io.StringIO()):
            original = installation.install(APP, self.home)
        binary = self.home / ".local/bin/oc-box"
        before = binary.read_bytes()
        with patch.object(
            installation.shutil, "copyfile", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                installation.install(APP, self.home)
        self.assertEqual(binary.read_bytes(), before)
        self.assertEqual(
            (self.home / ".local/share/oc-box/current").resolve(), original
        )
        self.assertEqual(list(original.parent.iterdir()), [original])

    def test_legacy_flat_install_is_migrated_without_deleting_unknown_files(self):
        target = self.home / ".local/share/oc-box"
        (target / "docs").mkdir(parents=True)
        (target / "docs/legacy").write_text("old manual")
        (target / "oc_box.py").write_text("old launcher")
        (target / "custom-note").write_text("user note")
        with contextlib.redirect_stdout(io.StringIO()):
            installed = installation.install(APP, self.home)
        self.assertEqual((target / "oc_box.py").resolve(), installed / "oc_box.py")
        self.assertEqual((target / "legacy-docs/legacy").read_text(), "old manual")
        self.assertEqual((target / "custom-note").read_text(), "user note")

    def test_real_git_upgrade_from_private_fixture_does_not_touch_source_checkout(self):
        self.exercise_real_git_upgrade(src_layout=False, legacy_updater=False)

    def test_released_updater_installs_new_src_layout_without_changing_its_protocol(
        self,
    ):
        self.exercise_real_git_upgrade(src_layout=True, legacy_updater=True)

    def test_v1_6_60_updater_installs_channel_bridge_without_new_flags(self):
        self.exercise_real_git_upgrade(src_layout=True, legacy_updater="v1.6.60")

    def test_real_published_channel_round_trip_preserves_old_releases_and_history(self):
        self.exercise_real_git_upgrade(
            src_layout=True, legacy_updater=False, roundtrip=True
        )

    def test_real_same_release_upgrade_adds_vault_in_a_new_immutable_release(self):
        self.exercise_real_git_upgrade(
            src_layout=True, legacy_updater=False, add_vault=True
        )

    def exercise_real_git_upgrade(
        self, *, src_layout, legacy_updater, roundtrip=False, add_vault=False
    ):
        active = updater
        if legacy_updater:
            retained = (
                "v1.6.60:src/oc_box/updater.py"
                if legacy_updater == "v1.6.60"
                else "v1.5.39:updater.py"
            )
            archived = subprocess.run(
                ["git", "-C", str(APP), "show", retained],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if archived.returncode:
                self.skipTest(
                    "Released updater comparison requires retained release Git tags."
                )
            original = archived.stdout
            active = ModuleType("oc_box._released_updater_fixture")
            active.__package__ = "oc_box"
            exec(compile(original, "released-updater-fixture", "exec"), active.__dict__)
        remote = self.root / "fixture-source"
        remote.mkdir()
        for name in installation.PROGRAM_FILES:
            target = (
                remote
                / (
                    "src/oc_box"
                    if src_layout and name in installation.RUNTIME_FILES
                    else ""
                )
                / name
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(installation.source_file(APP, name), target)
        if src_layout:
            shutil.copyfile(APP / "install.py", remote / "install.py")
            shutil.copyfile(APP / "pyproject.toml", remote / "pyproject.toml")
        version_file = installation.source_file(remote, "version_info.py")
        version_file.write_text(
            version_file.read_text()
            .replace("VERSION_BASE_COMMIT_COUNT = 29", "VERSION_BASE_COMMIT_COUNT = 1")
            .replace('RELEASE_CHANNEL = "devel"', 'RELEASE_CHANNEL = "stable"')
        )
        if add_vault:
            wheels = self.root / "offline-wheels"
            wheels.mkdir()
            dist = "cryptography-44.0.0.dist-info"
            files = {
                "cryptography/__init__.py": "SYNTHETIC_OPTIONAL_DEPENDENCY = True\n",
                dist
                + "/METADATA": "Metadata-Version: 2.3\nName: cryptography\nVersion: 44.0.0\n",
                dist
                + "/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }
            files[dist + "/RECORD"] = "".join(name + ",,\n" for name in files)
            with zipfile.ZipFile(
                wheels / "cryptography-44.0.0-py3-none-any.whl", "w"
            ) as archive:
                for name, value in files.items():
                    archive.writestr(name, value)
            # Change only this fixture source's dependency transport. The real
            # downloaded installer, venv/pip staging and activation still run.
            installer = installation.source_file(remote, "installation.py")
            source = installer.read_text()
            self.assertIn('"https://pypi.org/simple"', source)
            installer.write_text(
                source.replace(
                    '"--index-url",\n                "https://pypi.org/simple",',
                    '"--no-index",\n                "--find-links",\n                '
                    + repr(str(wheels))
                    + ",",
                )
            )
        shutil.copytree(APP / "docs", remote / "docs")
        shutil.copytree(APP / "completions", remote / "completions")
        env = {
            **updater.public_git_env(self.root),
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }

        def local(*args):
            return subprocess.run(
                ["git", "-C", str(remote), *args],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()

        local("init", "-b", "master")
        local("add", ".")
        local("commit", "-m", "First")
        first_commit = local("rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()):
            first = installation.install(remote, self.home)
        # Simulate the installed metadata of a pre-1.0 launcher. The full count
        # remains comparable even though the public patch baseline changes.
        metadata = first / "build-info.json"
        legacy = {
            "version": "0.1.1+g" + first_commit[:7],
            "commit": first_commit,
            "commit_count": 1,
            "dirty": False,
        }
        metadata.write_text(json.dumps(legacy))
        (remote / "README.md").write_text(
            (remote / "README.md").read_text() + "\nFixture update.\n"
        )
        local("add", "README.md")
        local("commit", "-m", "Second")
        expected = local("rev-parse", "HEAD")
        local("tag", "v" + VERSION_SERIES + ".1")
        self.releases.return_value = {
            "stable": {
                "version": VERSION_SERIES + ".1",
                "tag": "v" + VERSION_SERIES + ".1",
                "channel": "stable",
            },
            "devel": None,
        }
        calls = []

        def transport(arguments, temporary, **kwargs):
            calls.append(arguments)
            arguments = [
                str(remote) if item == active.UPSTREAM else item for item in arguments
            ]
            return subprocess.run(
                ["git", *arguments],
                cwd=temporary,
                env=active.public_git_env(temporary),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout.strip()

        with (
            patch.object(active.os, "geteuid", return_value=1000),
            patch.object(
                active, "editable_source_root", return_value=None, create=True
            ),
            patch.object(active, "git", side_effect=transport),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            second = active.upgrade(APP)
        self.assertIn("Installed oc-box: 0.1.1+g", output.getvalue())
        self.assertIn("Installed oc-box: " + VERSION_SERIES + ".1+g", output.getvalue())
        self.assertIsNotNone(second)
        target = self.home / ".local/share/oc-box"
        build = get_version_info(target)
        self.assertEqual(build["commit"], expected)
        self.assertEqual(build["commit_count"], 2)
        self.assertTrue(build["version"].startswith(VERSION_SERIES + ".1+g"))
        self.assertEqual(get_version_info(first)["commit"], first_commit)
        self.assertEqual(local("status", "--porcelain"), "")
        self.assertEqual(local("rev-parse", "HEAD"), expected)
        self.assertFalse(any("--depth" in command for command in calls))
        output = subprocess.run(
            [str(self.home / ".local/bin/oc-box"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(output.stdout.strip(), "oc-box " + build["version"])
        if add_vault:
            wrapper = (self.home / ".local/bin/oc-box").read_bytes()
            with (
                patch.object(active, "git", side_effect=transport),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertIsNone(active.upgrade(APP, check=True, with_vault=True))
                self.assertEqual(
                    (self.home / ".local/bin/oc-box").read_bytes(), wrapper
                )
                self.assertEqual((target / "current").resolve(), second)
                added = active.upgrade(APP, with_vault=True)
                self.assertNotEqual(added, second)
                self.assertEqual(get_version_info(added)["commit"], expected)
                self.assertTrue((added / "vault-support.json").is_file())
                self.assertIn(
                    "SYNTHETIC_OPTIONAL_DEPENDENCY",
                    (added / "vault-deps/cryptography/__init__.py").read_text(),
                )
                self.assertFalse((second / "vault-support.json").exists())
                self.assertTrue(second.is_dir())
                self.assertTrue(first.is_dir())
                enabled_wrapper = (self.home / ".local/bin/oc-box").read_bytes()
                self.assertIsNone(active.upgrade(APP, with_vault=True))
                self.assertEqual(
                    (self.home / ".local/bin/oc-box").read_bytes(), enabled_wrapper
                )
                self.assertEqual((target / "current").resolve(), added)
            self.assertEqual(local("status", "--porcelain"), "")
        if roundtrip:
            retained = self.home / ".local/state/oc-box/workspaces/synthetic/history"
            retained.parent.mkdir(parents=True)
            retained.write_text("unchanged private history")
            local("checkout", "-b", "devel")
            version_file.write_text(
                version_file.read_text()
                .replace(
                    f'VERSION_SERIES = "{VERSION_SERIES}"', 'VERSION_SERIES = "1.7"'
                )
                .replace('RELEASE_CHANNEL = "stable"', 'RELEASE_CHANNEL = "devel"')
            )
            local("add", str(version_file))
            local("commit", "-m", "Development channel fixture")
            development_commit = local("rev-parse", "HEAD")
            local("tag", "v1.7.2.dev0")
            self.releases.return_value["devel"] = {
                "version": "1.7.2.dev0",
                "tag": "v1.7.2.dev0",
                "channel": "devel",
            }
            with (
                patch.object(active, "git", side_effect=transport),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                development = active.upgrade(APP, channel="devel")
                self.assertEqual(get_version_info(target)["commit"], development_commit)
                self.assertTrue(
                    get_version_info(target)["version"].startswith("1.7.2.dev0+g")
                )
                self.assertIsNone(active.upgrade(APP))
                stable = active.upgrade(APP, channel="stable")
            self.assertEqual(get_version_info(target)["commit"], expected)
            self.assertTrue(development.is_dir())
            self.assertTrue(second.is_dir())
            self.assertNotEqual(second, stable)
            self.assertEqual(retained.read_text(), "unchanged private history")
            self.assertEqual(
                json.loads((self.home / ".config/oc-box/config.json").read_text())[
                    "update_channel"
                ],
                "stable",
            )
            local("checkout", "master")

    def test_bootstrap_completes_against_an_offline_transport_with_no_credentials(self):
        # The real shell/bootstrap/installer run; only id and the public Git
        # transport are fixtures so this works as root in the test container.
        binary = self.root / "bin"
        binary.mkdir()
        record = self.root / "bootstrap-git.json"
        fake_id = binary / "id"
        fake_id.write_text(
            '#!/bin/sh\nif [ "$1" = -u ]; then printf "1000\\n"; else /usr/bin/id "$@"; fi\n'
        )
        fake_id.chmod(0o755)
        fake_git = binary / "git"
        # A clone contains project source, not arbitrary local evidence or
        # private/unreadable untracked files in the developer's checkout.
        source_files = {
            str(installation.source_file(APP, name).relative_to(APP))
            for name in installation.PROGRAM_FILES
        } | {
            *("docs/" + name for name in installation.MANUAL_FILES),
            installation.COMPLETION_FILE,
            "install.py",
            "pyproject.toml",
        }
        fake_git.write_text(
            "#!"
            + sys.executable
            + "\n"
            + "import json,os,pathlib,shutil,sys\n"
            + "if 'clone' in sys.argv:\n"
            + " pathlib.Path("
            + repr(str(record))
            + ").write_text(json.dumps({'argv':sys.argv[1:],'env':dict(os.environ)}))\n"
            + " source=pathlib.Path("
            + repr(str(APP))
            + ")\n"
            + " for name in "
            + repr(sorted(source_files))
            + ":\n"
            + "  target=pathlib.Path(sys.argv[-1],name); target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source/name,target)\n"
            + " pathlib.Path(sys.argv[-1],'build-info.json').write_text(json.dumps({'version':'0.1.1+gfixture','commit':'"
            + "a" * 40
            + "','commit_count':1,'dirty':False}))\n"
            + "elif 'rev-parse' in sys.argv:\n print('"
            + "a" * 40
            + "')\n"
        )
        fake_git.chmod(0o755)
        fake_python = binary / "python3"
        fake_python.write_text(
            "#!"
            + sys.executable
            + "\n"
            + "import io,json,os,sys\n"
            + "from urllib import request\n"
            + "if sys.argv[1:3] != ['-I','-']:\n os.execv("
            + repr(sys.executable)
            + ",["
            + repr(sys.executable)
            + ",*sys.argv[1:]])\n"
            + "class Opener:\n"
            + " def open(self,req,timeout):\n"
            + "  assert req.full_url == 'https://api.github.com/repos/groopcd/oc-box/releases/latest'\n"
            + "  return io.BytesIO(json.dumps({'tag_name':'v0.1.1','draft':False,'prerelease':False}).encode())\n"
            + "request.build_opener=lambda *handlers: Opener()\n"
            + "code=sys.stdin.read(); sys.argv=['-',*sys.argv[3:]]\n"
            + "exec(compile(code,'<offline-bootstrap>','exec'))\n"
        )
        fake_python.chmod(0o755)
        command = ["sh", str(APP / "install.sh")]
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(binary) + ":" + os.environ["PATH"],
            "TMPDIR": str(self.root),
            "PERSONAL_GITHUB_TOKEN": "synthetic-bootstrap-token",
            "CUSTOM_SECRET": "synthetic-custom",
        }
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        called = json.loads(record.read_text())
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", called["env"])
        self.assertNotIn("CUSTOM_SECRET", called["env"])
        self.assertIn(updater.UPSTREAM, called["argv"])
        self.assertEqual(list(self.root.glob("oc-box-install.*")), [])
        self.assertTrue((self.home / ".local/bin/oc-box").is_file())
        self.assertEqual(
            get_version_info(self.home / ".local/share/oc-box")["version"],
            "0.1.1+gfixture",
        )

    def test_bootstrap_has_valid_posix_shell_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(APP / "install.sh")],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
