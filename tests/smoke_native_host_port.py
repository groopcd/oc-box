"""Native V2-only host plugin migration, inference and history contracts.

Private synthetic model/MCP, temporary config/data and a mocked sandbox engine.
No V1 SDK, factory or generic bridge is imported by the running service.
This verifies plugin behavior, not host filesystem/network sandbox isolation.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import opencode_v2_io as bridge
import oc_box as box


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


port = module("native_port_fixture", "tools/port_host_plugins.py")
fixture = module("native_provider_fixture", "tools/test_v2_podman.py")


def main(binary):
    binary = str(Path(binary).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="oc-box-native-host-") as temporary:
        root = Path(temporary)
        source = root / "config/opencode"
        project = root / "project"
        (source / "plugins").mkdir(parents=True)
        (source / "custom-plugins").mkdir()
        project.mkdir()
        for name, markers in port.MARKERS.items():
            folder = source / (
                "custom-plugins"
                if name in port.RETIRE or name == "pdf-to-text"
                else "plugins"
            )
            (folder / (name + ".ts")).write_text(
                "// " + " ".join(markers) + "\nexport default async () => ({})"
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
            receipt = port.apply(source)
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(source.parent),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                fixture.KEY: state["key"],
                "LANG": "C.UTF-8",
                "OC_BOX_CONTAINER": "0",
            }
            subprocess.run(
                ["npm", "install", "--prefix", str(source), "--no-audit", "--no-fund"],
                env=env,
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    main(parser.parse_args().opencode)
