import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import patch

import credentials
import oc_box as box
import vault
import secret_prompt
import installation

try:
    vault.crypto()
    HAS_CRYPTO = True
except vault.VaultError:
    HAS_CRYPTO = False

PASSPHRASE = "synthetic long test passphrase"
VALUE = "synthetic-value-never-in-plaintext-storage"


class VaultFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-vault-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "private/vault.json"
        environment = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "OC_BOX_CONTAINER": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.registry, self.data = box.create_workspace(self.project, "tumbleweed")

    def encrypted(self, values=None):
        with vault.locked(self.path, create=True):
            vault.write_encrypted(
                self.path, values or {"EXAMPLE_TOKEN": VALUE}, PASSPHRASE
            )
        return vault.read_envelope(self.path)


@unittest.skipUnless(
    HAS_CRYPTO,
    "optional cryptography not installed; run tests with oc-box[vault]/uv --with cryptography",
)
class EncryptedVaultTests(VaultFixture):
    def test_authenticated_encrypted_roundtrip_and_private_files(self):
        envelope = self.encrypted()
        raw = self.path.read_text()
        self.assertNotIn(VALUE, raw)
        self.assertNotIn("EXAMPLE_TOKEN", raw)
        self.assertNotIn(PASSPHRASE, raw)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(vault.unlock(envelope, PASSPHRASE), {"EXAMPLE_TOKEN": VALUE})
        self.assertEqual(list(self.path.parent.glob(".vault-*")), [])
        second = self.encrypted()
        self.assertNotEqual(second["cipher"]["nonce"], envelope["cipher"]["nonce"])
        self.assertNotEqual(second["kdf"]["salt"], envelope["kdf"]["salt"])
        self.assertNotEqual(second["ciphertext"], envelope["ciphertext"])

    def test_wrong_password_and_modified_ciphertext_fail_without_changes(self):
        envelope = self.encrypted()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(vault.VaultError, "incorrect passphrase"):
            vault.unlock(envelope, "wrong")
        changed = json.loads(json.dumps(envelope))
        ciphertext = bytearray(vault.decode(changed["ciphertext"]))
        ciphertext[0] ^= 1
        changed["ciphertext"] = vault.encode(ciphertext)
        with self.assertRaisesRegex(vault.VaultError, "modified/damaged"):
            vault.unlock(changed, PASSPHRASE)
        self.assertEqual(self.path.read_bytes(), before)

    def test_authenticated_header_salt_and_nonce_changes_do_not_decrypt(self):
        envelope = self.encrypted()
        for field in ("salt", "nonce"):
            changed = json.loads(json.dumps(envelope))
            section = "kdf" if field == "salt" else "cipher"
            raw = bytearray(vault.decode(changed[section][field]))
            raw[0] ^= 1
            changed[section][field] = vault.encode(raw)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(vault.VaultError, "modified/damaged"),
            ):
                vault.unlock(changed, PASSPHRASE)

    def test_failed_or_interrupted_unlock_never_replaces_ciphertext(self):
        self.encrypted()
        before = self.path.read_bytes()
        for failure in (KeyboardInterrupt(), EOFError()):
            with (
                patch.object(vault, "hidden", side_effect=failure),
                self.assertRaises(type(failure)),
            ):
                vault.manage("set", "EXAMPLE_TOKEN", path=self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_hidden_management_set_list_remove_and_passphrase_change(self):
        with (
            patch.object(vault, "hidden", side_effect=[PASSPHRASE, PASSPHRASE]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            vault.manage("init", path=self.path)
        with (
            patch.object(vault, "hidden", side_effect=[PASSPHRASE, VALUE, VALUE]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            vault.manage("set", "EXAMPLE_TOKEN", path=self.path)
        self.assertNotIn(VALUE, output.getvalue())
        with (
            patch.object(vault, "hidden", return_value=PASSPHRASE),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            vault.manage("list", path=self.path)
        self.assertEqual(output.getvalue(), "NAME\nEXAMPLE_TOKEN\n")
        new = "another synthetic passphrase"
        with (
            patch.object(vault, "hidden", side_effect=[PASSPHRASE, new, new]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            vault.manage("passwd", path=self.path)
        self.assertEqual(
            vault.unlock(vault.read_envelope(self.path), new), {"EXAMPLE_TOKEN": VALUE}
        )
        with self.assertRaises(vault.VaultError):
            vault.unlock(vault.read_envelope(self.path), PASSPHRASE)
        with (
            patch.object(vault, "hidden", return_value=new),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            vault.manage("remove", "EXAMPLE_TOKEN", path=self.path)
        self.assertEqual(vault.unlock(vault.read_envelope(self.path), new), {})

    def test_prompt_mismatch_and_duplicate_initialization_never_overwrite(self):
        self.encrypted()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(vault.VaultError, "will not overwrite"):
            vault.manage("init", path=self.path)
        with patch.object(vault, "hidden", side_effect=[PASSPHRASE, VALUE, "mismatch"]):
            with self.assertRaisesRegex(vault.VaultError, "do not match"):
                vault.manage("set", "EXAMPLE_TOKEN", path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_select_delivers_only_named_values_and_requires_presence(self):
        self.encrypted({"EXAMPLE_TOKEN": VALUE, "OTHER_TOKEN": "other"})
        with patch.object(vault, "hidden", return_value=PASSPHRASE):
            self.assertEqual(
                vault.select(["EXAMPLE_TOKEN"], path=self.path),
                {"EXAMPLE_TOKEN": VALUE},
            )
            with self.assertRaisesRegex(vault.VaultError, "missing"):
                vault.select(["MISSING_TOKEN"], path=self.path)

    def test_atomic_write_failure_retains_original_ciphertext(self):
        self.encrypted()
        before = self.path.read_bytes()
        with patch.object(vault.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                vault.write_encrypted(
                    self.path, {"EXAMPLE_TOKEN": "replacement"}, PASSPHRASE
                )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob(".vault-*")))

    def test_tampered_kdf_parameters_rejected_before_derivation(self):
        envelope = self.encrypted()
        envelope["kdf"]["n"] = 2**40
        self.path.write_text(json.dumps(envelope))
        with patch.object(vault, "derive") as derive:
            with self.assertRaisesRegex(vault.VaultError, "damaged vault"):
                vault.read_envelope(self.path)
        derive.assert_not_called()

    def tty_command(self, command, answers):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "XDG_DATA_HOME": str(self.root / "tty-data"),
            "OC_BOX_CONTAINER": "0",
            "PYTHONNOUSERSITE": "1",
        }
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve(
                sys.executable,
                [sys.executable, "-B", str(box.APP / "oc_box.py"), *command],
                env,
            )
        output = bytearray()
        status = None
        step = 0
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if step < len(answers) and answers[step][0].encode() in output:
                        os.write(terminal, answers[step][1].encode() + b"\n")
                        step += 1
                waited = os.waitpid(pid, os.WNOHANG)
                if waited[0]:
                    status = waited[1]
                    break
            if status is None:
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
            self.assertEqual(step, len(answers), bytes(output))
            self.assertEqual(status, 0, bytes(output))
            for _, value in answers:
                self.assertNotIn(value.encode(), output)
            return bytes(output).decode()
        finally:
            os.close(terminal)

    def test_real_controlling_terminal_init_set_and_list_never_echo_plaintext(self):
        self.tty_command(
            ["secrets", "init"],
            [
                ("New vault passphrase", PASSPHRASE),
                ("Confirm vault passphrase", PASSPHRASE),
            ],
        )
        self.tty_command(
            ["secrets", "set", "EXAMPLE_TOKEN"],
            [
                ("Vault passphrase", PASSPHRASE),
                ("Secret value:", VALUE),
                ("Confirm secret value", VALUE),
            ],
        )
        output = self.tty_command(
            ["secrets", "list"], [("Vault passphrase", PASSPHRASE)]
        )
        self.assertIn("EXAMPLE_TOKEN", output)
        saved = self.root / "tty-data/oc-box/credentials/vault.json"
        self.assertNotIn(VALUE, saved.read_text())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)


class VaultPolicyTests(VaultFixture):
    def test_env_workflow_is_default_and_never_opens_vault_or_loads_crypto(self):
        with (
            patch.dict(os.environ, {"EXAMPLE_TOKEN": VALUE}),
            patch.object(vault, "select") as unlock,
            patch.object(vault, "crypto") as crypto,
        ):
            with credentials.unlocked(self.data):
                self.assertEqual(box.container_env(self.data)["EXAMPLE_TOKEN"], VALUE)
        unlock.assert_not_called()
        crypto.assert_not_called()
        self.assertNotIn("credentials", self.data)

    def test_selected_mode_excludes_automatic_tokens_and_custom_pass_env(self):
        settings = self.root / "config/oc-box/config.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"pass_env":["UNRELATED_SECRET"]}')
        credentials.configure(
            self.data, mode="selected", env=["EXAMPLE_TOKEN"], secrets=["VAULT_TOKEN"]
        )
        with (
            patch.dict(
                os.environ,
                {
                    "EXAMPLE_TOKEN": VALUE,
                    "OTHER_API_KEY": "unrelated",
                    "UNRELATED_SECRET": "custom",
                },
            ),
            patch.object(vault, "select", return_value={"VAULT_TOKEN": "decrypted"}),
        ):
            with credentials.unlocked(self.data):
                env = box.container_env(self.data)
                command = box.exec_command(self.data, ["opencode"])
                self.assertEqual(env["EXAMPLE_TOKEN"], VALUE)
                self.assertEqual(env["VAULT_TOKEN"], "decrypted")
                self.assertNotIn("OTHER_API_KEY", env)
                self.assertNotIn("UNRELATED_SECRET", env)
                self.assertNotIn("decrypted", " ".join(command))
                self.assertEqual(command.environment["VAULT_TOKEN"], "decrypted")
                self.assertNotIn("VAULT_TOKEN", os.environ)
            self.assertNotIn("VAULT_TOKEN", box.container_env(self.data))

    def test_env_mode_can_add_vault_names_without_changing_existing_workflow(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        with (
            patch.dict(os.environ, {"EXAMPLE_TOKEN": VALUE}),
            patch.object(vault, "select", return_value={"VAULT_TOKEN": "decrypted"}),
        ):
            with credentials.unlocked(self.data):
                self.assertEqual(box.container_env(self.data)["EXAMPLE_TOKEN"], VALUE)
                self.assertEqual(
                    box.container_env(self.data)["VAULT_TOKEN"], "decrypted"
                )

    def test_launch_unlock_is_scoped_cleared_on_error_and_not_shared_to_other_workspace(
        self,
    ):
        values = {"VAULT_TOKEN": "decrypted"}
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        other = {**self.data, "id": "b" * 32}
        with patch.object(vault, "select", return_value=values) as unlock:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                with credentials.unlocked(self.data):
                    with credentials.unlocked(self.data):
                        self.assertEqual(
                            box.container_env(self.data)["VAULT_TOKEN"], "decrypted"
                        )
                        self.assertNotIn("VAULT_TOKEN", box.container_env(other))
                        raise RuntimeError("failed")
        self.assertEqual(values, {})
        unlock.assert_called_once()
        self.assertEqual(credentials.active_values(self.data), {})

    def test_missing_selected_env_stops_before_unlock_and_reset_destruction(self):
        credentials.configure(
            self.data,
            mode="selected",
            env=["UNSET_TEST_TOKEN"],
            secrets=["VAULT_TOKEN"],
        )
        box.atomic_json(self.registry, self.data)
        with (
            patch.object(vault, "select") as unlock,
            patch.object(box, "check_backend"),
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(box.BoxError, "unset/empty"):
                box.main(["reset", "--workspace", self.data["id"]])
        unlock.assert_not_called()
        remove.assert_not_called()

    def test_invalid_or_dangerous_names_and_values_fail_without_echoing_values(self):
        for name in (
            "invalid-name",
            "lowercase",
            "PATH",
            "BASH_ENV",
            "LD_PRELOAD",
            "OPENCODE_AUTH_CONTENT",
            "PYTHONPATH",
            "OC_BOX_TOKEN",
            "SSH_AUTH_SOCK",
            "HTTP_PROXY",
            "GIT_CONFIG_COUNT",
        ):
            with self.subTest(name=name), self.assertRaises(vault.VaultError):
                vault.validate_name(name)
        for value in ("", "contains\0nul", "x" * (vault.MAX_SECRET + 1)):
            with self.assertRaises(vault.VaultError) as raised:
                vault.validate_value(value)
            self.assertNotIn("contains", str(raised.exception))

    def test_configuration_saves_only_names_and_keeps_default_mode(self):
        with (
            patch.dict(os.environ, {"EXAMPLE_TOKEN": VALUE}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                box.main(
                    [
                        "credentials",
                        "--workspace",
                        self.data["id"],
                        "--vault-secret",
                        "VAULT_TOKEN",
                    ]
                ),
                0,
            )
        raw = self.registry.read_text()
        self.assertIn('"mode": "env"', raw)
        self.assertIn("VAULT_TOKEN", raw)
        self.assertNotIn(VALUE, raw)
        self.assertNotIn(VALUE, output.getvalue())
        self.assertFalse(vault.vault_path().exists())
        with contextlib.redirect_stdout(io.StringIO()):
            box.main(
                [
                    "credentials",
                    "--workspace",
                    self.data["id"],
                    "--credential-mode=selected",
                    "--clear-credentials",
                    "--env-secret=EXAMPLE_TOKEN",
                ]
            )
        self.assertEqual(
            box.read_json(self.registry)["credentials"],
            {"mode": "selected", "env": ["EXAMPLE_TOKEN"], "vault": []},
        )

    def test_conflicting_sources_and_invalid_policy_do_not_write(self):
        before = self.registry.read_bytes()
        with self.assertRaises(box.BoxError):
            box.main(
                [
                    "credentials",
                    "--workspace",
                    self.data["id"],
                    "--env-secret=EXAMPLE_TOKEN",
                    "--vault-secret=EXAMPLE_TOKEN",
                ]
            )
        self.assertEqual(self.registry.read_bytes(), before)
        with self.assertRaises(vault.VaultError):
            credentials.policy({"credentials": {"mode": "unknown"}})

    def test_credentials_configuration_obeys_workspace_lock(self):
        with box.workspace_lock(self.registry):
            with self.assertRaisesRegex(box.BoxError, "active oc-box"):
                box.main(["credentials", "--workspace", self.data["id"]])

    def test_vault_host_only_and_commands_never_require_podman(self):
        with (
            patch.object(vault, "manage") as manage,
            patch.object(box, "check_backend") as backend,
        ):
            self.assertEqual(box.main(["secrets", "list"]), 0)
        manage.assert_called_once_with("list", None)
        backend.assert_not_called()
        for arguments in (
            ["secrets", "set"],
            ["secrets", "list", "EXAMPLE_TOKEN"],
            ["--vault-secret=EXAMPLE_TOKEN"],
            ["credentials", "--new"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(box.BoxError):
                box.main(arguments)
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            for arguments in (
                ["secrets", "list"],
                ["credentials", "--workspace", self.data["id"]],
            ):
                with self.assertRaisesRegex(box.BoxError, "host"):
                    box.main(arguments)

    def test_informational_terminal_and_sync_operations_never_unlock(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        box.atomic_json(self.registry, self.data)
        with patch.object(vault, "select") as unlock:
            with contextlib.redirect_stdout(io.StringIO()):
                box.main(["list"])
                box.main(["distros"])
                box.main(["credentials", "--workspace", self.data["id"]])
            box.maintenance_command(self.data)
            with patch.object(box, "launch_environment", return_value=0):
                self.assertEqual(
                    box.launch(
                        self.registry,
                        self.data,
                        type("Args", (), {"command": "sync"})(),
                    ),
                    0,
                )
            unlock.assert_not_called()

    def test_vault_directory_cannot_be_exposed_as_project(self):
        target = vault.vault_path().parent
        target.mkdir(parents=True)
        with self.assertRaisesRegex(box.BoxError, "trusted application/state"):
            box.validate_project(target)

    def test_plaintext_is_not_url_rewritten(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        value = "http://localhost:1234/private"
        with patch.object(vault, "select", return_value={"VAULT_TOKEN": value}):
            with credentials.unlocked(self.data):
                self.assertEqual(box.container_env(self.data)["VAULT_TOKEN"], value)

    def test_launcher_failure_redacts_any_active_decrypted_value(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])

        def child(*args, **kwargs):
            kwargs["stderr"].write(("unexpected failure " + VALUE).encode())
            return subprocess.CompletedProcess([], 1)

        with (
            patch.object(vault, "select", return_value={"VAULT_TOKEN": VALUE}),
            credentials.unlocked(self.data),
            patch.object(box.subprocess, "run", side_effect=child),
        ):
            with self.assertRaises(box.BoxError) as error:
                box.run(["podman"], capture=True)
            self.assertNotIn(VALUE, str(error.exception))
            self.assertIn("redacted credential", str(error.exception))

    def test_vault_child_output_uses_anonymous_memory_not_plaintext_temp_files(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        with (
            patch.object(vault, "select", return_value={"VAULT_TOKEN": VALUE}),
            credentials.unlocked(self.data),
            patch.object(
                box.tempfile,
                "TemporaryFile",
                side_effect=AssertionError(
                    "No plaintext temporary files in vault capture"
                ),
            ),
        ):
            result = box.run(
                [
                    sys.executable,
                    "-c",
                    "import os,stat,sys; assert stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode); print('ok')",
                ],
                capture=True,
            )
            self.assertEqual(result.stdout.strip(), "ok")

    def test_crypto_installer_child_environment_contains_no_host_credentials(self):
        with (
            patch.dict(
                os.environ, {"PERSONAL_GITHUB_TOKEN": VALUE, "CUSTOM_SECRET": "custom"}
            ),
            patch.object(installation.venv, "EnvBuilder") as venv,
            patch.object(
                installation.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as execute,
        ):
            installation.optional_crypto_runtime(self.root)
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", execute.call_args.kwargs["env"])
        self.assertNotIn("CUSTOM_SECRET", execute.call_args.kwargs["env"])
        command = execute.call_args.args[0]
        self.assertIn("--target", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-cache-dir", command)
        self.assertNotIn(VALUE, " ".join(command))
        venv.assert_called_once_with(with_pip=True)

    def test_optional_installer_stages_crypto_before_activating(self):
        home = self.root / "home"
        with (
            patch.object(installation, "optional_crypto_runtime") as crypto,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            release = installation.install(Path(box.APP), home, with_vault=True)
        crypto.assert_called_once_with(release)
        self.assertTrue((release / "vault-support.json").is_file())
        self.assertIn(str(release), (home / ".local/bin/oc-box").read_text())

    def test_optional_crypto_failure_retains_the_existing_launcher(self):
        home = self.root / "home"
        with contextlib.redirect_stdout(io.StringIO()):
            previous = installation.install(Path(box.APP), home)
        binary = home / ".local/bin/oc-box"
        before = binary.read_bytes()
        with patch.object(
            installation,
            "optional_crypto_runtime",
            side_effect=ValueError("download failed"),
        ):
            with self.assertRaisesRegex(ValueError, "download failed"):
                installation.install(Path(box.APP), home, with_vault=True)
        self.assertEqual(binary.read_bytes(), before)
        self.assertEqual((home / ".local/share/oc-box/current").resolve(), previous)

    def test_wrong_vault_passphrase_aborts_reset_before_removing_any_resources(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        box.atomic_json(self.registry, self.data)
        with (
            patch.object(
                vault, "select", side_effect=vault.VaultError("unable to unlock")
            ),
            patch.object(box, "check_backend"),
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(box.BoxError, "unable to unlock"):
                box.main(["reset", "--workspace", self.data["id"]])
        remove.assert_not_called()
        self.assertTrue(self.registry.exists())

    def test_launch_unlock_happens_before_version_probe_or_container_setup(self):
        credentials.configure(self.data, secrets=["VAULT_TOKEN"])
        with (
            patch.object(
                vault, "select", side_effect=vault.VaultError("unable to unlock")
            ),
            patch.object(box, "launch_environment") as launch,
        ):
            with self.assertRaisesRegex(box.BoxError, "unable to unlock"):
                box.launch(
                    self.registry, self.data, type("Args", (), {"command": "start"})()
                )
        launch.assert_not_called()


class VaultFilesystemTests(VaultFixture):
    def test_symlinks_hardlinks_permissions_and_nonregular_files_are_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.root / "other"
        target.write_text("{}")
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            vault.read_envelope(self.path)
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaisesRegex(vault.VaultError, "hard links"):
            vault.read_envelope(self.path)
        self.path.unlink()
        self.path.write_text("{}")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(vault.VaultError, "0600"):
            vault.read_envelope(self.path)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(vault.VaultError):
            vault.read_envelope(self.path)

    def test_symlink_or_insecure_private_directory_is_not_used(self):
        self.path.parent.mkdir(mode=0o755)
        with self.assertRaisesRegex(vault.VaultError, "0700"):
            with vault.locked(self.path):
                pass
        link = self.root / "link"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaisesRegex(vault.VaultError, "symbolic links"):
            with vault.locked(link / "vault.json", create=True):
                pass

    def test_hidden_input_refuses_no_controlling_terminal(self):
        with (
            patch.object(vault.os, "open", side_effect=OSError("no tty")),
            patch.object(
                secret_prompt, "read_secret", wraps=secret_prompt.read_secret
            ) as prompt,
        ):
            with self.assertRaisesRegex(vault.VaultError, "controlling host terminal"):
                vault.hidden("Secret: ")
        prompt.assert_called_once()

    def test_missing_crypto_is_reported_without_initializing_vault(self):
        with (
            patch.object(
                vault,
                "crypto",
                side_effect=vault.VaultError("optional dependency missing"),
            ),
            patch.object(vault, "hidden") as prompt,
        ):
            with self.assertRaisesRegex(vault.VaultError, "optional dependency"):
                vault.manage("init", path=self.path)
        self.assertFalse(self.path.exists())
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
