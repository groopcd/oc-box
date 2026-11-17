"""Workspace policy resolution and relay fallback, without a native service."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import host_drop, oc_box as box, workspace_preferences as preferences


class HostDropPreferenceLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-drop-preferences-")
        self.addCleanup(temporary.cleanup)
        self.ident = "a" * 32
        self.path = Path(temporary.name) / self.ident / "workspace.json"
        self.path.parent.mkdir(mode=0o700)
        self.data = {
            "id": self.ident,
            "version": 2,
            "opencode_version": "2.3.0",
            "project": str(self.path.parent / "project"),
        }
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)
        self.original = self.path.read_bytes()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def resolve(self, requested=None, version="2.3.0"):
        args = SimpleNamespace(host_drop=requested)
        box.configure_host_drop(self.path, self.data, args, version)
        self.assertEqual(self.path.read_bytes(), self.original)
        return args

    def test_parser_keeps_automatic_mode_distinct_from_explicit_overrides(self):
        self.assertIsNone(box.parser().parse_args([]).host_drop)
        self.assertIs(box.parser().parse_args(["--host-drop"]).host_drop, True)
        self.assertIs(box.parser().parse_args(["--no-host-drop"]).host_drop, False)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            box.parser().parse_args(["--host-drop", "--no-host-drop"])

    def test_supported_default_enables_without_writing_policy(self):
        with patch.object(box, "validate_host_drop", return_value="konsole") as check:
            args = self.resolve()
        self.assertTrue(box.effective_host_drop(args))
        self.assertIsNone(args.host_drop)
        check.assert_called_once_with("2.3.0")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_automatic_mode_gracefully_skips_unsupported_versions_and_terminals(self):
        for version in ("1.19.5", "2.9.9", "2.3.0"):
            with (
                self.subTest(version=version),
                patch.object(
                    box, "validate_host_drop", side_effect=box.BoxError("Unavailable.")
                ),
            ):
                args = self.resolve(version=version)
                self.assertFalse(box.effective_host_drop(args))
        self.assertFalse((self.path.parent / preferences.FILE_NAME).exists())

    def test_disable_persists_and_prevents_preflight_on_future_clients(self):
        with patch.object(box, "validate_host_drop") as check:
            self.assertFalse(box.effective_host_drop(self.resolve(False)))
            self.assertFalse(box.effective_host_drop(self.resolve()))
        check.assert_not_called()
        self.assertFalse(preferences.enabled(self.path, self.ident))

    def test_explicit_enable_persists_but_failed_compatibility_does_not_change_policy(
        self,
    ):
        preferences.set_host_drop(self.path, self.ident, False)
        with (
            patch.object(
                box, "validate_host_drop", side_effect=box.BoxError("Unavailable.")
            ),
            self.assertRaises(box.BoxError),
        ):
            self.resolve(True)
        self.assertFalse(preferences.enabled(self.path, self.ident))
        with patch.object(box, "validate_host_drop", return_value="konsole"):
            self.assertTrue(box.effective_host_drop(self.resolve(True)))
        self.assertTrue(preferences.enabled(self.path, self.ident))

    def test_corrupt_policy_refuses_even_explicit_override(self):
        policy = self.path.parent / preferences.FILE_NAME
        policy.write_text("{invalid")
        policy.chmod(0o600)
        for requested in (None, True, False):
            with (
                self.subTest(requested=requested),
                patch.object(box, "validate_host_drop") as check,
                self.assertRaises(box.BoxError),
            ):
                self.resolve(requested)
            check.assert_not_called()
            self.assertEqual(policy.read_text(), "{invalid")

    def test_resolution_does_not_repeat_or_rewrite_for_same_client(self):
        with patch.object(box, "validate_host_drop", return_value="konsole") as check:
            args = self.resolve(True)
            policy = self.path.parent / preferences.FILE_NAME
            stamp = policy.stat().st_mtime_ns
            box.configure_host_drop(self.path, self.data, args, "2.3.0")
        check.assert_called_once()
        self.assertEqual(policy.stat().st_mtime_ns, stamp)

    def test_preference_dispatch_does_not_check_backend_or_unlock(self):
        with (
            patch.object(preferences, "cli", return_value=0) as cli,
            patch.object(box, "check_backend") as backend,
            patch.object(box, "launch_credentials") as unlock,
        ):
            self.assertEqual(box.main(["preferences", "--host-drop", "off"]), 0)
        cli.assert_called_once_with(box, ["--host-drop", "off"])
        backend.assert_not_called()
        unlock.assert_not_called()

    def test_attached_clients_use_saved_policy_without_refreshing_owner(self):
        connection = {"url": "http://127.0.0.1:12345", "session": None}
        with (
            patch.dict(box.os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(box, "check_backend"),
            patch.object(box, "state_root", return_value=self.path.parent.parent),
            patch.object(box, "find_workspace", return_value=(self.path, self.data)),
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(
                box.workspace_service, "controller_running", return_value=True
            ),
            patch.object(
                box.workspace_service,
                "attach",
                return_value=contextlib.nullcontext(connection),
            ),
            patch.object(
                box.workspace_service, "client_command", return_value=["native"]
            ),
            patch.object(box, "run_native_tui", return_value=0) as run,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "launch_credentials") as unlock,
        ):
            for flags, enabled in (
                ([], True),
                (["--no-host-drop"], False),
                ([], False),
            ):
                with self.subTest(flags=flags, enabled=enabled):
                    self.assertEqual(box.main(["--workspace", self.ident, *flags]), 0)
                    self.assertIs(run.call_args.kwargs["host_drop_enabled"], enabled)
                    self.assertFalse(run.call_args.kwargs["host_drop_required"])
            self.assertEqual(box.main(["--workspace", self.ident, "--host-drop"]), 0)
            self.assertTrue(run.call_args.kwargs["host_drop_enabled"])
            self.assertTrue(run.call_args.kwargs["host_drop_required"])
        prepare.assert_not_called()
        unlock.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_corrupt_policy_precedes_reset_unlock_and_removal(self):
        policy = self.path.parent / preferences.FILE_NAME
        policy.write_text("{invalid")
        policy.chmod(0o600)
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "state_root", return_value=self.path.parent.parent),
            patch.object(box, "find_workspace", return_value=(self.path, self.data)),
            patch.object(box, "launch_credentials") as unlock,
            patch.object(box, "remove_resources") as remove,
            self.assertRaises(box.BoxError),
        ):
            box.main(["reset", "--workspace", self.ident])
        unlock.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(policy.read_text(), "{invalid")

    def test_both_startup_flags_refuse_unrelated_commands_before_backend(self):
        for command in ("sync", "terminal", "list", "status", "purge"):
            for flag in ("--host-drop", "--no-host-drop"):
                with (
                    self.subTest(command=command, flag=flag),
                    patch.object(box, "check_backend") as backend,
                    self.assertRaises(box.BoxError),
                ):
                    box.main([command, flag])
                backend.assert_not_called()

    def test_relay_compatibility_fallback_is_automatic_only(self):
        for required in (True, False):
            with (
                self.subTest(required=required),
                patch.object(
                    box, "validate_host_drop", side_effect=box.BoxError("Unavailable.")
                ),
                patch.object(box, "run_tui", return_value=9) as run,
            ):
                if required:
                    with self.assertRaises(box.BoxError):
                        box.run_native_tui(
                            self.data,
                            ["native"],
                            {},
                            host_drop_enabled=True,
                            host_drop_required=required,
                        )
                    run.assert_not_called()
                else:
                    self.assertEqual(
                        box.run_native_tui(
                            self.data,
                            ["native"],
                            {},
                            host_drop_enabled=True,
                            host_drop_required=required,
                        ),
                        9,
                    )
                    run.assert_called_once_with(["native"])

    def test_automatic_setup_failure_falls_back_but_never_retries_started_client(self):
        with (
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(
                host_drop, "Relay", side_effect=host_drop.DropError("Unavailable.")
            ),
            patch.object(box, "run_tui", return_value=8) as run,
        ):
            self.assertEqual(
                box.run_native_tui(
                    self.data,
                    ["native"],
                    {},
                    host_drop_enabled=True,
                    host_drop_required=False,
                ),
                8,
            )
            run.assert_called_once_with(["native"])
        relay = Mock()
        relay.__enter__ = Mock(return_value=relay)
        relay.__exit__ = Mock(return_value=False)
        with (
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(host_drop, "Relay", return_value=relay),
            patch.object(
                box, "run_tui", side_effect=host_drop.DropError("Transfer failed.")
            ) as run,
            self.assertRaises(box.BoxError),
        ):
            box.run_native_tui(
                self.data,
                ["native"],
                {},
                host_drop_enabled=True,
                host_drop_required=False,
            )
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
