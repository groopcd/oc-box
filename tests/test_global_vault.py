"""Synthetic host fixtures only; never inspect a user's vault or workspace."""

import contextlib
from contextvars import Context
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import credentials, oc_box as box, vault


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
PASSPHRASE = "synthetic-global-vault-test-passphrase"
VALUES = {
    "SYNTHETIC_A_TOKEN": "synthetic-global-secret-a",
    "SYNTHETIC_PRIVATE_SETTING": "http://localhost:8123/synthetic-global-secret-b",
}


class SyntheticFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-global-vault-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "OC_BOX_CONTAINER": "0",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / "data/oc-box/credentials/vault.json"
        self.data = {
            "id": "a" * 32,
            "project": str(self.root / "synthetic-project"),
            "home": "/home/synthetic",
            "container": "oc-box-synthetic-global-fixture",
            "opencode_version": "2.2.6",
        }
        self.legacy = {
            "AMBIENT_TOKEN": "synthetic-ambient-token",
            "AMBIENT_API_KEY": "synthetic-ambient-api-key",
            "CUSTOM_PASS_ENV": "synthetic-custom-pass-env",
            "COLORTERM": "truecolor",
            "LANG": "C.UTF-8",
            "LEMONADE_BASE_URL": "http://localhost:13305/api/v1",
        }

    def saved(self, mode, *, env=(), secrets=()):
        self.data["credentials"] = {
            "mode": mode,
            "env": list(env),
            "vault": list(secrets),
        }

    def encrypted(self, values):
        with vault.locked(self.path, create=True):
            vault.write_encrypted(self.path, values, PASSPHRASE)

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def assert_clean(self):
        self.assertFalse(credentials.is_active(self.data))
        self.assertFalse(credentials.has_unlocked_values())
        self.assertEqual(credentials.active_values(self.data), {})
        self.assertIsNone(credentials._EFFECTIVE_POLICY.get())
        self.assertIsNone(credentials._UNLOCKED.get())


