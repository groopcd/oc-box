"""Bounded offline smoke test of native OpenCode tools, MCP, formatter, and LSP.

This does not simulate container isolation. It validates the mainline execution
path used inside Podman, with temporary config/data and local fixture services.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box", APP / "oc_box.py")
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)
sync_spec = importlib.util.spec_from_file_location("sync_config", APP / "sync_config.py")
sync = importlib.util.module_from_spec(sync_spec)
sync_spec.loader.exec_module(sync)


def bounded(command, *, env, cwd, timeout=45):
    process = subprocess.Popen(command, env={**env, "PWD": str(cwd)}, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=5)
        raise AssertionError(f"Smoke test stopped after {timeout}s. Last output:\n{out[-2000:]}\n{err[-2000:]}")
    assert process.returncode == 0, out[-4000:] + err[-4000:]
    return out, err


def warm_start_regression(root, env, project):
    """SDK-version drift must not make mainline re-resolve npm on warm start."""
    version, _ = bounded(["opencode", "--version"], env=env, cwd=project, timeout=10)
    version = version.strip()
    home = root / "warm-home"
    config = home / ".config/opencode"
    plugin_package = "@opencode-ai/plugin"
    incoming = io.BytesIO()
    source_files = {
        "package.json": json.dumps({"dependencies": {plugin_package: "1.0.0"}}),
        "plugins/warm-fixture.js": "import fs from 'node:fs'; export default async()=>({config:async()=>{fs.writeFileSync(process.env.OC_BOX_WARM_MARKER,'loaded')}})",
    }
    with tarfile.open(fileobj=incoming, mode="w:gz") as archive:
        for name, value in source_files.items():
            data = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    payload = incoming.getvalue()
    sync.sync_config(home, payload, version)
    # Simulate the config directory produced after first mainline SDK install.
    package = config / "node_modules" / plugin_package
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": plugin_package, "version": version, "type": "module"}))
    manifest = {"dependencies": {plugin_package: version}}
    (config / "package.json").write_text(json.dumps(manifest))
    (config / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {"": manifest}}))
    requests = []

    class Registry(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(503)
            self.end_headers()

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    warm_env = dict(env)
    warm_env.pop("OPENCODE_CONFIG_CONTENT", None)
    warm_env["XDG_CONFIG_HOME"] = str(home / ".config")
    warm_env["npm_config_registry"] = f"http://127.0.0.1:{server.server_port}"
    warm_env["npm_config_fetch_retries"] = "0"
    marker = root / "warm-plugin-loaded"
    warm_env["OC_BOX_WARM_MARKER"] = str(marker)
    try:
        durations = []
        for _ in range(2):
            assert sync.sync_config(home, payload, version), "Warm startup invalidated the generated SDK cache"
            marker.unlink(missing_ok=True)
            started = time.monotonic()
            bounded(["opencode", "debug", "agent", "build"], env=warm_env, cwd=project, timeout=20)
            durations.append(time.monotonic() - started)
            assert marker.is_file(), "The enabled external plugin did not initialize"
            assert json.loads((config / "package.json").read_text()) == manifest
        assert not requests, f"Warm mainline startup unexpectedly contacted npm: {requests}"
        print("PASS: two warm native plugin startups with an older source SDK and zero npm requests (" + ", ".join(f"{seconds:.2f}s" for seconds in durations) + ").")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def session_import_regression(root, env, project, ident):
    """Use production export/transfer/verify code with two real temporary DBs."""
    host = dict(env)
    host.pop("OPENCODE_CONFIG_CONTENT", None)
    isolated = {**host, "XDG_DATA_HOME": str(root / "import-container-data")}
    registry = root / "import-registry/workspace.json"
    registry.parent.mkdir(parents=True)
    (registry.parent / "controller").mkdir()
    data = {"id": "b" * 32, "project": str(project)}
    executable = shutil.which("opencode")
    child_env = {**isolated, "PWD": str(project)}
    # Substitute process transport only; run the production transfer and actual
    # mainline export/import/verification code without modification.
    trampoline = "import json,os,sys; env=json.loads(sys.argv[1]); os.chdir(env['PWD']); os.execvpe(sys.argv[2],sys.argv[2:],env)"

    def local_exec(_data, command, **_kwargs):
        return [sys.executable, "-c", trampoline, json.dumps(child_env), *command]

    with patch.object(box, "host_cli_env", return_value=host), patch.object(box, "CONTAINER_OPENCODE", executable), patch.object(box, "exec_command", side_effect=local_exec):
        source = box.export_host_session(registry, data, ident)
        # Exercise the multi-MiB export/transfer path without another model call.
        large = json.loads(json.dumps(source))
        text = next(part for message in large["messages"] for part in message["parts"] if part.get("type") == "text")
        text["text"] += "\n" + "transfer regression " * 140000
        fixture = root / "large-source.json"
        box.atomic_json(fixture, large)
        bounded(["opencode", "--pure", "session", "delete", ident], env=host, cwd=project, timeout=15)
        bounded(["opencode", "--pure", "import", str(fixture)], env=host, cwd=project, timeout=15)
        source = box.export_host_session(registry, data, ident)
        assert len(json.dumps(source)) > 2_000_000
        recorded = box.import_host_session(registry, data, source)
        assert recorded["last"] == ident
        assert box.refresh_sessions(registry, data)["last"] == ident
        try:
            box.import_host_session(registry, data, source)
        except box.BoxError as exc:
            assert "not overwritten" in str(exc), exc
        else:
            raise AssertionError("Duplicate host import overwrote the container session")
        assert box.export_host_session(registry, data, ident) == source, "Host source changed during transfer"
        copied = box.export_container_session(data, ident)
        assert copied["messages"] == source["messages"]
        bounded([executable, "--pure", "run", "--session", ident, "Finish without tools."], env={**env, "XDG_DATA_HOME": isolated["XDG_DATA_HOME"]}, cwd=project, timeout=20)
    print("PASS: host-to-container import (>2 MB), full transcript verification, duplicate protection, and imported-session resume.")


def main():
    with tempfile.TemporaryDirectory(prefix="oc-box-native-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OPENCODE_", "OC_BOX_"))}
        for kind in ("config", "data", "cache", "state"):
            env["XDG_" + kind.upper() + "_HOME"] = str(root / kind)
        env["OPENCODE_TEST_HOME"] = str(root / "empty-home")
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        bounded(["git", "init", "--quiet"], env=env, cwd=project, timeout=5)
        service = str(APP / "tests/fixture_services.py")
        target = project / "example.py"
        logs = {name: root / (name + ".jsonl") for name in ("mcp", "lsp", "formatter")}
        requests, failures = [], []

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(request)
                    tools = {item["function"]["name"]: item["function"] for item in request.get("tools", [])}
                    completed = sum(item.get("role") == "tool" for item in request.get("messages", []))
                    # Every response completes successfully; no assertion causes
                    # a dropped connection/retry loop in the fake provider.
                    delta, finish = {"content": "Smoke test complete"}, "stop"
                    if tools and len(requests) <= 10 and completed < 3:
                        if completed == 0:
                            name, args = "write", {"filePath": str(target), "content": "hello formatter\n"}
                        elif completed == 1:
                            name, args = "read", {"filePath": str(target)}
                        else:
                            name = next((name for name in tools if name.endswith("_ping")), "MISSING_MCP")
                            args = {}
                        if name not in tools:
                            failures.append(f"Native tool {name} missing from catalog: {list(tools)}")
                        else:
                            delta = {"tool_calls": [{"index": 0, "id": f"call_smoke_{completed}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                            finish = "tool_calls"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for value in (
                        {"id": "chatcmpl-smoke", "object": "chat.completion.chunk", "model": "gpt-4-smoke", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {"id": "chatcmpl-smoke", "object": "chat.completion.chunk", "model": "gpt-4-smoke", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                    ):
                        self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "oc-box-smoke/gpt-4-smoke", "permission": "allow",
            "snapshot": False,
            "provider": {"oc-box-smoke": {
                "npm": "@ai-sdk/openai-compatible", "name": "Smoke",
                "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "test"},
                "models": {"gpt-4-smoke": {"name": "Smoke", "limit": {"context": 32000, "output": 2000}}},
            }},
            "mcp": {"fixture": {"type": "local", "command": ["python3", service, "mcp", str(logs["mcp"])]}},
            "formatter": {"fixture": {"command": ["python3", service, "format", str(logs["formatter"]), "$FILE"], "extensions": [".py"]}},
            "lsp": {"pyright": {"disabled": True}, "fixture": {"command": ["python3", service, "lsp", str(logs["lsp"])], "extensions": [".py"]}},
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        try:
            resolved, _ = bounded(["opencode", "--pure", "debug", "config"], env=env, cwd=project, timeout=15)
            assert "fixture" in json.loads(resolved)["lsp"], resolved[-3000:]
            out, err = bounded(["opencode", "--pure", "run", "--model", "oc-box-smoke/gpt-4-smoke", "Write and read example.py, then call fixture ping."], env=env, cwd=project, timeout=50)
            assert not failures, failures
            assert target.is_file(), out[-2000:] + err[-2000:]
            assert target.read_text() == "HELLO FORMATTER\n", target.read_text()
            assert logs["formatter"].is_file(), "Native formatter was not invoked"
            mcp = [json.loads(line) for line in logs["mcp"].read_text().splitlines()]
            assert any(item.get("method") == "tools/call" for item in mcp), mcp
            if not logs["lsp"].exists():
                recent = "\n".join(file.read_text()[-5000:] for file in (root / "data/opencode/log").glob("*"))
                raise AssertionError("LSP fixture did not start. CLI output:\n" + out[-2000:] + err[-2000:] + "\nLogs:\n" + recent[-7000:])
            lsp = [json.loads(line) for line in logs["lsp"].read_text().splitlines()]
            assert any(item.get("method") == "initialize" for item in lsp), lsp
            assert any(item.get("method") == "textDocument/didOpen" for item in lsp), lsp
            print("PASS: native file write/read, automatic formatter, LSP initialize/didOpen, and MCP tool call.")
            listing, _ = bounded(["opencode", "--pure", "session", "list", "--format", "json"], env=env, cwd=project, timeout=15)
            sessions = json.loads(listing)
            ident = sessions[0]["id"]
            exported, _ = bounded(["opencode", "--pure", "export", ident], env=env, cwd=project, timeout=15)
            archived = box.archive_session(json.loads(exported), {"id": "a" * 32, "project": str(project)})
            archive = root / "archive.json"
            box.atomic_json(archive, archived)
            host = dict(env)
            host.pop("OPENCODE_CONFIG_CONTENT")
            host["XDG_DATA_HOME"] = str(root / "host-data")
            bounded(["opencode", "--pure", "import", str(archive)], env=host, cwd=project, timeout=15)
            copied, _ = bounded(["opencode", "--pure", "export", archived["info"]["id"]], env=host, cwd=project, timeout=15)
            assert len(json.loads(copied)["messages"]) == len(archived["messages"])
            # Resume the same native session without requiring another tool call.
            bounded(["opencode", "--pure", "run", "--session", ident, "Finish without tools."], env=env, cwd=project, timeout=20)
            print("PASS: session export/import into a separate host-style database, and exact-session resume.")
            session_import_regression(root, env, project, ident)
            warm_start_regression(root, env, project)
            print("All app data was temporary. Live Podman isolation remains a separate host-terminal check.")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
