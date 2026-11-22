"""Synthetic host migration; no Podman, native host service or real HOME."""

import contextlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import backup_crypto as crypto
from oc_box import (
    backups,
    vault,
)
from oc_box import host_backup_auth as native_auth
from oc_box import (
    host_backup_payload as payload,
)
from oc_box import (
    host_backup_restore as restore,
)
from oc_box import oc_box as box


class HostBackupTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="oc-box-host-backup-test-")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.source = self.root / "source"
        self.target = self.root / "target"

    def profile(self, root):
        return patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "OC_BOX_STATE_HOME": str(root / "state"),
                "OC_BOX_CONTAINER": "0",
            },
        )

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(content)
        path.chmod(0o600)

    def test_capture_does_not_follow_links_or_read_native_database_and_caches(self):
        with self.profile(self.source):
            roots = payload.destinations()
            cfg = roots[payload.OPENCODE_CONFIG]
            auth = roots[payload.OPENCODE_AUTH]
            self.write(cfg / "opencode.jsonc", b'// exact\n{"name":"synthetic"}')
            self.write(cfg / "node_modules/skip", b"skipped cache")
            self.write(auth / "opencode.db", b"not captured")
            self.write(auth / "mcp-auth.json", b'{"synthetic":{}}')
            snapshot = payload.capture()
            self.assertEqual(
                snapshot,
                {
                    (payload.OPENCODE_CONFIG, ""): None,
                    (
                        payload.OPENCODE_CONFIG,
                        "opencode.jsonc",
                    ): b'// exact\n{"name":"synthetic"}',
                    (payload.OPENCODE_AUTH, "mcp-auth.json"): b'{"synthetic":{}}',
                },
            )
            self.assertEqual(payload.decode(payload.encode(snapshot)), snapshot)
            (cfg / "outside").symlink_to(auth / "opencode.db")
            with self.assertRaises(payload.HostPayloadError):
                payload.capture()

    def test_schema_refuses_traversal_duplicates_and_unbound_native_children(self):
        for entries in (
            [[payload.OPENCODE_CONFIG, "../outside", "YQ=="]],
            [[payload.OPENCODE_CONFIG, "orphan.js", "YQ=="]],
            [[payload.OPENCODE_CONFIG, "", None]] * 2,
            [[payload.OPENCODE_AUTH, "opencode.db", "YQ=="]],
        ):
            raw = json.dumps(
                {"format": "oc-box-host", "version": 1, "entries": entries}
            ).encode()
            with (
                self.subTest(entries=entries),
                self.assertRaises(payload.HostPayloadError),
            ):
                payload.decode(raw)

    def test_preflight_is_read_only_and_apply_never_replaces_a_late_file(self):
        snapshot = {
            (payload.OC_BOX_CONFIG, "config.json"): b'{"pass_env":["SYNTHETIC"]}\n',
            (payload.OPENCODE_CONFIG, ""): None,
            (payload.OPENCODE_CONFIG, "plugin.js"): b"// synthetic plugin",
        }
        with self.profile(self.target):
            plan = restore.preflight(snapshot)
            self.assertFalse(self.target.exists())
            target = payload.destinations()[payload.OC_BOX_CONFIG] / "config.json"
            self.write(target, b'{"settings":"existing"}')
            with self.assertRaises(restore.HostRestoreError):
                restore.apply(plan)
            self.assertEqual(target.read_bytes(), b'{"settings":"existing"}')
            self.assertFalse(payload.destinations()[payload.OPENCODE_CONFIG].exists())

    def test_native_profile_requires_explicit_opt_in_and_refuses_existing_profile(self):
        snapshot = {
            (payload.OPENCODE_CONFIG, ""): None,
            (payload.OPENCODE_CONFIG, "plugin"): None,
            (payload.OPENCODE_CONFIG, "plugin/test.js"): b"// synthetic plugin",
            (payload.OPENCODE_AUTH, "auth.json"): b'{"synthetic":"opaque"}',
        }
        with self.profile(self.target):
            plan = restore.preflight(snapshot)
            self.assertEqual(restore.apply(plan), 0)
            self.assertFalse(self.target.exists())
            # The review uses /dev/tty, not redirected stdin.
            with (
                patch.object(restore, "preflight", return_value=plan),
                patch.object(backups.os, "open", side_effect=OSError),
                self.assertRaisesRegex(crypto.BackupError, "controlling terminal"),
            ):
                backups._prepare_host_restore(snapshot, True)
            self.assertFalse(self.target.exists())
            plan = restore.preflight(snapshot, include_opencode=True)
            self.assertEqual(restore.apply(plan, include_opencode=True), 2)
            native = payload.destinations()
            self.assertEqual(
                (native[payload.OPENCODE_CONFIG] / "plugin/test.js").read_bytes(),
                b"// synthetic plugin",
            )
            self.assertEqual(
                (native[payload.OPENCODE_AUTH] / "auth.json").read_bytes(),
                b'{"synthetic":"opaque"}',
            )
            with self.assertRaisesRegex(restore.HostRestoreError, "already exists"):
                restore.preflight(snapshot, include_opencode=True)

    def test_native_export_validation_and_conditional_destination(self):
        stored = [
            {
                "id": "cred_synthetic",
                "integrationID": "provider.example",
                "label": "Synthetic",
                "active": True,
                "value": {"type": "key", "key": "opaque-synthetic"},
            }
        ]
        canonical = native_auth.validate_native_export(json.dumps(stored).encode())
        self.assertEqual(json.loads(canonical), stored)
        with self.assertRaises(native_auth.NativeAuthError):
            native_auth.validate_native_export(json.dumps(stored * 2).encode())
        data = self.target / "data"
        native_auth.check_native_destination(data)
        self.assertFalse(data.exists())
        self.write(data / "opencode/auth.json", b"{}")
        native_auth.check_native_destination(data)
        self.write(data / "opencode/opencode.db", b"untouched store")
        with self.assertRaises(native_auth.NativeAuthError):
            native_auth.check_native_destination(data)
        self.assertEqual(
            (data / "opencode/opencode.db").read_bytes(), b"untouched store"
        )

    def test_native_database_publication_cannot_move_to_a_replaced_parent(self):
        with self.profile(self.target):
            root = payload.destinations()[payload.OPENCODE_AUTH]
            root.mkdir(parents=True, mode=0o700)
            plan = restore.preflight({}, include_opencode=True, native_database=True)
            root.rename(root.with_name("previous"))
            root.mkdir(mode=0o700)
            with self.assertRaises(restore.HostRestoreError):
                restore.apply(
                    plan,
                    include_opencode=True,
                    native_database=b"SQLite format 3\0synthetic",
                )
            self.assertEqual(list(root.iterdir()), [])

    def test_native_preparation_failure_precedes_workspace_and_host_activation(self):
        manifest = {"kind": "full", "components": {"host.age": {}, "host-auth.age": {}}}
        args = backups.parser_for("restore").parse_args(
            [
                "fixture.ocbox",
                "--host",
                "--include-opencode",
                "--project",
                str(self.target / "workspace"),
            ]
        )
        with (
            self.profile(self.target),
            patch.object(box, "check_backend"),
            patch.object(backups, "_workspace_destination", return_value=args.project),
            patch.object(
                crypto,
                "opened",
                return_value=contextlib.nullcontext(
                    (manifest, self.root, b"synthetic")
                ),
            ),
            patch.object(
                crypto, "decrypt_bytes", side_effect=[payload.encode({}), b"[]"]
            ),
            patch.object(
                backups,
                "_prepare_host_restore",
                return_value=(object(), "/native", self.target),
            ),
            patch.object(
                native_auth,
                "prepare_native_export",
                side_effect=crypto.BackupError("synthetic native refusal"),
            ),
            patch.object(backups, "_restore_workspace") as workspace,
            patch.object(backups, "_apply_host_restore") as host,
        ):
            with self.assertRaisesRegex(crypto.BackupError, "synthetic native refusal"):
                backups.restore_backup(box, args)
            workspace.assert_not_called()
            host.assert_not_called()
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(
        shutil.which("age") and shutil.which("age-keygen"), "age unavailable"
    )
    def test_native_export_is_a_separately_encrypted_opt_in_component(self):
        key = self.root / "identity"
        self.write(key, crypto.small(["age-keygen"]))
        recipient = crypto.small(["age-keygen", "-y", str(key)]).decode().strip()
        source = self.root / "native-export.json"
        private_export = [
            {
                "id": "cred_synthetic",
                "integrationID": "provider.synthetic",
                "label": "Synthetic",
                "active": True,
                "value": {"type": "key", "key": "opaque-export-sentinel"},
            }
        ]
        self.write(source, json.dumps(private_export).encode())
        archive = self.root / "host-with-auth.ocbox"
        with (
            self.profile(self.source),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            args = backups.parser_for("backup").parse_args(
                [
                    "--host",
                    "--opencode-auth-export",
                    str(source),
                    "--recipient",
                    recipient,
                    "--output",
                    str(archive),
                ]
            )
            backups.make_backup(box, args)
            with crypto.opened(archive, [key]) as (manifest, staging, private):
                self.assertEqual(
                    set(manifest["components"]), {"host.age", "host-auth.age"}
                )
                self.assertEqual(
                    json.loads(
                        crypto.decrypt_bytes(staging / "host-auth.age", private)
                    ),
                    private_export,
                )
            args = backups.parser_for("restore").parse_args(
                [
                    str(archive),
                    "--host",
                    "--identity",
                    str(key),
                ]
            )
            with (
                self.profile(self.target),
                patch.object(
                    backups,
                    "host_auth",
                    side_effect=AssertionError("native auth access"),
                ),
            ):
                backups.restore_backup(box, args)
            self.assertFalse(self.target.exists())
            self.assertNotIn("opaque-export-sentinel", output.getvalue())
        self.assertNotIn(b"opaque-export-sentinel", archive.read_bytes())

    @unittest.skipUnless(
        shutil.which("age") and shutil.which("age-keygen"), "age unavailable"
    )
    def test_real_host_archive_is_encrypted_and_restore_needs_no_workspace(self):
        key = self.root / "identity"
        self.write(key, crypto.small(["age-keygen"]))
        recipient = crypto.small(["age-keygen", "-y", str(key)]).decode().strip()
        archive = self.root / "host.ocbox"
        config = (
            b'{  "pass_env": ["SYNTHETIC_KEY"], "notes": "host-private-sentinel" }\n'
        )
        envelope = {
            "format": vault.FORMAT,
            "version": vault.VERSION,
            "kdf": {**vault.KDF, "salt": vault.encode(b"s" * 16)},
            "cipher": {"name": "AES-256-GCM", "nonce": vault.encode(b"n" * 12)},
            "ciphertext": vault.encode(b"opaque encrypted vault bytes"),
        }
        vault_bytes = json.dumps(envelope, indent=3).encode()
        with (
            self.profile(self.source),
            patch.object(box, "find_workspace") as workspace,
            patch.object(box, "check_backend") as backend,
            patch.object(vault, "unlock", side_effect=AssertionError("vault unlock")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            roots = payload.destinations()
            self.write(roots[payload.OC_BOX_CONFIG] / "config.json", config)
            self.write(roots[payload.VAULT] / "vault.json", vault_bytes)
            self.write(roots[payload.OPENCODE_CONFIG] / "cli.json", b"{}\n")
            args = backups.parser_for("backup").parse_args(
                ["--host", "--output", str(archive), "--recipient", recipient]
            )
            backups.make_backup(box, args)
            with crypto.opened(archive, [key]) as (manifest, _, _):
                self.assertEqual(manifest["kind"], "host")
                self.assertEqual(set(manifest["components"]), {"host.age"})
            with tarfile.open(archive) as outer:
                self.assertEqual(
                    set(outer.getnames()), {"key.age", "manifest.age", "host.age"}
                )
            self.assertNotIn(b"host-private-sentinel", archive.read_bytes())
            args = backups.parser_for("restore").parse_args(
                [str(archive), "--host", "--identity", str(key)]
            )
            with self.profile(self.target):
                backups.restore_backup(box, args)
                roots = payload.destinations()
                restored = roots[payload.OC_BOX_CONFIG] / "config.json"
                self.assertEqual(restored.read_bytes(), config)
                self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    (roots[payload.VAULT] / "vault.json").read_bytes(), vault_bytes
                )
                self.assertFalse(roots[payload.OPENCODE_CONFIG].exists())
                with self.assertRaises(restore.HostRestoreError):
                    backups.restore_backup(box, args)
            workspace.assert_not_called()
            backend.assert_not_called()
            self.assertNotIn("host-private-sentinel", output.getvalue())
