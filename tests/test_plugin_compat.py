import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import oc_box as box
import plugin_compat


class PluginCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node required for offline JS bridge contracts"
    )
    def test_bridge_native_hook_contracts_preserve_refusal_attachments_and_structured_output(
        self,
    ):
        app = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                "--no-warnings",
                str(app / "tests/plugin_bridge_fixture.mjs"),
                str(app / "plugin_bridge.js"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:", result.stdout)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-plugin-compat-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config/opencode"
        (self.config / "plugins").mkdir(parents=True)
        self.plugin = self.config / "plugins/fixture.ts"
        self.plugin.write_text(
            'import type { Plugin } from "@opencode-ai/plugin"\nexport const Fixture: Plugin = async () => ({"shell.env": async (_, output) => { output.env.TEST = "yes" }})\n'
        )
        self.data = {"home": "/home/example", "opencode_version": "2.2.6"}
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config.parent)})
        env.start()
        self.addCleanup(env.stop)

    def contents(self, release="2.2.6"):
        with tarfile.open(
            fileobj=io.BytesIO(
                box.config_archive({**self.data, "opencode_version": release})
            ),
            mode="r:gz",
        ) as archive:
            names = archive.getnames()
            self.assertEqual(
                len(names),
                len(set(names)),
                "Config archives cannot contain duplicate members",
            )
            return {name: archive.extractfile(name).read() for name in names}

    def test_v2_bridge_preserves_source_dependencies_and_native_files(self):
        (self.config / "plugins/native.js").write_text(
            'export default { id: "native", setup() {} }\n'
        )
        manifest = {
            "type": "module",
            "dependencies": {"@opencode-ai/plugin": "1.19.5", "custom": "1"},
        }
        (self.config / "package.json").write_text(json.dumps(manifest))
        before = self.plugin.read_bytes()
        files = self.contents()
        original, directory, script = plugin_compat.bridge_file(
            Path("plugins/fixture.ts")
        )
        self.assertNotIn("plugins/fixture.ts", files)
        self.assertEqual(files[str(original)], before)
        self.assertEqual(files[str(directory / "index.js")], script)
        self.assertTrue(original.name.endswith(".mts"))
        self.assertIn("plugins/native.js", files)
        self.assertEqual(json.loads(files["package.json"]), manifest)
        self.assertEqual(self.plugin.read_bytes(), before)
        self.assertEqual(
            json.loads((self.config / "package.json").read_text()), manifest
        )
        self.assertIn("oc-box-v1-bridge.js", files)

    def test_v1_archive_remains_original_with_no_generated_bridge_or_new_packages(self):
        files = self.contents("1.19.5")
        self.assertEqual(files["plugins/fixture.ts"], self.plugin.read_bytes())
        self.assertNotIn("oc-box-v1-bridge.js", files)
        self.assertNotIn("package.json", files)

    def test_generated_adapter_never_overwrites_an_existing_user_config_file(self):
        original, directory, _ = plugin_compat.bridge_file(Path("plugins/fixture.ts"))
        for path in (original, directory, Path("oc-box-v1-bridge.js")):
            with self.subTest(path=path):
                target = self.config / path
                target.write_text("user fixture")
                with self.assertRaisesRegex(box.BoxError, "conflict"):
                    self.contents()
                self.assertEqual(target.read_text(), "user fixture")
                target.unlink()

    def test_local_config_paths_and_sandbox_options_point_at_native_directories(self):
        values = {
            "plugin": [
                [str(self.plugin), {"setting": True}],
                "opencode-sandbox@0.6.0",
                "unrelated-v2",
            ],
            "providers": {
                "fixture": {"settings": {"baseURL": "http://localhost:13305/v1"}}
            },
        }
        (self.config / "opencode.json").write_text(json.dumps(values))
        files = self.contents()
        rewritten = json.loads(files["opencode.json"])
        _, directory, _ = plugin_compat.bridge_file(Path("plugins/fixture.ts"))
        self.assertEqual(
            rewritten["plugin"][0],
            ["/home/example/.config/opencode/" + str(directory), {"setting": True}],
        )
        self.assertEqual(
            rewritten["plugin"][1],
            "/home/example/.config/opencode/plugins/oc-box-compat-sandbox",
        )
        self.assertEqual(rewritten["plugin"][2], "unrelated-v2")
        manifest = json.loads(files["package.json"])
        self.assertEqual(
            manifest["dependencies"],
            {"@opencode-ai/plugin": "1.19.5", "opencode-sandbox": "0.6.0"},
        )
        self.assertEqual(
            json.loads(files["oc-box-v1-provider-config.json"])["providers"]["fixture"][
                "settings"
            ]["baseURL"],
            "http://host.containers.internal:13305/v1",
        )
        self.assertEqual(
            json.loads((self.config / "opencode.json").read_text()), values
        )

    def test_configured_sources_outside_autoload_directory_with_no_sdk_import_are_adapted(
        self,
    ):
        custom = self.config / "custom-plugins/pdf.ts"
        custom.parent.mkdir()
        custom.write_text(
            'export default async () => ({"experimental.chat.messages.transform": async (_, output) => {}})'
        )
        uri = custom.as_uri()
        (self.config / "opencode.json").write_text(json.dumps({"plugin": [uri]}))
        files = self.contents()
        relative = Path("custom-plugins/pdf.ts")
        original, directory, _ = plugin_compat.bridge_file(relative)
        self.assertEqual(files[str(original)], custom.read_bytes())
        self.assertNotIn(str(relative), files)
        self.assertEqual(
            json.loads(files["opencode.json"])["plugin"],
            ["/home/example/.config/opencode/" + str(directory)],
        )

    def test_local_plugin_resolution_is_bounded_to_copied_config_root(self):
        self.assertEqual(
            plugin_compat.local_source(self.plugin.as_uri(), self.config), self.plugin
        )
        self.assertIsNone(plugin_compat.local_source("../../outside.ts", self.config))
        self.assertIsNone(
            plugin_compat.local_source("file://remote.example/plugin.ts", self.config)
        )
        self.assertIsNone(plugin_compat.local_source("native-package", self.config))

    def test_copied_audit_caveat_reports_v2_json_schema_semantics(self):
        source = b'''"schemaChars is NOT: parameters arrives as the internal Zod object, measuring ~6.8x " +
        "the emitted JSON Schema. MCP tool definitions never reach this hook at all - only " +
        "built-in and plugin-registered tools appear here."'''
        copied = plugin_compat.copied_source(
            Path("plugins/tool-definition-audit.ts"), source
        )
        self.assertIn(b"JSON Schema supplied by the oc-box V2 request bridge", copied)
        self.assertNotIn(b"schemaChars is NOT", copied)
        self.assertEqual(
            plugin_compat.copied_source(Path("plugins/other.ts"), source), source
        )

    def test_copied_output_clamp_patch_is_exact_and_host_source_is_unchanged(self):
        content = b'const HEAD_BYTES = 5000; const TAIL_BYTES = 2000; const CLAMP_THRESHOLDS = 0;\nconst bytes = Buffer.byteLength(original, "utf8");\nif (original.length <= threshold) return\nconst raw = s.slice(0, budget)\nconst raw = s.slice(-budget)\nnl > budget * 0.8\nnl >= 0 && nl < budget * 0.2\n'
        corrected = plugin_compat.copied_source(
            Path("plugins/output-clamp.ts"), content
        )
        self.assertIn(b'Buffer.byteLength(original, "utf8") <= threshold', corrected)
        self.assertIn(b"utf8Slice(s, budget, true)", corrected)
        self.assertEqual(
            plugin_compat.copied_source(Path("plugins/another.ts"), content), content
        )
        self.assertEqual(
            plugin_compat.copied_source(
                Path("plugins/output-clamp.ts"), b"unfamiliar plugin"
            ),
            b"unfamiliar plugin",
        )

    @unittest.skipUnless(
        shutil.which("node"), "Node required for Unicode byte-budget behavior"
    )
    def test_copied_output_clamp_utf8_behavior_matches_byte_not_character_limits(self):
        source = """const HEAD_BYTES = 5000
const TAIL_BYTES = 2000
const CLAMP_THRESHOLDS = new Map()
function belowThreshold(original: string, threshold: number) {
  const bytes = Buffer.byteLength(original, "utf8")
  if (original.length <= threshold) return true
  return false
}
function headSlice(s: string, budget: number): string {
  const raw = s.slice(0, budget)
  const nl = raw.lastIndexOf("\\n")
  return nl > budget * 0.8 ? raw.slice(0, nl) : raw
}
function tailSlice(s: string, budget: number): string {
  const raw = s.slice(-budget)
  const nl = raw.indexOf("\\n")
  return nl >= 0 && nl < budget * 0.2 ? raw.slice(nl + 1) : raw
}
""".encode()
        corrected = plugin_compat.copied_source(Path("plugins/output-clamp.ts"), source)
        result = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                "--no-warnings",
                str(Path(__file__).with_name("copied_plugin_bytes.mjs")),
            ],
            input=json.dumps({"source": corrected.decode()}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:", result.stdout)

    def test_native_plugin_config_and_explicit_removals_keep_options_and_order(self):
        config = {
            "plugins": [
                {"package": "/old/plugin.ts", "options": {"setting": True}},
                "-/old/plugin.ts",
                "opencode-sandbox@0.6.0",
                "-opencode-sandbox",
                "native-plugin",
            ]
        }
        plugin_compat.rewrite_plugins(
            config, {"/old/plugin.ts": "/new/directory"}, "/new/sandbox"
        )
        self.assertEqual(
            config["plugins"],
            [
                {"package": "/new/directory", "options": {"setting": True}},
                "-/new/directory",
                "/new/sandbox",
                "-/new/sandbox",
                "native-plugin",
            ],
        )

    def test_bridge_paths_are_stable_and_safe_for_quotes_spaces_and_unicode(self):
        relative = Path('plugins/a "quoted" é.ts')
        original, directory, script = plugin_compat.bridge_file(relative)
        self.assertEqual(
            plugin_compat.bridge_file(relative), (original, directory, script)
        )
        self.assertIn(json.dumps("../" + original.name).encode(), script)
        self.assertEqual(
            plugin_compat.package_target("opencode-sandbox@0.6.0"),
            ("opencode-sandbox", "0.6.0"),
        )
        self.assertIsNone(plugin_compat.package_target("other-package"))
        self.assertIsNone(plugin_compat.package_target("opencode-sandbox@bad/path"))
