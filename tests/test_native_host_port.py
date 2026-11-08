import contextlib
import importlib.util
import io
import hashlib
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
    "native_host_port", APP / "tools/port_host_plugins.py"
)
assert spec and spec.loader
port = importlib.util.module_from_spec(spec)
spec.loader.exec_module(port)


class NativeHostPortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-native-port-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "opencode"
        (self.source / "plugins").mkdir(parents=True)
        (self.source / "custom-plugins").mkdir()
        for name, markers in port.MARKERS.items():
            folder = (
                "custom-plugins"
                if name in ("sandbox-uncloak", "pdf-to-text", "copilot-gpt6-effort")
                else "plugins"
            )
            (self.source / folder / (name + ".ts")).write_text(
                "// " + " ".join(markers) + "\nexport default async () => ({})\n"
            )
        self.config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "fixture/model",
            "permissions": [{"action": "edit", "resource": "*", "effect": "ask"}],
            "provider": {
                "lemonade": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {
                        "baseURL": "http://localhost:13305/api/v1",
                        "apiKey": "{env:LOCAL_KEY}",
                    },
                }
            },
            "plugin": [
                str(self.source / "custom-plugins/pdf-to-text.ts"),
                str(self.source / "custom-plugins/sandbox-uncloak.ts"),
                str(self.source / "custom-plugins/copilot-gpt6-effort.ts"),
                "opencode-sandbox@0.6.0",
                "native-unrelated-package",
            ],
        }
        (self.source / "opencode.jsonc").write_text(json.dumps(self.config))
        (self.source / "package.json").write_text(
            json.dumps(
                {
                    "private": True,
                    "dependencies": {
                        "@opencode-ai/plugin": "1.19.5",
                        "opencode-sandbox": "0.6.0",
                        "unrelated-library": "1",
                    },
                }
            )
        )

    def test_audit_changes_nothing_and_explains_each_keep_retire_choice(self):
        before = {
            file: file.read_bytes() for file in self.source.rglob("*") if file.is_file()
        }
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            port.main(["--config", str(self.source)])
        self.assertIn("Copilot builds reasoning variants", output.getvalue())
        self.assertIn("native PDF input", output.getvalue())
        for file, content in before.items():
            self.assertEqual(file.read_bytes(), content)
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_explicit_apply_installs_only_native_plugins_in_normal_config(self):
        before = (self.source / "opencode.jsonc").read_bytes()
        receipt = port.apply(self.source)
        self.assertEqual(set(receipt["native_plugins"]), set(port.KEEP))
        self.assertEqual(set(receipt["retired"]), set(port.RETIRE))
        config = json.loads((self.source / "opencode.jsonc").read_text())
        for key in ("model", "permissions", "provider"):
            self.assertEqual(config[key], self.config[key])
        self.assertNotIn("plugin", config)
        self.assertEqual(config["plugins"][-1], "native-unrelated-package")
        for name in port.KEEP:
            directory = self.source / "plugins" / name
            self.assertTrue(directory.is_dir())
            text = (directory / "index.js").read_text()
            self.assertNotIn("@opencode-ai/plugin", text)
            self.assertNotIn("adapt(", text)
        dependencies = json.loads((self.source / "package.json").read_text())[
            "dependencies"
        ]
        self.assertNotIn("@opencode-ai/plugin", dependencies)
        self.assertNotIn("opencode-sandbox", dependencies)
        self.assertEqual(dependencies["unrelated-library"], "1")
        self.assertEqual(dependencies["@opencode/plugin"], "2.2.6")
        backup = Path(receipt["safety_backup"])
        self.assertEqual((backup / "opencode.jsonc").read_bytes(), before)
        self.assertFalse(list((self.source / "custom-plugins").glob("*.ts")))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue(receipt["no_v1_sdk_or_bridge"])

    def test_unknown_sources_custom_audit_and_existing_destination_refuse_before_changes(
        self,
    ):
        extra = self.source / "plugins/unknown.ts"
        extra.write_text('import type { Plugin } from "@opencode-ai/plugin"')
        with self.assertRaisesRegex(ValueError, "Unknown V1 sources"):
            port.apply(self.source)
        extra.unlink()
        audit = self.source / "plugins/tool-definition-audit.ts"
        audit.write_text(
            '// tool.definition\nconst DESCRIPTION_OVERRIDES = new Map([["read", "custom override"]])'
        )
        with self.assertRaisesRegex(ValueError, "active description overrides"):
            port.apply(self.source)
        audit.write_text("// " + " ".join(port.MARKERS["tool-definition-audit"]))
        (self.source / "plugins/python-isolation").mkdir()
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            port.apply(self.source)
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def old_sandbox_install(self):
        receipt = port.apply(self.source)
        target = self.source / "plugins/opencode-sandbox/index.js"
        old = b"// synthetic previously-generated sandbox fixture"
        target.write_bytes(old)
        return receipt, target, old

    def test_sandbox_only_update_preserves_other_plugins_config_dependencies_and_previous_source(
        self,
    ):
        receipt, target, old = self.old_sandbox_install()
        before = {
            path: path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
            and path not in (target, self.source / "native-plugin-migration.json")
        }
        with patch.object(
            port, "PREVIOUS_SANDBOX_HASHES", {hashlib.sha256(old).hexdigest()}
        ):
            result = port.update_sandbox(self.source)
            self.assertEqual(result["status"], "planned")
            self.assertEqual(target.read_bytes(), old)
            updated = port.update_sandbox(self.source, apply_change=True)
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(Path(updated["backup"]).read_bytes(), old)
        self.assertEqual(
            target.read_bytes(), (APP / "tools/host_sandbox.js").read_bytes()
        )
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertTrue(Path(receipt["safety_backup"]).exists())
        self.assertEqual(
            port.update_sandbox(self.source, apply_change=True)["status"], "current"
        )

    def test_sandbox_only_update_refuses_custom_code_and_changed_runtime_dependency(
        self,
    ):
        _, target, old = self.old_sandbox_install()
        with self.assertRaisesRegex(ValueError, "customized or unrecognized"):
            port.update_sandbox(self.source, apply_change=True)
        self.assertEqual(target.read_bytes(), old)
        manifest = self.source / "package.json"
        value = json.loads(manifest.read_text())
        value["dependencies"]["@anthropic-ai/sandbox-runtime"] = "different"
        manifest.write_text(json.dumps(value))
        with (
            patch.object(
                port, "PREVIOUS_SANDBOX_HASHES", {hashlib.sha256(old).hexdigest()}
            ),
            self.assertRaisesRegex(ValueError, "runtime dependency changed"),
        ):
            port.update_sandbox(self.source, apply_change=True)
        self.assertFalse(list(self.source.glob(".native-sandbox-update-*")))

    def test_unrecognized_implementation_or_container_cannot_be_replaced(self):
        (self.source / "plugins/output-clamp.ts").write_text("custom unrelated code")
        with self.assertRaisesRegex(ValueError, "requires review"):
            port.apply(self.source)
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            port.main(["--config", str(self.source), "--apply"])

    def test_symlinked_plugin_directory_is_never_followed_for_removal(self):
        original = self.source / "plugins"
        moved = self.root / "external-plugins"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "not symlinked"):
            port.apply(self.source)
        self.assertTrue((moved / "python-isolation.ts").exists())
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_alternate_profile_or_second_apply_is_refused_without_deleting_evidence(
        self,
    ):
        marker = self.source / "host-bridge.js"
        marker.write_text("existing compatibility helper")
        with self.assertRaisesRegex(ValueError, "original normal host"):
            port.apply(self.source)
        marker.unlink()
        receipt = port.apply(self.source)
        # Completed native plugins remain exactly as installed on another run.
        before = (self.source / "plugins/python-isolation/index.js").read_bytes()
        with self.assertRaisesRegex(ValueError, "receipt already exists"):
            port.apply(self.source)
        self.assertEqual(
            (self.source / "plugins/python-isolation/index.js").read_bytes(), before
        )
        self.assertTrue(Path(receipt["safety_backup"]).is_dir())

    @unittest.skipUnless(
        shutil.which("node"), "Node required for native plugin contracts"
    )
    def test_native_hooks_work_without_legacy_module_or_bridge(self):
        result = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                "--no-warnings",
                str(APP / "tests/native_plugin_fixture.mjs"),
                str(APP / "tools/native_plugins"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without V1 SDK/bridge", result.stdout)
