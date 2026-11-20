import json
import os
import stat
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from oc_box import log_config
from oc_box.log_config import LogConfigError, LogPolicy, load


class LogConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-log-config-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.config = self.home / ".config/oc-box/config.json"
        environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, data, path=None):
        path = self.config if path is None else path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        path.chmod(0o644)
        return path

    def assert_invalid(self, **kwargs):
        with self.assertRaises(LogConfigError) as caught:
            load(**kwargs)
        error = caught.exception
        self.assertEqual(str(error), "Activity log configuration is invalid or unsafe.")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_missing_config_uses_immutable_defaults_without_creating_anything(self):
        policy = load()
        self.assertEqual(policy, LogPolicy(None, 256 * 1024, 4, 1024**2, 0, 0, 0))
        with self.assertRaises(FrozenInstanceError):
            policy.max_files = 5
        self.assertEqual(list(self.home.iterdir()), [])
        self.config.parent.mkdir(parents=True)
        self.assertEqual(load(), policy)
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_home_and_xdg_config_precedence_and_no_configuration_writes(self):
        self.write({"logs": {"SystemMaxFiles": 2}})
        xdg = self.base / "config"
        raw = (
            b'{ "backup_min_passphrase_length" : 14, "logs": {"SystemMaxFiles": 6} }\n'
        )
        config = self.write(raw, xdg / "oc-box/config.json")
        before = config.stat()
        self.assertEqual(load().max_files, 2)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            policy = load()
        self.assertEqual(policy.max_files, 6)
        self.assertEqual(policy.max_use, 6 * 256 * 1024)
        self.assertTrue(policy.configured)
        self.assertIsNone(policy.directory)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(load().max_files, 2)
        after = config.stat()
        self.assertEqual(config.read_bytes(), raw)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o644)
        self.assertEqual(list(config.parent.iterdir()), [config])

    def test_override_and_shell_free_lexical_directory(self):
        self.write({"logs": {"directory": "~/logs//./Café space/", "MaxFileSec": "1h"}})
        policy = load()
        self.assertEqual(policy.directory, self.home / "logs/Café space")
        self.assertFalse((self.home / "logs").exists())

        alias = self.base / "link"
        alias.symlink_to(self.base / "missing", target_is_directory=True)
        with patch.dict(os.environ, {"OC_BOX_LOG_DIR": str(alias)}):
            overridden = load()
        self.assertEqual(overridden.directory, alias)
        self.assertEqual(overridden.max_file_sec, policy.max_file_sec)
        self.assertTrue(alias.is_symlink())
        self.assertFalse((self.base / "missing").exists())

        self.config.unlink()
        with patch.dict(os.environ, {"OC_BOX_LOG_DIR": "~/env only"}):
            policy = load()
        self.assertTrue(policy.configured)
        self.assertEqual(policy.directory, self.home / "env only")
        self.assertFalse(policy.directory.exists())

    def test_quantity_units_limits_and_effective_size_left_to_writer(self):
        self.write(
            {
                "logs": {
                    "SystemMaxFileSize": "4M",
                    "SystemMaxFiles": 16,
                    "SystemMaxUse": "1K",
                    "SystemKeepFree": "2G",
                    "MaxFileSec": "52w",
                    "MaxRetentionSec": "365d",
                }
            }
        )
        self.assertEqual(
            load(),
            LogPolicy(
                None, 4 * 1024**2, 16, 1024, 2 * 1024**3, 52 * 604800, 31536000, True
            ),
        )
        self.write(
            {
                "logs": {
                    "SystemMaxFileSize": 1024,
                    "SystemMaxFiles": 1,
                    "SystemMaxUse": "16M",
                    "SystemKeepFree": 2**63 - 1,
                    "MaxFileSec": "1s",
                    "MaxRetentionSec": "1m",
                }
            }
        )
        self.assertEqual(
            load(), LogPolicy(None, 1024, 1, 16 * 1024**2, 2**63 - 1, 1, 60, True)
        )

    def test_derived_total_cap_and_internal_small_defaults(self):
        self.write({"logs": {"SystemMaxFileSize": "4M", "SystemMaxFiles": 16}})
        self.assertEqual(load().max_use, 16 * 1024**2)
        self.write({"logs": {}, "unrelated": ["untouched", True]})
        self.assertEqual(load(512, 3), LogPolicy(None, 512, 3, 1536, 0, 0, 0))
        self.assert_invalid(default_file_size=False)
        self.assert_invalid(default_max_files=0)
        self.write({"logs": {"SystemMaxFileSize": 512}})
        self.assert_invalid(default_file_size=512)

    def test_rejects_malformed_and_unsafe_directory_values_without_echoing_them(self):
        for value in [
            None,
            1,
            "",
            "relative/private",
            "~/../private",
            "/x/../private",
            "/",
            "/./",
            "//private",
            "/private\0hidden",
            "/private\tescape",
            "/private\nescape",
            "/private\x1bescape",
            "/private\u200bhidden",
        ]:
            with self.subTest(value=value):
                self.write({"logs": {"directory": value}})
                self.assert_invalid()
        self.write({"logs": {"directory": "/invalid\nsaved"}})
        with patch.dict(os.environ, {"OC_BOX_LOG_DIR": str(self.base)}):
            self.assert_invalid()
        self.write({"logs": {"directory": str(self.base)}})
        for value in ("", "relative", "/unsafe\x1bpath"):
            with (
                self.subTest(override=value),
                patch.dict(os.environ, {"OC_BOX_LOG_DIR": value}),
            ):
                self.assert_invalid()
        self.write({"logs": {"directory": "~/valid"}})
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.base / "nonprinting\nhome"),
                "XDG_CONFIG_HOME": str(self.config.parent.parent),
            },
        ):
            self.assert_invalid()

    def test_rejects_quantities_outside_the_closed_schema(self):
        invalid = {
            "SystemMaxFileSize": [
                0,
                1023,
                "5M",
                "1KB",
                "1024",
                "1k",
                "1.5K",
                "1M4K",
                "１K",
                " 1K",
            ],
            "SystemMaxFiles": [0, -1, 17, "4", "1K"],
            "SystemMaxUse": [1023, "17M", "1G", "1h"],
            "SystemKeepFree": [-1, 2**63, "9999999999999999999G"],
            "MaxFileSec": [-1, "366d", "1h30m", "1ms", "1D", "1s\n"],
            "MaxRetentionSec": [-1, 31536001, "53w", "1K", "99999999999999999999w"],
        }
        for key, values in invalid.items():
            for value in [*values, True, 2.0, None, {}]:
                with self.subTest(key=key, value=value):
                    self.write({"logs": {key: value}})
                    self.assert_invalid()

    def test_invalid_json_and_duplicate_or_unknown_keys_fail_closed(self):
        for raw in [
            b"",
            b"\xff",
            b"[]",
            b"null",
            b'{"logs": true}',
            b'{"logs":{"max_file_size": 1024}}',
            b'{"logs":{"directory":"/one","directory":"/two"}}',
            b'{"logs":{},"logs":{}}',
            b'{"other":{"duplicate":1,"duplicate":2}}',
            b'{"private-value": NaN}',
            b'{"private-value": "no closing object"',
        ]:
            with self.subTest(raw=raw[:64]):
                self.write(raw)
                self.assert_invalid()

    def test_config_ancestry_is_absolute_and_no_component_is_followed(self):
        for value in ("relative", str(self.base / ".." / "config")):
            with (
                self.subTest(base=value),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": value}),
            ):
                self.assert_invalid()
        config = self.write({"logs": {"SystemMaxFiles": 3}})
        linked_home = self.base / "linked-home"
        for target in (self.home, self.base / "missing"):
            with self.subTest(target=target):
                linked_home.symlink_to(target, target_is_directory=True)
                with patch.dict(
                    os.environ, {"XDG_CONFIG_HOME": str(linked_home / ".config")}
                ):
                    self.assert_invalid()
                linked_home.unlink()
        config.parent.rename(self.base / "actual")
        config.parent.symlink_to(self.base / "actual", target_is_directory=True)
        self.assert_invalid()

    def test_config_refuses_other_owners_modes_hardlinks_and_symlinks(self):
        config = self.write({})
        with patch.object(log_config.os, "getuid", return_value=os.getuid() + 1):
            self.assert_invalid()
        for mode in (0o664, 0o646):
            with self.subTest(mode=mode):
                config.chmod(mode)
                self.assert_invalid()
        config.chmod(0o644)
        target = self.base / "target"
        os.link(config, target)
        self.assert_invalid()
        config.unlink()
        for path in (target, self.base / "missing"):
            with self.subTest(path=path):
                config.symlink_to(path)
                self.assert_invalid()
                config.unlink()
        config.mkdir()
        self.assert_invalid()

    def test_config_opens_nonblocking_and_rejects_fifo(self):
        self.config.parent.mkdir(parents=True)
        os.mkfifo(self.config)
        real_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            if path == self.config.name:
                # Fail before a blocking open can hang a broken test run.
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(log_config.os, "open", side_effect=guarded_open):
            self.assert_invalid()

    def test_config_read_is_bounded_even_if_file_grows_after_stat(self):
        limit = 64 * 1024
        self.write(b"{}" + b" " * (limit - 2))
        self.assertFalse(load().configured)
        self.write(b"{}" + b" " * (limit - 1))
        self.assert_invalid()

        path = self.write(b"{}")
        real_read = os.read

        def grow_on_read(fd, size):
            self.assertEqual(size, limit + 1)
            with path.open("ab") as stream:
                stream.write(b" " * limit)
            return real_read(fd, size)

        with patch.object(log_config.os, "read", side_effect=grow_on_read) as read:
            self.assert_invalid()
        read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
