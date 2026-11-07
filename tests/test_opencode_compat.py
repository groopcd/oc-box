import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import oc_box as box
import opencode_compat as compat
import opencode_v2_io as v2
import sync_config


class OpenCodeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-compat-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)

    def test_version_output_and_major_specific_cli_contracts(self):
        for printed, normalized in (
            ("1.19.5\n", "1.19.5"),
            ("opencode v2.2.6\n", "2.2.6"),
            ("v2.0.1", "2.0.1"),
        ):
            self.assertEqual(compat.version(printed), normalized)
        for invalid in ("3.0.0", "0.0.0-beta", "2.2.6;evil", "unknown"):
            with self.assertRaises(ValueError):
                compat.version(invalid)
        self.assertEqual(
            compat.command("1.19.5", "oc", ["--pure", "export", "ses_test"]),
            ["oc", "--pure", "export", "ses_test"],
        )
        self.assertEqual(
            compat.command("2.2.6", "oc", ["--pure", "export", "ses_test"]),
            ["oc", "session", "export", "ses_test", "--standalone"],
        )
        self.assertIn("--max-count", compat.command("2.2.6", "oc", ["session", "list"]))
        command = compat.command("2.2.6", "oc", ["session", "list"])
        self.assertEqual(compat.command("2.2.6", "oc", command[1:]), command)
        tui = compat.tui("2.2.6", "oc", "/project", "ses_test", "provider/model")
        self.assertEqual(tui[0], "python3")
        self.assertNotIn("--model", tui)
        self.assertEqual(
            compat.tui("1.19.5", "oc", "/project", "ses_test", "provider/model"),
            ["oc", "/project", "--session", "ses_test", "--model", "provider/model"],
        )
        self.assertEqual(
            compat.model("provider/model#variant"),
            {"providerID": "provider", "id": "model", "variant": "variant"},
        )

    def test_legacy_auth_override_is_translated_without_secret_argv_or_files(self):
        entries = compat.legacy_credentials(
            '{"openai":{"type":"api","key":"synthetic-key"},"github-copilot":{"type":"oauth","access":"synthetic-access","refresh":"synthetic-refresh","expires":42}}'
        )
        self.assertEqual(entries[0]["value"], {"type": "key", "key": "synthetic-key"})
        self.assertEqual(entries[1]["value"]["methodID"], "device")
        self.assertTrue(
            all(
                entry["id"].startswith("cred_ocbox_") and entry["active"]
                for entry in entries
            )
        )

    def test_v2_exec_environment_is_explicit_and_secret_values_never_enter_argv(self):
        with patch.dict(
            os.environ,
            {
                "SYNTHETIC_TOKEN": "private-fixture-value",
                "UNSELECTED_SECRET": "not-forwarded",
            },
        ):
            command = box.exec_command(
                self.data,
                [
                    box.CONTAINER_OPENCODE,
                    "--pure",
                    "session",
                    "list",
                    "--format",
                    "json",
                ],
            )
        self.assertNotIn("private-fixture-value", " ".join(command))
        self.assertNotIn("UNSELECTED_SECRET", " ".join(command))
        self.assertEqual(
            command.environment["SYNTHETIC_TOKEN"], "private-fixture-value"
        )
        self.assertIn("--standalone", command)
        self.assertIn("OPENCODE_DB=oc-box-v2.db", command)
        self.assertIn("os.execvpe", " ".join(command))

    def test_production_v2_environment_bridge_clears_old_container_credentials(self):
        with patch.dict(
            os.environ, {"SYNTHETIC_TOKEN": "new-synthetic", "PATH": os.environ["PATH"]}
        ):
            command = box.exec_command(
                self.data,
                [
                    sys.executable,
                    "-c",
                    "import json,os; print(json.dumps({k:os.environ.get(k) for k in ('SYNTHETIC_TOKEN','OLD_PROVIDER_SECRET','HOME','OPENCODE_DB')}))",
                ],
            )
        start = command.index(self.data["container"]) + 1
        arguments = command[start:]
        environment = {
            **command.environment,
            **box.container_env(self.data),
            "SYNTHETIC_TOKEN": "new-synthetic",
            "OLD_PROVIDER_SECRET": "old-synthetic",
        }
        output = box.run(arguments, env=environment, capture=True)
        self.assertEqual(
            json.loads(output.stdout),
            {
                "SYNTHETIC_TOKEN": "new-synthetic",
                "OLD_PROVIDER_SECRET": None,
                "HOME": self.data["home"],
                "OPENCODE_DB": "oc-box-v2.db",
            },
        )

    def test_major_resume_state_is_separate_and_v1_remains_accessible(self):
        self.assertEqual(box.session_file(self.path).name, "sessions-v2.json")
        box.atomic_json(
            box.session_file(self.path), {"sessions": ["ses_v2"], "last": "ses_v2"}
        )
        self.data["opencode_version"] = "1.19.5"
        box.atomic_json(self.path, self.data)
        box.atomic_json(
            box.session_file(self.path), {"sessions": ["ses_v1"], "last": "ses_v1"}
        )
        self.assertEqual(box.session_state(self.path)["last"], "ses_v1")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        self.assertEqual(box.session_state(self.path)["last"], "ses_v2")

    def test_v2_profile_preserves_native_mcp_and_skips_host_service_privileges(self):
        source = self.root / "config/opencode"
        source.mkdir(parents=True)
        (source / "service.json").write_text(
            '{"env":{"SECRET":"private"},"remote":true}'
        )
        (source / "cli.json").write_text('{"theme":"test"}')
        (source / "opencode.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "servers": {
                            "fixture": {"type": "local", "command": ["uvx", "fixture"]}
                        }
                    },
                    "plugins": ["v2-plugin"],
                }
            )
        )
        with tarfile.open(
            fileobj=io.BytesIO(box.config_archive(self.data)), mode="r:gz"
        ) as archive:
            names = archive.getnames()
            self.assertNotIn("service.json", names)
            self.assertIn("cli.json", names)
            self.assertIn("plugins/oc-box-build-permissions/index.js", names)
            config = json.load(archive.extractfile("opencode.json"))
            self.assertEqual(
                config["mcp"]["servers"]["fixture"]["timeout"],
                {"catalog": 120000, "execution": 120000},
            )
            self.assertEqual(config["plugins"], ["v2-plugin"])
            profile = json.load(archive.extractfile("oc-box-profile.json"))
            self.assertIn("agents", profile)
            self.assertNotIn("lsp", profile)
            self.assertEqual(profile["update"], "disable")

    def test_v2_sdk_cache_retains_v1_sdk_as_user_dependency_and_reuses_warm_files(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            data = b'{"dependencies":{"@opencode-ai/plugin":"1.19.5","custom":"1"}}'
            member = tarfile.TarInfo("package.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        self.assertFalse(
            sync_config.sync_config(self.root, payload.getvalue(), "2.2.6")
        )
        config = self.root / ".config/opencode"
        dependencies = json.loads((config / "package.json").read_text())["dependencies"]
        self.assertEqual(
            dependencies,
            {
                "@opencode-ai/plugin": "1.19.5",
                "@opencode/plugin": "2.2.6",
                "custom": "1",
            },
        )
        (config / "node_modules").mkdir()
        (config / "node_modules/cached").touch()
        self.assertTrue(sync_config.sync_config(self.root, payload.getvalue(), "2.2.6"))
        self.assertTrue((config / "node_modules/cached").exists())

    def test_v2_runtime_cold_dependency_install_and_private_auth_paths(self):
        values = subprocess.CompletedProcess([], 0, "", "")

        def execute(command, **kwargs):
            if "auth" in command and "export" in command:
                return subprocess.CompletedProcess([], 0, "[]", "")
            return values

        with (
            patch.object(box, "ensure_opencode_version"),
            patch.object(box, "run", side_effect=execute) as run,
            patch.object(
                box.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    b"Plugin dependencies will be resolved by OpenCode if needed.\n",
                    b"",
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            box.prepare_runtime(self.path, self.data, "2.2.6")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(
            any(
                "npm" in command and "install" in command and "--prefix" in command
                for command in commands
            )
        )
        exported = next(
            call
            for call in run.call_args_list
            if "auth" in call.args[0] and "export" in call.args[0]
        )
        self.assertTrue(exported.kwargs["private"])
        self.assertIn("--standalone", exported.args[0])

    def test_exact_v2_package_is_installed_and_decorated_version_is_checked(self):
        results = [
            subprocess.CompletedProcess([], 0, "1.19.5", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "opencode v2.2.6", ""),
        ]
        with patch.object(box, "run", side_effect=results) as run:
            box.ensure_opencode_version(self.data, "2.2.6")
        self.assertEqual(run.call_args_list[1].args[0][-1], "@opencode/cli@2.2.6")
        self.assertIn("--force", run.call_args_list[1].args[0])

    def native(self):
        return {
            "info": {
                "id": "ses_source",
                "location": {"directory": str(self.project)},
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
            },
            "messages": [
                {
                    "id": "msg_source",
                    "type": "user",
                    "text": "unchanged msg_source",
                    "metadata": {"id": "msg_source"},
                    "time": {"created": 1},
                }
            ],
        }

    def test_native_archive_namespaces_ids_without_modifying_content_ids(self):
        export = self.native()
        archived = box.archive_session(export, self.data)
        self.assertNotEqual(archived["info"]["id"], "ses_source")
        self.assertNotIn("permissions", archived["info"])
        self.assertEqual(archived["messages"][0]["text"], "unchanged msg_source")
        self.assertEqual(archived["messages"][0]["metadata"]["id"], "msg_source")
        self.assertNotEqual(archived["messages"][0]["id"], "msg_source")
        self.assertEqual(export["info"]["id"], "ses_source")
        box.verify_session_import(self.data, export, json.loads(json.dumps(export)))
        changed = json.loads(json.dumps(export))
        changed["messages"][0]["text"] = "changed"
        with self.assertRaisesRegex(box.BoxError, "transcript differs"):
            box.verify_session_import(self.data, export, changed)

    def test_v1_archive_origin_is_verified_after_native_v2_migration(self):
        export = {
            "info": {"id": "ses_old", "directory": str(self.project)},
            "messages": [],
        }
        archive = box.archive_session(export, self.data)
        box.atomic_json(
            self.path.parent / "history" / (archive["info"]["id"] + ".json"), archive
        )
        migrated = {
            "info": {
                "id": archive["info"]["id"],
                "location": {"directory": str(self.path.parent / "controller")},
            },
            "messages": [],
        }
        self.assertTrue(box.restore_archive_directory(migrated, self.project))
        self.assertEqual(compat.directory(migrated), str(self.project))

    def test_private_capture_never_spools_or_repeats_secret_child_output(self):
        with patch.object(
            box.tempfile, "TemporaryFile", side_effect=AssertionError("no disk capture")
        ):
            output = box.run(
                [sys.executable, "-c", "print('synthetic-private')"],
                capture=True,
                private=True,
            )
            self.assertIn("synthetic-private", output.stdout)
            with self.assertRaises(box.BoxError) as error:
                box.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('synthetic-private'); sys.exit(1)",
                    ],
                    capture=True,
                    private=True,
                )
            self.assertNotIn("synthetic-private", str(error.exception))

    def test_unsupported_host_release_stops_reset_before_container_deletion(self):
        with (
            patch.object(box, "check_backend"),
            patch.object(
                box,
                "detect_opencode_release",
                side_effect=ValueError("unsupported release"),
            ),
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(ValueError, "unsupported release"):
                box.main(["reset", "--workspace", self.data["id"]])
        remove.assert_not_called()

    def test_major_mismatch_purge_preserves_resources_and_original_history(self):
        args = box.parser().parse_args(["purge", "--yes"])
        with (
            patch.object(box.dependencies, "require"),
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "remove_resources") as remove,
            patch.object(box, "stop_container") as stop,
        ):
            with self.assertRaisesRegex(box.BoxError, "major versions differ"):
                box.purge(self.path, self.data, args)
        remove.assert_not_called()
        stop.assert_not_called()

    def test_database_snapshot_keeps_original_bytes_and_never_reseeds_existing_v2(self):
        data = self.root / "data/opencode"
        data.mkdir(parents=True)
        source = data / "opencode.db"
        with contextlib.closing(sqlite3.connect(source)) as database:
            database.execute("CREATE TABLE fixture (value TEXT)")
            database.execute("INSERT INTO fixture VALUES ('original')")
            database.commit()
        before = source.read_bytes()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data.parent)}):
            v2.seed_database()
            target = data / "oc-box-v2.db"
            with contextlib.closing(sqlite3.connect(target)) as database:
                database.execute("UPDATE fixture SET value='v2 progress'")
                database.commit()
            v2.seed_database()
        self.assertEqual(source.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(target)) as database:
            self.assertEqual(
                database.execute("SELECT value FROM fixture").fetchone()[0],
                "v2 progress",
            )
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
