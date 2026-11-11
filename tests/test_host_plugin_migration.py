import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import migrate_host_plugins as migration

APP = Path(__file__).resolve().parents[1]


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

    def test_root_private_state_excluded_but_nested_same_basename_support_preserved(
        self,
    ):
        excluded = {
            "auth.json",
            "mcp-auth.json",
            ".env",
            ".npmrc",
            "service.json",
            "service.lock",
            "service.log",
            "service.pid",
            "service.sock",
            "package-lock.json",
            "bun.lock",
            "bun.lockb",
            "oc-box-profile.json",
            "oc-box-v1-bridge.js",
            "oc-box-v1-provider-config.json",
        }
        for name in excluded:
            (self.source / name).write_bytes(b"private root state must not copy\n")
        (self.source / "config.json").write_text('{"plugin":[]}')
        support = self.source / "custom-plugins/support"
        support.mkdir()
        expected = {
            name: ("plugin-owned " + name + "\n").encode()
            for name in sorted(
                excluded | {"opencode.jsonc", "config.json", "service.js"}
            )
        }
        for name, content in expected.items():
            (support / name).write_bytes(content)
        for folder in (
            "service-data",
            "oc-box-compat-support",
            "oc-box-build-permissions",
        ):
            (support / folder).mkdir()
            (support / folder / "value").write_bytes(b"nested support directory\n")
        for folder in ("oc-box-compat-0123456789abcdef", "oc-box-build-permissions"):
            generated = self.source / "plugins" / folder
            generated.mkdir()
            (generated / "index.js").write_text("must not reactivate old registration")
        # A root module is not a service registration merely because of its name.
        (self.source / "service.js").write_bytes(b"export const value = 42\n")
        before = self.originals()
        profile = migration.make_profile(self.source)
        copied = profile / "opencode"
        for name in sorted(excluded):
            with self.subTest(root_exclusion=name):
                self.assertFalse((copied / name).exists())
        for name, content in expected.items():
            with self.subTest(nested_support=name):
                self.assertEqual(
                    (copied / "custom-plugins/support" / name).read_bytes(), content
                )
        for folder in (
            "service-data",
            "oc-box-compat-support",
            "oc-box-build-permissions",
        ):
            self.assertEqual(
                (copied / "custom-plugins/support" / folder / "value").read_bytes(),
                b"nested support directory\n",
            )
        for folder in ("oc-box-compat-0123456789abcdef", "oc-box-build-permissions"):
            self.assertFalse((copied / "plugins" / folder).exists())
        self.assertEqual(
            (copied / "service.js").read_bytes(), b"export const value = 42\n"
        )
        self.assertEqual(self.originals(), before)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
        self.assertEqual(copied.stat().st_mode & 0o777, 0o700)
        for path in copied.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(
        shutil.which("node"), "Node required for supporting-module imports"
    )
    def test_migrated_plugin_required_service_and_config_imports_succeed(self):
        directory = self.source / "custom-plugins/required"
        (directory / "support/service").mkdir(parents=True)
        (directory / "package.json").write_text('{"type":"module","main":"index.js"}')
        (directory / "support/service.js").write_text(
            'export const marker = "service-import-known"\n'
        )
        (directory / "support/service/value.js").write_text(
            'export const value = "service-directory-known"\n'
        )
        (directory / "support/opencode.jsonc").write_text(
            '{"value":"config-support-known"}'
        )
        (directory / "support/auth.json").write_text('{"value":"auth-support-known"}')
        (directory / "index.js").write_text("""import { readFileSync } from "node:fs"
import { marker } from "./support/service.js"
import { value } from "./support/service/value.js"
const config = JSON.parse(readFileSync(new URL("./support/opencode.jsonc", import.meta.url)))
const auth = JSON.parse(readFileSync(new URL("./support/auth.json", import.meta.url)))
export const observed = [marker, value, config.value, auth.value]
export default { id: "required-support", setup() {} }
""")
        self.config["plugin"].append(str(directory))
        (self.source / "opencode.jsonc").write_text(json.dumps(self.config))
        before = self.originals()
        profile = migration.make_profile(self.source)
        config = json.loads((profile / "opencode/opencode.jsonc").read_text())
        entry = Path(config["plugins"][-1]) / "index.js"
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "const plugin = await import(process.argv[1]); console.log(JSON.stringify(plugin.observed))",
                entry.as_uri(),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "service-import-known",
                "service-directory-known",
                "config-support-known",
                "auth-support-known",
            ],
        )
        self.assertEqual(self.originals(), before)

    def test_discovery_helper_is_minimal_without_changing_intentional_config(self):
        for syntax in ("legacy", "native"):
            with self.subTest(syntax=syntax):
                provider = {
                    "name": "Private fixture",
                    "models": {
                        "inventory-model": {
                            "name": "Inventory model",
                            "id": "wire-model",
                            "modelID": "wire-model",
                            "tool_call": True,
                            "attachment": True,
                            "temperature": False,
                            "limit": {"context": 8192, "input": 4096, "output": 1024},
                            "capabilities": {
                                "tools": True,
                                "input": ["text", "image"],
                                "output": ["text"],
                            },
                            "options": {"apiKey": "model-options-private-sentinel"},
                            "settings": {"apiKey": "model-settings-private-sentinel"},
                            "headers": {
                                "authorization": "model-header-private-sentinel"
                            },
                            "variants": [
                                {
                                    "id": "private",
                                    "settings": {"value": "variant-private-sentinel"},
                                }
                            ],
                        },
                    },
                }
                options = {
                    "baseURL": "http://127.0.0.1:1234/v1",
                    "apiKey": "{env:DISCOVERY_PRIVATE_KEY}",
                    "headers": {"authorization": "nested-header-private-sentinel"},
                    "extra": {"unchanged": ["nested-option-private-sentinel", 7]},
                }
                if syntax == "legacy":
                    provider.update(
                        {"npm": "@ai-sdk/openai-compatible", "options": options}
                    )
                    document = {"provider": {"fixture": provider}, "plugin": []}
                else:
                    provider.update(
                        {
                            "package": "aisdk:@ai-sdk/openai-compatible",
                            "settings": options,
                            "headers": {
                                "authorization": "native-header-private-sentinel"
                            },
                        }
                    )
                    document = {"providers": {"fixture": provider}, "plugins": []}
                (self.source / "opencode.jsonc").write_text(json.dumps(document))
                before = self.originals()
                with patch.dict(
                    os.environ,
                    {
                        "DISCOVERY_PRIVATE_KEY": "expanded-private-sentinel",
                        "OPENCODE_CONFIG_CONTENT": '{"providers":{"fixture":{"settings":{"apiKey":"launch-private-sentinel"}}}}',
                    },
                ):
                    profile = migration.make_profile(self.source)
                copied = profile / "opencode"
                helper = json.loads((copied / "host-providers.json").read_text())
                self.assertTrue(
                    helper
                    == {
                        "provider": {
                            "fixture": {
                                **{
                                    key: provider[key]
                                    for key in ("name", "npm", "package")
                                    if key in provider
                                },
                                "options": {"baseURL": "http://127.0.0.1:1234/v1"},
                                "models": {
                                    "inventory-model": {
                                        "name": "Inventory model",
                                        "id": "wire-model",
                                        "modelID": "wire-model",
                                        "tool_call": True,
                                        "attachment": True,
                                        "temperature": False,
                                        "limit": {
                                            "context": 8192,
                                            "input": 4096,
                                            "output": 1024,
                                        },
                                        "capabilities": {
                                            "tools": True,
                                            "input": ["text", "image"],
                                            "output": ["text"],
                                        },
                                    }
                                },
                            }
                        }
                    },
                    "Discovery helper contains only endpoint/model inventory",
                )
                copied_config = json.loads((copied / "opencode.jsonc").read_text())
                self.assertEqual(
                    copied_config,
                    {**document, "plugins": []}
                    if syntax == "native"
                    else {"provider": document["provider"], "plugins": []},
                )
                self.assertEqual(self.originals(), before)
                for path in (
                    copied / "host-providers.json",
                    copied / "host-bridge.js",
                    profile / "run-opencode",
                    profile / "migration.json",
                ):
                    with self.subTest(artifact=path.name):
                        self.assertFalse(
                            "private-sentinel" in path.read_text(),
                            "Generated artifacts must not disclose synthetic secrets",
                        )

    def test_nested_excluded_basename_symlink_and_outside_source_still_refuse(self):
        support = self.source / "custom-plugins/support"
        support.mkdir()
        (support / "auth.json").symlink_to(self.source / "opencode.jsonc")
        with self.assertRaisesRegex(ValueError, "Symlinked"):
            migration.make_profile(self.source)
        self.assertFalse(list(self.source.parent.glob("opencode-host-v2-*")))
        (support / "auth.json").unlink()
        outside = self.root / "outside.js"
        outside.write_text('export default { id: "outside", setup() {} }')
        self.config["plugin"].append(str(outside))
        (self.source / "opencode.jsonc").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "outside the profile"):
            migration.make_profile(self.source)
        self.assertFalse(list(self.source.parent.glob("opencode-host-v2-*")))

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
