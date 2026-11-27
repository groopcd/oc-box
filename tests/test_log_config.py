import argparse
import ctypes
import errno
import fcntl
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from contextvars import Context
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

from oc_box import activity_log, log_config
from oc_box.log_config import (
    LogConfigError,
    LogPolicy,
    add_launch_arguments,
    directory_argument,
    load,
    load_host_settings,
    overrides,
    save_host_settings,
)


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
        self.assertIs(policy.journald, False)
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

    def test_override_and_lexical_directory(self):
        self.write({"logs": {"directory": "~/logs//./Café space/", "MaxFileSec": "1h"}})
        policy = load()
        self.assertEqual(policy.directory, self.home / "logs/Café space")
        self.assertFalse((self.home / "logs").exists())

        alias = self.base / "link"
        alias.symlink_to(self.base / "missing", target_is_directory=True)
        with overrides(directory=directory_argument(str(alias))):
            overridden = load()
        self.assertEqual(overridden.directory, alias)
        self.assertEqual(overridden.max_file_sec, policy.max_file_sec)
        self.assertTrue(alias.is_symlink())
        self.assertFalse((self.base / "missing").exists())

        self.config.unlink()
        with overrides(directory=directory_argument("~/CLI only")):
            policy = load()
        self.assertTrue(policy.configured)
        self.assertEqual(policy.directory, self.home / "CLI only")
        self.assertFalse(policy.directory.exists())

    def test_launch_arguments_have_tri_state_mirror_and_fixed_errors(self):
        parser = argparse.ArgumentParser(prog="oc-box start", allow_abbrev=False)
        add_launch_arguments(parser)
        self.assertEqual(
            vars(parser.parse_args([])),
            {
                "log_dir": None,
                "journald": None,
                "lifecycle_diagnostics": False,
                "capture_preparation": False,
                "verbose": None,
            },
        )
        self.assertEqual(
            vars(
                parser.parse_args(
                    [
                        "--log-dir",
                        "~/log space",
                        "--journald",
                        "--lifecycle-diagnostics",
                        "--capture-preparation",
                        "--verbose=ALL",
                    ]
                )
            ),
            {
                "log_dir": self.home / "log space",
                "journald": True,
                "lifecycle_diagnostics": True,
                "capture_preparation": True,
                "verbose": "all",
            },
        )
        self.assertIs(parser.parse_args(["--no-journald"]).journald, False)
        for arguments in (
            ["--journald", "--no-journald"],
            ["--log-dir", "relative-synthetic-private-value"],
            ["--verbose=synthetic-private-value"],
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                parser.parse_args(arguments)
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("synthetic-private-value", stderr.getvalue())

    def test_saved_mirror_requires_exact_boolean_even_with_cli_override(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.write({"logs": {"journald": value}})
                self.assertIs(load().journald, value)
                self.assertTrue(load().configured)
                with overrides(journald=not value):
                    self.assertIs(load().journald, not value)
                self.assertIs(load().journald, value)
        for value in (None, 0, 1, 1.0, "true", "false", {}, []):
            with self.subTest(value=value):
                self.write({"logs": {"journald": value}})
                with overrides(directory=self.base, journald=True):
                    self.assert_invalid()

    def test_overrides_inherit_independently_and_reset_on_exit_and_error(self):
        saved = self.base / "saved"
        selected = self.base / "selected"
        self.write({"logs": {"directory": str(saved), "journald": True}})
        baseline = load()
        environment = dict(os.environ)
        with overrides(directory=selected, journald=False):
            outer = load()
            self.assertEqual(outer.directory, selected)
            self.assertIs(outer.journald, False)
            self.assertEqual(Context().run(load), baseline)
            with overrides(directory=self.base):
                self.assertEqual(load().directory, self.base)
                self.assertIs(load().journald, False)
            with overrides():
                self.assertEqual(load(), outer)
            with (
                self.assertRaisesRegex(RuntimeError, "^synthetic exception$"),
                overrides(journald=True),
            ):
                self.assertIs(load().journald, True)
                self.assertEqual(load().directory, selected)
                raise RuntimeError("synthetic exception")
            self.assertEqual(load(), outer)
            with (
                self.assertRaises(LogConfigError),
                overrides(directory=Path("relative"), journald=True),
            ):
                self.fail("invalid directory was accepted")
            with self.assertRaises(LogConfigError), overrides(journald=1):
                self.fail("invalid mirror setting was accepted")
            self.assertEqual(load(), outer)
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(load(), baseline)
        self.assertFalse(selected.exists())
        self.assertFalse(saved.exists())

    def test_unreleased_environment_switches_are_ignored(self):
        defaults = load()
        with patch.dict(
            os.environ, {"OC_BOX_LOG_DIR": "/invalid\nignored", "OC_BOX_JOURNALD": "1"}
        ):
            self.assertEqual(load(), defaults)

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
            if isinstance(value, str):
                with (
                    self.subTest(argument=value),
                    self.assertRaises(argparse.ArgumentTypeError) as caught,
                ):
                    directory_argument(value)
                self.assertEqual(
                    str(caught.exception),
                    "must be an absolute non-root directory without '..' or non-printing characters",
                )
        self.write({"logs": {"directory": "/invalid\nsaved"}})
        with overrides(directory=self.base):
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

    def assert_save_refused(self, settings, match, **changes):
        with self.assertRaisesRegex(LogConfigError, match) as caught:
            save_host_settings(
                settings, **{"directory": None, "journald": True, **changes}
            )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(str(self.base), str(caught.exception))
        self.assertFalse(list(self.config.parent.glob(".host-logging.*.tmp")))

    def test_explicit_save_provisions_only_private_config_and_noop_is_write_free(self):
        settings = load_host_settings()
        self.assertIsNone(settings.directory)
        self.assertFalse(settings.journald)
        self.assertFalse(settings.journald_configured)
        self.assertEqual(
            save_host_settings(settings, directory=None, journald=False),
            (settings, False),
        )
        self.assertEqual(list(self.home.iterdir()), [])
        target = self.base / "untouched logs"
        saved, changed = save_host_settings(
            settings, directory=str(target), journald=False
        )
        self.assertTrue(changed)
        self.assertEqual(saved.effective_directory, target)
        self.assertFalse(saved.journald_configured)
        self.assertEqual(load().directory, target)
        self.assertFalse(target.exists())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertEqual(self.config.stat().st_nlink, 1)
        self.assertEqual(
            json.loads(self.config.read_bytes()), {"logs": {"directory": str(target)}}
        )

    def test_reset_removes_only_directory_and_preserves_full_unrelated_values(self):
        unrelated = {
            "auto_update": False,
            "backup_min_passphrase_length": 14,
            "update_channel": "stable",
            "package_update_channels": {"pipx": "devel"},
            "pass_env": ["EXAMPLE_TOKEN"],
        }
        rotation = {
            "SystemMaxFileSize": "1M",
            "SystemMaxFiles": 5,
            "SystemMaxUse": "8M",
            "SystemKeepFree": "2G",
            "MaxFileSec": "1h",
            "MaxRetentionSec": "7d",
        }
        special = b'[1.234567890123456789, 1e50000, 1e-50000, "\\ud800"]'
        raw = (
            json.dumps(
                {
                    **unrelated,
                    "logs": {**rotation, "directory": "~/historical", "journald": True},
                }
            ).encode()[:-1]
            + b', "synthetic": '
            + special
            + b"}"
        )
        path = self.write(raw)
        before = load_host_settings()
        old_dir = self.home / "historical"
        old_dir.mkdir()
        marker = old_dir / "retained"
        marker.write_bytes(b"synthetic old log")
        with overrides(directory=self.base, journald=False):
            self.assertEqual(load().directory, self.base)
            self.assertEqual(load_host_settings(), before)
            saved, changed = save_host_settings(before, directory=None, journald=False)
        self.assertTrue(changed)
        self.assertIsNone(saved.effective_directory)
        self.assertTrue(saved.journald_configured)
        self.assertFalse(saved.journald)
        after = json.loads(path.read_bytes())
        self.assertEqual(after.pop("logs"), {**rotation, "journald": False})
        after.pop("synthetic")
        self.assertEqual(after, unrelated)
        self.assertIn(special.replace(b", ", b","), path.read_bytes())
        self.assertIsNone(load().directory)
        self.assertEqual(marker.read_bytes(), b"synthetic old log")
        self.assertFalse((self.home / ".local").exists())

    def test_unmodified_saved_policy_does_not_replace_or_normalize_legacy_config(self):
        raw = b'{ "logs": {"directory":"~/two//./levels", "journald":false}, "other": 1.0 }\n'
        path = self.write(raw)
        baseline = path.stat()
        settings = load_host_settings()
        self.assertEqual(settings.directory, "~/two//./levels")
        self.assertEqual(settings.effective_directory, self.home / "two/levels")
        self.assertEqual(
            save_host_settings(settings, directory=settings.directory, journald=False),
            (settings, False),
        )
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_ino, baseline.st_ino)
        self.assertEqual(path.stat().st_mode, baseline.st_mode)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_or_oversized_candidate_does_not_create_config_dirs_or_lock(self):
        settings = load_host_settings()
        for value in ("relative path", "/directory\0extra", "/" + "a" * 65536):
            self.assert_save_refused(settings, "valid absolute", directory=value)
        self.assert_save_refused(settings, "must be on or off", journald=1)
        self.assertEqual(list(self.home.iterdir()), [])
        path = self.write(b'{"synthetic":"' + b"x" * 65504 + b'"}')
        before = path.read_bytes()
        self.assert_save_refused(
            load_host_settings(),
            "Could not safely save",
            directory="/synthetic/overflow",
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_stale_snapshot_and_config_reselection_refuse_without_overwrite(self):
        baseline = self.write({"logs": {"journald": False}})
        stale = load_host_settings()
        new = b'{"auto_update":true,"logs":{"journald":false}}'
        baseline.write_bytes(new)
        self.assert_save_refused(stale, "Host config changed")
        self.assertEqual(baseline.read_bytes(), new)
        self.assertEqual(list(baseline.parent.iterdir()), [baseline])
        selected = self.base / "other-config"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(selected)}):
            self.assert_save_refused(
                load_host_settings(), "valid absolute", directory="/"
            )
            self.assert_save_refused(stale, "Host config changed")
        self.assertFalse(selected.exists())

    def test_writer_refuses_unsafe_ancestry_and_file_without_touching_target(self):
        path = self.write({})
        settings = load_host_settings()
        for directory in (path.parent, path.parent.parent, self.home):
            mode = stat.S_IMODE(directory.stat().st_mode)
            with self.subTest(directory=directory):
                directory.chmod(mode | 0o002)
                try:
                    self.assert_save_refused(settings, "Could not safely save")
                    self.assertEqual(list(path.parent.iterdir()), [path])
                finally:
                    directory.chmod(mode)
        path.chmod(0o400)
        self.assert_save_refused(load_host_settings(), "Could not safely save")
        self.assertEqual(path.read_bytes(), b"{}")
        target = self.base / "unrelated"
        target.write_bytes(b"sentinel")
        path.unlink()
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, path)
                else:
                    os.mkfifo(path)
                with self.assertRaisesRegex(LogConfigError, "invalid or unsafe"):
                    load_host_settings()
                self.assert_save_refused(settings, "Could not safely save")
                self.assertEqual(target.read_bytes(), b"sentinel")
                path.unlink()

    def test_unsafe_or_busy_shared_lock_refuses_save(self):
        path = self.write({})
        settings = load_host_settings()
        lock = path.parent / ".update-settings.lock"
        target = self.base / "not-the-lock"
        target.write_bytes(b"test sentinel")
        lock.symlink_to(target)
        self.assert_save_refused(settings, "Could not safely save")
        self.assertEqual(target.read_bytes(), b"test sentinel")
        lock.unlink()
        os.mkfifo(lock, 0o600)
        self.assert_save_refused(settings, "Could not safely save")
        lock.unlink()
        lock.touch(mode=0o600)
        with lock.open("r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_save_refused(settings, "Could not safely save")
        self.assertEqual(path.read_bytes(), b"{}")
        self.assertTrue(save_host_settings(settings, directory=None, journald=True)[1])

    def test_rechecks_existing_file_and_parent_after_staging(self):
        def verify(change):
            path = self.write({})
            settings = load_host_settings()
            unsaved = b'{"outside-edit":true}'
            real_fsync = os.fsync
            old_parent = self.base / "old-config-dir"

            def edit_while_staging(fd):
                real_fsync(fd)
                if change == "parent":
                    path.parent.rename(old_parent)
                    path.parent.mkdir()
                path.write_bytes(unsaved)

            with patch.object(log_config.os, "fsync", side_effect=edit_while_staging):
                self.assert_save_refused(settings, "Host config changed")
            self.assertEqual(path.read_bytes(), unsaved)
            if change == "parent":
                self.assertEqual((old_parent / path.name).read_bytes(), b"{}")
                self.assertFalse(list(old_parent.glob(".host-logging.*.tmp")))

        for change in ("file", "parent"):
            with self.subTest(change=change):
                verify(change)

    def test_atomic_first_save_does_not_replace_a_last_instant_entry(self):
        self.config.parent.mkdir(parents=True)
        settings = load_host_settings()
        other = self.base / "unrelated"
        other.write_bytes(b"unchanged")
        publish = log_config._publish_config

        def inject(parent, name, *, missing):
            self.assertTrue(missing)
            self.config.symlink_to(other)
            return publish(parent, name, missing=missing)

        with patch.object(log_config, "_publish_config", side_effect=inject):
            self.assert_save_refused(settings, "Host config changed")
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(other.read_bytes(), b"unchanged")

    def test_first_save_uses_dirfd_hardlink_if_renameat2_is_unavailable(self):
        for unavailable in ("libc", errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            with self.subTest(unavailable=unavailable):
                settings = load_host_settings()
                libc = (
                    object()
                    if unavailable == "libc"
                    else Mock(renameat2=Mock(return_value=-1))
                )
                with (
                    patch.object(ctypes, "CDLL", return_value=libc),
                    patch.object(ctypes, "get_errno", return_value=unavailable),
                    patch.object(log_config.os, "link", wraps=os.link) as link,
                ):
                    saved, changed = save_host_settings(
                        settings, directory=None, journald=True
                    )
                self.assertTrue(changed)
                self.assertEqual(load_host_settings(), saved)
                self.assertTrue(load().journald)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
                self.assertEqual(self.config.stat().st_nlink, 1)
                link.assert_called_once()
                options = link.call_args.kwargs
                self.assertEqual(options["src_dir_fd"], options["dst_dir_fd"])
                self.assertIsInstance(options["src_dir_fd"], int)
                self.assertFalse(options["follow_symlinks"])
                self.assertFalse(list(self.config.parent.glob(".host-logging.*.tmp")))
                self.config.unlink()

    def test_link_fallback_never_clobbers_a_last_instant_config(self):
        settings = load_host_settings()
        other = self.base / "unrelated"
        other.write_bytes(b"untouched")
        real_link = os.link

        def inject(*args, **kwargs):
            self.config.symlink_to(other)
            return real_link(*args, **kwargs)

        with (
            patch.object(log_config, "_rename_config_noreplace", return_value=False),
            patch.object(log_config.os, "link", side_effect=inject),
        ):
            self.assert_save_refused(settings, "Host config changed")
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(other.read_bytes(), b"untouched")

    def test_link_fallback_unavailable_or_uncertain_after_install(self):
        settings = load_host_settings()
        private_error = OSError(errno.EOPNOTSUPP, str(self.config))
        with patch.object(log_config, "_rename_config_noreplace", return_value=False):
            with patch.object(log_config.os, "link", side_effect=private_error):
                self.assert_save_refused(settings, "Could not safely save")
            self.assertFalse(self.config.exists())
            settings = load_host_settings()
            with (
                patch.object(log_config.os, "unlink", side_effect=private_error),
                self.assertRaisesRegex(
                    LogConfigError, "save could not be verified"
                ) as caught,
            ):
                save_host_settings(settings, directory=None, journald=True)
        self.assertNotIn(str(self.config), str(caught.exception))
        [staging] = self.config.parent.glob(".host-logging.*.tmp")
        self.assertEqual(staging.stat().st_ino, self.config.stat().st_ino)
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        self.assertEqual(staging.stat().st_nlink, 2)
        self.assert_invalid()

    def test_save_preflight_refuses_known_unsafe_custom_paths_without_writes(self):
        settings = load_host_settings()
        target = self.base / "private"
        target.mkdir(mode=0o755)
        alias = self.base / "alias"
        alias.symlink_to(target, target_is_directory=True)
        for invalid in (target, alias, alias / "new"):
            self.assert_save_refused(
                settings, "directory is unsafe", directory=str(invalid)
            )
            self.assertEqual(list(target.iterdir()), [])
        target.chmod(0o700)
        for name in (
            "activity.jsonl",
            "activity.jsonl.1",
            "activity.jsonl.15",
            "activity.lock",
        ):
            with self.subTest(name=name):
                entry = target / name
                original = b"unrelated private data\n"
                entry.write_bytes(original)
                entry.chmod(0o600)
                before = entry.stat()
                self.assert_save_refused(
                    settings, "directory is unsafe", directory=str(target)
                )
                self.assertEqual(entry.read_bytes(), original)
                self.assertEqual(entry.stat().st_ino, before.st_ino)
                self.assertEqual(list(target.iterdir()), [entry])
                entry.unlink()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_save_accepts_existing_legacy_custom_jsonl_without_mutating_it(self):
        directory = self.base / "private"
        directory.mkdir(mode=0o700)
        fingerprint = (
            b'{"timestamp":"2000-01-01T00:00:00.000Z","event":"command.finished"}'
        )
        files = {
            directory / "activity.jsonl": fingerprint + b"\r\n",
            directory / "activity.jsonl.1": fingerprint,
            directory / "unrelated.txt": b"leave alone",
        }
        for path, data in files.items():
            path.write_bytes(data)
            path.chmod(0o600)
        saved, changed = save_host_settings(
            load_host_settings(), directory=str(directory), journald=False
        )
        self.assertTrue(changed)
        self.assertEqual(saved.effective_directory, directory)
        self.assertEqual(
            {path: path.read_bytes() for path in directory.iterdir()}, files
        )

    def test_append_rechecks_custom_after_save_and_reset_can_leave_it_untouched(self):
        directory = self.base / "not-yet-created" / "journal"
        saved, changed = save_host_settings(
            load_host_settings(), directory=str(directory), journald=False
        )
        self.assertTrue(changed)
        self.assertFalse(directory.parent.exists())

        directory.mkdir(mode=0o700, parents=True)
        active = directory / "activity.jsonl"
        active.write_bytes(b"synthetic unrelated data\n")
        active.chmod(0o600)
        self.assertFalse(activity_log.record_event("command.finished", command="stop"))
        self.assertEqual(active.read_bytes(), b"synthetic unrelated data\n")

        reset, changed = save_host_settings(saved, directory=None, journald=False)
        self.assertTrue(changed)
        self.assertIsNone(reset.directory)
        self.assertEqual(active.read_bytes(), b"synthetic unrelated data\n")

    def test_publication_and_durability_failures_report_fixed_distinct_errors(self):
        path = self.write({})
        settings = load_host_settings()
        private_error = OSError(str(path))
        with patch.object(log_config.os, "replace", side_effect=private_error):
            self.assert_save_refused(settings, "Could not safely save")
        self.assertEqual(path.read_bytes(), b"{}")
        real_fsync = os.fsync

        def fail_final_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise private_error
            return real_fsync(fd)

        with patch.object(
            log_config.os, "fsync", side_effect=fail_final_directory_sync
        ):
            self.assert_save_refused(settings, "save could not be verified")
        self.assertTrue(load().journald)


if __name__ == "__main__":
    unittest.main()
