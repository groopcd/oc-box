"""Offline native V2 check of the independent host profile migration.

Uses temporary config/data, synthetic models and a mocked sandbox runtime.
The configured-provider contract probe uses private APIs only, without inference.
This verifies native plugin loading/hooks, not real host sandbox isolation.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from tools import migrate_host_plugins as migration
from smoke_session_sync_v2 import FixtureFailure, private_environment, require

spec = importlib.util.spec_from_file_location(
    "v2_fixture_smoke", APP / "tools/test_v2_podman.py"
)
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from oc_box import opencode_v2_io as bridge


def configured_provider_refusal_probe(binary):
    """Measure deployed native state, not the plugin's pre-config setup prefix.

    Pinned 2.2.6 applies native config after external plugin transforms. Copied
    provider settings/addition/removal edits therefore refuse, rather than claim
    to implement the deferred successful-effective-settings-edit contract. The
    model-only control tests activation and source inventory, not precedence over
    later native configured model fields. No provider inference is requested.
    """
    fixed = (
        "oc-box V1 adapter: provider settings/addition/removal edits require a native V2 port; "
        "native config applies after plugin setup"
    )
    results = {}
    for mode in ("edit", "remove", "addition", "models", "throw"):
        try:
            with tempfile.TemporaryDirectory(
                prefix="oc-box-provider-" + mode + "-"
            ) as temporary:
                root = Path(temporary)
                source = root / "config/opencode"
                (source / "plugins").mkdir(parents=True)
                project = root / "project"
                project.mkdir()
                (
                    source / "plugins/copied.js"
                ).write_text("""import { appendFileSync } from "node:fs"
export default async () => ({ config: async config => {
  const provider = config.provider.openai
  const options = provider.options
  appendFileSync(process.env.HOOK_REPORT, JSON.stringify({ called: true,
    endpointVisible: options.baseURL === "http://127.0.0.1:9/original",
    deletionVisible: options.extra?.remove === "remove-known" }) + "\\n")
  const mode = process.env.PROBE_MODE
  if (mode === "throw") throw new Error("original-hook-fixed-refusal")
  if (mode === "edit") {
    // Queue an otherwise supported model edit before a later unsupported edit.
    // The full plan must refuse, without registering even this earlier edit.
    provider.models["must-not-register"] = { name: "Must not register" }
    options.baseURL = "http://127.0.0.1:9/edited"
    delete options.extra.remove
    config.provider["first-source-edit"] = { npm: "@ai-sdk/openai-compatible", options: { baseURL: "http://127.0.0.1:9/must-not-apply" }, models: {} }
  }
  if (mode === "remove") delete config.provider.openai
  if (mode === "addition") config.provider["first-source-edit"] = { npm: "@ai-sdk/openai-compatible", options: { baseURL: "http://127.0.0.1:9/must-not-apply" }, models: {} }
  if (mode === "models") provider.models["discovered"] = { name: "Discovered", limit: { context: 16384, output: 1024 } }
} })
""")
                observer = source / "observer"
                observer.mkdir()
                (observer / "package.json").write_text('{"type":"module"}')
                (observer / "index.js").write_text("""import { Schema } from "effect"
