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
