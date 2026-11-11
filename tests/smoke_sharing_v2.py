"""Bounded native offline sharing integration, not host/Podman acceptance.

Run from the checkout root with TMPDIR set if desired:
python3 -B tests/smoke_sharing_v2.py --opencode /path/to/opencode-v2
Only private synthetic state, a stdio-owned native server and a canned loopback
provider are used. Production Export, serve and transport_source remain real.
"""

import argparse
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oc_box import (
    oc_box as box,
    opencode_compat as compat,
    opencode_v2_io as bridge,
    sharing,
)
from smoke_session_sync_v2 import bounded, private_environment

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
IMAGE = "data:image/png;base64," + PNG
UNRELATED = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message.get('method')
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'unrelated', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'check', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': 'UNRELATED_MCP_OK'}]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
"""


class Failure(AssertionError):
    """Only fixed categories may reach the fixture terminal."""


def require(condition, category):
    if not condition:
        raise Failure(category)


def wait(check, category, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise Failure(category)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def snapshot(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


@contextlib.contextmanager
def relay(source, endpoint, project, name, env):
    ready, finished, failures = threading.Event(), threading.Event(), []
    command = [sys.executable, "-I", "-c", sharing.transport_source(box)]
    command.extend([endpoint["url"], str(project), name])
    child_env = {**env, "OPENCODE_PASSWORD": endpoint["password"]}
    require(
        str(source) not in json.dumps([command, child_env]), "source-path-forwarded"
    )
    with sharing.Export(source) as export:
        process = subprocess.Popen(
            command,
            cwd=project,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def serve():
            try:
                sharing.serve(process, export, ready=ready.set)
            except Exception:
                failures.append("share-serve")
            finally:
                finished.set()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        completed = False
        try:
            wait(lambda: ready.is_set() or finished.is_set(), "share-readiness", 35)
            require(ready.is_set() and not failures, "share-readiness")
            yield finished, failures
            completed = True
        finally:
            process.stdin.close()  # Exercise real pipe-EOF revocation, not a fake detach.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                failures.append("relay-stop")
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            worker.join(5)
            process.stdout.close()
            if completed:
                require(
                    not worker.is_alive() and not failures and process.returncode == 0,
                    "relay-shutdown",
                )


@contextlib.contextmanager
def canned_provider():
    state = {"code": "", "results": [], "failures": [], "requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= 2 * 1024 * 1024, "provider-size")
                require(self.path == "/v1/chat/completions", "provider-route")
                body = json.loads(self.rfile.read(size))
                state["requests"] += 1
                require(state["requests"] <= 80, "provider-request-bound")
                messages = body.get("messages", [])
                results = [
                    m.get("content") for m in messages if m.get("role") == "tool"
                ]
                names = {t["function"]["name"] for t in body.get("tools", [])}
                delta, finish = {"content": "SYNTHETIC_SHARING_COMPLETE"}, "stop"
                if results:
                    require(
                        len(results) == 1 and isinstance(results[0], str),
                        "provider-result",
                    )
                    state["results"].append(
                        {
                            "text": results[0],
                            "image": any(
                                n.get("url") == IMAGE for n in nodes(messages)
                            ),
                        }
                    )
                elif names:
                    require("execute" in names, "native-execute-missing")
                    function = dict(
                        name="execute", arguments=json.dumps({"code": state["code"]})
                    )
                    call = dict(
                        index=0,
                        id="call_sharing_fixture",
                        type="function",
                        function=function,
                    )
                    delta = {"tool_calls": [call]}
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for value, reason in ((delta, None), ({}, finish)):
                    choice = dict(index=0, delta=value, finish_reason=reason)
                    event = dict(
                        id="chatcmpl-sharing",
                        object="chat.completion.chunk",
                        model="probe",
                        choices=[choice],
                    )
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception:
                state["failures"].append("provider")
                self.send_error(500, "Synthetic fixture failed")

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *_):
            state["failures"].append("provider-worker")

    server = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        require(not worker.is_alive(), "provider-stop")


def invoke(native, project, state, code):
    state["code"], state["results"] = code, []
    ident = native(
        "POST",
        "/api/session",
        {
            "location": {"directory": str(project)},
            "title": "Synthetic sharing fixture",
            "agent": "build",
            "model": {"providerID": "fixture", "id": "probe"},
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        },
    )["data"]["id"]
    native(
        "POST",
        "/api/session/" + ident + "/prompt",
        {"text": "Run the synthetic sharing check."},
    )

    def done():
        require(not state["failures"], "provider")
        messages = native(
            "GET", "/api/session/" + ident + "/message?limit=100&order=asc"
        )["data"]
        complete = any(
            m.get("type") == "assistant" and m.get("time", {}).get("completed")
            for m in messages
        )
        return (
            messages
            if complete and ident not in native("GET", "/api/session/active")["data"]
            else None
        )

    messages = wait(done, "native-session-timeout")
    require(len(state["results"]) == 1, "native-tool-result")
    return state["results"][0], messages


def exercise_tools(native, env, state, project, foreign, source, config):
    query = "?" + urlencode({"location[directory]": str(project)})

    def configured():
        return any(
            m["name"] == "unrelated" and m["status"]["status"] == "connected"
            for m in native("GET", "/api/mcp" + query)["data"]
        )

    def run(code, directory=project):
        before = snapshot(source)
        result = invoke(native, directory, state, code)
        require(snapshot(source) == before, "source-mutated")
        return result

    def read(name, method, path, directory=project):
        arguments = json.dumps({"path": path})
        return run(f"return await tools.{name}.{method}({arguments})", directory)

    def live(name, path, expected):
        listing = f"await tools.{name}.list_directory({{path:'.'}})"
        reading = f"await tools.{name}.read_file({json.dumps({'path': path})})"
        result, _ = run(f"return [{listing}, {reading}]")
        entries, text = json.loads(result["text"])
        require(
            text == expected
            and {i["name"] for i in entries} == {p.name for p in source.iterdir()},
            "live-list-read",
        )

    wait(configured, "configured-mcp")
    # Native initialization, not sharing, owns any initial runtime scaffolding.
    config_before, project_before = snapshot(config.parent), snapshot(project)
    expected = "synthetic-before 中"
    for ending in ("pipe-close", "reload"):
        name = "oc_box_share_test_" + secrets.token_hex(6)
        with relay(source, native.endpoint, project, name, env) as (finished, failures):
            live(name, "text.txt", expected)
            if ending == "pipe-close":
                expected = "synthetic-after 🙂"
                (source / "text.txt").write_text(expected, encoding="utf-8")
                (source / "new.txt").write_text("synthetic-new-file", encoding="utf-8")
                live(name, "text.txt", expected)
                live(name, "new.txt", "synthetic-new-file")
                result, messages = read(name, "read_image", "pixel.bin")
                image_block = any(
                    n.get("type") == "file" and n.get("mime") == "image/png"
                    for n in nodes(messages)
                )
                require(result["image"] and image_block, "image-preservation")
                cases = (
                    (
                        project,
                        "../not-exported.txt",
                        "Only confined relative share paths are accepted.",
                    ),
                    (foreign, "text.txt", f"Unknown tool '{name}.read_file'"),
                )
                for directory, path, reason in cases:
                    result, _ = read(name, "read_file", path, directory)
                    require(
                        reason in result["text"]
                        and "OUTSIDE_NOT_DISCLOSED" not in result["text"],
                        "share-refusal",
                    )
            else:
                require(native("POST", "/api/location/reload") is None, "native-reload")
                require(finished.wait(10) and not failures, "relay-reload-stop")
        result, _ = read(name, "read_file", "text.txt")
        require(
            f"Unknown tool '{name}.read_file'" in result["text"], "revocation-" + ending
        )
        wait(configured, "configured-mcp-survival")
        result, _ = run("return await tools.unrelated.check({})")
        require(result["text"] == "UNRELATED_MCP_OK", "configured-mcp-tool")
        require(
            snapshot(config.parent) == config_before
            and snapshot(project) == project_before,
            "configuration-mutated",
        )


def exercise(binary, root):
    project, foreign, source = (
        root / name for name in ("project", "foreign", "host-export")
    )
    for path in (project, foreign, source):
        path.mkdir()
    (source / "text.txt").write_text("synthetic-before 中", encoding="utf-8")
    (source / ".hidden").write_text("synthetic-hidden", encoding="utf-8")
    (source / "pixel.bin").write_bytes(base64.b64decode(PNG))
    (root / "not-exported.txt").write_text("OUTSIDE_NOT_DISCLOSED", encoding="utf-8")
    env = private_environment(root / "native")
    release = compat.version(bounded([binary, "--version"], env, project))
    require(compat.major(release) == 2, "native-version")
    print(
        "Native OpenCode " + release + "; private offline sharing fixture.", flush=True
    )
    identities = {
        "commit": ["rev-parse", "HEAD"],
        "commit_count": ["rev-list", "--count", "HEAD"],
    }
    for label, arguments in identities.items():
        identity = subprocess.check_output(
            ["git", *arguments], cwd=REPO, stderr=subprocess.DEVNULL, text=True
        ).strip()
        require(
            identity and all(c in "0123456789abcdef" for c in identity),
            "source-identity",
        )
        print(f"SOURCE {label}={identity}", flush=True)
    names = ("sharing.py", "session_terminal.py", "opencode_v2_io.py", "oc_box.py")
    helpers = [Path(__file__), Path(__file__).with_name("smoke_session_sync_v2.py")]
    for path in [*helpers, *(box.PACKAGE / name for name in names)]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"SOURCE sha256 {path.relative_to(REPO)}={digest}", flush=True)

    previous = Path.cwd()
    with canned_provider() as (port, state):
        config = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        config.parent.mkdir(exist_ok=True)
        model = {
            "name": "Synthetic sharing",
            "limit": {"context": 32000, "output": 2000},
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        }
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "models": {"probe": model},
            "options": {
                "baseURL": f"http://127.0.0.1:{port}/v1",
                "apiKey": "synthetic-only",
            },
        }
        mcp = {
            "type": "local",
            "command": [sys.executable, "-I", "-u", "-c", UNRELATED],
        }
        configuration = {
            "provider": {"fixture": provider},
            "model": "fixture/probe",
            "lsp": False,
            "mcp": {"servers": {"unrelated": mcp}},
        }
        config.write_text(json.dumps(configuration), encoding="utf-8")
        try:
            os.chdir(project)
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary) as native,
            ):
                require(
                    native("GET", "/api/credential")["data"] == [],
                    "inherited-credentials",
                )
                paths = native("GET", "/openapi.json")["paths"]
                require(
                    paths.get("/api/experimental/client-mcp", {})
                    .get("get", {})
                    .get("x-websocket")
                    is True,
                    "client-mcp-unavailable",
                )
                exercise_tools(native, env, state, project, foreign, source, config)
                require(not state["failures"], "provider")
        finally:
            os.chdir(previous)
    print(
        "PASS: production sharing relay; native live listings, UTF-8 reads, updates and new files."
    )
    print(
        "PASS: image blocks survive native Code Mode and subsequent provider input; traversal and location refusals."
    )
    print(
        "PASS: pipe-close/reload revocation; unrelated configured MCP survives; source/configuration preserved."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    args = parser.parse_args()

    def timed_out(*_):
        raise Failure("fixture-timeout")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(300)
    try:
        with tempfile.TemporaryDirectory(prefix="oc-box-share-native-") as temporary:
            exercise(str(args.opencode.resolve()), Path(temporary).resolve())
    except Failure as error:
        print("FAIL: native sharing (" + str(error) + ").", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("FAIL: native sharing (unexpected).", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
