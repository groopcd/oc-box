import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "native_host_port", APP / "tools/port_host_plugins.py"
)
assert spec and spec.loader
port = importlib.util.module_from_spec(spec)
spec.loader.exec_module(port)


# Synthetic legacy sources exercise explicit, caller-reviewed fingerprints.
# None is a canonical personal implementation or a production trusted hash.
SYNTHETIC_LEGACY_SOURCES = {
    "opencode-sandbox": 'export default async () => ({ "tool.execute.before": async () => {} })\n',
    "python-isolation": 'export default async () => ({ "shell.env": async (_, out) => { out.env.PIP_REQUIRE_VIRTUALENV = "1" } })\n',
    "output-clamp": 'export default async () => ({ "tool.execute.after": async (_, out) => { out.output = out.output.slice(0, 8192) } })\n',
    "lemonade-models": "export default async () => ({ config: async config => { config.provider.lemonade = { models: {} } } })\n",
    "no-nested-sandbox": 'export default async () => ({ "tool.execute.before": async (_, out) => { if (out.args.command.includes("bwrap")) throw Error("nested fixture") } })\n',
    "pdf-to-text": 'export default async () => ({ "experimental.chat.messages.transform": async (_, out) => { out.messages = out.messages } })\n',
    "sandbox-uncloak": 'export default async () => ({ "experimental.chat.messages.transform": async () => {} })\n',
    "tool-definition-audit": 'const DESCRIPTION_OVERRIDES = new Map<string, string>([ /* no overrides */ ])\nexport default async () => ({ "tool.definition": async ({ toolID }, out) => { const replacement = DESCRIPTION_OVERRIDES.get(toolID); if (replacement) out.description = replacement } })\n',
    "copilot-gpt6-effort": 'export default async () => ({ "chat.headers": async (_, out) => { out.headers["x-oc-copilot-effort"] = "high" } })\n',
}


class NativeHostPortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-native-port-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "opencode"
        (self.source / "plugins").mkdir(parents=True)
        (self.source / "custom-plugins").mkdir()
        self.reviewed_sources = {}
        for name, content in SYNTHETIC_LEGACY_SOURCES.items():
            folder = (
                "custom-plugins"
                if name in ("sandbox-uncloak", "pdf-to-text", "copilot-gpt6-effort")
                else "plugins"
            )
            file = self.source / folder / (name + ".ts")
            file.write_text(content)
            self.reviewed_sources[file.relative_to(self.source)] = hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
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

    def apply_reviewed(self):
        return port.apply(self.source, reviewed_sources=self.reviewed_sources)

    def review_arguments(self):
        return [
            argument
            for path, digest in self.reviewed_sources.items()
            for argument in ("--reviewed-source", str(path) + "=" + digest)
        ]

    def assert_refused_unchanged(self, message, source=None, reviewed_sources=None):
        source = source or self.source
        before = {
            file.relative_to(source): file.read_bytes() if file.is_file() else None
            for file in source.rglob("*")
        }
        with self.assertRaisesRegex(ValueError, message):
            port.apply(source, reviewed_sources=reviewed_sources)
        after = {
            file.relative_to(source): file.read_bytes() if file.is_file() else None
            for file in source.rglob("*")
        }
        self.assertEqual(after, before)
        self.assertFalse(list(source.glob(".native-plugin-backup-*")))
        self.assertFalse((source / "native-plugin-migration.json").exists())

    def isolated_source(self, name="output-clamp", content=None):
        source = self.root / "isolated"
        (source / "plugins").mkdir(parents=True)
        (source / "opencode.json").write_text('{"plugins":[]}')
        file = source / "plugins" / (name + ".ts")
        file.write_text(content or SYNTHETIC_LEGACY_SOURCES[name])
        return source, file

    def test_activation_inventory_matrix_does_not_select_dormant_or_disabled_sources(
        self,
    ):
        cases = {
            "configured custom": (
                "custom-plugins",
                ["./custom-plugins/output-clamp.ts"],
                True,
            ),
            "autoload plural": ("plugins", [], True),
            "autoload singular": ("plugin", [], True),
            "dormant reviewed custom": ("custom-plugins", [], False),
            "disabled custom path": (
                "custom-plugins",
                ["-./custom-plugins/output-clamp.ts"],
                False,
            ),
            "removed autoload id": ("plugins", ["-output-clamp"], False),
            "removed autoload path": ("plugins", ["-./plugins/output-clamp.ts"], False),
            "removed wildcard": ("plugins", ["-*"], False),
            "removed then selected": (
                "plugins",
                ["-*", "./plugins/output-clamp.ts"],
                True,
            ),
        }
        for index, (label, (folder, entries, active)) in enumerate(cases.items()):
            with self.subTest(case=label):
                source = self.root / ("activation-" + str(index))
                (source / folder).mkdir(parents=True)
                file = source / folder / "output-clamp.ts"
                content = SYNTHETIC_LEGACY_SOURCES["output-clamp"].encode()
                file.write_bytes(content)
                (source / "opencode.json").write_text(json.dumps({"plugins": entries}))
                reviews = {
                    file.relative_to(source): hashlib.sha256(content).hexdigest()
                }
                self.assertEqual(
                    port.audit(source, reviewed_sources=reviews)[2],
                    {"output-clamp"} if active else set(),
                )
                if active:
                    result = port.apply(source, reviewed_sources=reviews)
                    self.assertEqual(result["native_plugins"], ["output-clamp"])
                    self.assertFalse(file.exists())
                else:
                    self.assert_refused_unchanged("nothing to port", source, reviews)
                    self.assertEqual(file.read_bytes(), content)

    def test_preserved_entrypoint_and_support_matrix_requires_exact_review_and_keeps_sdk(
        self,
    ):
        cases = {
            "SDK free V1 factory": (
                "plugins/mystery.ts",
                'export default async () => ({ "shell.env": async () => {} })',
            ),
            "dormant custom factory": (
                "custom-plugins/mystery.ts",
                "export default async () => ({})",
            ),
            "disabled factory": (
                "plugins/mystery.ts",
                "export default async () => ({})",
            ),
            "local directory": (
                "vendor/custom/index.js",
                'export default { id: "custom", setup() {} }',
            ),
            "nested support": (
                "plugins/custom/support/deep.js",
                'import { tool } from "@opencode-ai/plugin"; export { tool }',
            ),
            "ambiguous native marker": (
                "plugins/mystery.ts",
                '// Plugin.define({ id: "safe" })\nexport default async () => ({})',
            ),
        }
        for index, (label, (relative, text)) in enumerate(cases.items()):
            with self.subTest(case=label):
                source = self.root / ("ownership-" + str(index))
                (source / "plugins").mkdir(parents=True)
                selected = source / "plugins/output-clamp.ts"
                selected.write_text(SYNTHETIC_LEGACY_SOURCES["output-clamp"])
                file = source / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
                entries = (
                    ["./vendor/custom"]
                    if label == "local directory"
                    else ["-mystery"]
                    if label == "disabled factory"
                    else []
                )
                (source / "opencode.json").write_text(
                    json.dumps(
                        {
                            "plugins": entries,
                            "permissions": [
                                {"action": "shell", "resource": "*", "effect": "deny"}
                            ],
                        }
                    )
                )
                (source / "package.json").write_text(
                    '{"dependencies":{"@opencode-ai/plugin":"1.19.5","unrelated":"unchanged"}}'
                )
                reviews = {
                    Path("plugins/output-clamp.ts"): hashlib.sha256(
                        selected.read_bytes()
                    ).hexdigest()
                }
                self.assert_refused_unchanged(
                    "Unknown V1 sources|requires review", source, reviews
                )
                reviews[Path(relative)] = hashlib.sha256(file.read_bytes()).hexdigest()
                if label == "local directory":
                    metadata = source / "vendor/custom/package.json"
                    metadata.write_text(
                        '{"name":"custom","exports":"./index.js","type":"module"}'
                    )
                    self.assert_refused_unchanged(
                        "requires review|Unknown V1 sources", source, reviews
                    )
                    reviews[metadata.relative_to(source)] = hashlib.sha256(
                        metadata.read_bytes()
                    ).hexdigest()
                original = file.read_bytes()
                receipt = port.apply(source, reviewed_sources=reviews)
                self.assertEqual(receipt["native_plugins"], ["output-clamp"])
                self.assertEqual(file.read_bytes(), original)
                dependencies = json.loads((source / "package.json").read_bytes())[
                    "dependencies"
                ]
                self.assertEqual(dependencies["@opencode-ai/plugin"], "1.19.5")
                self.assertEqual(dependencies["unrelated"], "unchanged")
                self.assertFalse(receipt["no_v1_sdk_or_bridge"])
                self.assertEqual(
                    json.loads((source / "opencode.json").read_bytes())["permissions"],
                    [{"action": "shell", "resource": "*", "effect": "deny"}],
                )

    def test_reviewed_dormant_known_source_is_preserved_during_other_plugin_migration(
        self,
    ):
        source, selected = self.isolated_source()
        dormant = source / "custom-plugins/pdf-to-text.ts"
        dormant.parent.mkdir()
        dormant.write_text(SYNTHETIC_LEGACY_SOURCES["pdf-to-text"])
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (selected, dormant)
        }
        result = port.apply(source, reviewed_sources=reviews)
        self.assertEqual(result["native_plugins"], ["output-clamp"])
        self.assertEqual(dormant.read_text(), SYNTHETIC_LEGACY_SOURCES["pdf-to-text"])
        self.assertFalse((source / "plugins/pdf-to-text").exists())

    def test_relative_import_ownership_inventory_preserves_nested_sdk_dependency(self):
        source, selected = self.isolated_source()
        entry = source / "custom-plugins/retained.mjs"
        entry.parent.mkdir()
        entry.write_text('export { default } from "../shared/nested/factory.mjs"')
        support = source / "shared/nested/factory.mjs"
        support.parent.mkdir(parents=True)
        support.write_text(
            'import { tool } from "@opencode-ai/plugin"; export default async () => ({ tool: { tool } })'
        )
        (source / "package.json").write_text(
            '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
        )
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (selected, entry)
        }
        self.assert_refused_unchanged(
            "requires review|Unknown V1 sources", source, reviews
        )
        reviews[support.relative_to(source)] = hashlib.sha256(
            support.read_bytes()
        ).hexdigest()
        result = port.apply(source, reviewed_sources=reviews)
        self.assertEqual(
            entry.read_text(), 'export { default } from "../shared/nested/factory.mjs"'
        )
        self.assertTrue(support.exists())
        self.assertEqual(
            json.loads((source / "package.json").read_bytes())["dependencies"][
                "@opencode-ai/plugin"
            ],
            "1.19.5",
        )
        self.assertFalse(result["no_v1_sdk_or_bridge"])

    def test_native_plugin_key_does_not_activate_overridden_legacy_entries(self):
        source, selected = self.isolated_source()
        dormant = source / "custom-plugins/pdf-to-text.ts"
        dormant.parent.mkdir()
        dormant.write_text(SYNTHETIC_LEGACY_SOURCES["pdf-to-text"])
        (source / "opencode.json").write_text(
            json.dumps({"plugin": ["./custom-plugins/pdf-to-text.ts"], "plugins": []})
        )
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (selected, dormant)
        }
        result = port.apply(source, reviewed_sources=reviews)
        self.assertEqual(result["native_plugins"], ["output-clamp"])
        self.assertEqual(dormant.read_text(), SYNTHETIC_LEGACY_SOURCES["pdf-to-text"])

    def test_preserved_source_importing_selected_source_requires_manual_port(self):
        source, selected = self.isolated_source()
        retained = source / "custom-plugins/retained.mjs"
        retained.parent.mkdir()
        retained.write_text('export { default } from "../plugins/output-clamp.ts"')
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (selected, retained)
        }
        self.assert_refused_unchanged("Retained plugin code imports", source, reviews)

    def test_bounded_inventory_refusals_precede_snapshot_and_source_writes(self):
        source, selected = self.isolated_source()
        reviews = {
            selected.relative_to(source): hashlib.sha256(
                selected.read_bytes()
            ).hexdigest()
        }
        for label, limit, value in (
            ("count", "MAX_INVENTORY_FILES", 1),
            ("bytes", "MAX_INVENTORY_BYTES", 1),
            ("depth", "MAX_INVENTORY_DEPTH", 1),
        ):
            with self.subTest(case=label), patch.object(port, limit, value):
                self.assert_refused_unchanged("inventory .*limit", source, reviews)

    def test_new_inventory_source_between_inspection_and_snapshot_is_not_overwritten(
        self,
    ):
        source, selected = self.isolated_source()
        reviews = {
            selected.relative_to(source): hashlib.sha256(
                selected.read_bytes()
            ).hexdigest()
        }
        original_inspect = port.inspect
        calls = []
        original = selected.read_bytes()
        extra = source / "plugins/late.mjs"

        def inspecting(*args, **kwargs):
            plan = original_inspect(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                extra.write_text("export default async () => ({})")
            return plan

        with (
            patch.object(port, "inspect", side_effect=inspecting),
            self.assertRaisesRegex(ValueError, "inventory changed"),
        ):
            port.apply(source, reviewed_sources=reviews)
        self.assertEqual(selected.read_bytes(), original)
        self.assertEqual(extra.read_text(), "export default async () => ({})")
        self.assertFalse(list(source.glob(".native-plugin-backup-*")))
        self.assertFalse((source / "plugins/output-clamp").exists())

    def test_dormant_review_bytes_are_admission_not_activation_or_future_approval(self):
        source, selected = self.isolated_source()
        dormant = source / "custom-plugins/pdf-to-text.ts"
        dormant.parent.mkdir()
        dormant.write_text(SYNTHETIC_LEGACY_SOURCES["pdf-to-text"])
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (selected, dormant)
        }
        dormant.write_text(dormant.read_text() + "// changed after review\n")
        self.assert_refused_unchanged("requires review", source, reviews)

    def test_same_name_disabled_source_is_not_retired_by_other_source_selection(self):
        source, disabled = self.isolated_source()
        active = source / "custom-plugins/output-clamp.ts"
        active.parent.mkdir()
        active.write_text(
            SYNTHETIC_LEGACY_SOURCES["output-clamp"]
            + "// separately selected implementation\n"
        )
        entries = [
            "-./plugins/output-clamp.ts",
            ["./custom-plugins/output-clamp.ts", {"chosen": True}],
        ]
        (source / "opencode.json").write_text(json.dumps({"plugins": entries}))
        reviews = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (active, disabled)
        }
        result = port.apply(source, reviewed_sources=reviews)
        self.assertEqual(result["native_plugins"], ["output-clamp"])
        self.assertFalse(active.exists())
        self.assertEqual(disabled.read_text(), SYNTHETIC_LEGACY_SOURCES["output-clamp"])
        plugins = json.loads((source / "opencode.json").read_bytes())["plugins"]
        self.assertIn(entries[0], plugins)
        self.assertIn(
            {
                "package": str(source / "plugins/output-clamp"),
                "options": {"chosen": True},
            },
            plugins,
        )
        self.assertFalse(result["no_v1_sdk_or_bridge"])

    def test_preserved_package_entries_cannot_certify_legacy_sdk_removal(self):
        for index, entry in enumerate(
            (
                "@unrelated/legacy",
                "-@unrelated/legacy",
                {"package": "@unrelated/legacy", "options": {"keep": True}},
                ["@unrelated/legacy", {"keep": True}],
            )
        ):
            with self.subTest(entry=entry):
                source = self.root / ("retained-package-" + str(index))
                (source / "plugins").mkdir(parents=True)
                selected = source / "plugins/output-clamp.ts"
                selected.write_text(SYNTHETIC_LEGACY_SOURCES["output-clamp"])
                (source / "opencode.json").write_text(json.dumps({"plugins": [entry]}))
                (source / "package.json").write_text(
                    '{"dependencies":{"@opencode-ai/plugin":"1.19.5","opencode-sandbox":"0.6.0"}}'
                )
                reviews = {
                    selected.relative_to(source): hashlib.sha256(
                        selected.read_bytes()
                    ).hexdigest()
                }
                result = port.apply(source, reviewed_sources=reviews)
                dependencies = json.loads((source / "package.json").read_bytes())[
                    "dependencies"
                ]
                self.assertEqual(dependencies.get("@opencode-ai/plugin"), "1.19.5")
                self.assertEqual(dependencies.get("opencode-sandbox"), "0.6.0")
                self.assertIn(
                    entry,
                    json.loads((source / "opencode.json").read_bytes())["plugins"],
                )
                self.assertFalse(result["no_v1_sdk_or_bridge"])

    def test_fully_selected_local_inventory_can_remove_unused_legacy_sdk(self):
        source, selected = self.isolated_source()
        (source / "package.json").write_text(
            '{"dependencies":{"@opencode-ai/plugin":"1.19.5","opencode-sandbox":"0.6.0"}}'
        )
        reviews = {
            selected.relative_to(source): hashlib.sha256(
                selected.read_bytes()
            ).hexdigest()
        }
        result = port.apply(source, reviewed_sources=reviews)
        dependencies = json.loads((source / "package.json").read_bytes())[
            "dependencies"
        ]
        self.assertNotIn("@opencode-ai/plugin", dependencies)
        self.assertNotIn("opencode-sandbox", dependencies)
        self.assertTrue(result["no_v1_sdk_or_bridge"])

    def test_only_ts_js_immediate_files_autoload_not_mts_or_mjs_or_nested_files(self):
        for index, (folder, suffix) in enumerate(
            (("plugins", ".mts"), ("plugin", ".mjs"), ("plugins/support", ".ts"))
        ):
            with self.subTest(folder=folder, suffix=suffix):
                source = self.root / ("not-autoload-" + str(index))
                directory = source / folder
                directory.mkdir(parents=True)
                file = directory / ("output-clamp" + suffix)
                file.write_text(SYNTHETIC_LEGACY_SOURCES["output-clamp"])
                (source / "opencode.json").write_text('{"plugins":[]}')
                reviews = {
                    file.relative_to(source): hashlib.sha256(
                        file.read_bytes()
                    ).hexdigest()
                }
                self.assertEqual(port.audit(source, reviewed_sources=reviews)[2], set())
                self.assert_refused_unchanged("nothing to port", source, reviews)

    def test_exact_package_identities_preserve_unrelated_packages_and_options(self):
        source = self.root / "package-identities"
        source.mkdir()
        # A coincidentally matching local vendor path is not a scoped package.
        (source / "@unrelated/pdf-to-text").mkdir(parents=True)
        (source / "https://example.invalid/output-clamp").mkdir(parents=True)
        vendor_file = source / "@unrelated/output-clamp.ts"
        vendor_file.write_text(SYNTHETIC_LEGACY_SOURCES["output-clamp"])
        unrelated = [
            "@unrelated/output-clamp",
            "@unrelated/output-clamp.ts",
            {"package": "@unrelated/pdf-to-text", "options": {"keep": True}},
            ["@unrelated/opencode-sandbox", {"keep": True}],
            "output-clamp",
            "pdf-to-text",
            "https://example.invalid/output-clamp",
            "-@unrelated/lemonade-models",
        ]
        config = source / "opencode.json"
        config.write_text(
            json.dumps({"plugins": [*unrelated, "opencode-sandbox@0.6.0"]})
        )
        _, files, names, unknown = port.audit(source)
        self.assertEqual(files, {})
        self.assertEqual(names, {"opencode-sandbox"})
        self.assertEqual(unknown, [])
        receipt = port.apply(source)
        self.assertEqual(receipt["native_plugins"], ["opencode-sandbox"])
        self.assertEqual(json.loads(config.read_text())["plugins"][1:], unrelated)
        self.assertEqual(
            vendor_file.read_text(), SYNTHETIC_LEGACY_SOURCES["output-clamp"]
        )

    def test_supported_sandbox_package_spellings_are_exact(self):
        source = self.root / "sandbox-identities"
        source.mkdir()
        config = source / "opencode.json"
        for target in (
            "opencode-sandbox",
            "opencode-sandbox@latest",
            "opencode-sandbox@0.6.0",
        ):
            with self.subTest(target=target):
                config.write_text(json.dumps({"plugins": [target]}))
                self.assertEqual(port.audit(source)[2], {"opencode-sandbox"})
        for target in ("opencode-sandbox@0.7.0", "opencode-sandbox@npm:other"):
            with self.subTest(target=target):
                config.write_text(json.dumps({"plugins": [target]}))
                with self.assertRaisesRegex(ValueError, "version needs review"):
                    port.audit(source)
        self.assertFalse(list(source.glob(".native-plugin-backup-*")))

    def test_local_source_spellings_and_extensions_require_the_same_exact_review(self):
        source = self.root / "local-identities"
        folder = source / 'custom with spaces "é"'
        folder.mkdir(parents=True)
        config = source / "opencode.json"
        for extension in (".ts", ".js", ".mts", ".mjs", ".oc-box-v1.mts"):
            file = folder / ("output-clamp" + extension)
            content = SYNTHETIC_LEGACY_SOURCES["output-clamp"].encode()
            file.write_bytes(content)
            relative = file.relative_to(source)
            approved = {relative: hashlib.sha256(content).hexdigest()}
            for target in (
                str(file),
                file.as_uri(),
                "./" + str(relative),
                str(relative),
            ):
                with self.subTest(extension=extension, target=target):
                    config.write_text(
                        json.dumps({"plugins": [[target, {"keep": True}]]})
                    )
                    before = config.read_bytes()
                    _, files, names, unknown = port.audit(
                        source, reviewed_sources=approved
                    )
                    self.assertEqual(names, {"output-clamp"})
                    self.assertEqual(files, {relative: content})
                    self.assertEqual(unknown, [])
                    self.assertEqual(config.read_bytes(), before)
                    self.assertEqual(file.read_bytes(), content)
                    config.write_text(
                        json.dumps(
                            {"plugins": [[target, {"keep": True}], "-" + target]}
                        )
                    )
                    _, disabled_files, disabled_names, disabled_unknown = port.audit(
                        source, reviewed_sources=approved
                    )
                    self.assertEqual(disabled_names, set())
                    self.assertEqual(disabled_files, {relative: content})
                    self.assertEqual(disabled_unknown, [])
                    self.assertEqual(file.read_bytes(), content)
        self.assertFalse(list(source.glob(".native-plugin-backup-*")))

    def test_configured_recognized_directories_require_manual_port_before_writes(self):
        for index, spelling in enumerate(
            ("relative", "absolute", "uri", "bare-relative")
        ):
            with self.subTest(spelling=spelling):
                source = self.root / ("directory-" + str(index))
                directory = source / "vendor/output-clamp"
                directory.mkdir(parents=True)
                (directory / "index.js").write_text(
                    'export default { id: "custom-output-clamp", setup() {} }'
                )
                target = {
                    "relative": "./vendor/output-clamp",
                    "absolute": str(directory),
                    "uri": directory.as_uri(),
                    "bare-relative": "vendor/output-clamp",
                }[spelling]
                (source / "opencode.json").write_text(json.dumps({"plugins": [target]}))
                self.assert_refused_unchanged("director|manual port", source)
                # An index-file digest is not a supported directory identity.
                self.assert_refused_unchanged(
                    "director|manual port",
                    source,
                    {
                        Path("vendor/output-clamp"): hashlib.sha256(
                            (directory / "index.js").read_bytes()
                        ).hexdigest()
                    },
                )
                self.assertFalse((source / "plugins/output-clamp").exists())

    def test_escaped_override_identifiers_and_interpolations_refuse_even_with_review(
        self,
    ):
        prefix = "const DESCRIPTION_OVERRIDES = new Map([]);\n"
        cases = {
            "leading escaped identifier": prefix
            + r'const alias = \u0044ESCRIPTION_OVERRIDES; alias.set("read", "custom");',
            "middle escaped identifier": prefix
            + r'const alias = DESCRIPTI\u004fN_OVERRIDES; alias.set("read", "custom");',
            "leading escaped interpolation": prefix
            + r'`${\u0044ESCRIPTION_OVERRIDES.set("read", "custom")}`;',
            "middle escaped interpolation": prefix
            + r'`${DESCRIPTI\u004fN_OVERRIDES.set("read", "custom")}`;',
            "Unicode comment terminator": prefix
            + "// example only\u2028"
            + r'const alias = \u0044ESCRIPTION_OVERRIDES; alias.set("read", "custom");',
            "quoted regex character": prefix
            + r'const pattern = /["]/; const alias = \u0044ESCRIPTION_OVERRIDES; alias.set("read", "custom");',
            "quoted declaration is not evidence": 'const example = "const DESCRIPTION_OVERRIDES = new Map([])";',
            "comment declaration is not evidence": "// const DESCRIPTION_OVERRIDES = new Map([])\n",
        }
        for index, (label, declaration) in enumerate(cases.items()):
            with self.subTest(case=label):
                source = self.root / ("escaped-" + str(index))
                (source / "plugins").mkdir(parents=True)
                (source / "opencode.json").write_text('{"plugins":[]}')
                relative = Path("plugins/tool-definition-audit.ts")
                content = (
                    declaration
                    + '\nexport default async () => ({ "tool.definition": async () => {} })\n'
                ).encode()
                (source / relative).write_bytes(content)
                self.assert_refused_unchanged(
                    "active description overrides|nonliteral|unsupported",
                    source,
                    {relative: hashlib.sha256(content).hexdigest()},
                )

    def test_escaped_quoted_and_comment_data_is_not_executable_override_evidence(self):
        content = r"""const DESCRIPTION_OVERRIDES = new Map([]);
const sample = "\u0044ESCRIPTION_OVERRIDES.set('read', 'data only')";
const other = 'DESCRIPTI\u004fN_OVERRIDES';
const staticTemplate = `\u0044ESCRIPTION_OVERRIDES`;
// \u0044ESCRIPTION_OVERRIDES.set("read", "comment only")
/* DESCRIPTI\u004fN_OVERRIDES.set("read", "comment only") */
export default async () => ({ "tool.definition": async ({ toolID }, out) => {
  const replacement = DESCRIPTION_OVERRIDES.get(toolID);
  if (replacement) out.description = replacement;
} });
"""
        source, file = self.isolated_source("tool-definition-audit", content)
        approved = {
            file.relative_to(source): hashlib.sha256(content.encode()).hexdigest()
        }
        receipt = port.apply(source, reviewed_sources=approved)
        self.assertEqual(receipt["retired"], ["tool-definition-audit"])
        self.assertEqual(
            (Path(receipt["safety_backup"]) / file.relative_to(source)).read_bytes(),
            content.encode(),
        )

    def test_marker_only_source_refuses_without_review_before_any_writes(self):
        source, _ = self.isolated_source(
            content="// tool.execute.after CLAMP_THRESHOLDS\nexport default async () => ({})\n"
        )
        before = {
            path: path.read_bytes() for path in source.rglob("*") if path.is_file()
        }
        with self.assertRaisesRegex(ValueError, "requires review"):
            port.apply(source)
        self.assertEqual(
            {path: path.read_bytes() for path in source.rglob("*") if path.is_file()},
            before,
        )
        self.assertFalse(list(source.glob(".native-plugin-backup-*")))
        # Python 3.10 does not restrict a trailing-slash glob to directories.
        self.assertFalse(
            [path for path in (source / "plugins").iterdir() if path.is_dir()]
        )

    def test_retained_markers_cannot_authorize_changed_reviewed_bytes(self):
        source, file = self.isolated_source(
            content="// tool.execute.after CLAMP_THRESHOLDS\n"
            + SYNTHETIC_LEGACY_SOURCES["output-clamp"]
        )
        approved = {
            file.relative_to(source): hashlib.sha256(file.read_bytes()).hexdigest()
        }
        file.write_text(
            file.read_text() + 'export const addedPolicy = "must not disappear"\n'
        )
        self.assert_refused_unchanged("requires review", source, approved)

    def test_explicit_review_binds_exact_relative_path_and_bytes_not_test_markers(self):
        source, file = self.isolated_source()
        content = file.read_bytes()
        approved = {file.relative_to(source): hashlib.sha256(content).hexdigest()}
        receipt = port.apply(source, reviewed_sources=approved)
        self.assertEqual(receipt["native_plugins"], ["output-clamp"])
        self.assertEqual(
            receipt["reviewed_source_sha256"],
            {str(path): digest for path, digest in approved.items()},
        )
        self.assertEqual(
            (Path(receipt["safety_backup"]) / file.relative_to(source)).read_bytes(),
            content,
        )
        self.assertFalse(file.exists())

    def test_invalid_missing_or_unused_review_approvals_refuse_before_writes(self):
        source, file = self.isolated_source()
        relative = file.relative_to(source)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        for label, approved in (
            ("missing", {}),
            ("wrong bytes", {relative: "0" * 64}),
            ("wrong path", {Path("plugins/python-isolation.ts"): digest}),
            ("absolute", {file: digest}),
            ("parent", {Path("../output-clamp.ts"): digest}),
            ("malformed digest", {relative: "not-a-sha256"}),
            ("not a mapping", []),
            ("invalid path type", {42: digest}),
            (
                "unused",
                {relative: digest, Path("custom-plugins/python-isolation.ts"): digest},
            ),
        ):
            with self.subTest(case=label):
                self.assert_refused_unchanged("review|approval", source, approved)

    def test_review_approval_cannot_override_active_or_nonliteral_audit_tables(self):
        audit = self.source / "plugins/tool-definition-audit.ts"
        declarations = (
            'const DESCRIPTION_OVERRIDES = new Map([["read", "custom"]])',
            "const DESCRIPTION_OVERRIDES = new Map([['read', 'custom']])",
            "const DESCRIPTION_OVERRIDES = new Map([[`read`, `custom`]])",
            "const DESCRIPTION_OVERRIDES = new Map(overrides)",
            "const DESCRIPTION_OVERRIDES = makeOverrides()",
            "const DESCRIPTION_OVERRIDES = new Map([...overrides])",
            "// const DESCRIPTION_OVERRIDES = new Map([])\nconst DESCRIPTION_OVERRIDES = makeOverrides()",
            'const decoy = "const DESCRIPTION_OVERRIDES = new Map([])"; const DESCRIPTION_OVERRIDES = makeOverrides()',
            'const DESCRIPTION_OVERRIDES = new Map([]); DESCRIPTION_OVERRIDES.set("read", "custom")',
            'const DESCRIPTION_OVERRIDES = new Map([]); const alias = DESCRIPTION_OVERRIDES; alias.set("read", "custom")',
            'const DESCRIPTION_OVERRIDES = new Map([]); `${DESCRIPTION_OVERRIDES.set("read", "custom")}`',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                audit.write_text(
                    "// tool.definition\n"
                    + declaration
                    + "\nexport default async () => ({})"
                )
                approved = {
                    **self.reviewed_sources,
                    audit.relative_to(self.source): hashlib.sha256(
                        audit.read_bytes()
                    ).hexdigest(),
                }
                self.assert_refused_unchanged(
                    "active description overrides|nonliteral", reviewed_sources=approved
                )

    def test_literal_empty_audit_tables_with_comments_and_direct_reads_can_be_reviewed(
        self,
    ):
        audit = self.source / "plugins/tool-definition-audit.ts"
        for expression in (
            "new Map()",
            "new Map([])",
            "new Map<string, string>([ /* an example override is not active */ ])",
            'new Map([\n// ["read", "example only"]\n])',
        ):
            with self.subTest(expression=expression):
                audit.write_text(
                    'const decoy = "DESCRIPTION_OVERRIDES";\n'
                    + "const DESCRIPTION_OVERRIDES = "
                    + expression
                    + ";\n"
                    + 'export default async () => ({ "tool.definition": async ({ toolID }, out) => { if (DESCRIPTION_OVERRIDES.has(toolID)) out.description = DESCRIPTION_OVERRIDES.get(toolID); const count = DESCRIPTION_OVERRIDES.size } })'
                )
                approved = {
                    **self.reviewed_sources,
                    audit.relative_to(self.source): hashlib.sha256(
                        audit.read_bytes()
                    ).hexdigest(),
                }
                self.assertEqual(
                    port.audit(self.source, reviewed_sources=approved)[2],
                    set(SYNTHETIC_LEGACY_SOURCES),
                )
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_duplicate_cli_review_approval_is_rejected_without_writes(self):
        argument = self.review_arguments()[:2]
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            port.main(["--config", str(self.source), "--apply", *argument, *argument])
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_audit_changes_nothing_and_explains_each_keep_retire_choice(self):
        before = {
            file: file.read_bytes() for file in self.source.rglob("*") if file.is_file()
        }
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            port.main(["--config", str(self.source), *self.review_arguments()])
        self.assertIn("Copilot builds reasoning variants", output.getvalue())
        self.assertIn("native PDF input", output.getvalue())
        for file, content in before.items():
            self.assertEqual(file.read_bytes(), content)
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_explicit_apply_installs_only_native_plugins_in_normal_config(self):
        before = (self.source / "opencode.jsonc").read_bytes()
        receipt = self.apply_reviewed()
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
        # The uninspected retained package must not be certified SDK-free.
        self.assertEqual(dependencies["@opencode-ai/plugin"], "1.19.5")
        self.assertEqual(dependencies["opencode-sandbox"], "0.6.0")
        self.assertEqual(dependencies["unrelated-library"], "1")
        self.assertEqual(dependencies["@opencode/plugin"], "2.2.6")
        backup = Path(receipt["safety_backup"])
        self.assertEqual((backup / "opencode.jsonc").read_bytes(), before)
        self.assertFalse(list((self.source / "custom-plugins").glob("*.ts")))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertFalse(receipt["no_v1_sdk_or_bridge"])
        self.assertEqual(receipt["preserved_packages"], ["native-unrelated-package"])
        for relative, digest in self.reviewed_sources.items():
            self.assertEqual(
                hashlib.sha256((backup / relative).read_bytes()).hexdigest(), digest
            )
        self.assertEqual(len(list(self.source.glob(".native-plugin-backup-*"))), 1)

    def test_plugin_migration_does_not_inspect_or_snapshot_unrelated_auth_data_or_cli(
        self,
    ):
        protected = {
            "auth.json": b'{"synthetic-private":"unchanged"}',
            "cli.json": b'{"synthetic-cli-preference":true}',
            "storage/database.bin": b"synthetic-database-bytes\x00\xff",
            "unrelated.env": b"SYNTHETIC_KEEP=unchanged\n",
        }
        for relative, content in protected.items():
            file = self.source / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
            file.chmod(0o640)
        original_read = port.read

        def guarded_read(path):
            self.assertNotIn(str(path.relative_to(self.source)), protected)
            return original_read(path)

        with patch.object(port, "read", side_effect=guarded_read):
            receipt = self.apply_reviewed()
        for relative, content in protected.items():
            file = self.source / relative
            self.assertEqual(file.read_bytes(), content)
            self.assertEqual(file.stat().st_mode & 0o777, 0o640)
            self.assertFalse((Path(receipt["safety_backup"]) / relative).exists())

    def test_unknown_sources_custom_audit_and_existing_destination_refuse_before_changes(
        self,
    ):
        extra = self.source / "plugins/unknown.ts"
        extra.write_text('import type { Plugin } from "@opencode-ai/plugin"')
        with self.assertRaisesRegex(ValueError, "Unknown V1 sources"):
            self.apply_reviewed()
        extra.unlink()
        audit = self.source / "plugins/tool-definition-audit.ts"
        audit.write_text(
            '// tool.definition\nconst DESCRIPTION_OVERRIDES = new Map([["read", "custom override"]])'
        )
        with self.assertRaisesRegex(ValueError, "active description overrides"):
            self.apply_reviewed()
        audit.write_text(SYNTHETIC_LEGACY_SOURCES["tool-definition-audit"])
        (self.source / "plugins/python-isolation").mkdir()
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            self.apply_reviewed()
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def old_sandbox_install(self):
        receipt = self.apply_reviewed()
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
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            # The dedicated receipt/update route must not audit the generated
            # native directories as if this were another full V1 migration.
            status = port.main(["--config", str(self.source), "--update-sandbox"])
        self.assertEqual(status, 0)
        self.assertIn("update current", output.getvalue())

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

    def test_sandbox_update_receipt_accepts_reviewed_preserved_sources_without_sdk_free_claim(
        self,
    ):
        retained = self.source / "custom-plugins/dormant.mjs"
        retained.write_text("export default async () => ({})")
        self.reviewed_sources[retained.relative_to(self.source)] = hashlib.sha256(
            retained.read_bytes()
        ).hexdigest()
        receipt = self.apply_reviewed()
        self.assertFalse(receipt["no_v1_sdk_or_bridge"])
        self.assertTrue(receipt["legacy_dependencies_retained"])
        self.assertEqual(port.update_sandbox(self.source)["status"], "current")
        self.assertEqual(retained.read_text(), "export default async () => ({})")

    def test_unrecognized_implementation_or_container_cannot_be_replaced(self):
        (self.source / "plugins/output-clamp.ts").write_text("custom unrelated code")
        with self.assertRaisesRegex(ValueError, "requires review"):
            self.apply_reviewed()
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
            self.apply_reviewed()
        self.assertTrue((moved / "python-isolation.ts").exists())
        self.assertFalse(list(self.source.glob(".native-plugin-backup-*")))

    def test_alternate_profile_or_second_apply_is_refused_without_deleting_evidence(
        self,
    ):
        marker = self.source / "host-bridge.js"
        marker.write_text("existing compatibility helper")
        with self.assertRaisesRegex(ValueError, "original normal host"):
            self.apply_reviewed()
        marker.unlink()
        receipt = self.apply_reviewed()
        # Completed native plugins remain exactly as installed on another run.
        before = (self.source / "plugins/python-isolation/index.js").read_bytes()
        with self.assertRaisesRegex(ValueError, "receipt already exists"):
            self.apply_reviewed()
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
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without V1 SDK/bridge", result.stdout)
