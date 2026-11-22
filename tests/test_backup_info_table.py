import contextlib
import io
import json
import os
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from oc_box import backups, cli_table
from oc_box import oc_box as box


class BackupInfoTableTests(unittest.TestCase):
    def setUp(self):
        guards = contextlib.ExitStack()
        self.addCleanup(guards.close)
        guards.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        guards.enter_context(patch.object(backups.os, "geteuid", return_value=1000))
        guards.enter_context(patch.object(backups.resource, "setrlimit"))
        guards.enter_context(patch.object(backups.crypto, "tools"))
        guards.enter_context(
            patch.object(box, "check_backend", side_effect=AssertionError("no Podman"))
        )
        for name in ("host_payload", "host_restore", "host_auth"):
            guards.enter_context(
                patch.object(backups, name, side_effect=AssertionError("no host data"))
            )
        guards.enter_context(
            patch.object(
                backups.crypto,
                "decrypt_bytes",
                side_effect=AssertionError("do not decrypt any payload components"),
            )
        )

    def workspace(self, *, combined=False):
        names = ["project.age", "home.age", "registry.age"]
        if combined:
            names += ["image.age", "host.age", "host-auth.age"]
        return {
            "format": "oc-box-backup",
            "format_version": 2 if combined else 1,
            "kind": "full" if combined else "portable",
            "created_at": "2026-01-02T03:04:05+00:00",
            "architecture": "x86_64",
            "source_workspace_id": "a" * 32,
            "project": "/work/example",
            "container_project": "/work/container",
            "configuration": {
                "podman_args": ["--env=TOP_SECRET=private-option", "--privileged"],
                "distro": "tumbleweed",
                "home": "/home/workspace",
                "cpus": "2",
                "memory": "4g",
                "pids": 64,
                "ready": True,
                "devices": [],
                "credentials": {"mode": "env", "env": [], "vault": []},
            },
            "sessions": {"sessions": ["ses_example"], "last": "ses_example"},
            "image_id": "b" * 64 if combined else None,
            "components": {name: {"sha256": "c" * 64, "bytes": 128} for name in names},
            "expanded_byte_limit": 4096,
        }

    def inspect(self, manifest, *flags, interaction=None):
        argv = ["backup-info", "example.ocbox", "--identity", "identity.txt", *flags]
        output = io.StringIO()
        with (
            patch.object(
                backups.crypto,
                "opened",
                return_value=contextlib.nullcontext((manifest, None, None)),
            ) as opened,
            contextlib.redirect_stdout(output),
        ):
            result = (
                box.main(argv)
                if interaction is None
                else backups.cli(box, argv, interaction=interaction)
            )
        self.assertEqual(result, 0)
        opened.assert_called_once_with(Path("example.ocbox"), [Path("identity.txt")])
        return output.getvalue()

    def test_workspace_json_default_and_explicit_keep_exact_masked_output(self):
        for combined in (False, True):
            with self.subTest(combined=combined):
                manifest = self.workspace(combined=combined)
                sanitized = deepcopy(manifest)
                sanitized["configuration"]["podman_args"] = [
                    "--env=[value omitted]",
                    "--privileged=[value omitted]",
                ]
                expected = json.dumps(sanitized, indent=2) + "\n"
                self.assertEqual(self.inspect(manifest), expected)
                self.assertEqual(self.inspect(manifest, "--json"), expected)
                self.assertIn(
                    "private-option", manifest["configuration"]["podman_args"][0]
                )

    def test_workspace_table_uses_the_same_sanitized_metadata(self):
        for combined in (False, True):
            with self.subTest(combined=combined):
                manifest = self.workspace(combined=combined)
                with patch.object(
                    cli_table, "fields", wraps=cli_table.fields
                ) as fields:
                    output = self.inspect(manifest, "--table")
                fields.assert_called_once_with(json.loads(self.inspect(manifest)))
                self.assertIn("oc-box-backup", output)
                self.assertIn("/work/example", output)
                self.assertIn("--env=[value omitted]", output)
                self.assertNotIn("TOP_SECRET", output)
                self.assertNotIn("private-option", output)

    def test_host_only_table_and_json_do_not_disclose_host_component_metadata(self):
        manifest = {
            "kind": "host",
            "format_version": 2,
            "components": {"host.age": {}, "host-auth.age": {}},
            "extra": "private-host-details",
        }
        projection = {"format": "host", "format_version": 2}
        expected = json.dumps(projection, indent=2) + "\n"
        self.assertEqual(self.inspect(manifest), expected)
        self.assertEqual(self.inspect(manifest, "--json"), expected)
        with patch.object(cli_table, "fields", wraps=cli_table.fields) as fields:
            output = self.inspect(manifest, "--table")
        fields.assert_called_once_with(projection)
        self.assertIn("host", output)
        self.assertNotIn("host-auth.age", output)
        self.assertNotIn("private-host-details", output)

    def test_private_dashboard_ignores_stdout_format_flags(self):
        manifest = self.workspace(combined=True)
        for flags in ((), ("--json",), ("--table",)):
            with self.subTest(flags=flags):
                child = Mock()
                self.assertEqual(self.inspect(manifest, *flags, interaction=child), "")
                child.stage.assert_called_once_with("unlocking")
                child.inspection.assert_called_once_with(
                    {
                        "format": "workspace",
                        "kind": "full",
                        "host_component": True,
                        "native_auth_export": True,
                        "created_at": manifest["created_at"],
                        "architecture": "x86_64",
                        "source_workspace_id": "a" * 32,
                        "project": "/work/example",
                        "container_project": "/work/container",
                        "distro": "tumbleweed",
                        "session_count": 1,
                    }
                )

    def test_table_refuses_invalid_manifest_without_output(self):
        manifest = {**self.workspace(), "project": "relative"}
        output = io.StringIO()
        with (
            patch.object(
                backups,
                "_opened_archive",
                return_value=contextlib.nullcontext((manifest, None, None)),
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(box.BoxError, "Invalid project path"),
        ):
            box.main(["backup-info", "example.ocbox", "--table"])
        self.assertEqual(output.getvalue(), "")

    def test_conflicting_format_options_refuse_before_tool_checks(self):
        with (
            patch.object(backups.crypto, "tools") as tools,
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as exc,
        ):
            box.main(["backup-info", "example.ocbox", "--table", "--json"])
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("--json", errors.getvalue())
        self.assertIn("--table", errors.getvalue())
        tools.assert_not_called()

    def test_help_explains_archive_modes_without_tool_checks(self):
        common = ("regular Linux host user", "age-keygen")
        cases = {
            "backup": (
                "rootless Podman",
                "only the host",
                "current directory",
                "never overwrite",
            ),
            "backup-info": (
                "without extracting payloads or using Podman",
                "encrypted backup archive",
                "human-readable metadata table",
                "JSON metadata (default)",
            ),
            "restore": (
                "rootless Podman",
                "encrypted backup archive",
                "only the host component",
                "separate typed confirmation",
                "is left stopped",
            ),
        }
        for action, expected in cases.items():
            with self.subTest(action=action):
                with (
                    patch.object(backups.crypto, "tools") as tools,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    self.assertRaises(SystemExit) as exc,
                ):
                    box.main([action, "--help"])
                self.assertEqual(exc.exception.code, 0)
                tools.assert_not_called()
                normalized = " ".join(output.getvalue().split())
                for phrase in (*common, *expected):
                    self.assertIn(phrase, normalized)
