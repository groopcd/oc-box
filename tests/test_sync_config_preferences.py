import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, sync_config


def config_archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            content = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class NativeCliPreferencesTests(unittest.TestCase):
    def verified_sdk_cache(self, home, *, content, version="2.3.0"):
        config = home / ".config/opencode"
        modules = config / "node_modules"
        package = modules / "@opencode/plugin"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": version}))
        lock = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/@opencode/plugin": {"version": version}},
            }
        )
        (config / "package-lock.json").write_text(lock)
        (modules / ".package-lock.json").write_text(lock)
        (modules / "cached").write_text(content)
        self.assertTrue(sync_config.mark_sdk_installed(home, version))
        return modules

    def test_real_host_archive_seeds_once_and_never_receives_native_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "container-home"
            source = root / "host-config/opencode"
            source.mkdir(parents=True)
            host_preferences = source / "cli.json"
            host_preferences.write_bytes(
                b'{\n  "session": {"permissions": "prompt"},\n'
                b'  "attention": {"notifications": false}\n}\n'
            )
            original_host = host_preferences.read_bytes()
            host_config = source / "opencode.json"
            host_config.write_text('{"model":"fixture/before"}')
            data = {"home": str(home), "opencode_version": "2.3.0"}

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}):
                sync_config.sync_config(home, box.config_archive(data), "2.3.0")
                preferences = home / ".config/opencode/cli.json"
                self.assertEqual(preferences.read_bytes(), original_host)
                native = (
                    b'{"session":{"permissions":"autoaccept"},'
                    b'"attention":{"notifications":true}}\n'
                )
                preferences.write_bytes(native)
                host_config.write_text('{"model":"fixture/after"}')

                sync_config.sync_config(home, box.config_archive(data), "2.3.0")

            self.assertEqual(preferences.read_bytes(), native)
            self.assertEqual(host_preferences.read_bytes(), original_host)
            self.assertEqual(
                json.loads((home / ".config/opencode/opencode.json").read_text())[
                    "model"
                ],
                "fixture/after",
            )

    def test_explicit_native_preferences_survive_host_config_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config/opencode"
            host = {
                "cli.json": '{"session":{"permissions":"prompt"}}',
                "opencode.json": '{"model":"fixture/before"}',
                "plugins/current.js": "before",
            }
            sync_config.sync_config(home, config_archive(host), "2.3.0")
            native = (
                '{\n  "$schema": "https://opencode.ai/v2/cli.json",\n'
                '  "session": {"permissions": "autoaccept", "thinking": "show"},\n'
                '  "attention": {"notifications": true},\n'
                '  "future-setting": {"preserve": [true, false, "opaque"]}\n}\n'
            )
            (config / "cli.json").write_text(native)
            (config / "stale-plugin.js").write_text("stale")
            host.update(
                {
                    "cli.json": '{"attention":{"notifications":false}}',
                    "opencode.json": '{"model":"fixture/after"}',
                    "plugins/current.js": "after",
                }
            )

            sync_config.sync_config(home, config_archive(host), "2.3.0")

            self.assertEqual((config / "cli.json").read_text(), native)
            self.assertEqual(
                json.loads((config / "opencode.json").read_text()),
                {"model": "fixture/after"},
            )
            self.assertEqual((config / "plugins/current.js").read_text(), "after")
            self.assertFalse((config / "stale-plugin.js").exists())

    def test_existing_or_restored_preferences_remain_opaque_and_container_local(self):
        contents = (
            b'{"session":{"permissions":"prompt"},"attention":{"notifications":false}}\n',
            b'{"future-native-setting":{"enabled":false},"theme":{"name":"fixture"}}\n',
            b"native file awaiting a valid edit \xff\n",
            b"",
        )
        for content in contents:
            for host_cli in (None, '{"session":{"permissions":"autoaccept"}}'):
                with (
                    self.subTest(content=content, host_cli=host_cli),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    home = Path(temporary)
                    config = home / ".config/opencode"
                    config.mkdir(parents=True)
                    preferences = config / "cli.json"
                    preferences.write_bytes(content)
                    preferences.chmod(0o644)
                    inode = preferences.stat().st_ino
                    host = {"opencode.json": "{}"}
                    if host_cli is not None:
                        host["cli.json"] = host_cli

                    for _ in range(2):
                        sync_config.sync_config(home, config_archive(host), "2.3.0")
                        self.assertEqual(preferences.read_bytes(), content)
                        self.assertEqual(preferences.stat().st_ino, inode)
                        self.assertEqual(preferences.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(host.get("cli.json"), host_cli)

    def test_host_preferences_seed_only_an_absent_file_without_inventing_defaults(self):
        for host_cli in (
            None,
            '{"session":{"permissions":"prompt"},"attention":{"notifications":false}}',
            '{"session":{"permissions":"autoaccept"},"attention":{"notifications":true}}',
        ):
            with (
                self.subTest(host_cli=host_cli),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = Path(temporary)
                host = {"opencode.json": "{}"}
                if host_cli is not None:
                    host["cli.json"] = host_cli

                sync_config.sync_config(home, config_archive(host), "2.3.0")

                preferences = home / ".config/opencode/cli.json"
                if host_cli is None:
                    self.assertFalse(preferences.exists())
                else:
                    self.assertEqual(preferences.read_text(), host_cli)
                    self.assertEqual(preferences.stat().st_mode & 0o777, 0o600)

    def test_preferences_do_not_cross_container_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            homes = [Path(temporary) / name for name in ("first", "second")]
            host = {"cli.json": '{"theme":{"name":"seed"}}'}
            native = '{"theme":{"name":"container-only"}}'
            sync_config.sync_config(homes[0], config_archive(host), "2.3.0")
            (homes[0] / ".config/opencode/cli.json").write_text(native)

            for home in homes:
                sync_config.sync_config(home, config_archive(host), "2.3.0")

            self.assertEqual(
                (homes[0] / ".config/opencode/cli.json").read_text(), native
            )
            self.assertEqual(
                (homes[1] / ".config/opencode/cli.json").read_text(), host["cli.json"]
            )

    def test_preferences_survive_reused_and_invalidated_dependency_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            host = {
                "cli.json": '{"theme":{"name":"seed"}}',
                "package.json": '{"dependencies":{"custom":"1"}}',
            }
            sync_config.sync_config(home, config_archive(host), "2.3.0")
            config = home / ".config/opencode"
            (config / "cli.json").write_text("native opaque preferences")
            modules = self.verified_sdk_cache(home, content="retained")

            self.assertTrue(
                sync_config.sync_config(home, config_archive(host), "2.3.0")
            )
            self.assertEqual((modules / "cached").read_text(), "retained")
            host["package.json"] = '{"dependencies":{"custom":"2"}}'
            self.assertFalse(
                sync_config.sync_config(home, config_archive(host), "2.3.1")
            )
            self.assertFalse(modules.exists())
            self.assertEqual(
                (config / "cli.json").read_text(), "native opaque preferences"
            )

    def test_unverified_sdk_cache_is_discarded_without_losing_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            host = config_archive({"cli.json": '{"theme":{"name":"seed"}}'})
            self.assertFalse(sync_config.sync_config(home, host, "2.3.0"))
            config = home / ".config/opencode"
            preferences = config / "cli.json"
            preferences.write_text("native opaque preferences")
            modules = config / "node_modules"
            modules.mkdir()
            (modules / "partial-install").touch()

            self.assertFalse(sync_config.sync_config(home, host, "2.3.0"))
            self.assertFalse(modules.exists())
            self.assertEqual(preferences.read_text(), "native opaque preferences")

    def test_v1_config_refresh_retains_legacy_host_copy_behavior(self):
        for runtime in (None, "1.19.5"):
            with (
                self.subTest(runtime=runtime),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = Path(temporary)
                config = home / ".config/opencode"
                config.mkdir(parents=True)
                (config / "cli.json").write_text("old")
                host = {"cli.json": '{"legacy":"host-copy"}'}

                sync_config.sync_config(home, config_archive(host), runtime)

                preferences = config / "cli.json"
                self.assertEqual(preferences.read_text(), host["cli.json"])
                self.assertEqual(preferences.stat().st_mode & 0o777, 0o644)

    def test_unsafe_existing_preferences_refuse_before_config_is_removed(self):
        kinds = ("symlink", "dangling symlink", "directory", "fifo", "hardlink")
        for kind in kinds:
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = Path(temporary)
                config = home / ".config/opencode"
                config.mkdir(parents=True)
                marker = config / "opencode.json"
                marker.write_text("original config")
                outside = home / "outside"
                outside.write_text("unrelated file")
                preferences = config / "cli.json"
                if kind == "symlink":
                    preferences.symlink_to(outside)
                elif kind == "dangling symlink":
                    preferences.symlink_to(home / "absent")
                elif kind == "directory":
                    preferences.mkdir()
                elif kind == "fifo":
                    os.mkfifo(preferences)
                else:
                    os.link(outside, preferences)
                original = preferences.lstat()

                with self.assertRaisesRegex(ValueError, "Unsafe native CLI"):
                    sync_config.sync_config(
                        home, config_archive({"opencode.json": "{}"}), "2.3.0"
                    )

                self.assertEqual(preferences.lstat(), original)
                self.assertEqual(marker.read_text(), "original config")
                self.assertEqual(outside.read_text(), "unrelated file")
                self.assertFalse((config.parent / ".oc-box-dependencies.json").exists())

    def test_linked_config_directory_is_not_a_preferences_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / "outside"
            outside.mkdir()
            (outside / "cli.json").write_text("unrelated preferences")
            (home / ".config").mkdir()
            config = home / ".config/opencode"
            config.symlink_to(outside, target_is_directory=True)
            host = {"cli.json": '{"theme":{"name":"seed"}}'}

            sync_config.sync_config(home, config_archive(host), "2.3.0")

            self.assertFalse(config.is_symlink())
            self.assertEqual((config / "cli.json").read_text(), host["cli.json"])
            self.assertEqual(
                (outside / "cli.json").read_text(), "unrelated preferences"
            )

    def test_foreign_owned_preferences_refuse_before_config_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config/opencode"
            config.mkdir(parents=True)
            preferences = config / "cli.json"
            preferences.write_text("foreign preferences")
            expected_uid = os.getuid() + 1

            with (
                patch.object(sync_config.os, "getuid", return_value=expected_uid),
                self.assertRaisesRegex(ValueError, "Unsafe native CLI"),
            ):
                sync_config.sync_config(home, config_archive({}), "2.3.0")

            self.assertEqual(preferences.read_text(), "foreign preferences")
            self.assertFalse((config.parent / ".oc-box-dependencies.json").exists())

    def test_linked_config_parent_is_refused_without_writing_outside_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (home / ".config").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "Unsafe native CLI"):
                sync_config.sync_config(
                    home, config_archive({"cli.json": "{}"}), "2.3.0"
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_preferences_path_conflicts_refuse_before_changing_existing_config(self):
        for member in ("cli.json/nested", "./cli.json/nested"):
            with (
                self.subTest(member=member),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = Path(temporary)
                config = home / ".config/opencode"
                config.mkdir(parents=True)
                (config / "cli.json").write_text("native preferences")

                with self.assertRaisesRegex(ValueError, "Unsafe config"):
                    sync_config.sync_config(
                        home, config_archive({member: "bad"}), "2.3.0"
                    )

                self.assertEqual(
                    (config / "cli.json").read_text(), "native preferences"
                )

    def test_normalized_host_preferences_cannot_overwrite_native_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config/opencode"
            config.mkdir(parents=True)
            (config / "cli.json").write_text("native preferences")

            sync_config.sync_config(
                home, config_archive({"./cli.json": "host preferences"}), "2.3.0"
            )

            self.assertEqual((config / "cli.json").read_text(), "native preferences")

    def test_native_preferences_remain_in_place_and_cache_survives_copy_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            host = {"opencode.json": "{}"}
            sync_config.sync_config(home, config_archive(host), "2.3.0")
            config = home / ".config/opencode"
            (config / "cli.json").write_text("native preferences")
            modules = self.verified_sdk_cache(home, content="cached dependency")

            def fail_copy(*_):
                self.assertEqual(
                    (config / "cli.json").read_text(), "native preferences"
                )
                raise OSError("fixture")

            with (
                patch.object(sync_config.shutil, "copyfileobj", side_effect=fail_copy),
                self.assertRaises(OSError),
            ):
                sync_config.sync_config(home, config_archive(host), "2.3.0")

            self.assertEqual((config / "cli.json").read_text(), "native preferences")
            self.assertEqual((modules / "cached").read_text(), "cached dependency")


if __name__ == "__main__":
    unittest.main()
