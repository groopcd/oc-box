"""Future-client clipboard policy; synthetic state, no desktop clipboard reads."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import host_clipboard, oc_box as box, workspace_preferences as preferences


class HostClipboardPreferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-clipboard-preferences-")
        self.addCleanup(temporary.cleanup)
        self.ident = "a" * 32
        self.path = Path(temporary.name) / self.ident / "workspace.json"
        self.path.parent.mkdir(mode=0o700)
        self.data = {
            "id": self.ident,
            "version": 2,
            "opencode_version": "2.3.2",
            "project": str(self.path.parent / "project"),
        }
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)
        self.original = self.path.read_bytes()
        self.reader = Mock()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.check = stack.enter_context(
            patch.object(box, "validate_host_drop", return_value="konsole")
        )
        self.discover = stack.enter_context(
            patch.object(host_clipboard, "preflight", return_value=self.reader)
        )

    def resolve(self, *flags, version="2.3.2"):
        args = box.parser().parse_args(list(flags))
        box.configure_host_clipboard_preferences(self.path, self.data, args, version)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.reader.read_selection.assert_not_called()
        return args

    def saved(self):
        return preferences.read(self.path, self.ident)

    def test_default_and_legacy_preferences_stay_off_without_discovery_or_writes(self):
        self.assertFalse(box.effective_host_clipboard(self.resolve()))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        preferences.set_host_drop(self.path, self.ident, False)
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.read_bytes()
        self.assertFalse(box.effective_host_clipboard(self.resolve()))
        self.assertEqual(policy.read_bytes(), before)
        self.check.assert_not_called()
        self.discover.assert_not_called()

    def test_explicit_enable_persists_and_future_clients_inherit_without_rewriting(
        self,
    ):
        enabled = self.resolve("--host-clipboard", "--no-host-drop")
        self.assertIs(enabled.host_clipboard, True)
        self.assertTrue(box.effective_host_clipboard(enabled))
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.stat()
        inherited = self.resolve()
        self.assertIsNone(inherited.host_clipboard)
        self.assertTrue(box.effective_host_clipboard(inherited))
        after = policy.stat()
        self.assertEqual(
            (before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns)
        )
        self.assertTrue(self.saved()["host_clipboard"])
        self.assertEqual(self.discover.call_count, 2)

    def test_disable_persists_without_discovery_and_does_not_change_open_client(self):
        enabled = self.resolve("--host-clipboard")
        self.check.reset_mock()
        self.discover.reset_mock()
        self.assertFalse(
            box.effective_host_clipboard(self.resolve("--no-host-clipboard"))
        )
        self.assertFalse(box.effective_host_clipboard(self.resolve()))
        self.assertFalse(self.saved()["host_clipboard"])
        self.assertTrue(box.effective_host_clipboard(enabled))
        self.check.assert_not_called()
        self.discover.assert_not_called()

    def test_strict_symlink_policy_persists_and_can_be_changed_with_saved_enable(self):
        strict = self.resolve("--host-clipboard", "--no-host-clipboard-symlinks")
        self.assertFalse(box.effective_host_clipboard_symlinks(strict))
        self.assertFalse(box.effective_host_clipboard_symlinks(self.resolve()))
        self.assertFalse(
            box.effective_host_clipboard_symlinks(self.resolve("--host-clipboard"))
        )
        following = self.resolve("--host-clipboard-symlinks")
        self.assertTrue(box.effective_host_clipboard(following))
        self.assertTrue(box.effective_host_clipboard_symlinks(following))
        self.assertTrue(self.saved()["host_clipboard_symlinks"])
        self.assertFalse(box.effective_host_clipboard_symlinks(strict))
        self.resolve("--no-host-clipboard-symlinks")
        self.resolve("--no-host-clipboard")
        self.resolve("--host-clipboard")
        self.assertFalse(box.effective_host_clipboard_symlinks(self.resolve()))

    def test_symlink_options_require_effective_enable_and_conflicting_flags_refuse(
        self,
    ):
        for flag in ("--host-clipboard-symlinks", "--no-host-clipboard-symlinks"):
            with self.subTest(flag=flag), self.assertRaises(box.BoxError):
                self.resolve(flag)
        self.discover.assert_not_called()
        self.assertFalse((self.path.parent / preferences.FILE_NAME).exists())
        for flags in (
            ["--host-clipboard", "--no-host-clipboard"],
            ["--host-clipboard-symlinks", "--no-host-clipboard-symlinks"],
        ):
            with (
                self.subTest(flags=flags),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                box.parser().parse_args(flags)

    def test_explicit_preflight_failure_never_changes_saved_policy(self):
        preferences.set_preferences(self.path, self.ident, host_clipboard=False)
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.read_bytes()
        for failed in ("terminal", "helper"):
            with self.subTest(failed=failed):
                self.check.side_effect = (
                    box.BoxError("Unavailable.") if failed == "terminal" else None
                )
                self.discover.side_effect = (
                    host_clipboard.ClipboardError("Unavailable.")
                    if failed == "helper"
                    else None
                )
                with self.assertRaises(box.BoxError):
                    self.resolve("--host-clipboard")
                self.assertEqual(policy.read_bytes(), before)

    def test_saved_enable_skips_unavailable_client_but_retains_policy(self):
        preferences.set_preferences(
            self.path, self.ident, host_clipboard=True, host_clipboard_symlinks=False
        )
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.read_bytes()
        for version, failure in (
            ("1.19.5", "terminal"),
            ("2.3.2", "terminal"),
            (None, "terminal"),
            ("2.3.2", "helper"),
        ):
            with self.subTest(version=version, failure=failure):
                self.check.side_effect = (
                    box.BoxError("Unavailable.") if failure == "terminal" else None
                )
                self.discover.side_effect = (
                    host_clipboard.ClipboardError("Unavailable.")
                    if failure == "helper"
                    else None
                )
                with patch.object(box, "detect_opencode_release") as detect:
                    args = self.resolve(version=version)
                self.assertFalse(box.effective_host_clipboard(args))
                self.assertFalse(box.effective_host_clipboard_symlinks(args))
                detect.assert_not_called()
                self.assertEqual(policy.read_bytes(), before)

    def test_resolution_is_once_per_client_and_preflight_never_reads_clipboard(self):
        args = self.resolve("--host-clipboard")
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.stat().st_mtime_ns
        box.configure_host_clipboard_preferences(self.path, self.data, args, "2.3.2")
        box.configure_host_clipboard(args, "2.3.2")
        self.discover.assert_called_once()
        self.reader.read_selection.assert_not_called()
        self.assertEqual(policy.stat().st_mtime_ns, before)

    def test_corrupt_or_unsafe_policy_refuses_overrides_before_discovery(self):
        policy = self.path.parent / preferences.FILE_NAME
        for raw, mode in ((b"{invalid", 0o600), (b"{}", 0o644)):
            policy.write_bytes(raw)
            policy.chmod(mode)
            for flags in ((), ("--host-clipboard",), ("--no-host-clipboard",)):
                with (
                    self.subTest(raw=raw, flags=flags),
                    self.assertRaises(box.BoxError),
                ):
                    self.resolve(*flags)
            self.assertEqual(policy.read_bytes(), raw)
        self.discover.assert_not_called()
        self.assertFalse((self.path.parent / preferences.LOCK_NAME).exists())

    def main_fixture(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(box.os.environ, {"OC_BOX_CONTAINER": "1"}))
        stack.enter_context(patch.object(box, "check_backend"))
        stack.enter_context(
            patch.object(box, "state_root", return_value=self.path.parent.parent)
        )
        stack.enter_context(
            patch.object(box, "find_workspace", return_value=(self.path, self.data))
        )
        return stack

    def test_attached_clients_inherit_and_save_without_owner_refresh_or_unlock(self):
        preferences.set_preferences(self.path, self.ident, startup_logo="graphics")
        stack = self.main_fixture()
        connection = {"url": "http://127.0.0.1:12345", "session": None}
        stack.enter_context(
            patch.object(box.workspace_service, "controller_running", return_value=True)
        )
        stack.enter_context(
            patch.object(
                box.workspace_service,
                "attach",
                return_value=contextlib.nullcontext(connection),
            )
        )
        stack.enter_context(
            patch.object(
                box.workspace_service, "client_command", return_value=["native"]
            )
        )
        run = stack.enter_context(patch.object(box, "run_native_tui", return_value=0))
        unlock = stack.enter_context(patch.object(box, "launch_credentials"))
        prepare = stack.enter_context(patch.object(box, "prepare_runtime"))
        for flags, enabled in (
            (["--host-clipboard", "--no-host-clipboard-symlinks"], True),
            ([], True),
            (["--no-host-clipboard"], False),
            ([], False),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(
                    box.main(["--workspace", self.ident, "--no-host-drop", *flags]), 0
                )
                self.assertFalse(run.call_args.kwargs["host_drop_enabled"])
                self.assertEqual(run.call_args.kwargs["startup_logo"], "graphics")
                if enabled:
                    self.assertIs(
                        run.call_args.kwargs["host_clipboard_reader"], self.reader
                    )
                    self.assertFalse(run.call_args.kwargs["host_clipboard_symlinks"])
                else:
                    self.assertNotIn("host_clipboard_reader", run.call_args.kwargs)
        unlock.assert_not_called()
        prepare.assert_not_called()
        self.reader.read_selection.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_owning_start_and_reset_resolve_saved_policy_before_credentials(self):
        preferences.set_preferences(
            self.path, self.ident, host_drop=False, host_clipboard=True
        )
        stack = self.main_fixture()
        stack.enter_context(
            patch.object(
                box.workspace_service, "controller_running", return_value=False
            )
        )
        stack.enter_context(
            patch.object(box, "detect_opencode_release", return_value="2.3.2")
        )
        stack.enter_context(
            patch.object(box, "workspace_lock", return_value=contextlib.nullcontext())
        )
        remove = stack.enter_context(patch.object(box, "remove_resources"))

        class BeforeUnlock(Exception):
            pass

        def unlock(data, args):
            self.assertTrue(box.effective_host_clipboard(args))
            self.assertIs(args._host_clipboard_reader, self.reader)
            raise BeforeUnlock

        stack.enter_context(patch.object(box, "launch_credentials", side_effect=unlock))
        for command in ("start", "reset"):
            with self.subTest(command=command), self.assertRaises(BeforeUnlock):
                box.main([command, "--workspace", self.ident])
        remove.assert_not_called()
        self.reader.read_selection.assert_not_called()

    def test_unrelated_launch_configuration_never_resolves_clipboard_policy(self):
        preferences.set_preferences(self.path, self.ident, host_clipboard=True)
        for command in ("sync", "terminal", "web"):
            with self.subTest(command=command):
                args = box.parser().parse_args([command])
                self.assertIsNone(box.configure_host_clipboard(args, "2.3.2"))
        self.discover.assert_not_called()

    def test_owning_native_client_receives_saved_reader_and_strict_symlinks(self):
        preferences.set_preferences(
            self.path,
            self.ident,
            host_drop=False,
            host_clipboard=True,
            host_clipboard_symlinks=False,
            startup_logo="graphics",
        )
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.read_bytes()
        args = self.resolve("start", "--resume")
        box.configure_host_drop(self.path, self.data, args, "2.3.2")
        self.assertIsNone(args.host_clipboard)
        self.assertIsNone(args.host_clipboard_symlinks)
        data = {**self.data, "distro": "debian"}
        connection = {"url": "http://127.0.0.1:12345"}
        owner = Mock(connection=connection)
        command = ["native-fixture"]
        with (
            patch.object(
                box, "refresh_sessions", return_value={"last": "ses_existing"}
            ),
            patch.object(
                box.workspace_service, "client_command", return_value=command
            ) as client,
            patch.object(box, "run_native_tui", return_value=0) as run,
            patch.object(box.workspace_service, "wait_history"),
            patch.object(box, "sync_sessions"),
        ):
            self.assertEqual(
                box.launch_client(self.path, data, args, "2.3.2", None, owner), 0
            )
        run.assert_called_once_with(
            data,
            command,
            connection,
            host_drop_enabled=False,
            host_drop_required=False,
            host_clipboard_reader=self.reader,
            host_clipboard_symlinks=False,
            startup_logo="graphics",
        )
        self.assertEqual(
            client.call_args.kwargs,
            # Saved clipboard consent never opts a source launch into opening
            # container files in host applications.
            {"interactive": True, "ui_owner": True, "file_preview": False},
        )
        owner.publish.assert_called_once_with("ses_existing")
        owner.wait_clients.assert_called_once_with()
        self.reader.read_selection.assert_not_called()
        self.assertEqual(policy.read_bytes(), before)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_web_attach_ignores_saved_clipboard_without_discovery_or_policy_changes(
        self,
    ):
        preferences.set_preferences(self.path, self.ident, host_clipboard=True)
        policy = self.path.parent / preferences.FILE_NAME
        before = policy.read_bytes()
        stack = self.main_fixture()
        stack.enter_context(patch.dict(box.os.environ, {"OC_BOX_CONTAINER": "0"}))
        stack.enter_context(
            patch.object(box, "detect_opencode_release", return_value="2.3.2")
        )
        stack.enter_context(
            patch.object(
                box, "run", side_effect=AssertionError("unexpected host command")
            )
        )
        connection = {"url": "http://127.0.0.1:12345", "session": None}
        stack.enter_context(
            patch.object(box.workspace_service, "controller_running", return_value=True)
        )
        stack.enter_context(
            patch.object(
                box.workspace_service,
                "attach",
                return_value=contextlib.nullcontext(connection),
            )
        )
        stack.enter_context(
            patch.object(box.workspace_service, "web_session", return_value=0)
        )
        unlock = stack.enter_context(patch.object(box, "launch_credentials"))
        self.assertEqual(box.main(["web", "--workspace", self.ident, "--no-open"]), 0)
        self.discover.assert_not_called()
        unlock.assert_not_called()
        self.assertEqual(policy.read_bytes(), before)

    def test_new_workspace_saves_explicit_choice_before_owner_credentials(self):
        stack = self.main_fixture()
        stack.enter_context(
            patch.object(box, "find_workspace", return_value=(None, None))
        )
        project = Path(self.data["project"])
        stack.enter_context(patch.object(box, "validate_project", return_value=project))
        stack.enter_context(
            patch.object(box, "detect_opencode_release", return_value="2.3.2")
        )
        stack.enter_context(patch.object(box, "check_private_output"))
        create = stack.enter_context(
            patch.object(box, "create_workspace", return_value=(self.path, self.data))
        )
        stack.enter_context(
            patch.object(
                box.workspace_service, "controller_running", return_value=False
            )
        )
        stack.enter_context(
            patch.object(box, "workspace_lock", return_value=contextlib.nullcontext())
        )

        class BeforeUnlock(Exception):
            pass

        def unlock(data, args):
            self.assertTrue(self.saved()["host_clipboard"])
            self.assertFalse(self.saved()["host_clipboard_symlinks"])
            self.assertTrue(box.effective_host_clipboard(args))
            raise BeforeUnlock

        stack.enter_context(patch.object(box, "launch_credentials", side_effect=unlock))
        with self.assertRaises(BeforeUnlock):
            box.main(
                [
                    "--project",
                    str(project),
                    "--distro",
                    "debian",
                    "--host-clipboard",
                    "--no-host-clipboard-symlinks",
                    "--no-host-drop",
                ]
            )
        create.assert_called_once_with(project, "debian")
        self.reader.read_selection.assert_not_called()

    def test_startup_options_reject_unrelated_commands_before_backend(self):
        for command in ("sync", "terminal", "list", "status", "purge"):
            for flag in (
                "--host-clipboard",
                "--no-host-clipboard",
                "--host-clipboard-symlinks",
                "--no-host-clipboard-symlinks",
            ):
                with (
                    self.subTest(command=command, flag=flag),
                    patch.object(box, "check_backend") as backend,
                    self.assertRaises(box.BoxError),
                ):
                    box.main([command, flag])
                backend.assert_not_called()
        self.discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
