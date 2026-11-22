import contextlib
import io
import json
import os
import shutil
import tempfile
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
        self.check_crypto_tools = backups.crypto.tools
        decrypt_bytes = backups.crypto.decrypt_bytes

        def decrypt_manifest(path, identity):
            self.assertEqual(path.name, "manifest.age", "do not decrypt payloads")
            return decrypt_bytes(path, identity)

        guards.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        guards.enter_context(patch.object(backups.os, "geteuid", return_value=1000))
        guards.enter_context(patch.object(backups.resource, "setrlimit"))
        guards.enter_context(patch.object(backups.crypto, "tools"))
        guards.enter_context(
            patch.object(box, "check_backend", side_effect=AssertionError("no Podman"))
        )
        guards.enter_context(
            patch.object(backups, "command", side_effect=AssertionError("no Podman"))
        )
        for name in ("host_payload", "host_restore", "host_auth"):
            guards.enter_context(
                patch.object(backups, name, side_effect=AssertionError("no host data"))
            )
        self.decrypt_manifest = guards.enter_context(
            patch.object(backups.crypto, "decrypt_bytes", side_effect=decrypt_manifest)
        )
        guards.enter_context(
            patch.object(
                backups.crypto,
                "decrypt_stream",
                side_effect=AssertionError("do not decrypt payloads"),
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
            "oc_box": {
                "version": "1.6.1+g1234567",
                "commit": "d" * 40,
                "commit_count": 30,
                "dirty": False,
                "release_channel": "stable",
            },
            "source_workspace_id": "a" * 32,
            "project": "/work/example",
            "container_project": "/work/container",
            "configuration": {
                "podman_args": ["--env=TOP_SECRET=private-option", "--privileged"],
                "distro": "tumbleweed",
                "image": "example.org/tumbleweed:latest",
                "home": "/home/workspace",
                "cpus": "2",
                "memory": "4g",
                "pids": 64,
                "created": 1767323045,
                "ready": True,
                "devices": ["/dev/accel/accel0"],
                "credentials": {"mode": "env", "env": ["EXAMPLE_TOKEN"], "vault": []},
                "opencode_version": "2.3.0",
            },
            "sessions": {"sessions": ["ses_example"], "last": "ses_example"},
            "image_id": "b" * 64 if combined else None,
            "components": {name: {"sha256": "c" * 64, "bytes": 128} for name in names},
            "expanded_byte_limit": 4096,
            "limitations": ["not a running-process checkpoint"],
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
                manifest["extra"] = {"nested": ["legacy-json-metadata"]}
                manifest["configuration"]["extra"] = {"nested": ["legacy-json-config"]}
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

    def test_workspace_table_projects_standard_metadata(self):
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

    def test_malformed_podman_options_are_omitted_in_both_formats(self):
        manifest = self.workspace()
        options = manifest["configuration"]["podman_args"]
        options += [
            "--read-only=",
            "private-standalone",
            "arbitrary-prefix=private-value",
            "-e private-short-flag",
            "--env PRIVATE_SPLIT",
            "--env PRIVATE_ASSIGNMENT=secret",
            "--bad?private=value",
            "--PRIVATE_USES_CAPS=value",
            "--" + "x" * 128 + "=private-oversized",
            "--" + "y" * 128,
        ]
        original = options.copy()
        expected = [
            "--env=[value omitted]",
            "--privileged=[value omitted]",
            "--read-only=[value omitted]",
        ]
        output = self.inspect(manifest, "--json")
        self.assertEqual(json.loads(output)["configuration"]["podman_args"], expected)
        table = self.inspect(manifest, "--table")
        for value in expected:
            self.assertIn(value, table)
        for marker in (
            "private-",
            "arbitrary-prefix",
            "PRIVATE_",
            "--bad?",
            "x" * 64,
            "y" * 64,
        ):
            for text in (output, table):
                self.assertNotIn(marker, text)
        self.assertEqual(options, original)

    def test_large_workspace_manifest_projects_bounded_fields_before_rendering(self):
        manifest = self.workspace()
        config = manifest["configuration"]
        nested = {"private-leaf": "private-nested-secret"}
        for _ in range(60):
            nested = {"private-nested-key": [nested]}
        manifest["private-label-" + "k" * 1024**2] = [nested, "x" * 2_800_000]
        manifest["oc_box"]["commit"] = nested
        manifest["oc_box"]["future"] = nested
        config["future"] = nested
        config["image"] = nested
        config["created"] = [nested]
        config["pids"] = 10**400
        manifest["sessions"]["future"] = nested
        manifest["image_id"] = nested
        manifest["components"]["project.age"]["future"] = nested
        manifest["architecture"] += "\x1b[31m" + "a" * 4096 + "private-display-tail"
        manifest["project"] += "p" * 4096 + "private-display-tail"
        sessions = ["ses_" + "s" * 4096 + "private-display-tail"] + [
            f"ses_example_{i}" for i in range(8192)
        ]
        manifest["sessions"].update(sessions=sessions, last=sessions[0])
        config["devices"] *= 128
        config["podman_args"] = [f"--env=PRIVATE_{i}" for i in range(128)]
        config["credentials"] = {
            "mode": "selected",
            "env": [f"ENV_{i}" for i in range(128)],
            "vault": [f"VAULT_{i}" for i in range(128)],
        }
        manifest["limitations"] += [nested, "l" * 4096 + "private-display-tail"] + [
            "known-text"
        ] * 128
        size = len(json.dumps(manifest).encode())
        self.assertGreater(size, 3 * 1024**2)
        self.assertLess(size, backups.crypto.MAX_SMALL - 64 * 1024)

        with patch.object(cli_table, "fields", wraps=cli_table.fields) as fields:
            output = self.inspect(manifest, "--table")
        fields.assert_called_once()
        projection = fields.call_args.args[0]
        for entries in (
            projection["configuration"]["devices"],
            projection["configuration"]["podman_args"],
            projection["configuration"]["credentials"]["env"],
            projection["configuration"]["credentials"]["vault"],
            projection["sessions"]["sessions"],
            projection["limitations"],
        ):
            self.assertLessEqual(len(entries), 17)
            self.assertRegex(entries[-1], r"^\[\d+ more omitted\]$")
            self.assertTrue(
                all(type(item) is str and len(item) <= 160 for item in entries)
            )
        for value in (
            projection["project"],
            projection["architecture"],
            projection["sessions"]["last"],
        ):
            self.assertLessEqual(len(value), 160)
            self.assertTrue(value.endswith("…"))
        self.assertLess(len(output.encode()), 32 * 1024)
        self.assertIn(r"\u001b[31m", output)
        self.assertNotIn("\x1b", output)
        for marker in ("private-", "PRIVATE_", "future", "configuration.image"):
            self.assertNotIn(marker, output)

    @unittest.skipUnless(
        shutil.which("age") and shutil.which("age-keygen"),
        "optional age host tools unavailable",
    )
    def test_table_reads_real_age_portable_and_host_envelopes(self):
        crypto = backups.crypto
        crypto.tools.side_effect = self.check_crypto_tools
        with tempfile.TemporaryDirectory(prefix="oc-box-backup-info-age-") as directory:
            root = Path(directory)
            private = crypto.small(["age-keygen"])
            recipient = (
                crypto.small(["age-keygen", "-y"], data=private).decode().strip()
            )
            identity = root / "identity.txt"
            with identity.open("xb") as output:
                # The enclosing directory is private; enforce the identity file
                # policy before writing any key bytes.
                os.fchmod(output.fileno(), 0o600)
                output.write(private)
            for kind in ("portable", "host"):
                with self.subTest(kind=kind):
                    stage = root / kind
                    stage.mkdir()
                    if kind == "portable":
                        manifest = self.workspace()
                        manifest["configuration"]["podman_args"].append(
                            "private-malformed"
                        )
                        manifest["configuration"]["extra"] = {
                            "nested": ["private-config-extension"]
                        }
                        manifest["sessions"]["host_sessions"] = {
                            "ses_example": "ses_private_archive"
                        }
                    else:
                        manifest = {
                            "format": "oc-box-backup",
                            "format_version": 2,
                            "kind": "host",
                            "components": {"host.age": {}, "host-auth.age": {}},
                        }
                    manifest["extra"] = {"nested": ["private-manifest-extension"]}
                    _, archive_recipient = crypto.generate_key(
                        [recipient], stage / "key.age"
                    )
                    for name in manifest["components"]:
                        component = stage / name
                        crypto.encrypt_bytes(
                            b"private-synthetic-payload", archive_recipient, component
                        )
                        manifest["components"][name] = crypto.digest(component)
                    crypto.encrypt_bytes(
                        json.dumps(manifest).encode(),
                        archive_recipient,
                        stage / "manifest.age",
                    )
                    archive = root / f"{kind}.ocbox"
                    with archive.open("wb") as stream:
                        crypto.pack(stage, stream)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(
                            box.main(
                                [
                                    "backup-info",
                                    str(archive),
                                    "--identity",
                                    str(identity),
                                    "--table",
                                ]
                            ),
                            0,
                        )
                    text = output.getvalue()
                    if kind == "portable":
                        self.assertIn("/work/example", text)
                        self.assertIn("--env=[value omitted]", text)
                        self.assertIn(
                            manifest["components"]["project.age"]["sha256"], text
                        )
                    else:
                        self.assertEqual(
                            text,
                            cli_table.fields({"format": "host", "format_version": 2})
                            + "\n",
                        )
                    self.assertNotIn("private-", text)
                    self.assertNotIn("ses_private_archive", text)
        self.assertEqual(self.decrypt_manifest.call_count, 2)

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
