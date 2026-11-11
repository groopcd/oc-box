"""Native V2-only host plugin migration, inference and history contracts.

Private synthetic model/MCP, temporary config/data and a mocked sandbox engine.
No V1 SDK, factory or generic bridge is imported by the running service.
This verifies plugin behavior, not host filesystem/network sandbox isolation.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import opencode_v2_io as bridge


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


port = module("native_port_fixture", "tools/port_host_plugins.py")
fixture = module("native_provider_fixture", "tools/test_v2_podman.py")


# These literal bytes belong only to this disposable fixture. They are NOT
# canonical personal plugins or a production implementation trust list. Approve
# exactly these bytes, never a hash obtained by discovering arbitrary files.
LEGACY_FIXTURES = {
    "plugins/opencode-sandbox.ts": b'export default async () => ({ "tool.execute.before": async () => {} })\n',
    "plugins/python-isolation.ts": b'export default async () => ({ "shell.env": async (_, out) => { out.env.PIP_REQUIRE_VIRTUALENV = "1" } })\n',
    "plugins/output-clamp.ts": b'export default async () => ({ "tool.execute.after": async (_, out) => { out.output = out.output.slice(0, 8192) } })\n',
    "plugins/lemonade-models.ts": b"export default async () => ({ config: async config => { config.provider.lemonade = { models: {} } } })\n",
    "plugins/no-nested-sandbox.ts": b'export default async () => ({ "tool.execute.before": async (_, out) => { if (out.args.command.includes("bwrap")) throw Error("nested fixture") } })\n',
    "custom-plugins/pdf-to-text.ts": b'export default async () => ({ "experimental.chat.messages.transform": async (_, out) => { out.messages = out.messages } })\n',
    "custom-plugins/sandbox-uncloak.ts": b'export default async () => ({ "experimental.chat.messages.transform": async () => {} })\n',
    "custom-plugins/tool-definition-audit.ts": b'const DESCRIPTION_OVERRIDES = new Map<string, string>([ /* no active overrides */ ])\nexport default async () => ({ "tool.definition": async ({ toolID }, out) => { const replacement = DESCRIPTION_OVERRIDES.get(toolID); if (replacement) out.description = replacement } })\n',
    "custom-plugins/copilot-gpt6-effort.ts": b'export default async () => ({ "chat.headers": async (_, out) => { out.headers["x-oc-copilot-effort"] = "high" } })\n',
}


def activation_inventory_contract(binary, root):
    """Provider-free pinned-runtime check; every profile byte is synthetic."""
    source = root / "activation-config/opencode"
    project = root / "activation-project"
    source.mkdir(parents=True)
    project.mkdir()
    literal = {
        "plugins/output-clamp.ts": LEGACY_FIXTURES["plugins/output-clamp.ts"],
        "plugins/unloaded.mjs": b'export default { id: "fixture.extension", setup() {} }\n',
        "custom-plugins/pdf-to-text.ts": LEGACY_FIXTURES[
            "custom-plugins/pdf-to-text.ts"
        ],
        "custom-plugins/dormant/index.js": b'export default { id: "fixture.dormant", setup() {} }\n',
        "plugin/disabled/index.js": b'export default { id: "fixture.disabled", setup() {} }\n',
        "vendor/retained/package.json": b'{"name":"fixture-retained","type":"module","exports":"./src/index.js"}\n',
        "vendor/retained/src/index.js": b'import { id } from "./support/deep.mjs"; export default { id, setup() {} }\n',
        "vendor/retained/src/support/deep.mjs": b'export const id = "fixture.retained"\n',
    }
    for relative, content in literal.items():
        file = source / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    (source / "opencode.json").write_text(
        json.dumps(
            {
                "plugin": ["./custom-plugins/dormant"],
                "plugins": [
                    "./vendor/retained",
                    "-fixture.disabled",
                    "-output-clamp",
                    "./plugins/output-clamp.ts",
                ],
                "permissions": [{"action": "shell", "resource": "*", "effect": "deny"}],
            }
        )
    )
    legacy_config = (
        b'{"plugins":["./custom-plugins/dormant","./custom-plugins/pdf-to-text.ts"]}\n'
    )
    (source / "config.json").write_bytes(legacy_config)
    reviews = {
        relative: hashlib.sha256(content).hexdigest()
        for relative, content in literal.items()
    }
    receipt = port.apply(source, reviewed_sources=reviews)
    assert receipt["native_plugins"] == ["output-clamp"]
    assert (
        receipt["legacy_dependencies_retained"] and not receipt["no_v1_sdk_or_bridge"]
    )
    for relative, content in literal.items():
        if relative != "plugins/output-clamp.ts":
            assert (source / relative).read_bytes() == content, relative
    assert not (source / "plugins/pdf-to-text").exists()
    assert (source / "config.json").read_bytes() == legacy_config
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root / "activation-home"),
        "XDG_CONFIG_HOME": str(source.parent),
        "XDG_DATA_HOME": str(root / "activation-data"),
        "XDG_STATE_HOME": str(root / "activation-state"),
        "XDG_CACHE_HOME": str(root / "activation-cache"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "LANG": "C.UTF-8",
    }
    old_cwd = Path.cwd()
    try:
        os.chdir(project)
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as request:
            request("GET", "/api/model/default")
            plugins = request("GET", "/api/plugin")["data"]
            active = {
                item.get("id")
                for item in plugins
                if item["state"]["status"] == "active"
            }
            assert {"output-clamp", "fixture.retained"}.issubset(active), plugins
            assert {
                "fixture.disabled",
                "fixture.dormant",
                "fixture.extension",
                "pdf-to-text",
            }.isdisjoint(active), plugins
            assert not [
                item for item in plugins if item["state"]["status"] == "failed"
            ], plugins
    finally:
        os.chdir(old_cwd)
    print(
        "PASS: native 2.2.6 autoload/configured directory and nested support load; removals stay disabled and reviewed custom sources stay dormant."
    )


def main(binary):
    binary = str(Path(binary).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="oc-box-native-host-") as temporary:
        root = Path(temporary)
        source = root / "config/opencode"
        project = root / "project"
        (source / "plugins").mkdir(parents=True)
        (source / "custom-plugins").mkdir()
        project.mkdir()
        (root / "tmp").mkdir(mode=0o700)
        activation_inventory_contract(binary, root)
        reviewed = {}
        review_arguments = []
        for relative, content in LEGACY_FIXTURES.items():
            (source / relative).write_bytes(content)
            reviewed[relative] = hashlib.sha256(content).hexdigest()
            review_arguments.extend(
                ["--reviewed-source", relative + "=" + reviewed[relative]]
            )
        state = {"key": "synthetic-key", "markers": set(), "requests": 0, "errors": []}
        server, worker = fixture.make_provider(state)
        reasoning = []
        try:
            handler = server.RequestHandlerClass
            original_get, original_post = handler.do_GET, handler.do_POST

            def get(self):
                if self.path.endswith("/models") or self.path.endswith(
                    "/api/v1/health"
                ):
                    value = (
                        {
                            "data": [
                                {
                                    "id": "native-lemonade",
                                    "labels": ["tool-calling"],
                                    "max_context_window": 32000,
                                    "recipe_options": {"ctx_size": 16000},
                                }
                            ]
                        }
                        if self.path.endswith("/models")
                        else {"model_loaded": "native-lemonade"}
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(value).encode())
                else:
                    original_get(self)

            def post(self):
                if not self.path.endswith("/responses"):
                    original_post(self)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                reasoning.append(body)
                assert body["reasoning"]["effort"] == "high", body
                assert self.headers.get("x-oc-copilot-effort") is None
                item = {
                    "id": "msg_response",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "NATIVE_REASONING_OK",
                            "annotations": [],
                        }
                    ],
                }
                response = {
                    "id": "resp_fixture",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "gpt-6-fixture",
                    "output": [item],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "status": "in_progress", "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "NATIVE_REASONING_OK",
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": item,
                    },
                    {"type": "response.completed", "response": response},
                ):
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())

            handler.do_GET, handler.do_POST = get, post
            config = fixture.fixture_config(server.server_port)
            config["permission"] = "allow"  # isolated synthetic fixture only
            config["provider"]["fixture"]["options"]["baseURL"] = (
                f"http://127.0.0.1:{server.server_port}/v1"
            )
            config["provider"]["lemonade"] = {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/api/v1"},
                "models": {},
            }
            config["providers"] = {
                "effort-fixture": {
                    "package": "aisdk:@ai-sdk/openai",
                    "settings": {
                        "apiKey": "synthetic",
                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                    },
                    "models": {
                        "gpt-6-fixture": {
                            "variants": [
                                {
                                    "id": "high",
                                    "settings": {
                                        "reasoningEffort": "high",
                                        "reasoningSummary": "auto",
                                        "include": ["reasoning.encrypted_content"],
                                    },
                                }
                            ]
                        }
                    },
                }
            }
            config["plugin"] = [
                "opencode-sandbox@0.6.0",
                *[str(file) for file in (source / "custom-plugins").glob("*.ts")],
            ]
            (source / "opencode.jsonc").write_text(json.dumps(config))
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(source.parent),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "TMPDIR": str(root / "tmp"),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                fixture.KEY: state["key"],
                "LANG": "C.UTF-8",
                "OC_BOX_CONTAINER": "0",
            }
            # Exercise the actual review CLI against only the freshly-created
            # synthetic profile, with no inherited host config or credentials.
            with patch.dict(os.environ, env, clear=True):
                migration_status = port.main(
                    ["--config", str(source), "--apply", *review_arguments]
                )
            assert migration_status == 0
            receipt = json.loads((source / "native-plugin-migration.json").read_text())
            assert receipt["reviewed_source_sha256"] == reviewed
            snapshot = Path(receipt["safety_backup"])
            assert all(
                (snapshot / relative).read_bytes() == content
                for relative, content in LEGACY_FIXTURES.items()
            ), "Explicitly reviewed fixture bytes were not preserved"
            print(
                "PASS: explicit --reviewed-source CLI approvals bind the literal fixture bytes and preserve their safety snapshot."
            )
            subprocess.run(
                ["npm", "install", "--prefix", str(source), "--no-audit", "--no-fund"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=180,
            )
            runtime = (
                source / "node_modules/@anthropic-ai/sandbox-runtime/dist/index.js"
            )
            runtime.write_text("""export const SandboxManager = {
  async initialize(policy) { this.policy = policy }, getConfig() { return this.policy }, async wrapWithSandbox(command) { return "env NATIVE_SANDBOX_FIXTURE=1 " + command },
  cleanupAfterCommand() {}, async reset() {}
}\n""")
            assert not (source / "node_modules/@opencode-ai/plugin").exists()

            def cli(*args):
                value = subprocess.run(
                    [binary, *args, "--standalone"],
                    cwd=project,
                    env=env,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                assert value.returncode == 0, value.stderr[-3000:]
                return value.stdout

            output = cli(
                "run",
                "--model",
                "fixture/gpt-smoke",
                "Exercise native host credential and MCP tools.",
            )
            assert (
                "HOST_V2_ACCEPTANCE_OK" in output
                and not state["errors"]
                and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
            )
            ident = json.loads(cli("session", "list", "--format", "json"))[0]["id"]
            exported = json.loads(cli("session", "export", ident))
            assert "env NATIVE_SANDBOX_FIXTURE=1" not in json.dumps(exported), (
                "Native wrapper leaked into durable transcript"
            )
            output = cli(
                "run",
                "--model",
                "effort-fixture/gpt-6-fixture#high",
                "Verify native selected effort.",
            )
            assert "NATIVE_REASONING_OK" in output and reasoning
            if shutil.which("pdftotext"):
                text = b"BT /F1 12 Tf 50 700 Td (NATIVE_PDF_FIXTURE) Tj ET"
                objects = [
                    b"<< /Type /Catalog /Pages 2 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                    b"<< /Length "
                    + str(len(text)).encode()
                    + b" >>\nstream\n"
                    + text
                    + b"\nendstream",
                ]
                document = bytearray(b"%PDF-1.4\n")
                offsets = [0]
                for index, value in enumerate(objects, 1):
                    offsets.append(len(document))
                    document.extend(
                        str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n"
                    )
                position = len(document)
                document.extend(b"xref\n0 6\n0000000000 65535 f \n")
                for offset in offsets[1:]:
                    document.extend(f"{offset:010d} 00000 n \n".encode())
                document.extend(
                    b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
                    + str(position).encode()
                    + b"\n%%EOF\n"
                )
                pdf = project / "native.pdf"
                pdf.write_bytes(document)
                output = cli(
                    "run",
                    "--file",
                    str(pdf),
                    "--model",
                    "effort-fixture/gpt-6-fixture#high",
                    "Read this PDF using its text fallback.",
                )
                assert "NATIVE_REASONING_OK" in output
                assert "<attached-pdf" in json.dumps(
                    reasoning[-1]
                ) and "NATIVE_PDF_FIXTURE" in json.dumps(reasoning[-1]), (
                    "Native PDF fallback did not reach the provider"
                )
                print(
                    "PASS: native PDF fallback converts real fixture bytes for a model without PDF capability."
                )
            else:
                print(
                    "SKIP: actual PDF extraction; install pdftotext for this optional feature test."
                )
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
                ids = {item.get("id") for item in plugins}
                assert set(port.RETIRE).isdisjoint(ids)
                assert {
                    "python-isolation",
                    "pdf-to-text",
                    "output-clamp",
                    "lemonade-models",
                    "no-nested-sandbox",
                    "oc-box.host-sandbox",
                }.issubset(ids), ids
                models = request("GET", "/api/model" + query)["data"]
                model = next(
                    item for item in models if item["providerID"] == "lemonade"
                )
                assert (
                    model["id"] == "native-lemonade"
                    and model["limit"]["context"] == 16000
                )
            # The real /reload operation rebuilds Location services without
            # replacing the server process or clearing cached npm modules.
            # The mock engine deliberately retains its config after reset,
            # matching the real runtime contract that exposed the host bug.
            old_cwd = Path.cwd()
            try:
                os.chdir(project)
                with (
                    patch.dict(os.environ, env, clear=True),
                    bridge.server(binary) as request,
                ):
                    request("GET", "/api/model/default")
                    pid = request("GET", "/api/info")["pid"]
                    for index in range(3):
                        request("POST", "/api/location/reload")
                        request("GET", "/api/model/default")
                        state["markers"].clear()
                        resumed = subprocess.run(
                            [
                                binary,
                                "run",
                                "--server",
                                request.endpoint["url"],
                                "--session",
                                ident,
                                "--model",
                                "fixture/gpt-smoke",
                                "Exercise harmless shell/MCP tools after reload "
                                + str(index),
                            ],
                            env={
                                **env,
                                "OPENCODE_PASSWORD": request.endpoint["password"],
                            },
                            cwd=project,
                            input="",
                            capture_output=True,
                            text=True,
                            timeout=90,
                        )
                        assert (
                            resumed.returncode == 0
                            and "HOST_V2_ACCEPTANCE_OK" in resumed.stdout
                            and not state["errors"]
                            and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
                        ), resumed.stderr[-2000:] + resumed.stdout[-2000:]
                        assert request("GET", "/api/info")["pid"] == pid
                print(
                    "PASS: three native /reload cycles keep the same service process and reinitialize shell/MCP execution with cached runtime config."
                )
            finally:
                os.chdir(old_cwd)
            assert receipt["no_v1_sdk_or_bridge"]
            print(
                "PASS: V2-only native plugin loading, inference/MCP, live Lemonade context and unwrapped durable history with no V1 SDK/bridge."
            )
            print(
                "PASS: selected reasoning effort sent natively to Responses, with no Copilot fetch/header workaround."
            )
            print(
                "LIMIT: sandbox engine mocked; real host enforcement still requires host validation."
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            assert not worker.is_alive(), "Synthetic provider worker did not stop"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    main(parser.parse_args().opencode)