class GlobalPolicyTests(SyntheticFixture):
    def test_defaults_follow_runtime_version_then_saved_version_then_v1(self):
        for data, runtime, mode in (
            ({}, None, "env"),
            ({"opencode_version": "1.4.2"}, None, "env"),
            ({"opencode_version": "2.2.6"}, None, "vault"),
            ({}, "2.2.6", "vault"),
            ({"opencode_version": "1.4.2"}, "2.2.6", "vault"),
            ({"opencode_version": "2.2.6"}, "1.4.2", "env"),
        ):
            before = copy.deepcopy(data)
            with self.subTest(data=data, runtime=runtime):
                self.assertEqual(
                    credentials.policy(data, version=runtime),
                    {"mode": mode, "env": [], "vault": []},
                )
                self.assertEqual(data, before)
        self.assertEqual(self.snapshot(), {})

    def test_explicit_modes_override_runtime_default_and_return_private_copies(self):
        for mode in ("env", "selected", "vault"):
            self.saved(mode)
            for version in ("1.4.2", "2.2.6"):
                with self.subTest(mode=mode, version=version):
                    result = credentials.policy(self.data, version=version)
                    self.assertEqual(result["mode"], mode)
                    result["env"].append("SYNTHETIC_TOKEN")
                    self.assertEqual(self.data["credentials"]["env"], [])

    def test_nonempty_global_lists_refuse_names_with_fixed_conversion_guidance(self):
        for source in ("env", "vault"):
            self.saved("vault")
            self.data["credentials"][source] = ["SYNTHETIC_PRIVATE_NAME"]
            with (
                self.subTest(source=source),
                self.assertRaises(vault.VaultError) as error,
            ):
                credentials.policy(self.data)
            message = str(error.exception)
            self.assertIn("selected/env", message)
            self.assertIn("--clear-credentials", message)
            self.assertNotIn("SYNTHETIC_PRIVATE_NAME", message)

    def test_conversion_requires_clear_and_never_saves_an_automatic_list(self):
        for mode in ("env", "selected"):
            self.saved(mode, env=["SYNTHETIC_ENV"], secrets=["SYNTHETIC_TOKEN"])
            before = copy.deepcopy(self.data)
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(vault.VaultError, "--clear-credentials"):
                    credentials.configure(self.data, mode="vault")
                self.assertEqual(self.data, before)
                self.assertEqual(
                    credentials.configure(self.data, mode="vault", clear=True),
                    {"mode": "vault", "env": [], "vault": []},
                )
        before = copy.deepcopy(self.data)
        with self.assertRaisesRegex(vault.VaultError, "selected/env"):
            credentials.configure(self.data, secrets=["SYNTHETIC_TOKEN"])
        self.assertEqual(self.data, before)

    def test_explicit_intent_repairs_only_known_global_selection_incompatibility(self):
        for source in ("env", "vault"):
            for options, expected_mode in (
                ({"clear": True}, "vault"),
                ({"mode": "env"}, "env"),
                ({"mode": "selected"}, "selected"),
                ({"mode": "vault", "clear": True}, "vault"),
            ):
                with self.subTest(source=source, options=options):
                    self.saved("vault")
                    self.data["credentials"][source] = ["SYNTHETIC_TOKEN"]
                    self.data["unrelated"] = {"keep": ["metadata"]}
                    before = copy.deepcopy(self.data)
                    with (
                        patch.object(vault, "select") as selected,
                        patch.object(vault, "select_all") as all_entries,
                    ):
                        result = credentials.configure(self.data, **options)
                    self.assertEqual(result["mode"], expected_mode)
                    self.assertEqual(
                        result[source],
                        [] if options.get("clear") else ["SYNTHETIC_TOKEN"],
                    )
                    self.assertEqual(self.data["unrelated"], before["unrelated"])
                    selected.assert_not_called()
                    all_entries.assert_not_called()

    def test_repair_does_not_discard_unsupported_policy_or_mutate_failed_conversion(
        self,
    ):
        cases = (
            None,
            {"mode": "unknown", "env": [], "vault": []},
            {"mode": "vault", "env": [], "vault": [], "private": "keep"},
            {"mode": "vault", "env": "SYNTHETIC_TOKEN", "vault": []},
            {"mode": "vault", "env": ["invalid-private-name"], "vault": []},
            {"mode": "vault", "env": [None], "vault": []},
        )
        for saved in cases:
            for options in ({"clear": True}, {"mode": "env", "clear": True}):
                with self.subTest(saved=saved, options=options):
                    self.data["credentials"] = copy.deepcopy(saved)
                    before = copy.deepcopy(self.data)
                    with self.assertRaises(vault.VaultError):
                        credentials.configure(self.data, **options)
                    self.assertEqual(self.data, before)
        for options in (
            {},
            {"mode": "vault"},
            {"env": ["SECOND_TOKEN"]},
            {"mode": "selected", "secrets": ["SYNTHETIC_TOKEN"]},
        ):
            self.saved("vault", env=["SYNTHETIC_TOKEN"])
            before = copy.deepcopy(self.data)
            with self.subTest(options=options), self.assertRaises(vault.VaultError):
                credentials.configure(self.data, **options)
            self.assertEqual(self.data, before)

    def test_invalid_explicit_policies_do_not_become_defaults(self):
        for policy in (None, {}, {"mode": "other", "env": [], "vault": []}):
            with self.subTest(policy=policy), self.assertRaises(vault.VaultError):
                credentials.policy({"credentials": policy}, version="2.2.6")

    def test_v1_global_policy_uses_unchanged_legacy_without_presence_or_unlock(self):
        self.saved("vault")
        before = copy.deepcopy(self.data)
        with (
            patch.object(vault, "is_present") as presence,
            patch.object(vault, "select_all") as all_entries,
            patch.object(vault, "select") as selected,
            patch.object(vault, "crypto") as crypto,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            with credentials.unlocked(self.data, version="1.4.2") as values:
                self.assertEqual(values, {})
                self.assertTrue(credentials.is_active(self.data))
                self.assertEqual(
                    credentials.forwarded(self.data, self.legacy), self.legacy
                )
        for call in (presence, all_entries, selected, crypto):
            call.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.data, before)
        self.data["opencode_version"] = "1.4.2"
        self.assertEqual(credentials.forwarded(self.data, self.legacy), self.legacy)
        self.assert_clean()

    def test_v2_no_vault_global_opt_out_uses_legacy_without_inspection(self):
        for saved in (False, True):
            if saved:
                self.saved("vault")
            before = copy.deepcopy(self.data)
            with (
                self.subTest(saved=saved),
                patch.object(vault, "is_present") as presence,
                patch.object(vault, "select_all") as all_entries,
                patch.object(vault, "crypto") as crypto,
                patch.object(vault, "hidden") as prompt,
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                with credentials.unlocked(self.data, no_vault=True):
                    self.assertEqual(
                        credentials.forwarded(self.data, self.legacy), self.legacy
                    )
                    with credentials.unlocked(self.data):
                        self.assertEqual(
                            credentials.forwarded(self.data, self.legacy), self.legacy
                        )
            for call in (presence, all_entries, crypto, prompt):
                call.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(self.data, before)
            self.assert_clean()

    def test_v1_no_vault_is_refused_before_vault_inspection(self):
        with (
            patch.object(vault, "is_present") as presence,
            patch.object(vault, "select") as selected,
        ):
            for mode in ("env", "selected", "vault"):
                self.saved(mode)
                with (
                    self.subTest(mode=mode),
                    self.assertRaisesRegex(
                        vault.VaultError, "--no-vault requires native OpenCode 2.x"
                    ),
                ):
                    with credentials.unlocked(
                        self.data, version="1.4.2", no_vault=True
                    ):
                        self.fail("V1 opt-out must not enter")
        presence.assert_not_called()
        selected.assert_not_called()
        self.assert_clean()

    def test_explicit_env_and_selected_keep_named_vault_semantics_on_both_majors(self):
        for version in ("1.4.2", "2.2.6"):
            for mode in ("env", "selected"):
                self.saved(mode, env=["SYNTHETIC_ENV"], secrets=["SYNTHETIC_TOKEN"])
                with (
                    self.subTest(version=version, mode=mode),
                    patch.dict(os.environ, {"SYNTHETIC_ENV": "synthetic-env-value"}),
                    patch.object(vault, "is_present") as presence,
                    patch.object(vault, "select_all") as all_entries,
                    patch.object(
                        vault, "select", return_value={"SYNTHETIC_TOKEN": "named-value"}
                    ) as selected,
                ):
                    with credentials.unlocked(self.data, version=version):
                        result = credentials.forwarded(self.data, self.legacy)
                        self.assertEqual(result["SYNTHETIC_TOKEN"], "named-value")
                        if mode == "env":
                            self.assertEqual(
                                result["CUSTOM_PASS_ENV"],
                                self.legacy["CUSTOM_PASS_ENV"],
                            )
                        else:
                            self.assertEqual(
                                result["SYNTHETIC_ENV"], "synthetic-env-value"
                            )
                            self.assertNotIn("CUSTOM_PASS_ENV", result)
                            self.assertNotIn("AMBIENT_TOKEN", result)
                selected.assert_called_once_with(["SYNTHETIC_TOKEN"])
                presence.assert_not_called()
                all_entries.assert_not_called()
                self.assert_clean()

    def test_no_vault_omits_named_vault_values_and_retains_env_constraints(self):
        for mode in ("env", "selected"):
            self.saved(mode, env=["SYNTHETIC_ENV"], secrets=["SYNTHETIC_TOKEN"])
            before = copy.deepcopy(self.data)
            with (
                self.subTest(mode=mode),
                patch.dict(os.environ, {"SYNTHETIC_ENV": "synthetic-env-value"}),
                patch.object(vault, "select") as selected,
            ):
                with credentials.unlocked(self.data, no_vault=True):
                    result = credentials.forwarded(self.data, self.legacy)
                    self.assertNotIn("SYNTHETIC_TOKEN", result)
                    if mode == "env":
                        self.assertEqual(result, self.legacy)
                    else:
                        self.assertEqual(result["SYNTHETIC_ENV"], "synthetic-env-value")
                        self.assertNotIn("CUSTOM_PASS_ENV", result)
                        self.assertNotIn("AMBIENT_API_KEY", result)
            selected.assert_not_called()
            self.assertEqual(self.data, before)
            self.assert_clean()
        with self.assertRaisesRegex(vault.VaultError, "unset/empty") as error:
            with credentials.unlocked(self.data, no_vault=True):
                self.fail("Missing selected environment must fail even with opt-out")
        self.assertNotIn("SYNTHETIC_ENV", str(error.exception))

    def test_missing_vault_falls_back_without_crypto_prompt_files_or_config_import(
        self,
    ):
        settings = self.root / "config/oc-box/config.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {"pass_env": ["CUSTOM_PASS_ENV"], "private": "synthetic-config-secret"}
            )
        )
        before = self.snapshot()
        metadata = copy.deepcopy(self.data)
        with (
            patch.object(vault, "crypto") as crypto,
            patch.object(vault, "hidden") as prompt,
            patch.object(vault, "locked") as locked,
            patch.object(vault, "select_all") as all_entries,
            patch.object(vault, "read_envelope") as read,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            with credentials.unlocked(self.data):
                self.assertEqual(
                    credentials.forwarded(self.data, self.legacy), self.legacy
                )
                self.assertTrue(credentials.is_active(self.data))
                with credentials.unlocked(self.data):
                    self.assertEqual(credentials.active_values(self.data), {})
        for call in (crypto, prompt, locked, all_entries, read):
            call.assert_not_called()
        text = output.getvalue()
        self.assertEqual(text.count("Recommended setup:"), 1)
        for command in (
            "oc-box doctor --feature vault",
            "oc-box secrets init",
            "oc-box secrets set NAME",
        ):
            self.assertIn(command, text)
        self.assertNotIn("synthetic-config-secret", text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.data, metadata)
        self.assertFalse(self.path.parent.exists())
        self.assert_clean()

    def test_runtime_override_scopes_default_to_forwarding_without_metadata_updates(
        self,
    ):
        self.data.pop("opencode_version")
        values = dict(VALUES)
        before = copy.deepcopy(self.data)
        with (
            patch.object(vault, "is_present", return_value=True),
            patch.object(vault, "select_all", return_value=values),
        ):
            with credentials.unlocked(self.data, version="2.2.6"):
                self.assertNotIn(
                    "AMBIENT_TOKEN", credentials.forwarded(self.data, self.legacy)
                )
                self.assertEqual(credentials.policy(self.data)["mode"], "env")
        self.assertEqual(credentials.forwarded(self.data, self.legacy), self.legacy)
        self.assertEqual(self.data, before)
        self.assertEqual(values, {})
        self.assert_clean()

    def test_effective_policy_exposes_scoped_fallback_opt_out_and_private_copies(self):
        self.saved("vault")
        for no_vault in (False, True):
            with (
                self.subTest(no_vault=no_vault),
                patch.object(vault, "is_present", return_value=False),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with credentials.unlocked(self.data, no_vault=no_vault):
                    self.assertEqual(credentials.policy(self.data)["mode"], "vault")
                    effective = credentials.effective_policy(self.data)
                    self.assertEqual(effective["mode"], "env")
                    effective["env"].append("SYNTHETIC_PRIVATE_NAME")
                    self.assertEqual(credentials.effective_policy(self.data)["env"], [])
            self.assertEqual(credentials.effective_policy(self.data)["mode"], "vault")
        self.assertEqual(
            credentials.effective_policy(self.data, version="1.4.2")["mode"], "env"
        )
        self.assertEqual(
            self.data["credentials"], {"mode": "vault", "env": [], "vault": []}
        )
        self.assert_clean()

    def test_global_environment_budget_counts_utf8_names_equals_and_nuls(self):
        name, value = "SYNTHETIC_TOKEN", "synthetic-\u00e9-value"
        size = len(name.encode("utf-8")) + len(value.encode("utf-8")) + 2
        for budget in (size, size - 1):
            values = {name: value}
            with (
                self.subTest(budget=budget),
                patch.object(credentials, "MAX_GLOBAL_ENV_BYTES", budget),
                patch.object(vault, "is_present", return_value=True),
                patch.object(vault, "select_all", return_value=values),
            ):
                if budget == size:
                    with credentials.unlocked(self.data) as current:
                        self.assertEqual(current, {name: value})
                else:
                    with self.assertRaisesRegex(
                        vault.VaultError, "environment size limit exceeded"
                    ):
                        with credentials.unlocked(self.data):
                            self.fail("Budget must include every encoded byte")
            self.assertEqual(values, {})
            self.assert_clean()

    def test_global_environment_budget_does_not_change_explicit_selection_limits(self):
        for mode in ("env", "selected"):
            self.saved(mode, secrets=["SYNTHETIC_TOKEN"])
            values = {"SYNTHETIC_TOKEN": "synthetic-explicit-value"}
            with (
                self.subTest(mode=mode),
                patch.object(credentials, "MAX_GLOBAL_ENV_BYTES", 1),
                patch.object(vault, "select", return_value=values),
                patch.object(credentials, "_check_global_env_budget") as budget,
            ):
                with credentials.unlocked(self.data):
                    self.assertEqual(
                        credentials.forwarded(self.data, self.legacy)[
                            "SYNTHETIC_TOKEN"
                        ],
                        "synthetic-explicit-value",
                    )
            budget.assert_not_called()
            self.assertEqual(values, {})
            self.assert_clean()

    def test_nested_owner_unlocks_once_and_context_cleanup_survives_errors(self):
        values = dict(VALUES)
        other = {**self.data, "id": "b" * 32}
        with (
            patch.object(vault, "is_present", return_value=True) as presence,
            patch.object(vault, "select_all", return_value=values) as all_entries,
            self.assertRaisesRegex(RuntimeError, "synthetic failure"),
        ):
            with credentials.unlocked(self.data) as outer:
                self.assertTrue(credentials.is_active(self.data))
                self.assertFalse(credentials.is_active(other))
                self.assertEqual(credentials.active_values(other), {})
                self.assertNotIn(
                    "SYNTHETIC_A_TOKEN", credentials.forwarded(other, self.legacy)
                )
                with credentials.unlocked(self.data, no_vault=True) as inner:
                    self.assertIs(outer, inner)
                    self.assertIn(
                        "SYNTHETIC_A_TOKEN",
                        credentials.forwarded(self.data, self.legacy),
                    )
                    self.assertFalse(Context().run(credentials.is_active, self.data))
                    raise RuntimeError("synthetic failure")
        presence.assert_called_once_with()
        all_entries.assert_called_once_with()
        self.assertEqual(values, {})
        self.assert_clean()

    def test_different_nested_owners_restore_outer_values_and_effective_policy(self):
        other = {**self.data, "id": "b" * 32}
        outer_values = {"SYNTHETIC_OUTER_TOKEN": "synthetic-outer-secret"}
        inner_values = {"SYNTHETIC_INNER_TOKEN": "synthetic-inner-secret"}
        with (
            patch.object(vault, "is_present", return_value=True),
            patch.object(
                vault, "select_all", side_effect=[outer_values, inner_values]
            ) as all_entries,
        ):
            with credentials.unlocked(self.data):
                with credentials.unlocked(other):
                    self.assertTrue(credentials.is_active(other))
                    self.assertFalse(credentials.is_active(self.data))
                    self.assertEqual(credentials.active_values(self.data), {})
                self.assertTrue(credentials.is_active(self.data))
                self.assertEqual(credentials.active_values(self.data), outer_values)
                self.assertEqual(inner_values, {})
                self.assertNotIn(
                    "AMBIENT_TOKEN", credentials.forwarded(self.data, self.legacy)
                )
        self.assertEqual(all_entries.call_count, 2)
        self.assertEqual(outer_values, {})
        self.assert_clean()


class VaultPresenceTests(SyntheticFixture):
    def test_missing_probe_is_read_only_including_existing_private_parent(self):
        for existing_parent in (False, True):
            if existing_parent:
                self.path.parent.mkdir(parents=True, mode=0o700)
            before = self.snapshot()
            with (
                self.subTest(existing_parent=existing_parent),
                patch.object(vault, "crypto") as crypto,
                patch.object(vault, "read_envelope") as read,
                patch.object(
                    vault.os,
                    "open",
                    side_effect=AssertionError("Presence must not open files"),
                ),
            ):
                self.assertFalse(vault.is_present(path=self.path))
            crypto.assert_not_called()
            read.assert_not_called()
            self.assertEqual(self.snapshot(), before)
            self.assertFalse((self.path.parent / ".vault.lock").exists())

    def test_presence_does_not_read_even_damaged_content(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_bytes(b"not an encrypted envelope")
        self.path.chmod(0o600)
        before = self.snapshot()
        with (
            patch.object(vault, "crypto") as crypto,
            patch.object(vault, "read_envelope") as read,
            patch.object(
                vault.os,
                "open",
                side_effect=AssertionError("Presence must not open files"),
            ),
        ):
            self.assertTrue(vault.is_present())
        crypto.assert_not_called()
        read.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.path.parent / ".vault.lock").exists())

    def test_unsafe_paths_never_count_as_absence(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.symlink_to(self.root / "missing-target")
        with self.assertRaisesRegex(vault.VaultError, "symbolic links"):
            vault.is_present(path=self.path)
        self.path.unlink()
        self.path.write_bytes(b"{}")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(vault.VaultError, "0600"):
            vault.is_present(path=self.path)
        self.path.chmod(0o600)
        link = self.root / "hardlink"
        os.link(self.path, link)
        with self.assertRaisesRegex(vault.VaultError, "hard links"):
            vault.is_present(path=self.path)
        link.unlink()
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaisesRegex(vault.VaultError, "regular file"):
            vault.is_present(path=self.path)
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaisesRegex(vault.VaultError, "regular file"):
            vault.is_present(path=self.path)

    def test_unsafe_parent_symlink_permissions_and_inspection_failure_fail_closed(self):
        self.path.parent.mkdir(parents=True, mode=0o755)
        with self.assertRaisesRegex(vault.VaultError, "0700"):
            vault.is_present(path=self.path)
        self.path.parent.chmod(0o700)
        link = self.root / "directory-link"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaisesRegex(vault.VaultError, "symbolic links"):
            vault.is_present(path=link / "vault.json")
        with (
            patch.object(
                Path, "lstat", side_effect=PermissionError("synthetic-private-details")
            ),
            self.assertRaises(vault.VaultError) as error,
        ):
            vault.is_present(path=self.path)
        self.assertNotIn("synthetic-private-details", str(error.exception))


@unittest.skipUnless(HAS_CRYPTO, "run with uv --no-project --with cryptography")
class EncryptedGlobalVaultTests(SyntheticFixture):
    def test_all_current_entries_are_delivered_across_new_launches_and_directories(
        self,
    ):
        entries = dict(VALUES)
        for index in range(3):
            project = self.root / f"synthetic-project-{index}"
            project.mkdir()
            data = {**self.data, "id": str(index + 1) * 32, "project": str(project)}
            metadata = copy.deepcopy(data)
            self.encrypted(entries)
            with (
                patch.object(vault, "hidden", return_value=PASSPHRASE) as prompt,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with credentials.unlocked(data) as values:
                    self.assertEqual(values, entries)
                    with credentials.unlocked(data):
                        forwarded = credentials.forwarded(data, self.legacy)
                        for name, value in entries.items():
                            self.assertEqual(forwarded[name], value)
                        for name in (
                            "AMBIENT_TOKEN",
                            "AMBIENT_API_KEY",
                            "CUSTOM_PASS_ENV",
                        ):
                            self.assertNotIn(name, forwarded)
            prompt.assert_called_once_with("Vault passphrase: ")
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(data, metadata)
            self.assertEqual(values, {})
            self.assertNotIn("credentials", data)
            self.assert_clean()
            entries[f"SYNTHETIC_ADDITION_{index}"] = f"synthetic-new-secret-{index}"

    def test_same_workspace_sees_additions_on_next_launch_without_saved_name_list(self):
        self.saved("vault")
        metadata = copy.deepcopy(self.data)
        for values in ({"SYNTHETIC_FIRST": "first-secret"}, dict(VALUES)):
            self.encrypted(values)
            with patch.object(vault, "hidden", return_value=PASSPHRASE) as prompt:
                with credentials.unlocked(self.data) as current:
                    self.assertEqual(current, values)
            prompt.assert_called_once()
            self.assertEqual(self.data, metadata)
            self.assert_clean()

    def test_empty_initialized_vault_unlocks_once_never_uses_ambient_or_imports_config(
        self,
    ):
        self.encrypted({})
        config = self.root / "config/oc-box/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            '{"pass_env":["CUSTOM_PASS_ENV"],"key":"synthetic-config-secret"}'
        )
        before = self.snapshot()
        with (
            patch.dict(os.environ, self.legacy),
            patch.object(vault, "hidden", return_value=PASSPHRASE) as prompt,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            with credentials.unlocked(self.data) as values:
                self.assertEqual(values, {})
                with credentials.unlocked(self.data):
                    result = box.container_env(self.data)
                    for name in ("AMBIENT_TOKEN", "AMBIENT_API_KEY", "CUSTOM_PASS_ENV"):
                        self.assertNotIn(name, result)
        prompt.assert_called_once_with("Vault passphrase: ")
        self.assertIn("Global vault is empty", output.getvalue())
        self.assertIn("oc-box secrets set NAME", output.getvalue())
        self.assertEqual(output.getvalue().count("Recommended setup:"), 1)
        self.assertNotIn("synthetic-config-secret", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assert_clean()

    def test_values_never_enter_host_environment_argv_metadata_or_plaintext_storage(
        self,
    ):
        self.encrypted(VALUES)
        metadata = json.dumps(self.data, sort_keys=True)
        environment = dict(os.environ)
        argv = list(sys.argv)
        files = self.snapshot()
        with (
            patch.object(vault, "hidden", return_value=PASSPHRASE),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with credentials.unlocked(self.data):
                result = box.container_env(self.data)
                command = box.exec_command(self.data, ["/bin/true"])
                for name, value in VALUES.items():
                    self.assertEqual(result[name], value)
                    self.assertEqual(command.environment[name], value)
                    self.assertNotIn(value, " ".join(command))
                    self.assertNotIn(value, json.dumps(self.data))
                    self.assertNotIn(name, json.dumps(self.data))
                    self.assertNotIn(name, os.environ)
                    self.assertIn(
                        "[redacted credential]", credentials.redact("failure " + value)
                    )
                    self.assertNotIn(value, credentials.redact("failure " + value))
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(sys.argv, argv)
        self.assertEqual(json.dumps(self.data, sort_keys=True), metadata)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(self.snapshot(), files)
        for text in (stdout.getvalue(), stderr.getvalue(), self.path.read_text()):
            self.assertNotIn(PASSPHRASE, text)
            for name, value in VALUES.items():
                self.assertNotIn(name, text)
                self.assertNotIn(value, text)
        self.assert_clean()

    def test_oversized_global_environment_aborts_before_context_or_reset_deletion(self):
        self.saved("vault")
        entries = {
            f"SYNTHETIC_LARGE_{index}_TOKEN": "s" * vault.MAX_SECRET
            for index in range(16)
        }
        # Individual values and the encrypted file are valid; aggregate exec
        # environment bytes exceed 1 MiB once names, '=' and NULs are included.
        vault.validate_secrets(entries)
        self.encrypted(entries)
        before = self.snapshot()
        metadata = copy.deepcopy(self.data)
        environment = dict(os.environ)
        returned = []
        select_all = vault.select_all
        reset_deletion = Mock()

        def capture_values():
            values = select_all()
            returned.append(values)
            return values

        with (
            patch.object(vault, "hidden", return_value=PASSPHRASE) as prompt,
            patch.object(vault, "select_all", side_effect=capture_values),
            patch.object(
                credentials, "_UNLOCKED", wraps=credentials._UNLOCKED
            ) as owner_context,
            patch.object(
                credentials, "_EFFECTIVE_POLICY", wraps=credentials._EFFECTIVE_POLICY
            ) as policy_context,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(vault.VaultError) as error,
        ):
            with credentials.unlocked(self.data):
                reset_deletion()
        prompt.assert_called_once_with("Vault passphrase: ")
        owner_context.set.assert_not_called()
        policy_context.set.assert_not_called()
        reset_deletion.assert_not_called()
        self.assertEqual(returned, [{}])
        self.assertEqual(
            str(error.exception),
            "Global vault environment size limit exceeded (1 MiB). Use selected mode with fewer credentials.",
        )
        for text in (str(error.exception), stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn(PASSPHRASE, text)
            for name, value in entries.items():
                self.assertNotIn(name, text)
                self.assertNotIn(value, text)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.data, metadata)
        self.assertEqual(dict(os.environ), environment)
        self.assert_clean()

    def test_wrong_password_fails_closed_and_never_emits_private_data(self):
        self.encrypted(VALUES)
        before = self.snapshot()
        with (
            patch.object(
                vault, "hidden", return_value="synthetic-wrong-passphrase"
            ) as prompt,
            contextlib.redirect_stderr(io.StringIO()) as output,
            self.assertRaisesRegex(vault.VaultError, "incorrect passphrase") as error,
        ):
            with credentials.unlocked(self.data):
                self.fail("Wrong password must not fall back")
        prompt.assert_called_once()
        self.assertEqual(output.getvalue(), "")
        for name, value in VALUES.items():
            self.assertNotIn(name, str(error.exception))
            self.assertNotIn(value, str(error.exception))
        self.assertNotIn("synthetic-wrong-passphrase", str(error.exception))
        self.assertEqual(self.snapshot(), before)
        self.assert_clean()

    def test_corrupt_configured_vault_fails_before_crypto_prompt_or_fallback(self):
        self.encrypted(VALUES)
        self.path.write_bytes(b"corrupt configured vault")
        before = self.snapshot()
        with (
            patch.object(vault, "crypto") as crypto,
            patch.object(vault, "hidden") as prompt,
            contextlib.redirect_stderr(io.StringIO()) as output,
            self.assertRaisesRegex(vault.VaultError, "damaged vault"),
        ):
            with credentials.unlocked(self.data):
                self.fail("Configured corruption must not fall back")
        crypto.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.snapshot(), before)
        self.assert_clean()

    def test_missing_crypto_fails_before_prompt_even_for_initialized_empty_vault(self):
        for values in ({}, VALUES):
            self.encrypted(values)
            before = self.snapshot()
            with (
                self.subTest(empty=not values),
                patch.object(
                    vault,
                    "crypto",
                    side_effect=vault.VaultError(
                        "optional cryptography dependency missing"
                    ),
                ),
                patch.object(vault, "hidden") as prompt,
                contextlib.redirect_stderr(io.StringIO()) as output,
                self.assertRaisesRegex(vault.VaultError, "cryptography"),
            ):
                with credentials.unlocked(self.data):
                    self.fail("Configured vault requires crypto, never fallback")
            prompt.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(self.snapshot(), before)
            self.assert_clean()

    def test_invalid_authenticated_names_and_values_fail_without_disclosure(self):
        aes, _, _ = vault.crypto()
        for name, value in (
            ("PATH", "synthetic-reserved-value"),
            ("OC_BOX_PRIVATE_TOKEN", "synthetic-reserved-value"),
            ("OPENCODE_PRIVATE_TOKEN", "synthetic-reserved-value"),
            ("invalid-private-name", "synthetic-invalid-name-value"),
            ("SYNTHETIC_NUL_TOKEN", "synthetic\0private-value"),
            ("SYNTHETIC_EMPTY_TOKEN", ""),
            ("SYNTHETIC_SURROGATE_TOKEN", "synthetic\ud800value"),
        ):
            self.encrypted({})
            envelope = vault.read_envelope(self.path)
            key = vault.derive(PASSPHRASE, vault.decode(envelope["kdf"]["salt"]))
            envelope["ciphertext"] = vault.encode(
                aes(key).encrypt(
                    vault.decode(envelope["cipher"]["nonce"]),
                    json.dumps({"secrets": {name: value}}).encode(),
                    vault.header(envelope),
                )
            )
            self.path.write_text(json.dumps(envelope))
            before = self.snapshot()
            with (
                self.subTest(name=name),
                patch.object(vault, "hidden", return_value=PASSPHRASE),
                self.assertRaises(vault.VaultError) as error,
            ):
                with credentials.unlocked(self.data):
                    self.fail("Every global entry must validate before forwarding")
            self.assertNotIn(name, str(error.exception))
            if value:
                self.assertNotIn(value, str(error.exception))
            self.assertEqual(self.snapshot(), before)
            self.assert_clean()

    def test_select_all_preserves_named_select_and_private_file_checks(self):
        self.encrypted(VALUES)
        with patch.object(vault, "hidden", return_value=PASSPHRASE):
            self.assertEqual(vault.select_all(path=self.path), VALUES)
            self.assertEqual(
                vault.select(["SYNTHETIC_A_TOKEN"], path=self.path),
                {"SYNTHETIC_A_TOKEN": VALUES["SYNTHETIC_A_TOKEN"]},
            )
        self.path.chmod(0o644)
        with (
            patch.object(vault, "hidden") as prompt,
            self.assertRaisesRegex(vault.VaultError, "0600"),
        ):
            vault.select_all(path=self.path)
        prompt.assert_not_called()

    def test_explicit_named_selection_still_fails_when_missing_and_opt_out_skips_corruption(
        self,
    ):
        self.encrypted(VALUES)
        self.saved("selected", secrets=["SYNTHETIC_MISSING_TOKEN"])
        with (
            patch.object(vault, "hidden", return_value=PASSPHRASE),
            self.assertRaisesRegex(vault.VaultError, "missing") as error,
        ):
            with credentials.unlocked(self.data):
                self.fail("Missing explicit vault selection must fail")
        self.assertNotIn("SYNTHETIC_MISSING_TOKEN", str(error.exception))
        self.path.write_bytes(b"corrupt configured vault")
        self.saved("vault")
        with patch.object(vault, "hidden") as prompt:
            with credentials.unlocked(self.data, no_vault=True):
                self.assertEqual(
                    credentials.forwarded(self.data, self.legacy), self.legacy
                )
        prompt.assert_not_called()
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
