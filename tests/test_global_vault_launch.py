"""Owner-only global-vault lifecycle contracts; no actual Podman or host state."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

import credentials
import oc_box as box
import vault
import workspace_service

PREFERRED_TEMP = Path("/tmp/opencode")


class GlobalVaultLaunchTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-global-launch-",
            dir=PREFERRED_TEMP if PREFERRED_TEMP.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "OC_BOX_CONTAINER": "0",
                "BRAVE_API_KEY": "synthetic-ambient-brave",
                "UNRELATED_TOKEN": "synthetic-ambient-unrelated",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        (self.root / "runtime").mkdir(mode=0o700)
        self.value = "synthetic-global-value-not-a-real-key"
        # Presence is real; the decrypt/prompt operation is mocked below. This
        # placeholder never contains a plaintext secret or any real user data.
        target = vault.vault_path()
        target.parent.mkdir(parents=True, mode=0o700)
        target.write_text("{}")
        target.chmod(0o600)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(box, "check_backend"))
        self.stack.enter_context(
            patch.object(workspace_service, "controller_running", return_value=False)
        )
        self.release = self.stack.enter_context(
            patch.object(box, "detect_opencode_release", return_value="2.2.6")
        )
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_missing_preferred_temp_directory_uses_standard_fallback(self):
        missing = self.root / "missing-temp-parent"
        self.assertFalse(missing.exists())
        fixture = GlobalVaultLaunchTests(
            "test_v1_still_forwards_environment_and_never_unlocks_global_vault"
        )
        try:
            with patch(__name__ + ".PREFERRED_TEMP", missing):
                fixture.setUp()
            self.assertEqual(fixture.root.parent, Path(tempfile.gettempdir()))
            self.assertFalse(missing.exists())
        finally:
            fixture.doCleanups()

    def existing(self, release="2.2.6"):
        path, data = box.create_workspace(self.project, "tumbleweed")
        data.update(opencode_version=release, ready=True, devices=[], podman_args=[])
        box.atomic_json(path, data)
        return path, data

    def test_first_import_receives_global_values_before_export_or_container_work(self):
        observed = []

        def launch(path, data, args):
            self.assertEqual(args._opencode_release, "2.2.6")
            self.assertEqual(args.import_session, "ses_synthetic_connect")
            env = box.container_env(data)
            self.assertEqual(env["BRAVE_API_KEY"], self.value)
            self.assertNotIn("UNRELATED_TOKEN", env)
            self.assertNotIn(self.value, json.dumps(data))
            self.assertNotIn(self.value, json.dumps(vars(args), default=str))
            self.assertNotIn(self.value, path.read_text())
            self.assertNotIn("BRAVE_API_KEY", path.read_text())
            observed.append(data["id"])
            return 0

        with (
            patch.object(
                vault, "select_all", side_effect=lambda: {"BRAVE_API_KEY": self.value}
            ) as unlock,
            patch.object(box, "launch_environment", side_effect=launch),
        ):
            self.assertEqual(
                box.main(
                    [
                        "--project",
                        str(self.project),
                        "--import-session",
                        "ses_synthetic_connect",
                    ]
                ),
                0,
            )
        unlock.assert_called_once()
        self.release.assert_called_once()
        self.assertEqual(len(observed), 1)
        self.assertFalse(credentials.is_active({"id": observed[0]}))
        self.assertEqual(os.environ["BRAVE_API_KEY"], "synthetic-ambient-brave")

    def test_failed_first_unlock_keeps_original_files_without_starting_or_exporting(
        self,
    ):
        original = self.project / "important.txt"
        original.write_text("synthetic project content")
        with (
            patch.object(
                vault, "select_all", side_effect=vault.VaultError("unable to unlock")
            ),
            patch.object(box, "launch_environment") as launch,
            patch.object(box, "export_host_session") as export,
            self.assertRaisesRegex(box.BoxError, "unable to unlock"),
        ):
            box.main(
                [
                    "--project",
                    str(self.project),
                    "--import-session",
                    "ses_synthetic_connect",
                ]
            )
        launch.assert_not_called()
        export.assert_not_called()
        self.assertEqual(original.read_text(), "synthetic project content")
        path, data = box.find_workspace(self.project)
        self.assertTrue(path.is_file())
        self.assertNotIn("credentials", data)
        self.assertNotIn(self.value, path.read_text())

    def test_reset_uses_current_major_before_global_unlock_or_destruction(self):
        path, data = self.existing("1.19.5")
        before = path.read_bytes()
        with (
            patch.object(
                vault, "select_all", side_effect=vault.VaultError("unable to unlock")
            ) as unlock,
            patch.object(box, "remove_resources") as remove,
            patch.object(box, "launch_environment") as launch,
            self.assertRaisesRegex(box.BoxError, "unable to unlock"),
        ):
            box.main(["reset", "--workspace", data["id"]])
        unlock.assert_called_once()
        remove.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_cli_switch_during_reset_is_rejected_before_resource_removal(self):
        path, data = self.existing()
        before = path.read_bytes()
        self.release.side_effect = ["2.2.6", "1.19.5"]
        with (
            patch.object(
                vault, "select_all", side_effect=lambda: {"BRAVE_API_KEY": self.value}
            ),
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "release changed"),
        ):
            box.main(["reset", "--workspace", data["id"]])
        remove.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(credentials.is_active(data))

    def test_oversized_global_environment_aborts_reset_before_resource_removal(self):
        path, data = self.existing()
        before = path.read_bytes()
        values = {
            f"FIXTURE_{index}_TOKEN": "x" * vault.MAX_SECRET for index in range(16)
        }
        with (
            patch.object(vault, "select_all", return_value=values),
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "environment size limit"),
        ):
            box.main(["reset", "--workspace", data["id"]])
        remove.assert_not_called()
        self.assertEqual(values, {})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(credentials.is_active(data))

    def test_no_vault_is_one_launch_compatibility_not_a_saved_policy(self):
        path, data = self.existing()
        before = path.read_bytes()

        def launch(path, data, args):
            env = box.container_env(data)
            self.assertEqual(env["BRAVE_API_KEY"], "synthetic-ambient-brave")
            self.assertEqual(env["UNRELATED_TOKEN"], "synthetic-ambient-unrelated")
            self.assertNotIn("credentials", data)
            self.assertTrue(args.no_vault)
            return 0

        with (
            patch.object(vault, "select_all") as unlock,
            patch.object(box, "launch_environment", side_effect=launch),
        ):
            self.assertEqual(box.main(["--workspace", data["id"], "--no-vault"]), 0)
        unlock.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(credentials.policy(data)["mode"], "vault")

    def test_v1_still_forwards_environment_and_never_unlocks_global_vault(self):
        path, data = self.existing("1.19.5")
        self.release.return_value = "1.19.5"

        def launch(path, data, args):
            env = box.container_env(data)
            self.assertEqual(env["BRAVE_API_KEY"], "synthetic-ambient-brave")
            self.assertEqual(env["UNRELATED_TOKEN"], "synthetic-ambient-unrelated")
            return 0

        with (
            patch.object(vault, "select_all") as unlock,
            patch.object(box, "launch_environment", side_effect=launch),
        ):
            self.assertEqual(box.main(["--workspace", data["id"]]), 0)
        unlock.assert_not_called()
        self.assertNotIn("credentials", box.read_json(path))

    def test_no_vault_on_v1_fails_before_launch_or_reset(self):
        _, data = self.existing("1.19.5")
        self.release.return_value = "1.19.5"
        with (
            patch.object(box, "launch_environment") as launch,
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "2.x|V2|2\\."),
        ):
            box.main(["reset", "--workspace", data["id"], "--no-vault"])
        launch.assert_not_called()
        remove.assert_not_called()

    def test_no_vault_on_fresh_v1_does_not_register_a_workspace(self):
        self.release.return_value = "1.19.5"
        with (
            patch.object(vault, "select_all") as unlock,
            patch.object(box, "create_workspace") as create,
            self.assertRaisesRegex(box.BoxError, "native OpenCode 2.x"),
        ):
            box.main(["--project", str(self.project), "--no-vault"])
        create.assert_not_called()
        unlock.assert_not_called()
        self.assertFalse((self.project / box.MARKER).exists())

    def test_unrelated_no_vault_options_fail_without_probing_or_unlocking(self):
        cases = (
            ["list"],
            ["distros"],
            ["status"],
            ["--version"],
            ["--about"],
            ["--status"],
            ["credentials"],
            ["sync"],
            ["stop"],
            ["repair"],
            ["terminal"],
            ["purge"],
            ["upgrade"],
        )
        with patch.object(vault, "select_all") as unlock:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(box.BoxError):
                    box.main([*args, "--no-vault"])
        self.release.assert_not_called()
        unlock.assert_not_called()

    def test_attached_clients_cannot_change_vault_policy(self):
        _, data = self.existing()
        with (
            patch.object(workspace_service, "controller_running", return_value=True),
            patch.object(vault, "select_all") as unlock,
            patch.object(workspace_service, "attach") as attach,
        ):
            with self.assertRaisesRegex(box.BoxError, "active V2 workspace"):
                box.main(["--workspace", data["id"], "--no-vault"])
            self.release.assert_not_called()
            with self.assertRaisesRegex(box.BoxError, "attached web client"):
                box.main(["web", "--workspace", data["id"], "--no-vault"])
        unlock.assert_not_called()
        attach.assert_not_called()

    def test_explicit_old_selected_unlock_order_is_preserved(self):
        _, data = self.existing("1.19.5")
        credentials.configure(data, mode="selected", secrets=["EXAMPLE_TOKEN"])
        path, _ = box.find_workspace(self.project)
        box.atomic_json(path, data)
        with (
            patch.object(
                vault, "select", side_effect=vault.VaultError("unable to unlock")
            ) as select,
            patch.object(vault, "select_all") as global_unlock,
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "unable to unlock"),
        ):
            box.main(["reset", "--workspace", data["id"]])
        select.assert_called_once_with(["EXAMPLE_TOKEN"])
        global_unlock.assert_not_called()
        self.release.assert_not_called()
        remove.assert_not_called()

    def test_global_launch_ignores_unselected_legacy_pass_env_configuration(self):
        path, data = self.existing()
        settings = self.root / "config/oc-box/config.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"pass_env":[123]}')
        with patch.object(
            vault, "select_all", side_effect=lambda: {"BRAVE_API_KEY": self.value}
        ):
            with box.launch_credentials(data, box.parser().parse_args([])):
                self.assertEqual(box.container_env(data)["BRAVE_API_KEY"], self.value)
        self.assertFalse(credentials.is_active(data))

    def test_reusing_launch_arguments_does_not_cache_an_obsolete_major(self):
        _, data = self.existing()
        args = box.parser().parse_args([])
        with patch.object(
            vault, "select_all", side_effect=lambda: {"BRAVE_API_KEY": self.value}
        ) as unlock:
            with box.launch_credentials(data, args):
                self.assertEqual(args._opencode_release, "2.2.6")
            self.assertFalse(hasattr(args, "_opencode_release"))
            self.release.return_value = "1.19.5"
            with box.launch_credentials(data, args):
                self.assertEqual(args._opencode_release, "1.19.5")
                self.assertEqual(
                    box.container_env(data)["BRAVE_API_KEY"], "synthetic-ambient-brave"
                )
        unlock.assert_called_once()
        self.assertFalse(hasattr(args, "_opencode_release"))

    def test_invalid_reset_option_is_rejected_before_unlock_or_resource_removal(self):
        _, data = self.existing()
        with (
            patch.object(vault, "select_all") as unlock,
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "--pids-limit"),
        ):
            box.main(["reset", "--workspace", data["id"], "--pids-limit", "1"])
        self.release.assert_not_called()
        unlock.assert_not_called()
        remove.assert_not_called()

    def test_direct_sync_cannot_silently_ignore_vault_opt_out(self):
        path, data = self.existing()
        args = box.parser().parse_args(["sync", "--no-vault"])
        with (
            patch.object(vault, "select_all") as unlock,
            patch.object(box, "launch_environment") as launch,
            self.assertRaisesRegex(box.BoxError, "history-sync"),
        ):
            box.launch(path, data, args)
        unlock.assert_not_called()
        launch.assert_not_called()

    def test_global_mode_preserves_native_oauth_without_legacy_env_auth_override(self):
        path, data = self.existing()
        auth = self.root / "data/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("unused-legacy-v2-auth-file")
        entries = [
            {
                "id": "fixture",
                "integrationID": "fixture",
                "label": "Fixture",
                "value": {"type": "oauth", "access": "synthetic-native-oauth"},
            }
        ]

        def execute(command, **kwargs):
            if "auth" in command and "export" in command:
                return subprocess.CompletedProcess([], 0, json.dumps(entries), "")
            return subprocess.CompletedProcess([], 0, "", "")

        original_read = Path.read_text

        def read_text(target, *args, **kwargs):
            if target == auth:
                raise AssertionError("V2 must not read unused legacy auth.json")
            return original_read(target, *args, **kwargs)

        with (
            patch.dict(
                os.environ, {"OPENCODE_AUTH_CONTENT": "unused-invalid-legacy-env-blob"}
            ),
            patch.object(
                vault, "select_all", side_effect=lambda: {"BRAVE_API_KEY": self.value}
            ),
            patch.object(box, "ensure_opencode_version"),
            patch.object(box, "config_archive", return_value=b"fixture-config"),
            patch.object(Path, "read_text", read_text),
            patch.object(box, "run", side_effect=execute) as run,
            patch.object(
                box.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, b"Reusing container plugin dependencies.\n", b""
                ),
            ),
            patch.object(
                box.opencode_compat,
                "legacy_credentials",
                side_effect=AssertionError(
                    "No ambient legacy overrides in global mode"
                ),
            ),
            credentials.unlocked(data),
        ):
            box.prepare_runtime(path, data, "2.2.6")
        exports = [
            call
            for call in run.call_args_list
            if "auth" in call.args[0] and "export" in call.args[0]
        ]
        imports = [
            call
            for call in run.call_args_list
            if call.kwargs.get("input") == json.dumps(entries)
        ]
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(imports), 1)
        self.assertTrue(exports[0].kwargs["private"])
        self.assertTrue(imports[0].kwargs["private"])
        self.assertNotIn(self.value, exports[0].kwargs["env"].values())
