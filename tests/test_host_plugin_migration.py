import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "migrate_host_plugins", APP / "tools/migrate_host_plugins.py"
)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class HostPluginMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-host-plugins-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "config/opencode"
        (self.source / "plugins").mkdir(parents=True)
        (self.source / "custom-plugins").mkdir()
        self.plugin = self.source / "plugins/isolation.ts"
        self.plugin.write_text(
            'import type { Plugin } from "@opencode-ai/plugin"\nexport default async () => ({ "shell.env": async (_, output) => { output.env.FIXTURE = "yes" } })'
        )
        self.custom = self.source / "custom-plugins/headers.ts"
        self.custom.write_text(
            'export default async () => ({"chat.headers": async (_, output) => {}})'
        )
        self.config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "fixture/model",
            "permission": {"edit": "ask"},
            "mcp": {"fixture": {"type": "local", "command": ["fixture"]}},
            "provider": {
                "fixture": {
                    "options": {
                        "apiKey": "{env:FIXTURE_KEY}",
                        "baseURL": "http://localhost:1234/v1",
                    }
                }
            },
            "plugin": [[str(self.custom), {"option": True}], "opencode-sandbox@0.6.0"],
        }
        (self.source / "opencode.jsonc").write_text(
            "// preserved settings\n" + json.dumps(self.config)
        )
        (self.source.parent / "opencode-sandbox").mkdir()
        (self.source.parent / "opencode-sandbox/config.json").write_text(
            '{"mode":"enforce"}'
        )

    def originals(self):
        return {
            path.relative_to(self.source): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def test_dry_run_does_not_change_source_or_create_profile_or_invoke_commands(self):
        original = self.originals()
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(migration.main(["--config", str(self.source)]), 0)
        self.assertIn("Dry run only", output.getvalue())
        self.assertIn("native V2 port", output.getvalue())
        self.assertEqual(self.originals(), original)
        self.assertEqual(list(self.source.parent.glob("opencode-host-v2-*")), [])
        run.assert_not_called()

    def test_separate_profile_preserves_settings_originals_and_host_sandbox_policy(
        self,
    ):
        original = self.originals()
        (self.source / "auth.json").write_text("must not copy auth")
        (self.source / "service.json").write_text("must not copy host service")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules/package").write_text("do not copy dependencies")
        root = migration.make_profile(self.source)
        copied = root / "opencode"
        config = json.loads((copied / "opencode.jsonc").read_text())
        for key in ("model", "permission", "mcp", "provider"):
            self.assertEqual(config[key], self.config[key])
        self.assertNotIn("plugin", config)
        self.assertEqual(config["plugins"][0]["options"], {"option": True})
        self.assertTrue(Path(config["plugins"][0]["package"]).is_dir())
        self.assertEqual(config["plugins"][1], str(copied / "plugins/host-sandbox"))
        self.assertIn(
            "SandboxManager", (copied / "plugins/host-sandbox/index.js").read_text()
        )
        self.assertNotIn("oc-box-build-permissions", str(list(copied.rglob("*"))))
        self.assertEqual(
            (root / "opencode-sandbox/config.json").read_bytes(),
            (self.source.parent / "opencode-sandbox/config.json").read_bytes(),
        )
        for relative, content in original.items():
            self.assertEqual((self.source / relative).read_bytes(), content)
        self.assertFalse((copied / "auth.json").exists())
        self.assertFalse((copied / "service.json").exists())
        self.assertFalse((copied / "node_modules").exists())
        provider = json.loads((copied / "host-providers.json").read_text())
        self.assertNotIn("apiKey", json.dumps(provider))
        self.assertIn("localhost", json.dumps(provider))
        manifest = json.loads((copied / "package.json").read_text())
        self.assertEqual(
            manifest["dependencies"]["@anthropic-ai/sandbox-runtime"], "0.0.76"
        )
        self.assertNotIn("opencode-sandbox", manifest["dependencies"])
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((copied / "opencode.jsonc").stat().st_mode & 0o777, 0o600)

    def test_new_profile_refuses_existing_output_and_nested_source_without_touching_files(
        self,
    ):
        output = self.root / "existing"
        output.mkdir()
        (output / "user-file").write_text("kept")
        with self.assertRaises(FileExistsError):
            migration.make_profile(self.source, output)
        with self.assertRaisesRegex(ValueError, "separate"):
            migration.make_profile(self.source, self.source / "nested")
        self.assertEqual((output / "user-file").read_text(), "kept")

    def test_symlink_or_unbridged_event_hook_requires_review_before_writing(self):
        (self.source / "plugins/link.ts").symlink_to(self.plugin)
        with self.assertRaisesRegex(ValueError, "regular"):
            migration.make_profile(self.source)
        self.assertFalse(list(self.source.parent.glob("opencode-host-v2-*")))
        (self.source / "plugins/link.ts").unlink()
        self.plugin.write_text("export default async () => ({ event: async () => {} })")
        with self.assertRaisesRegex(ValueError, "native port"):
            migration.make_profile(self.source)

    def test_native_directory_entry_and_supporting_files_preserved(self):
        directory = self.source / "plugins/native"
        directory.mkdir()
        (directory / "package.json").write_text(
            '{"type":"module","exports":{".":"./index.js"}}'
        )
        (directory / "index.js").write_text(
            'export default { id:"native", setup() {} }'
        )
        self.config["plugin"].append(str(directory))
        (self.source / "opencode.jsonc").write_text(json.dumps(self.config))
        profile = migration.make_profile(self.source)
        config = json.loads((profile / "opencode/opencode.jsonc").read_text())
        self.assertEqual(
            config["plugins"][-1], str(profile / "opencode/plugins/native")
        )
        self.assertEqual(
            (profile / "opencode/plugins/native/package.json").read_bytes(),
            (directory / "package.json").read_bytes(),
        )

    def test_configured_native_single_file_is_packaged_and_executable_helpers_keep_permissions(
        self,
    ):
        native = self.source / "custom-plugins/native.js"
        native.write_text('export default { id: "native", setup(ctx) {} }')
        script = self.source / "helper.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.config["plugin"].append(native.as_uri())
        (self.source / "opencode.jsonc").write_text(json.dumps(self.config))
        profile = migration.make_profile(self.source)
        config = json.loads((profile / "opencode/opencode.jsonc").read_text())
        self.assertTrue(Path(config["plugins"][-1]).is_dir())
        self.assertEqual((profile / "opencode/helper.sh").stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            native.read_text(), 'export default { id: "native", setup(ctx) {} }'
        )

    def test_launcher_is_explicit_standalone_and_does_not_embed_or_clear_credentials(
        self,
    ):
        root = migration.make_profile(self.source)
        script = (root / "run-opencode").read_text()
        self.assertIn("--standalone", script)
        self.assertIn(str(root), script)
        self.assertNotIn("OPENCODE_DISABLE_SANDBOX", script)
        self.assertNotIn("OPENCODE_PASSWORD", script)
        self.assertNotIn("OPENCODE_AUTH_CONTENT", script)
        self.assertNotIn("XDG_DATA_HOME", script)
        self.assertNotIn("allow", script)

    def test_tool_refuses_container_profile_before_any_modification(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            migration.main(["--apply", "--config", str(self.source)])
        self.assertFalse(list(self.source.parent.glob("opencode-host-v2-*")))

    @unittest.skipUnless(
        shutil.which("node"), "Node required for native sandbox hook contracts"
    )
    def test_host_sandbox_contract_is_fail_closed_without_using_container_adapter(self):
        result = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                "--no-warnings",
                str(APP / "tests/host_sandbox_fixture.mjs"),
                str(APP / "tools/host_sandbox.js"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not isolation validation", result.stdout)
