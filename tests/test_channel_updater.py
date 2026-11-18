import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import update_channels as channels, updater


class ChannelUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-channel-updater-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.current = {
            "version": "1.6.1+g" + "b" * 7,
            "commit": "b" * 40,
            "commit_count": 30,
            "dirty": False,
        }
        self.releases = {
            "stable": {"version": "1.6.2", "tag": "v1.6.2", "channel": "stable"},
            "devel": {
                "version": "1.7.3.dev0",
                "tag": "v1.7.3.dev0",
                "channel": "devel",
            },
        }
        self.chosen = "stable"
        self.commands = []
        self.installs = []
        self.failure = None
        self.target = self.home / ".local/share/oc-box"
        (self.target / "current").mkdir(parents=True)
        for context in (
            patch.object(Path, "home", return_value=self.home),
            patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(self.home / "custom-config")}
            ),
            patch.object(updater.os, "geteuid", return_value=1000),
            patch.object(updater, "package_managed", return_value=False),
            patch.object(updater.dependencies, "require"),
            patch.object(
                updater, "current_install", side_effect=lambda _: self.current
            ),
            patch.object(
                updater, "available_releases", side_effect=lambda: self.releases
            ),
            patch.object(updater, "git", side_effect=self.git),
            patch.object(updater.subprocess, "run", side_effect=self.install),
        ):
            context.start()
            self.addCleanup(context.stop)

    def git(self, args, work, **kwargs):
        self.commands.append(args)
        if self.failure == "fetch":
            raise updater.UpdateError("synthetic fetch failure")
        release = self.releases[self.chosen]
        commit = "a" * 40 if self.chosen == "stable" else "c" * 40
        if args[0] == "ls-remote":
            return commit + "\trefs/tags/" + release["tag"]
        if args[0] == "clone":
            source = Path(args[-1])
            source.mkdir()
            series = "1.6" if self.chosen == "stable" else "1.7"
            (source / "version_info.py").write_text(
                f"VERSION_SERIES = '{series}'\nVERSION_BASE_COMMIT_COUNT = 29\nRELEASE_CHANNEL = '{self.chosen}'\n"
            )
            (source / "install.py").write_text(
                "'--with-vault'\n"
                + ("'--with-tui'\n" if self.chosen == "devel" else "")
            )
        if "rev-parse" in args:
            return commit
        if "rev-list" in args:
            return "31" if self.chosen == "stable" else "32"
        return ""

    def install(self, command, **kwargs):
        self.installs.append((command, kwargs))
        if self.failure == "install":
            raise subprocess.CalledProcessError(1, command)
        commit = "a" * 40 if self.chosen == "stable" else "c" * 40
        self.current = {
            "version": self.releases[self.chosen]["version"] + "+g" + commit[:7],
            "commit": commit,
            "commit_count": 31 if self.chosen == "stable" else 32,
            "dirty": False,
        }
        (self.target / "build-info.json").write_text(json.dumps(self.current))
        if self.failure != "missing-feature":
            for name in ("vault", "tui"):
                if "--with-" + name in command:
                    (self.target / "current" / (name + "-support.json")).write_text(
                        "{}"
                    )
        return subprocess.CompletedProcess(command, 0)

    def run_upgrade(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = updater.upgrade(self.home, **kwargs)
        return result, output.getvalue()

    def test_default_stable_warns_even_when_already_current(self):
        self.current.update(version="1.6.2+g" + "a" * 7, commit="a" * 40)
        _, output = self.run_upgrade()
        self.assertIn("Development release 1.7.3.dev0", output)
        self.assertIn("up to date", output)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(channels.config_path().exists())

    def test_saved_development_and_explicit_stable_round_trip_with_features(self):
        self.chosen = "devel"
        self.run_upgrade(channel="devel")
        self.assertEqual(channels.selected_channel(), "devel")
        self.run_upgrade()  # No selector: persists on devel and already current.
        self.assertEqual(len(self.installs), 1)
        (self.target / "current/tui-support.json").write_text("{}")
        (self.target / "current/vault-support.json").write_text("{}")
        self.chosen = "stable"
        _, output = self.run_upgrade(channel="stable")
        self.assertIn("downgrade", output)
        self.assertEqual(channels.selected_channel(), "stable")
        command, environment = self.installs[-1]
        self.assertIn("--with-vault", command)
        self.assertNotIn("--with-tui", command)
        self.assertEqual(
            environment["env"]["XDG_CONFIG_HOME"], str(self.home / "custom-config")
        )
        self.assertEqual(
            json.loads((self.target / "update-features.json").read_text()),
            {"vault": True, "tui": True},
        )
        self.chosen = "devel"
        self.run_upgrade(channel="devel")
        self.assertIn("--with-tui", self.installs[-1][0])
        self.assertTrue(
            any("refs/remotes/origin/devel" in args for args in self.commands)
        )

    def test_check_is_a_preview_and_never_saves_channel(self):
        self.chosen = "devel"
        _, output = self.run_upgrade(check=True, channel="devel")
        self.assertIn("check only", output)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(self.installs)
        self.assertFalse(channels.config_path().exists())

    def test_explicit_features_are_independent_from_stable_or_development(self):
        for origin in ("stable", "devel"):
            for features in (("vault",), ("tui",), ("vault", "tui")):
                with self.subTest(origin=origin, features=features):
                    self.chosen = "devel"
                    self.current.update(
                        version="1.6.1+gbbbbbbb"
                        if origin == "stable"
                        else "1.7.2.dev0+gbbbbbbb",
                        commit="b" * 40,
                        commit_count=30,
                    )
                    for name in ("vault", "tui"):
                        (self.target / "current" / (name + "-support.json")).unlink(
                            missing_ok=True
                        )
                    (self.target / "update-features.json").unlink(missing_ok=True)
                    kwargs = {"with_" + name: True for name in features}
                    self.run_upgrade(channel="devel", **kwargs)
                    command = self.installs[-1][0]
                    self.assertEqual(
                        {
                            name
                            for name in ("vault", "tui")
                            if "--with-" + name in command
                        },
                        set(features),
                    )
                    self.assertEqual(channels.selected_channel(), "devel")

    def test_matching_release_adds_missing_features_then_is_idempotent(self):
        self.chosen = "devel"
        self.current.update(
            version="1.7.3.dev0+gccccccc", commit="c" * 40, commit_count=32
        )
        # A sticky preference is not proof the current release has TUI support.
        (self.target / "update-features.json").write_text('{"tui":true}')
        self.run_upgrade(channel="devel", with_tui=True)
        self.assertIn("--with-tui", self.installs[-1][0])
        self.assertTrue(any("merge-base" in args for args in self.commands))
        self.run_upgrade(with_vault=True)
        self.assertIn("--with-vault", self.installs[-1][0])
        self.assertIn("--with-tui", self.installs[-1][0])
        self.assertEqual(len(self.installs), 2)
        _, output = self.run_upgrade(with_vault=True, with_tui=True)
        self.assertIn("up to date", output)
        self.assertEqual(len(self.installs), 2)

    def test_feature_check_validates_source_without_installing_or_saving(self):
        self.chosen = "devel"
        self.current.update(
            version="1.7.3.dev0+gccccccc", commit="c" * 40, commit_count=32
        )
        before = sorted(path.relative_to(self.home) for path in self.home.rglob("*"))
        _, output = self.run_upgrade(
            check=True, channel="devel", with_vault=True, with_tui=True
        )
        self.assertIn("Optional support available: tui, vault (check only)", output)
        self.assertTrue(any("merge-base" in args for args in self.commands))
        self.assertFalse(self.installs)
        self.assertEqual(
            sorted(path.relative_to(self.home) for path in self.home.rglob("*")), before
        )

    def test_explicit_unsupported_feature_refuses_even_on_current_release_or_check(
        self,
    ):
        self.current.update(version="1.6.2+gaaaaaaa", commit="a" * 40, commit_count=31)
        for check in (False, True):
            with (
                self.subTest(check=check),
                self.assertRaisesRegex(
                    updater.UpdateError, "does not support requested optional tui"
                ),
            ):
                self.run_upgrade(check=check, channel="stable", with_tui=True)
        self.assertFalse(self.installs)
        self.assertFalse(channels.config_path().exists())
        self.assertFalse((self.target / "update-features.json").exists())

    def test_feature_failure_or_missing_support_never_saves_preferences(self):
        self.chosen = "devel"
        channels.remember_channel("stable")
        previous = channels.config_path().read_bytes()
        for failure in ("install", "missing-feature"):
            self.failure = failure
            with self.subTest(failure=failure), self.assertRaises(updater.UpdateError):
                self.run_upgrade(channel="devel", with_tui=True, with_vault=True)
            self.assertEqual(channels.config_path().read_bytes(), previous)
            self.assertFalse((self.target / "update-features.json").exists())

    def test_explicit_features_do_not_authorize_an_implicit_downgrade(self):
        self.current.update(
            version="1.7.4.dev0+gddddddd", commit="d" * 40, commit_count=33
        )
        _, output = self.run_upgrade(with_vault=True, with_tui=True)
        self.assertIn("ahead", output)
        self.assertFalse(self.commands)
        self.assertFalse(self.installs)

    def test_failed_switch_leaves_preference_unchanged(self):
        channels.remember_channel("stable")
        before = channels.config_path().read_bytes()
        self.chosen = "devel"
        for failure in ("fetch", "install"):
            self.failure = failure
            with self.subTest(failure=failure), self.assertRaises(updater.UpdateError):
                self.run_upgrade(channel="devel")
            self.assertEqual(channels.config_path().read_bytes(), before)

    def test_ordinary_upgrade_does_not_downgrade(self):
        self.current.update(
            version="1.7.3.dev0+gccccccc", commit="c" * 40, commit_count=32
        )
        _, output = self.run_upgrade()
        self.assertIn("ahead", output)
        self.assertFalse(self.commands)
        self.assertFalse(self.installs)

    def test_repeated_devel_selector_does_not_authorize_same_channel_downgrade(self):
        channels.remember_channel("devel")
        self.chosen = "devel"
        self.current.update(
            version="1.7.4.dev0+gddddddd", commit="d" * 40, commit_count=33
        )
        _, output = self.run_upgrade(channel="devel")
        self.assertIn("ahead", output)
        self.assertFalse(self.commands)
        self.assertFalse(self.installs)

    def test_invalid_source_identity_is_refused_before_execution(self):
        self.releases["stable"]["version"] = "1.6.999"
        with self.assertRaisesRegex(updater.UpdateError, "does not match"):
            self.run_upgrade()
        self.assertFalse(self.installs)

    def test_annotated_tags_are_peeled_and_unexpected_refs_refused(self):
        ref = "refs/tags/v1.6.2"
        self.assertEqual(
            updater.resolve_tag(
                "b" * 40 + "\t" + ref + "\n" + "a" * 40 + "\t" + ref + "^{}", ref
            ),
            "a" * 40,
        )
        for response in (
            "a" * 40 + "\trefs/heads/master",
            "a" * 40 + "\t" + ref + "^{}",
            "",
        ):
            with (
                self.subTest(response=response),
                self.assertRaises(updater.UpdateError),
            ):
                updater.resolve_tag(response, ref)


if __name__ == "__main__":
    unittest.main()