import { appendFileSync } from "node:fs"
export default { id: "configured-observer", async setup(ctx) {
  await ctx.provider.transform(editor => {
    const source = editor.get("openai")
    appendFileSync(process.env.SOURCE_REPORT, JSON.stringify({ callbackRan: true,
      sourceEndpointEdited: source?.provider.settings?.baseURL === "http://127.0.0.1:9/edited",
      sourceProviderPresent: !!source, sourceAdditionPresent: !!editor.get("first-source-edit"),
      partialModelPresent: !!source?.models.has("must-not-register"),
      discoveredModelPresent: !!source?.models.has("discovered") }) + "\\n")
  })
  await ctx.rpc.register({ id: "configured-observer", methods: { reload: { input: Schema.Unknown, output: Schema.Boolean } }, events: {} }, {
    reload: async () => { await ctx.provider.reload(); return true }
  })
} }
""")
                config = {
                    "plugins": [str(observer)],
                    "providers": {
                        "openai": {
                            "package": "aisdk:@ai-sdk/openai-compatible",
                            "settings": {
                                "baseURL": "http://127.0.0.1:9/original",
                                "apiKey": "{env:FIXTURE_PRIVATE}",
                                "extra": {
                                    "retained": "nested-known",
                                    "remove": "remove-known",
                                },
                                "headers": {"x-nested": "{env:FIXTURE_PRIVATE}"},
                            },
                            "headers": {"x-native": "{env:FIXTURE_PRIVATE}"},
                            "models": {
                                "known-model": {
                                    "name": "Known",
                                    "limit": {"context": 8192, "output": 1024},
                                }
                            },
                        }
                    },
                }
                (source / "opencode.json").write_text(json.dumps(config))
                original = {
                    file.relative_to(source): (file.read_bytes(), file.stat().st_mode)
                    for file in source.rglob("*")
                    if file.is_file()
                }
                # Import the actual candidate migration, never a frozen older Git
                # blob. Its own make_profile copies the actual candidate bridge.
                profile = migration.make_profile(source, root / "profile")
                helper = profile / "opencode/host-providers.json"
                require(
                    json.loads(helper.read_text())
                    == {
                        "provider": {
                            "openai": {
                                "package": "aisdk:@ai-sdk/openai-compatible",
                                "models": config["providers"]["openai"]["models"],
                                "options": {"baseURL": "http://127.0.0.1:9/original"},
                            }
                        }
                    },
                    "Configured-provider probe did not use the minimal migration helper.",
                )
                require(
                    (profile / "opencode/host-bridge.js").read_bytes()
                    == (migration.box.PACKAGE / "plugin_bridge.js")
                    .read_bytes()
                    .replace(
                        b"./oc-box-v1-provider-config.json", b"./host-providers.json"
                    ),
                    "Configured-provider probe did not copy the candidate bridge.",
                )
                wrappers = list(
                    (profile / "opencode/plugins").glob("host-compat-*/index.js")
                )
                require(
                    len(wrappers) == 1,
                    "Configured-provider probe has an ambiguous copied hook.",
                )
                match = re.search(
                    r'export default adapt\(original, ("[^"]+")\)',
                    wrappers[0].read_text(),
                )
                require(
                    match is not None,
                    "Configured-provider copied hook registration is invalid.",
                )
                copied_id = json.loads(match[1])
                helper_before = helper.read_bytes()
                # Reuse the already installed pinned native SDK; no npm install,
                # provider credentials, external service, or new dependency here.
                (profile / "opencode/node_modules").symlink_to(
                    Path(binary).parents[3],
                    target_is_directory=True,
                )
                env = private_environment(root / "private")
                env.update(
                    {
                        "XDG_CONFIG_HOME": str(profile),
                        "FIXTURE_PRIVATE": "synthetic-configured-probe-only",
                        "OPENAI_API_KEY": "synthetic-configured-probe-only",
                        "PROBE_MODE": mode,
                        "HOOK_REPORT": str(root / "hook.jsonl"),
                        "SOURCE_REPORT": str(root / "source.jsonl"),
                        # Plugins are the subject here, so do not inherit the history
                        # fixture's plugin-denial policy. The launch view supplies a
                        # synthetic deletion target, not final effective credentials.
                        "OPENCODE_CONFIG_CONTENT": json.dumps(
                            {
                                "snapshots": False,
                                "formatter": False,
                                "update": "disable",
                                "providers": {
                                    "openai": {
                                        "settings": {
                                            "extra": {"remove": "remove-known"}
                                        }
                                    }
                                },
                            }
                        ),
                    }
                )
                observations = {}
                with (
                    patch.dict(os.environ, env, clear=True),
                    bridge.server(binary) as request,
                ):
                    query = "?directory=" + quote(str(project), safe="")
                    request("GET", "/api/model/default" + query)
                    observer_reads = 0
                    for phase in ("activated", "reloaded"):
                        if phase == "reloaded":
                            require(
                                request(
                                    "POST",
                                    "/api/rpc/configured-observer/reload" + query,
                                    {"input": None},
                                ).get("output")
                                is True,
                                "Configured-provider supported reload RPC failed.",
                            )
                        plugins = request("GET", "/api/plugin" + query)["data"]
                        copied = [
                            plugin
                            for plugin in plugins
                            if plugin.get("id") == copied_id
                        ]
                        require(
                            len(copied) == 1,
                            "Configured-provider copied plugin was not observed.",
                        )
                        state = copied[0].get("state", {})
                        failed = state.get("status") == "failed"
                        active = state.get("status") == "active"
                        error = str(state.get("error", ""))
                        if mode in ("edit", "remove", "addition"):
                            require(
                                failed and fixed in error,
                                "Unsupported configured-provider edit did not fail with the fixed refusal.",
                            )
                        elif mode == "throw":
                            require(
                                failed
                                and "original-hook-fixed-refusal" in error
                                and fixed not in error,
                                "Configured-provider original hook refusal was replaced or lost.",
                            )
                        else:
                            require(
                                active,
                                "Read-only model-inventory control did not remain active.",
                            )
                        require(
                            any(
                                plugin.get("id") == "configured-observer"
                                and plugin.get("state", {}).get("status") == "active"
                                for plugin in plugins
                            ),
                            "Configured-provider observer did not activate.",
                        )
                        info = request("GET", "/api/provider/openai" + query)["data"]
                        settings = info.get("settings", {})
                        headers = {
                            key.lower(): value
                            for key, value in info.get("headers", {}).items()
                        }
                        current = request("GET", "/api/provider" + query)["data"]
                        checks = {
                            "copied_plugin_expected_state": failed
                            if mode != "models"
                            else active,
                            "configured_endpoint_preserved": settings.get("baseURL")
                            == "http://127.0.0.1:9/original",
                            "expanded_key_preserved": settings.get("apiKey")
                            == env["FIXTURE_PRIVATE"],
                            "nested_sibling_preserved": settings.get("extra", {}).get(
                                "retained"
                            )
                            == "nested-known",
                            "deletion_target_preserved": settings.get("extra", {}).get(
                                "remove"
                            )
                            == "remove-known",
                            "normalized_native_headers_preserved": headers.get(
                                "x-native"
                            )
                            == env["FIXTURE_PRIVATE"]
                            and headers.get("x-nested") == env["FIXTURE_PRIVATE"],
                            "no_partial_addition": not any(
                                provider["id"] == "first-source-edit"
                                for provider in current
                            ),
                        }
                        if mode in ("edit", "remove", "addition"):
                            checks["fixed_native_port_refusal"] = fixed in error
                        elif mode == "throw":
                            # HTTP preserves the original marker; exception-object
                            # identity itself remains the Node fixture's contract.
                            checks["original_hook_error_marker_preserved"] = (
                                "original-hook-fixed-refusal" in error
                                and fixed not in error
                            )
                        else:
                            checks["read_only_discovery_model_control_active"] = active
                        require(
                            all(checks.values()),
                            "Configured-provider deployed HTTP information was partially changed.",
                        )
                        source_rows = [
                            json.loads(line)
                            for line in (root / "source.jsonl").read_text().splitlines()
                        ]
                        fresh = source_rows[observer_reads:]
                        require(
                            bool(fresh)
                            and all(
                                row.get("callbackRan") is True
                                and row.get("sourceProviderPresent") is True
                                and row.get("sourceEndpointEdited") is False
                                and row.get("sourceAdditionPresent") is False
                                and row.get("partialModelPresent") is False
                                for row in fresh
                            ),
                            "Configured-provider source fold contains a partial adapter edit.",
                        )
                        if mode == "models":
                            require(
                                all(
                                    row.get("discoveredModelPresent") is True
                                    for row in fresh
                                ),
                                "Configured-provider model control did not reach the native source inventory.",
                            )
                            checks["discovered_source_model_observed"] = True
                        observer_reads = len(source_rows)
                        checks["native_source_fold_preserved"] = True
                        observations[phase] = checks
                hooks = [
                    json.loads(line)
                    for line in (root / "hook.jsonl").read_text().splitlines()
                ]
                require(
                    bool(hooks)
                    and all(
                        row
                        == {
                            "called": True,
                            "endpointVisible": True,
                            "deletionVisible": True,
                        }
                        for row in hooks
                    ),
                    "Configured-provider probe did not execute the copied V1 hook.",
                )
                require(
                    original
                    == {
                        file.relative_to(source): (
                            file.read_bytes(),
                            file.stat().st_mode,
                        )
                        for file in source.rglob("*")
                        if file.is_file()
                    },
                    "Configured-provider probe modified original source files.",
                )
                require(
                    helper.read_bytes() == helper_before,
                    "Configured-provider activation or reload modified the discovery helper.",
                )
                require(
                    not any(
                        marker in helper.read_text()
                        for marker in (
                            env["FIXTURE_PRIVATE"],
                            "apiKey",
                            "headers",
                            "x-native",
                            "nested-known",
                            "remove-known",
                        )
                    ),
                    "Configured-provider discovery helper persisted private effective settings.",
                )
                # Scan only generated regular sources/config, not external SDK or
                # private native DB/cache. The synthetic expanded value must stay
                # in native memory; intentional env templates remain unchanged.
                require(
                    all(
                        env["FIXTURE_PRIVATE"].encode() not in file.read_bytes()
                        for file in (profile / "opencode").rglob("*")
                        if file.is_file()
                        and "node_modules" not in file.relative_to(profile).parts
                    ),
                    "Configured-provider generated profile persisted an expanded credential.",
                )
                results[mode] = {
                    "observations": observations,
                    "actual_copied_hook_called": True,
                    "source_bytes_and_modes_unchanged": True,
                    "minimal_helper_unchanged_and_unexpanded": True,
                    "generated_profile_unexpanded": True,
                    "helper_sha256": hashlib.sha256(helper_before).hexdigest(),
                }
        except FixtureFailure:
            raise
        except Exception:
            # Native errors/settings can contain private values. Fixed categories
            # only, including failure during service shutdown or profile creation.
            raise FixtureFailure(
                "Configured-provider native probe failed; private diagnostics were suppressed."
            ) from None
        print(
            "PASS: native configured-provider "
            + mode
            + " checks after activation and provider reload.",
            flush=True,
        )
    print(
        "LIMIT: copied provider settings/addition/removal edits refuse; successful effective-settings edits remain deferred. "
        "Model control does not promise configured-model override precedence.",
        flush=True,
    )
    return results


def main(binary, copied_sources=None):
    binary = str(Path(binary).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="oc-box-host-profile-") as temporary:
        root = Path(temporary)
        source = root / "config/opencode"
        (source / "plugins").mkdir(parents=True)
        project = root / "project"
        project.mkdir()
        (source / "plugins/fixture.ts").write_text("""export default async () => ({
  "shell.env": async (_, output) => { output.env.PROFILE_ENV_FIXTURE = "yes" },
  "tool.definition": async (_, output) => { output.description += " HOST_DEFINITION_FIXTURE" },
  "tool.execute.before": async (input, output) => {
    if (input.tool === "bash" && output.args.command.includes("BLOCK_FIXTURE")) throw Error("host refusal fixture")
  },
  "tool.execute.after": async (input, output) => { if (input.tool === "bash") output.output += " HOST_OUTPUT_FIXTURE" }
})""")
        custom = source / "custom-plugins"
        custom.mkdir()
        (custom / "headers.ts").write_text("""export default async () => ({
  "chat.headers": async (_, output) => { output.headers["x-fixture-profile"] = "yes" }
})""")
        if copied_sources:
            # Test actual files from oc-box's non-autoloaded copies; never
            # modify host files or consult their real providers/auth/config.
            for folder in ("plugins", "custom-plugins"):
                (source / folder).mkdir(exist_ok=True)
                for file in (Path(copied_sources) / folder).glob("*.oc-box-v1.mts"):
                    name = file.name.replace(".oc-box-v1.mts", ".ts")
                    (source / folder / name).write_bytes(file.read_bytes())
        config = fixture.fixture_config(12345)
        config["permission"] = (
            "allow"  # synthetic fixture only; never generated by migration
        )
        config["plugin"] = [str(custom / "headers.ts"), "opencode-sandbox@0.6.0"]
        config["plugin"].extend(
            str(file) for file in custom.glob("*.ts") if file.name != "headers.ts"
        )
        (source / "opencode.jsonc").write_text(json.dumps(config))
        original = {
            path: path.read_bytes() for path in source.rglob("*") if path.is_file()
        }
        profile = migration.make_profile(source)
        env = {
            "HOME": str(root / "home"),
            "PATH": os.environ["PATH"],
            "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": str(profile),
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_TOOLDEF_REPORT": str(root / "audit.json"),
            "OPENCODE_CLAMP_DIR": str(root / "clamp"),
            "OC_BOX_CONTAINER": "0",
        }
        for kind in ("DATA", "CACHE", "STATE"):
            env["XDG_" + kind + "_HOME"] = str(root / kind.lower())
        subprocess.run(
            [
                "npm",
                "install",
                "--prefix",
                str(profile / "opencode"),
                "--no-audit",
                "--no-fund",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            check=True,
            timeout=180,
        )
        # Install a runtime stand-in at the exact generated import boundary.
        # The real runtime is tested only by the user on the host, never here.
        runtime = (
            profile
            / "opencode/node_modules/@anthropic-ai/sandbox-runtime/dist/index.js"
        )
        runtime.write_text("""export const SandboxManager = {
  async initialize(policy) { this.policy = policy }, getConfig() { return this.policy }, async wrapWithSandbox(command) { return command },
  cleanupAfterCommand() {}, async reset() {}
}\n""")
        state = {
            "key": "synthetic-profile-key",
            "markers": set(),
            "requests": 0,
            "errors": [],
        }
        server, thread = fixture.make_provider(state)
        try:
            handler = server.RequestHandlerClass
            original_post = handler.do_POST

            def check_headers(self):
                assert self.headers.get("x-fixture-profile") == "yes", (
                    "Native host header hook missing"
                )
                original_post(self)

            handler.do_POST = check_headers
            config = fixture.fixture_config(server.server_port)
            config["provider"]["fixture"]["options"]["baseURL"] = (
                f"http://127.0.0.1:{server.server_port}/v1"
            )
            copied_config = json.loads(
                (profile / "opencode/opencode.jsonc").read_text()
            )
            copied_config.update(
                {
                    key: value
                    for key, value in config.items()
                    if key not in ("plugin", "plugins")
                }
            )
            (profile / "opencode/opencode.jsonc").write_text(json.dumps(copied_config))
            env[fixture.KEY] = state["key"]
            args = [
                binary,
                "run",
                "--standalone",
                "--model",
                "fixture/gpt-smoke",
                "Exercise host-profile native hooks using synthetic tools.",
            ]
            result = subprocess.run(
                args,
                env=env,
                cwd=project,
                input="",
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert (
                result.returncode == 0 and "HOST_V2_ACCEPTANCE_OK" in result.stdout
            ), result.stderr[-2000:]
            assert not state["errors"] and state["markers"] == {
                "VAULT_SHELL_OK",
                "VAULT_MCP_OK",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary) as request,
            ):
                query = "?directory=" + quote(str(project), safe="")
                request("GET", "/api/model/default" + query)
                plugins = request("GET", "/api/plugin" + query)["data"]
                assert all(item["state"]["status"] == "active" for item in plugins), [
                    (item.get("id"), item["state"]["status"]) for item in plugins
                ]
                assert any(item.get("id") == "oc-box.host-sandbox" for item in plugins)
            if copied_sources:
                assert (root / "audit.json").is_file(), "Tool audit failed to run"
                assert all(
                    item["schema"] > 0
                    for item in json.loads((root / "audit.json").read_text())["tools"]
                )
            assert all(
                path.read_bytes() == content for path, content in original.items()
            )
            print(
                "PASS: generated host V2 profile runs native tools, local/configured legacy hooks and synthetic MCP with unchanged original files."
            )
            print(
                "LIMIT: sandbox runtime was mocked; real host filesystem/network enforcement still needs user validation."
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument(
        "--copied-sources",
        type=Path,
        help="optional oc-box copied plugin root, sources only",
    )
    args = parser.parse_args()
    configured_provider_refusal_probe(str(Path(args.opencode).resolve(strict=True)))
    main(args.opencode, args.copied_sources)
