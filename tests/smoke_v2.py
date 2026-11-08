"""Bounded offline tests against a separately installed, unmodified OpenCode 2.x.

Usage: python3 -B tests/smoke_v2.py --opencode /path/to/v2/opencode
No host registry, credentials, providers, or managed service is touched.
"""

import argparse
import base64
import http.cookiejar
import importlib.util
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import termios
from urllib.parse import quote
from urllib.request import (
    Request,
    build_opener,
    HTTPCookieProcessor,
    ProxyHandler,
)
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import (
    oc_box as box,
    opencode_compat as compat,
    opencode_v2_io as bridge,
    sync_config,
    workspace_service,
)


def host_fixture_probe(binary):
    """Exercise the opt-in host fixture's exact native config/model contract.

    Podman lifecycle remains host-only; this catches fixture protocol mistakes
    in CI before asking the operator to run the full disposable host test.
    """
    spec = importlib.util.spec_from_file_location(
        "v2_acceptance_probe", APP / "tools/test_v2_podman.py"
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    # Check retained version selection against actual native executables, not
    # just scripts that print fixture versions. V1 is intentionally installed
    # separately on PATH by the mainline CI job.
    v1 = shutil.which("opencode")
    assert v1 is not None
    with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
        root = Path(temporary)
        fixture.prepare_native_cli(root, Path(v1).resolve(), Path(binary).resolve())
        (root / "host-major").write_text("2")
        fixture.prepare_native_cli(
            root, Path(v1).resolve(), Path(binary).resolve(), resume=True
        )
    print(
        "PASS: fresh and retained fixture version routing with native OpenCode 1.19.5/2.2.6.",
        flush=True,
    )
    state = {
        "key": "synthetic-native-fixture-key",
        "markers": set(),
        "requests": 0,
        "errors": [],
    }
    provider, worker = fixture.make_provider(state)
    try:
        with tempfile.TemporaryDirectory(prefix="oc-box-host-contract-") as temporary:
            root = Path(temporary)
            config = root / "config/opencode"
            config.mkdir(parents=True)
            value = fixture.fixture_config(provider.server_port)
            value["provider"]["fixture"]["options"]["baseURL"] = (
                f"http://127.0.0.1:{provider.server_port}/v1"
            )
            (config / "opencode.json").write_text(json.dumps(value))
            env = {
                "HOME": str(root / "home"),
                "PATH": os.environ["PATH"],
                "LANG": "C.UTF-8",
                fixture.KEY: state["key"],
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    box.container_build_config("2.2.6")
                ),
            }
            for name in ("CONFIG", "DATA", "STATE", "CACHE"):
                env["XDG_" + name + "_HOME"] = str(root / name.lower())
            # Podman exec -i supplies a FIFO inside the container. V2 reads
            # that FIFO until EOF even when the prompt is already in argv.
            # Reproduce the retained host-fixture timeout with an open pipe,
            # then close it and verify the same native --server request runs.
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary) as request,
            ):
                session = request(
                    "POST", "/api/session", {"location": {"directory": str(root)}}
                )["data"]["id"]
                client_env = {**env, "OPENCODE_PASSWORD": request.endpoint["password"]}
                process = subprocess.Popen(
                    [
                        binary,
                        "run",
                        "--server",
                        request.endpoint["url"],
                        "--session",
                        session,
                        "--agent",
                        "build",
                        "--model",
                        "fixture/gpt-smoke",
                        "Exercise synthetic vault delivery.",
                    ],
                    env=client_env,
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    time.sleep(3)
                    assert process.poll() is None and state["requests"] == 0, (
                        "Expected open-FIFO input wait before provider request"
                    )
                    output, errors = process.communicate(input="", timeout=90)
                    assert (
                        process.returncode == 0 and "HOST_V2_ACCEPTANCE_OK" in output
                    ), errors[-2000:]
                    assert not state["errors"] and state["markers"] == {
                        "VAULT_SHELL_OK",
                        "VAULT_MCP_OK",
                    }, state["errors"]
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            print(
                "PASS: reproduced V2 open-FIFO stdin wait; explicit EOF allows native --server session request and tools.",
                flush=True,
            )
            state["markers"].clear()
            result = bounded(
                [
                    binary,
                    "run",
                    "--standalone",
                    "--model",
                    "fixture/gpt-smoke",
                    "Exercise synthetic vault delivery without a real provider.",
                ],
                env,
                root,
                timeout=90,
            )
            assert (
                "HOST_V2_ACCEPTANCE_OK" in result
                and not state["errors"]
                and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
            ), state["errors"]
            state["key"] = "synthetic-native-fixture-rotated"
            env[fixture.KEY] = state["key"]
            state["markers"].clear()
            result = bounded(
                [
                    binary,
                    "run",
                    "--standalone",
                    "--model",
                    "fixture/gpt-smoke",
                    "Exercise rotated synthetic credentials.",
                ],
                env,
                root,
                timeout=90,
            )
            assert (
                "HOST_V2_ACCEPTANCE_OK" in result
                and not state["errors"]
                and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
            ), state["errors"]
        print(
            "PASS: host acceptance fixture's exact native provider/MCP contract and rotated environment (offline; no Podman lifecycle).",
            flush=True,
        )
    finally:
        provider.shutdown()
        provider.server_close()
        worker.join(timeout=3)


def bounded(command, env, cwd, timeout=60):
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, errors = process.communicate(timeout=5)
        raise AssertionError("V2 smoke timeout: " + output[-3000:] + errors[-3000:])
    assert process.returncode == 0, output[-3000:] + errors[-3000:]
    return output


def main(binary, legacy_plugin_dir=None):
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix="oc-box-native-v2-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "OPENCODE_TOOLDEF_REPORT": str(root / "tool-definitions.json"),
            "OPENCODE_CLAMP_DIR": str(root / "clamped-output"),
        }
        for name in ("CONFIG", "DATA", "CACHE", "STATE"):
            env["XDG_" + name + "_HOME"] = str(root / name.lower())
        release = compat.version(bounded([binary, "--version"], env, project))
        assert release.startswith("2."), release
        bounded(["git", "init", "--quiet"], env, project)

        def cli(*args, scoped=None):
            return bounded(
                compat.command(release, binary, args), scoped or env, project
            )

        source = root / "source-config/opencode"
        source.mkdir(parents=True)
        legacy_plugins = source / "plugins"
        legacy_plugins.mkdir()
        (
            legacy_plugins / "fixture.ts"
        ).write_text("""import { tool } from "@opencode-ai/plugin"
export const Fixture = async () => ({
  "shell.env": async (_, output) => { output.env.OC_BOX_LEGACY_ENV = "bridged" },
  "tool.execute.before": async (input, output) => {
    if (input.tool === "bash" && output.args.command.includes("BLOCK_FIXTURE")) throw new Error("legacy tool block")
  },
  "tool.execute.after": async (input, output) => {
    if (input.tool === "bash") output.output += "\\nLEGACY_AFTER_OK"
  },
  "tool.definition": async (_, output) => { output.description += " Legacy definition hook." },
  config: async config => {
    if (config.provider.lemonade) config.provider.lemonade.models = {
      "discovered-fixture": { name: "Discovered", tool_call: true, limit: { context: 12345, output: 1024 } }
    }
  },
  tool: { legacy_probe: tool({ description: "Legacy probe", args: { value: tool.schema.string() },
    execute: async args => ({ title: "probe", output: "LEGACY_TOOL_OK " + args.value }) }) }
})
""")
        custom = source / "custom-plugins"
        custom.mkdir()
        (custom / "chat.ts").write_text("""export default async () => ({
  "experimental.chat.messages.transform": async (_, output) => {
    for (const message of output.messages) for (const part of message.parts) {
      if (part.type === "text" && part.text.startsWith("Exercise")) part.text += " CONTEXT_BRIDGE_OK"
    }
  },
  "chat.headers": async (_, output) => { output.headers["x-oc-box-chat-fixture"] = "HEADERS_BRIDGE_OK" }
})\n""")
        configured_plugins = ["opencode-sandbox@0.6.0", (custom / "chat.ts").as_uri()]
        if legacy_plugin_dir:
            for plugin in Path(legacy_plugin_dir).glob("*.ts"):
                shutil.copyfile(plugin, legacy_plugins / plugin.name)
            for plugin in (Path(legacy_plugin_dir).parent / "custom-plugins").glob(
                "*.ts"
            ):
                if plugin.name in (
                    "sandbox-uncloak.ts",
                    "pdf-to-text.ts",
                    "copilot-gpt6-effort.ts",
                ):
                    shutil.copyfile(plugin, custom / plugin.name)
                    configured_plugins.append((custom / plugin.name).as_uri())
        (source / "opencode.json").write_text(
            json.dumps(
                {
                    "plugins": configured_plugins,
                    "agents": {
                        "build": {
                            "permissions": [
                                {"action": "shell", "resource": "*", "effect": "deny"}
                            ]
                        }
                    },
                    "mcp": {
                        "servers": {
                            "fixture": {
                                "type": "local",
                                "command": [
                                    sys.executable,
                                    str(APP / "tests/fixture_services.py"),
                                    "mcp",
                                    str(root / "mcp.jsonl"),
                                ],
                            }
                        }
                    },
                }
            )
        )
        (source / "service.json").write_text(
            '{"env":{"SHOULD_NOT_COPY":"private"},"remote":{"route":"private"}}'
        )
        data = {
            "home": env["HOME"],
            "project": str(project),
            "id": "a" * 32,
            "container": "oc-box-synthetic",
            "opencode_version": release,
        }
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}):
            payload = box.config_archive(data)
        sync_config.sync_config(env["HOME"], payload, release)
        config = Path(env["HOME"]) / ".config/opencode"
        bounded(
            ["npm", "install", "--prefix", str(config), "--no-audit", "--no-fund"],
            env,
            project,
            timeout=120,
        )
        env["XDG_CONFIG_HOME"] = str(config.parent)
        env["OPENCODE_CONFIG"] = str(config / "oc-box-profile.json")
        env["OPENCODE_DISABLE_SANDBOX"] = "1"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(box.container_build_config(release))
        assert not (config / "service.json").exists()
        (project / "opencode.json").write_text(
            json.dumps(
                {
                    "agents": {
                        "build": {
                            "permissions": [
                                {"action": "edit", "resource": "*", "effect": "deny"}
                            ]
                        }
                    }
                }
            )
        )
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as request:
            query = "?directory=" + quote(str(project), safe="")
            request("GET", "/api/model/default" + query)
            agents = request("GET", "/api/agent" + query)["data"]
            plugins = request("GET", "/api/plugin" + query)
        build = next(agent for agent in agents if agent["id"] == "build")
        plan = next(agent for agent in agents if agent["id"] == "plan")
        for action in ("shell", "edit", "read"):
            matching = [
                rule for rule in build["permissions"] if rule["action"] in ("*", action)
            ]
            assert matching[-1]["effect"] == "allow", build
        assert "oc-box.build-permissions" in json.dumps(plugins), plugins
        assert plan["permissions"] != build["permissions"], plan
        print(
            "PASS: V2 native Build plugin, Plan preservation, legacy/native config, service-file exclusion.",
            flush=True,
        )

        target = project / "example.py"
        requests = []

        class Model(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    data = {
                        "data": [
                            {
                                "id": "lemonade-fixture",
                                "labels": ["tool-calling", "vision"],
                                "max_context_window": 32768,
                                "recipe_options": {"ctx_size": 16384},
                            }
                        ]
                    }
                elif self.path.endswith("/health"):
                    data = {"model_loaded": "lemonade-fixture"}
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_POST(self):
                assert self.headers.get("x-oc-box-chat-fixture") == "HEADERS_BRIDGE_OK"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/responses"):
                    assert body["reasoning"]["effort"] == "high", body
                    assert "reasoning.encrypted_content" in body["include"], body
                    assert self.headers.get("x-oc-copilot-effort") is None
                    requests.append(body)
                    item = {
                        "id": "msg_response_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "RESPONSES_EFFORT_OK",
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
                            "response": {
                                **response,
                                "status": "in_progress",
                                "output": [],
                            },
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
                            "delta": "RESPONSES_EFFORT_OK",
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": item,
                        },
                        {"type": "response.completed", "response": response},
                    ):
                        self.wfile.write(
                            ("data: " + json.dumps(event) + "\n\n").encode()
                        )
                    return
                requests.append(body)
                tools = {
                    tool["function"]["name"]: tool for tool in body.get("tools", [])
                }
                completed = sum(
                    message.get("role") == "tool"
                    for message in body.get("messages", [])
                )
                if "PDF_BRIDGE_FIXTURE" in str(body.get("messages")):
                    completed = 100
                delta, finish = {"content": "Smoke complete"}, "stop"
                maximum = 11 if legacy_plugin_dir else 6
                if tools and completed < maximum:
                    if completed == 0:
                        name, arguments = (
                            "patch",
                            {
                                "patchText": "*** Begin Patch\n*** Add File: "
                                + str(target)
                                + "\n+hello formatter\n*** End Patch"
                            },
                        )
                    elif completed == 1:
                        name, arguments = "read", {"path": str(target)}
                    elif completed == 2:
                        name, arguments = (
                            "shell",
                            {
                                "command": 'test "$OC_BOX_SYNTHETIC_TOKEN" = synthetic-v2-secret && test "$OC_BOX_LEGACY_ENV" = bridged && printf ENV_OK'
                            },
                        )
                    elif completed == 3:
                        name, arguments = (
                            "execute",
                            {"code": "return await tools.fixture.ping({})"},
                        )
                    elif completed == 4:
                        name, arguments = "legacy_probe", {"value": "fixture"}
                    elif completed == 5:
                        name, arguments = (
                            "shell",
                            {"command": "printf BLOCK_FIXTURE > SHOULD_NOT_EXIST"},
                        )
                    elif completed == 6:
                        name, arguments = "lemonade", {"action": "list"}
                    elif completed == 7:
                        name, arguments = (
                            "shell",
                            {
                                "command": "python3 -c 'print(\"OUTPUT_CLAMP_FIXTURE\" * 1200)'"
                            },
                        )
                    elif completed == 8:
                        name, arguments = (
                            "shell",
                            {
                                "command": 'test "$PIP_REQUIRE_VIRTUALENV" = 1 && test "$PYTHONDONTWRITEBYTECODE" = 1 && printf PYTHON_ISOLATION_OK'
                            },
                        )
                    elif completed == 9:
                        name, arguments = (
                            "shell",
                            {"command": "python3 -c 'print(\"中\" * 3000)'"},
                        )
                    else:
                        name, arguments = (
                            "shell",
                            {"command": "python3 -c 'print(\"🙂\" * 6000)'"},
                        )
                    assert name in tools, list(tools)
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_" + str(completed),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for item in (
                    {
                        "id": "chatcmpl-smoke",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    },
                    {
                        "id": "chatcmpl-smoke",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                ):
                    self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

        model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        settings = {
            **box.container_build_config(release),
            "snapshots": False,
            "update": "disable",
            "providers": {
                "responses-fixture": {
                    "package": "aisdk:@ai-sdk/openai",
                    "settings": {
                        "baseURL": f"http://127.0.0.1:{model.server_port}/v1",
                        "apiKey": "synthetic",
                    },
                    "models": {
                        "gpt-6-fixture": {
                            "name": "Effort fixture",
                            "variants": [{"id": "high"}],
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                },
                "lemonade": {
                    "package": "aisdk:@ai-sdk/openai-compatible",
                    "settings": {
                        "baseURL": f"http://127.0.0.1:{model.server_port}/api/v1"
                    },
                    "models": {
                        "fallback": {
                            "name": "Fallback",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                },
                "fixture": {
                    "package": "aisdk:@ai-sdk/openai-compatible",
                    "settings": {
                        "baseURL": f"http://127.0.0.1:{model.server_port}/v1",
                        "apiKey": "{env:OC_BOX_SYNTHETIC_TOKEN}",
                    },
                    "models": {
                        "gpt-smoke": {
                            "name": "Smoke",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                },
            },
            "model": "fixture/gpt-smoke",
            "formatter": {
                "fixture": {
                    "command": [
                        sys.executable,
                        str(APP / "tests/fixture_services.py"),
                        "format",
                        str(root / "formatter.jsonl"),
                        "$FILE",
                    ],
                    "extensions": [".py"],
                }
            },
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(settings)
        env["OC_BOX_SYNTHETIC_TOKEN"] = "synthetic-v2-secret"
        try:
            with patch.dict(os.environ, env, clear=True):
                previous = Path.cwd()
                try:
                    os.chdir(project)
                    bridge.main(["plugins", binary])
                finally:
                    os.chdir(previous)
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary) as request,
            ):
                query = "?directory=" + quote(str(project), safe="")
                request("GET", "/api/model/default" + query)
                plugins = request("GET", "/api/plugin" + query)["data"]
                assert all(item["state"]["status"] == "active" for item in plugins), (
                    plugins
                )
                models = request("GET", "/api/model" + query)["data"]
                model_id = (
                    "lemonade-fixture" if legacy_plugin_dir else "discovered-fixture"
                )
                discovered = next(
                    item
                    for item in models
                    if item["providerID"] == "lemonade" and item["id"] == model_id
                )
                assert discovered["limit"]["context"] == (
                    16384 if legacy_plugin_dir else 12345
                ), discovered
            print(
                "PASS: V1 custom tool, definition, shell environment, execution/output and provider-discovery hooks; sandbox container opt-out.",
                flush=True,
            )
            if legacy_plugin_dir:
                with (
                    patch.dict(os.environ, env, clear=True),
                    bridge.server(binary) as request,
                ):
                    query = "?directory=" + quote(str(project), safe="")
                    request("GET", "/api/model/default" + query)
                    plugins = request("GET", "/api/plugin" + query)["data"]
                    failed = [
                        item for item in plugins if item["state"]["status"] == "failed"
                    ]
                    assert not failed, failed
                    models = request("GET", "/api/model" + query)["data"]
                    discovered = next(
                        item
                        for item in models
                        if item["providerID"] == "lemonade"
                        and item["id"] == "lemonade-fixture"
                    )
                    assert discovered["limit"]["context"] == 16384, discovered
                    assert any(
                        item.get("id") == "oc-box.v1.opencode-sandbox"
                        and item["state"]["status"] == "active"
                        for item in plugins
                    ), plugins
                print(
                    "PASS: actual copied user V1 plugins activate in V2; Lemonade discovery uses live capabilities/context.",
                    flush=True,
                )
            output = cli(
                "run",
                "--model",
                "fixture/gpt-smoke",
                "Exercise native write/read and shell tools.",
            )
            assert target.is_file(), output
            assert any(
                "CONTEXT_BRIDGE_OK" in str(request.get("messages"))
                for request in requests
            ), "Legacy outbound messages hook missing"
            assert not (project / "SHOULD_NOT_EXIST").exists(), (
                "Legacy before-hook refusal failed"
            )
            assert any(
                message.get("role") == "tool"
                and "legacy tool block" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            ), "Legacy refusal missing from native tool results"
            assert target.read_text() == "HELLO FORMATTER\n", output
            assert any(
                "Legacy definition hook." in tool["function"]["description"]
                for request in requests
                for tool in request.get("tools", [])
            ), "Definition transform missing"
            if legacy_plugin_dir:
                messages = [
                    message
                    for request in requests
                    for message in request.get("messages", [])
                    if message.get("role") == "tool"
                ]
                assert any(
                    "lemonade-fixture" in str(message.get("content"))
                    for message in messages
                ), "Live Lemonade tool result missing"
                assert any(
                    "PYTHON_ISOLATION_OK" in str(message.get("content"))
                    for message in messages
                ), "Python isolation environment missing"
                assert any(
                    "[output-clamp]" in str(message.get("content"))
                    for message in messages
                ), "Actual output clamp hook missing"
                assert (root / "tool-definitions.json").is_file(), (
                    "Actual tool-definition audit report missing"
                )
                assert list((root / "clamped-output").glob("*.txt")), (
                    "Actual output clamp spill missing"
                )
                for call in ("call_9", "call_10"):
                    result = next(
                        message
                        for message in messages
                        if message.get("tool_call_id") == call
                    )
                    text = str(result["content"])
                    assert "[output-clamp]" in text and len(text.encode()) <= 8192, (
                        call,
                        len(text.encode()),
                    )
                    assert "�" not in text, (
                        call,
                        text[max(0, text.index("�") - 30) : text.index("�") + 30],
                    )
                audit = json.loads((root / "tool-definitions.json").read_text())
                assert all(row["schema"] > 0 for row in audit["tools"]), audit
                print(
                    "PASS: actual output-clamp Unicode thresholds/retained bytes and nonzero request JSON-schema audit.",
                    flush=True,
                )
            assert any(
                message.get("role") == "tool"
                and "ENV_OK" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            ), requests
            assert (root / "mcp.jsonl").is_file()
            mcp_log = [
                json.loads(line)
                for line in (root / "mcp.jsonl").read_text().splitlines()
            ]
            assert any(item.get("method") == "tools/call" for item in mcp_log), mcp_log
            assert not (Path(env["XDG_STATE_HOME"]) / "opencode/service.json").exists()
            assert any(
                message.get("role") == "tool"
                and "MCP_SMOKE_OK" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            ), "MCP Code Mode output missing"
            assert any(
                message.get("role") == "tool"
                and "LEGACY_AFTER_OK" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            )
            assert any(
                message.get("role") == "tool"
                and "LEGACY_TOOL_OK" in str(message.get("content"))
                for request in requests
                for message in request.get("messages", [])
            )
            print(
                "PASS: V2 native tools, formatter, MCP initialization and server/shell environment inheritance.",
                flush=True,
            )
            listed = json.loads(cli("session", "list", "--format", "json"))
            ident = listed[0]["id"]
            exported = json.loads(cli("export", ident))
            box.validate_session_export(exported, ident)
            if legacy_plugin_dir:
                response_output = cli(
                    "run",
                    "--model",
                    "responses-fixture/gpt-6-fixture#high",
                    "Use the selected effort without tools.",
                )
                assert "RESPONSES_EFFORT_OK" in response_output, response_output
                print(
                    "PASS: actual copied Copilot effort hook injects selected effort at native V2 HTTP request boundary.",
                    flush=True,
                )
                # Produce a minimal real PDF and verify its configured plugin
                # converts it at native V2 request time, before model lowering.
                pdf = project / "fixture.pdf"
                content = b"BT /F1 12 Tf 50 700 Td (PDF_BRIDGE_FIXTURE) Tj ET"
                objects = [
                    b"<< /Type /Catalog /Pages 2 0 R >>",
                    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                    b"<< /Length "
                    + str(len(content)).encode()
                    + b" >>\nstream\n"
                    + content
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
                pdf.write_bytes(document)
                cli(
                    "run",
                    "--model",
                    "fixture/gpt-smoke",
                    "--file",
                    str(pdf),
                    "Read this attachment without tools.",
                )
                assert any(
                    "<attached-pdf" in str(request.get("messages"))
                    and "PDF_BRIDGE_FIXTURE" in str(request.get("messages"))
                    for request in requests
                ), "Actual PDF request conversion missing"
                historical = json.loads(json.dumps(exported))
                shell = next(
                    part
                    for message in historical["messages"]
                    if message["type"] == "assistant"
                    for part in message["content"]
                    if part.get("type") == "tool"
                    and part["name"] == "shell"
                    and part["state"]["status"] == "completed"
                )
                shell["id"] = "call_historical_uncloak"
                shell["state"]["input"]["command"] = (
                    "bwrap unix-block.bpf /usr/bin/bash -c 'printf UNCLOAK_RECOVERED'"
                )
                old_file = root / "wrapped-transcript.json"
                old_file.write_text(json.dumps(historical))
                historical_env = {**env, "XDG_DATA_HOME": str(root / "uncloak-data")}
                cli("import", str(old_file), scoped=historical_env)
                before_requests = len(requests)
                cli(
                    "run",
                    "--session",
                    ident,
                    "Finish without tools.",
                    scoped=historical_env,
                )
                assert any(
                    "UNCLOAK_RECOVERED" in str(request["messages"])
                    and "bwrap unix-block.bpf" not in str(request["messages"])
                    for request in requests[before_requests:]
                ), "Actual sandbox-uncloak request transform missing"
                print(
                    "PASS: configured PDF extraction and historical sandbox-uncloak behavior at native V2 request boundary.",
                    flush=True,
                )
            archived = box.archive_session(exported, data)
            archive = root / "archive.json"
            archive.write_text(json.dumps(archived))
            destination = {**env, "XDG_DATA_HOME": str(root / "destination")}
            cli("import", str(archive), scoped=destination)
            copied = json.loads(
                cli("export", archived["info"]["id"], scoped=destination)
            )
            box.verify_session_import(data, archived, copied)
            cli("run", "--session", ident, "Finish without tools.")
            print(
                "PASS: V2 export/import transcript verification, archive identity and exact-session resume.",
                flush=True,
            )
            # Real credential rotation through the production private bridge.
            entry = {
                "id": "cred_fixture",
                "integrationID": "fixture",
                "label": "fixture",
                "value": {"type": "key", "key": "synthetic-first"},
                "active": True,
            }
            with patch.dict(os.environ, destination, clear=True):
                bridge.refresh_credentials(binary, [entry])
                entry["value"]["key"] = "synthetic-second"
                bridge.refresh_credentials(binary, [entry])
                inactive = {
                    **entry,
                    "id": "cred_inactive",
                    "active": False,
                    "value": {"type": "key", "key": "synthetic-inactive"},
                }
                bridge.refresh_credentials(binary, [inactive, entry])
            values = json.loads(cli("auth", "export", scoped=destination))
            selected = next(value for value in values if value["active"])
            assert selected["value"]["key"] == "synthetic-second", values
            with patch.dict(os.environ, destination, clear=True):
                bridge.refresh_credentials(
                    binary,
                    compat.legacy_credentials(
                        '{"legacy-fixture":{"type":"api","key":"synthetic-override"}}'
                    ),
                )
            print(
                "PASS: V2 supported credential import/update with private-memory transport.",
                flush=True,
            )
            # Production history import/export transport against two real V2
            # databases, including multi-MiB native messages and retry checks.
            registry = root / "registry/workspaces" / data["id"] / "workspace.json"
            box.atomic_json(registry, data)
            (registry.parent / "controller").mkdir()
            large = json.loads(json.dumps(exported))
            user = next(
                message for message in large["messages"] if message["type"] == "user"
            )
            user["text"] += "\n" + "x" * 2200000
            isolated = {**env, "XDG_DATA_HOME": str(root / "imported-data")}

            def local_exec(_data, command, **_kwargs):
                if command[0] == binary and "--version" not in command:
                    command = compat.command(release, binary, command[1:])
                trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
                return box.ContainerCommand(
                    [sys.executable, "-c", trampoline, *command],
                    {**isolated, "PWD": str(project)},
                )

            with (
                patch.object(box, "exec_command", side_effect=local_exec),
                patch.object(box, "CONTAINER_OPENCODE", binary),
                patch.object(box, "host_cli_env", return_value=env),
                patch.object(
                    box,
                    "host_command",
                    side_effect=lambda _data, *args: compat.command(
                        release, binary, args
                    ),
                ),
            ):
                saved = box.import_host_session(registry, data, large)
                assert saved["last"] == ident
                copied = box.export_container_session(data, ident)
                box.verify_session_import(data, large, copied)
                try:
                    box.import_host_session(registry, data, large)
                except box.BoxError as error:
                    assert "not overwritten" in str(error)
                else:
                    raise AssertionError("V2 duplicate transcript was overwritten")
                box.sync_sessions(registry, data)
            print(
                "PASS: production V2 large-history import/export, duplicate protection and host archive sync.",
                flush=True,
            )

            # Source V1 remains byte-identical while mainline migrates a private
            # snapshot. Resuming V1 never points at the V2 database.
            legacy_env = {
                **env,
                "XDG_DATA_HOME": str(root / "legacy-data"),
                "XDG_CONFIG_HOME": str(root / "legacy-config"),
            }
            legacy_env["OPENCODE_CONFIG_CONTENT"] = (
                '{"snapshot":false,"formatter":false,"lsp":false}'
            )
            legacy_env.pop("OPENCODE_CONFIG", None)
            legacy_env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
            legacy = {
                "info": {
                    "id": "ses_legacy_fixture",
                    "directory": str(project),
                    "projectID": "global",
                    "version": "1.19.5",
                    "slug": "legacy-fixture",
                    "time": {"created": 1, "updated": 2},
                    "title": "Legacy fixture",
                },
                "messages": [
                    {
                        "info": {
                            "id": "msg_legacy_fixture",
                            "sessionID": "ses_legacy_fixture",
                            "role": "user",
                            "agent": "build",
                            "model": {"providerID": "fixture", "modelID": "gpt-smoke"},
                            "time": {"created": 1},
                        },
                        "parts": [
                            {
                                "id": "prt_legacy_fixture",
                                "sessionID": "ses_legacy_fixture",
                                "messageID": "msg_legacy_fixture",
                                "type": "text",
                                "text": "Legacy text",
                            }
                        ],
                    }
                ],
            }
            legacy_file = root / "legacy.json"
            legacy_file.write_text(json.dumps(legacy))
            bounded(
                ["opencode", "--pure", "import", str(legacy_file)], legacy_env, project
            )
            old_database = Path(legacy_env["XDG_DATA_HOME"]) / "opencode/opencode.db"
            before = old_database.read_bytes()
            migration = {
                **legacy_env,
                "OPENCODE_DB": "oc-box-v2.db",
                "OPENCODE_CONFIG_CONTENT": '{"snapshots":false,"formatter":false,"update":"disable"}',
            }
            with patch.dict(os.environ, migration, clear=True):
                bridge.seed_database()
                with bridge.server(binary) as request:
                    migrated = request(
                        "GET", "/api/experimental/session/ses_legacy_fixture/export"
                    )["data"]
            assert "Legacy text" in json.dumps(migrated)
            assert old_database.read_bytes() == before
            v2_database = old_database.with_name("oc-box-v2.db")
            before_v2 = v2_database.read_bytes()
            for arguments in (
                ["--pure", "debug", "agent", "build"],
                ["--pure", "session", "list", "--format", "json"],
            ):
                bounded(["opencode", *arguments], legacy_env, project)
            restored_v1 = json.loads(
                bounded(
                    ["opencode", "--pure", "export", "ses_legacy_fixture"],
                    legacy_env,
                    project,
                )
            )
            assert restored_v1["messages"] == legacy["messages"], (
                "V1 reopened transcript changed"
            )
            assert v2_database.read_bytes() == before_v2, "V1 wrote the V2 database"
            print(
                "PASS: V2 snapshot migration leaves V1 bytes intact; reopening native V1 retains the exact transcript and leaves V2 bytes intact.",
                flush=True,
            )
            # Real foreground owner, same-user concurrent attach and browser
            # transport; only the Podman exec transport is local in this fixture.
            live_env = {
                **env,
                "XDG_RUNTIME_DIR": str(root),
                "OC_BOX_STATE_HOME": str(root / "live-state"),
            }
            info = {
                "State": {"Running": True},
                "Mounts": [{"Source": str(project), "Destination": str(project)}],
            }

            def live_exec(_data, arguments, **_kwargs):
                if arguments[0] == binary and "--version" not in arguments:
                    arguments = compat.command(release, binary, arguments[1:])
                trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
                return box.ContainerCommand(
                    [sys.executable, "-B", "-c", trampoline, *arguments],
                    {**live_env, "PWD": str(project)},
                )

            original_client_command = workspace_service.client_command

            def local_client(_box, _data, connection, arguments, **kwargs):
                command = original_client_command(
                    _box, _data, connection, arguments, **kwargs
                )
                begin = command.index(_data["container"]) + 1
                environment = {
                    **command.environment,
                    "OPENCODE_PASSWORD": connection["password"],
                }
                return box.ContainerCommand(command[begin:], environment)

            def local_relay(_box, _data, port):
                return box.ContainerCommand(
                    [sys.executable, "-B", "-c", workspace_service.PROXY, str(port)],
                    box.maintenance_host_env(),
                )

            with (
                patch.dict(os.environ, live_env, clear=True),
                patch.object(box, "exec_command", side_effect=live_exec),
                patch.object(box, "CONTAINER_OPENCODE", binary),
                patch.object(
                    workspace_service, "client_command", side_effect=local_client
                ),
                patch.object(
                    workspace_service, "relay_command", side_effect=local_relay
                ),
                patch.object(workspace_service, "validate_workspace"),
                workspace_service.Owner(box, registry, data) as owner,
            ):
                owner.publish(ident)
                with workspace_service.attach(box, registry, data) as joined:
                    assert joined["session"] == ident and owner.clients == 1
                    workspace_service.api(
                        box, data, joined, "GET", "/api/session/" + ident
                    )
                    with workspace_service.Gateway(box, data, joined) as gateway:
                        authorization = (
                            "Basic "
                            + base64.b64encode(
                                ("opencode:" + joined["password"]).encode()
                            ).decode()
                        )
                        browser = build_opener(
                            ProxyHandler({}),
                            HTTPCookieProcessor(http.cookiejar.CookieJar()),
                        )
                        pair = workspace_service.api(
                            box, data, joined, "POST", "/api/pair"
                        )
                        with browser.open(
                            Request(
                                gateway.url + "/auth/connect/" + pair["code"],
                                headers={"Accept": "text/html"},
                            ),
                            timeout=15,
                        ) as response:
                            assert response.url.endswith(gateway.session_route()), (
                                response.url
                            )
                            assert b"<html" in response.read().lower()
                        with browser.open(
                            gateway.url + "/api/session/" + ident, timeout=10
                        ) as response:
                            assert json.load(response)["data"]["id"] == ident
                        stream = browser.open(gateway.url + "/api/event", timeout=10)
                        assert (
                            b"server.connected" in stream.readline() + stream.readline()
                        )
                        with browser.open(
                            Request(
                                gateway.url + "/api/info",
                                headers={"Authorization": authorization},
                            ),
                            timeout=10,
                        ) as response:
                            assert json.load(response)["version"] == release
                        stream.close()
                        # Original server process identity is shared by CLI and
                        # browser; no standalone service is started by attaches.
                        first = workspace_service.api(
                            box, data, joined, "GET", "/api/info"
                        )
                        with browser.open(
                            gateway.url + "/api/info", timeout=10
                        ) as response:
                            assert json.load(response)["pid"] == first["pid"]
                        # Attach the session's actual shell while the browser
                        # and owner stay alive, then reattach to preserved state.
                        master, slave = pty.openpty()
                        compact = local_client(
                            box,
                            data,
                            joined,
                            [
                                sys.executable,
                                "-B",
                                "-c",
                                (box.PACKAGE / "session_terminal.py").read_text(),
                                joined["url"],
                                ident,
                                str(project),
                            ],
                        )
                        process = subprocess.Popen(
                            compact,
                            env=compact.environment,
                            cwd=project,
                            stdin=slave,
                            stdout=slave,
                            stderr=slave,
                            start_new_session=True,
                        )
                        os.close(slave)
                        terminal_output = bytearray()
                        sent = False
                        try:
                            deadline = time.monotonic() + 20
                            while (
                                process.poll() is None and time.monotonic() < deadline
                            ):
                                if select.select([master], [], [], 0.1)[0]:
                                    try:
                                        terminal_output.extend(os.read(master, 65536))
                                    except OSError:
                                        break
                                if not sent and terminal_output:
                                    fcntl.ioctl(
                                        master,
                                        termios.TIOCSWINSZ,
                                        struct.pack("HHHH", 31, 95, 0, 0),
                                    )
                                    os.kill(process.pid, signal.SIGWINCH)
                                    os.write(
                                        master,
                                        b"export OC_BOX_PTY_TEST=retained; printf '\\nSHELL_TERMINAL_OK\\n'\r",
                                    )
                                    sent = True
                                if b"SHELL_TERMINAL_OK\r\n" in terminal_output:
                                    os.write(master, b"\x1d")
                                    break
                            if sent:
                                process.wait(timeout=5)
                            assert sent and process.returncode == 0, bytes(
                                terminal_output[-3000:]
                            )
                        finally:
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                            os.close(master)
                        with browser.open(
                            gateway.url
                            + "/api/experimental/session/"
                            + ident
                            + "/terminal/read",
                            timeout=10,
                        ) as response:
                            screen = json.load(response)["data"]
                            assert "SHELL_TERMINAL_OK" in screen["screen"]["text"], (
                                screen
                            )
                            assert (
                                screen["screen"]["cols"],
                                screen["screen"]["rows"],
                            ) == (95, 31), screen
                        terminals = workspace_service.api(
                            box,
                            data,
                            joined,
                            "GET",
                            "/api/experimental/session/" + ident + "/terminal",
                        )["data"]
                        assert (
                            len(terminals) == 1 and terminals[0]["status"] == "running"
                        ), terminals
                        master, slave = pty.openpty()
                        resumed = subprocess.Popen(
                            compact,
                            env=compact.environment,
                            cwd=project,
                            stdin=slave,
                            stdout=slave,
                            stderr=slave,
                            start_new_session=True,
                        )
                        os.close(slave)
                        replay = bytearray()
                        try:
                            deadline = time.monotonic() + 20
                            sent = False
                            while (
                                resumed.poll() is None and time.monotonic() < deadline
                            ):
                                if select.select([master], [], [], 0.1)[0]:
                                    try:
                                        replay.extend(os.read(master, 65536))
                                    except OSError:
                                        break
                                if not sent and b"SHELL_TERMINAL_OK" in replay:
                                    os.write(
                                        master,
                                        b'test "$OC_BOX_PTY_TEST" = retained && printf "REATTACH_OK\\n"; exit\r',
                                    )
                                    sent = True
                            resumed.wait(timeout=5)
                            assert (
                                sent
                                and resumed.returncode == 0
                                and b"REATTACH_OK\r\n" in replay
                            ), bytes(replay[-3000:])
                        finally:
                            if resumed.poll() is None:
                                os.killpg(resumed.pid, signal.SIGKILL)
                            resumed.wait(timeout=5)
                            os.close(master)
                        assert (
                            workspace_service.api(
                                box, data, joined, "GET", "/api/info"
                            )["pid"]
                            == first["pid"]
                        )
                    print(
                        "PASS: native session shell terminal/browser/API, detach/replay/preserved shell state, pairing/cookies and event streaming.",
                        flush=True,
                    )
                owner.wait_clients()
            assert not (config / "service.json").exists()
            assert not (Path(env["XDG_STATE_HOME"]) / "opencode/service.json").exists()
        finally:
            model.shutdown()
            model.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument(
        "--legacy-plugin-dir",
        help="optional local V1 plugin sources copied into disposable fixture storage",
    )
    args = parser.parse_args()
    main(args.opencode, args.legacy_plugin_dir)
    host_fixture_probe(str(Path(args.opencode).resolve()))
