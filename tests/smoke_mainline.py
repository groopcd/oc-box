"""Bounded offline smoke test of native OpenCode tools, MCP, formatter, and LSP.

This does not simulate container isolation. It validates the mainline execution
path used inside Podman, with temporary config/data and local fixture services.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fnmatch
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
sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location("oc_box", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)
sync_spec = importlib.util.spec_from_file_location(
    "sync_config", APP / "sync_config.py"
)
assert sync_spec is not None and sync_spec.loader is not None
sync = importlib.util.module_from_spec(sync_spec)
sync_spec.loader.exec_module(sync)


def bounded(command, *, env, cwd, timeout=45):
    process = subprocess.Popen(
        command,
        env={**env, "PWD": str(cwd)},
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=5)
        raise AssertionError(
            f"Smoke test stopped after {timeout}s. Last output:\n{out[-2000:]}\n{err[-2000:]}"
        )
    assert process.returncode == 0, out[-4000:] + err[-4000:]
    return out, err


def build_permission_regression(root, env, project):
    """Use mainline's resolved rules to verify container-only Build override."""
    source = root / "permission-host-config/opencode"
    source.mkdir(parents=True)
    inherited = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"bash": "ask", "external_directory": "ask"},
        "agent": {"build": {"permission": {"bash": "deny", "edit": "ask"}}},
    }
    original = json.dumps(inherited)
    (source / "opencode.json").write_text(original)
    home = root / "permission-container-home"
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}):
        payload = box.config_archive({"home": str(home)})
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(home / ".config/opencode", filter="data")
    # This fixture plugin is plain JS and does not import the SDK. Seed the
    # exact SDK manifest so mainline does not contact npm during an offline
    # permission test (a cold package install can otherwise take 60+ seconds).
    installed, _ = bounded(["opencode", "--version"], env=env, cwd=project, timeout=10)
    config_directory = home / ".config/opencode"
    package = config_directory / "node_modules/@opencode-ai/plugin"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@opencode-ai/plugin",
                "version": installed.strip(),
                "type": "module",
            }
        )
    )
    (config_directory / "package.json").write_text(
        json.dumps({"dependencies": {"@opencode-ai/plugin": installed.strip()}})
    )
    profile = home / ".config/opencode/oc-box-profile.json"
    scoped = dict(env)
    scoped["XDG_CONFIG_HOME"] = str(home / ".config")
    scoped["OPENCODE_TEST_HOME"] = str(home)
    scoped["OPENCODE_CONFIG"] = str(profile)
    scoped["OPENCODE_DISABLE_PROJECT_CONFIG"] = "0"
    settings = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    scoped["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {
            **box.container_build_config(),
            "provider": settings["provider"],
            "model": settings["model"],
        }
    )
    local = project / "opencode.json"
    local.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "agent": {
                    "build": {
                        "permission": {
                            "bash": "deny",
                            "external_directory": "deny",
                            "fixture_ping": "ask",
                        }
                    }
                },
            }
        )
    )

    def effective(agent, permission, pattern):
        matching = [
            rule
            for rule in agent["permission"]
            if fnmatch.fnmatchcase(permission, rule["permission"])
            and fnmatch.fnmatchcase(pattern, rule["pattern"])
        ]
        return matching[-1]["action"] if matching else "ask"

    try:
        agents = {}
        for name in ("build", "plan", "general"):
            output, _ = bounded(
                ["opencode", "debug", "agent", name],
                env=scoped,
                cwd=project,
                timeout=15,
            )
            agents[name] = json.loads(output)
        for permission, pattern in (
            ("bash", "zypper install git"),
            ("edit", "/etc/example"),
            ("external_directory", "/etc/*"),
            ("read", "settings.env"),
            ("fixture_ping", "*"),
            ("doom_loop", "*"),
        ):
            assert effective(agents["build"], permission, pattern) == "allow", (
                permission,
                agents["build"],
            )
        assert effective(agents["plan"], "edit", "source.py") == "deny", agents["plan"]
        assert effective(agents["plan"], "bash", "echo test") == "ask", agents["plan"]
        assert effective(agents["general"], "bash", "echo test") == "ask", agents[
            "general"
        ]
        # Without the container profile the original host-style restrictions win.
        host = {
            **scoped,
            "XDG_CONFIG_HOME": str(source.parent),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }
        host.pop("OPENCODE_CONFIG")
        host["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {"provider": settings["provider"], "model": settings["model"]}
        )
        output, _ = bounded(
            ["opencode", "--pure", "debug", "agent", "build"],
            env=host,
            cwd=project,
            timeout=15,
        )
        assert effective(json.loads(output), "bash", "echo test") == "deny"
        assert (source / "opencode.json").read_text() == original
        print(
            "PASS: container Build allows all tools over inherited/project restrictions; Plan and host permissions remain effective."
        )
    finally:
        local.unlink(missing_ok=True)


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
    assert (
        json.loads((config / "package.json").read_text())["dependencies"][
            plugin_package
        ]
        == version
    )
    # Simulate the config directory produced after first mainline SDK install.
    package = config / "node_modules" / plugin_package
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": plugin_package, "version": version, "type": "module"})
    )
    manifest = {"dependencies": {plugin_package: version}}
    (config / "package.json").write_text(json.dumps(manifest))
    (config / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": manifest}})
    )
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
            assert sync.sync_config(home, payload, version), (
                "Warm startup invalidated the generated SDK cache"
            )
            marker.unlink(missing_ok=True)
            started = time.monotonic()
            bounded(
                ["opencode", "debug", "agent", "build"],
                env=warm_env,
                cwd=project,
                timeout=20,
            )
            durations.append(time.monotonic() - started)
            assert marker.is_file(), "The enabled external plugin did not initialize"
            assert json.loads((config / "package.json").read_text()) == manifest
        assert not requests, (
            f"Warm mainline startup unexpectedly contacted npm: {requests}"
        )
        print(
            "PASS: two warm native plugin startups with an older source SDK and zero npm requests ("
            + ", ".join(f"{seconds:.2f}s" for seconds in durations)
            + ")."
        )
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
    trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"

    def local_exec(_data, command, **_kwargs):
        return box.ContainerCommand(
            [sys.executable, "-c", trampoline, *command], child_env
        )

    with (
        patch.object(box, "host_cli_env", return_value=host),
        patch.object(box, "CONTAINER_OPENCODE", executable),
        patch.object(box, "exec_command", side_effect=local_exec),
    ):
        source = box.export_host_session(registry, data, ident)
        # Exercise the multi-MiB export/transfer path without another model call.
        large = json.loads(json.dumps(source))
        text = next(
            part
            for message in large["messages"]
            for part in message["parts"]
            if part.get("type") == "text"
        )
        text["text"] += "\n" + "transfer regression " * 140000
        fixture = root / "large-source.json"
        box.atomic_json(fixture, large)
        bounded(
            ["opencode", "--pure", "session", "delete", ident],
            env=host,
            cwd=project,
            timeout=15,
        )
        bounded(
            ["opencode", "--pure", "import", str(fixture)],
            env=host,
            cwd=project,
            timeout=15,
        )
        source = box.export_host_session(registry, data, ident)
        assert len(json.dumps(source)) > 2_000_000
        host_before = json.loads(json.dumps(source))
        # Older persisted tool states may retain `raw` after completion. The
        # real mainline importer strips it for nonpending states, but preserves
        # pending raw and arbitrary input/metadata fields named raw.
        owner = next(
            message
            for message in source["messages"]
            if any(
                part.get("type") == "tool" and part["state"]["status"] == "completed"
                for part in message["parts"]
            )
        )
        completed = next(
            part
            for part in owner["parts"]
            if part.get("type") == "tool" and part["state"]["status"] == "completed"
        )
        completed["state"]["raw"] = "obsolete completed buffer"
        completed["state"]["metadata"]["raw"] = "metadata must survive"
        for status in ("error", "pending", "running"):
            state = {
                "status": status,
                "input": {"raw": "input must survive"},
                "raw": "buffer " + status,
            }
            if status != "pending":
                state["time"] = {"start": 1}
            if status == "error":
                state["error"] = "fixture interrupted"
                state["time"]["end"] = 2
            owner["parts"].append(
                {
                    "id": "prt_zzzz_import_" + status,
                    "messageID": owner["info"]["id"],
                    "sessionID": ident,
                    "type": "tool",
                    "tool": "fixture",
                    "callID": "import_" + status,
                    "state": state,
                }
            )
        recorded = box.import_host_session(registry, data, source)
        assert recorded["last"] == ident
        assert box.refresh_sessions(registry, data)["last"] == ident
        copied = box.export_container_session(data, ident)
        assert box.verify_session_import(data, source, copied) == 3
        assert copied["messages"] != source["messages"], (
            "Fixture did not reproduce raw-field schema stripping"
        )
        # Reproduce the user's old verification failure: the import exists and
        # a source backup is saved, but no successful import record was written.
        box.session_file(registry).unlink()
        with patch.object(
            box,
            "transfer_host_session",
            side_effect=AssertionError("Recovery must not reimport"),
        ):
            recovered = box.import_host_session(registry, data, source)
        assert recovered["last"] == ident
        try:
            box.import_host_session(registry, data, source)
        except box.BoxError as exc:
            assert "not overwritten" in str(exc), exc
        else:
            raise AssertionError(
                "Duplicate host import overwrote the container session"
            )
        assert box.export_host_session(registry, data, ident) == host_before, (
            "Host source changed during transfer"
        )
        bounded(
            [executable, "--pure", "run", "--session", ident, "Finish without tools."],
            env={**env, "XDG_DATA_HOME": isolated["XDG_DATA_HOME"]},
            cwd=project,
            timeout=20,
        )
    print(
        "PASS: host-to-container import (>2 MB), mainline tool raw normalization, no-write failed-import recovery, duplicate protection, and resume."
    )


def archive_import_regression(root, env, project, exported):
    """Round-trip production archives through mainline's controller-directory import."""
    host = {**env, "XDG_DATA_HOME": str(root / "archive-host-data")}
    host.pop("OPENCODE_CONFIG_CONTENT", None)
    isolated = {**host, "XDG_DATA_HOME": str(root / "archive-container-data")}
    executable = shutil.which("opencode")
    trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"

    def local_exec(_data, command, **_kwargs):
        return box.ContainerCommand(
            [sys.executable, "-c", trampoline, *command],
            {**isolated, "PWD": str(project)},
        )

    with patch.dict(os.environ, {"OC_BOX_STATE_HOME": str(root / "archive-registry")}):
        origin_path, origin = box.create_workspace(project, "tumbleweed")
        # A fresh workspace after the marker was removed must import old archives.
        (project / box.MARKER).unlink()
        target_path, target = box.create_workspace(project, "tumbleweed")
        (project / box.MARKER).unlink()
        archive = box.archive_session(exported, origin)
        ident = archive["info"]["id"]
        saved = origin_path.parent / "history" / (ident + ".json")
        box.atomic_json(saved, archive)
        bounded(
            [executable, "--pure", "import", str(saved)],
            env=host,
            cwd=origin_path.parent / "controller",
            timeout=15,
        )
        raw, _ = bounded(
            [executable, "--pure", "export", ident],
            env=host,
            cwd=target_path.parent / "controller",
            timeout=15,
        )
        host_before = json.loads(raw)
        assert host_before["info"]["directory"] == str(
            origin_path.parent / "controller"
        )
        # Retiring a duplicate must not make its host archive unrecoverable.
        box.retain_archive_provenance(origin_path, origin)
        shutil.rmtree(origin_path.parent)
        assert origin_path not in box.workspace_paths()
        with (
            patch.object(box, "host_cli_env", return_value=host),
            patch.object(box, "CONTAINER_OPENCODE", executable),
            patch.object(box, "exec_command", side_effect=local_exec),
        ):
            recovered = box.export_host_session(target_path, target, ident)
            assert recovered["info"]["directory"] == str(project)
            assert recovered["messages"] == host_before["messages"]
            state = box.import_host_session(target_path, target, recovered)
            assert state["last"] == ident
            copied = box.export_container_session(target, ident)
            box.verify_session_import(target, recovered, copied)
            assert box.refresh_sessions(target_path, target)["last"] == ident
        raw, _ = bounded(
            [executable, "--pure", "export", ident],
            env=host,
            cwd=target_path.parent / "controller",
            timeout=15,
        )
        assert json.loads(raw) == host_before, (
            "Archive recovery changed the host session"
        )
    print(
        "PASS: controller-directory archive recovery after workspace retirement, verified transcript, and unchanged host archive."
    )


def main():
    with tempfile.TemporaryDirectory(prefix="oc-box-native-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENCODE_", "OC_BOX_"))
            and not key.endswith(("_TOKEN", "_API_KEY"))
        }
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
                    request = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    requests.append(request)
                    tools = {
                        item["function"]["name"]: item["function"]
                        for item in request.get("tools", [])
                    }
                    completed = sum(
                        item.get("role") == "tool"
                        for item in request.get("messages", [])
                    )
                    # Every response completes successfully; no assertion causes
                    # a dropped connection/retry loop in the fake provider.
                    delta, finish = {"content": "Smoke test complete"}, "stop"
                    if tools and len(requests) <= 10 and completed < 3:
                        if completed == 0:
                            name, args = (
                                "write",
                                {
                                    "filePath": str(target),
                                    "content": "hello formatter\n",
                                },
                            )
                        elif completed == 1:
                            name, args = "read", {"filePath": str(target)}
                        else:
                            name = next(
                                (name for name in tools if name.endswith("_ping")),
                                "MISSING_MCP",
                            )
                            args = {}
                        if name not in tools:
                            failures.append(
                                f"Native tool {name} missing from catalog: {list(tools)}"
                            )
                        else:
                            delta = {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_smoke_{completed}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ]
                            }
                            finish = "tool_calls"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for value in (
                        {
                            "id": "chatcmpl-smoke",
                            "object": "chat.completion.chunk",
                            "model": "gpt-4-smoke",
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}
                            ],
                        },
                        {
                            "id": "chatcmpl-smoke",
                            "object": "chat.completion.chunk",
                            "model": "gpt-4-smoke",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": finish}
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        },
                    ):
                        self.wfile.write(
                            ("data: " + json.dumps(value) + "\n\n").encode()
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "oc-box-smoke/gpt-4-smoke",
            "permission": "ask",
            "agent": box.container_build_config()["agent"],
            "snapshot": False,
            "provider": {
                "oc-box-smoke": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Smoke",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                        "apiKey": "test",
                    },
                    "models": {
                        "gpt-4-smoke": {
                            "name": "Smoke",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                }
            },
            "mcp": {
                "fixture": {
                    "type": "local",
                    "command": ["python3", service, "mcp", str(logs["mcp"])],
                }
            },
            "formatter": {
                "fixture": {
                    "command": [
                        "python3",
                        service,
                        "format",
                        str(logs["formatter"]),
                        "$FILE",
                    ],
                    "extensions": [".py"],
                }
            },
            "lsp": {
                "pyright": {"disabled": True},
                "fixture": {
                    "command": ["python3", service, "lsp", str(logs["lsp"])],
                    "extensions": [".py"],
                },
            },
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        try:
            build_permission_regression(root, env, project)
            resolved, _ = bounded(
                ["opencode", "--pure", "debug", "config"],
                env=env,
                cwd=project,
                timeout=15,
            )
            assert "fixture" in json.loads(resolved)["lsp"], resolved[-3000:]
            out, err = bounded(
                [
                    "opencode",
                    "--pure",
                    "run",
                    "--model",
                    "oc-box-smoke/gpt-4-smoke",
                    "Write and read example.py, then call fixture ping.",
                ],
                env=env,
                cwd=project,
                timeout=50,
            )
            assert not failures, failures
            assert target.is_file(), out[-2000:] + err[-2000:]
            assert target.read_text() == "HELLO FORMATTER\n", target.read_text()
            assert logs["formatter"].is_file(), "Native formatter was not invoked"
            mcp = [json.loads(line) for line in logs["mcp"].read_text().splitlines()]
            assert any(item.get("method") == "tools/call" for item in mcp), mcp
            if not logs["lsp"].exists():
                recent = "\n".join(
                    file.read_text()[-5000:]
                    for file in (root / "data/opencode/log").glob("*")
                )
                raise AssertionError(
                    "LSP fixture did not start. CLI output:\n"
                    + out[-2000:]
                    + err[-2000:]
                    + "\nLogs:\n"
                    + recent[-7000:]
                )
            lsp = [json.loads(line) for line in logs["lsp"].read_text().splitlines()]
            assert any(item.get("method") == "initialize" for item in lsp), lsp
            assert any(item.get("method") == "textDocument/didOpen" for item in lsp), (
                lsp
            )
            print(
                "PASS: native file write/read, automatic formatter, LSP initialize/didOpen, and MCP tool call."
            )
            listing, _ = bounded(
                ["opencode", "--pure", "session", "list", "--format", "json"],
                env=env,
                cwd=project,
                timeout=15,
            )
            sessions = json.loads(listing)
            ident = sessions[0]["id"]
            exported, _ = bounded(
                ["opencode", "--pure", "export", ident],
                env=env,
                cwd=project,
                timeout=15,
            )
            archived = box.archive_session(
                json.loads(exported), {"id": "a" * 32, "project": str(project)}
            )
            archive = root / "archive.json"
            box.atomic_json(archive, archived)
            host = dict(env)
            host.pop("OPENCODE_CONFIG_CONTENT")
            host["XDG_DATA_HOME"] = str(root / "host-data")
            bounded(
                ["opencode", "--pure", "import", str(archive)],
                env=host,
                cwd=project,
                timeout=15,
            )
            copied, _ = bounded(
                ["opencode", "--pure", "export", archived["info"]["id"]],
                env=host,
                cwd=project,
                timeout=15,
            )
            assert len(json.loads(copied)["messages"]) == len(archived["messages"])
            # Resume the same native session without requiring another tool call.
            bounded(
                [
                    "opencode",
                    "--pure",
                    "run",
                    "--session",
                    ident,
                    "Finish without tools.",
                ],
                env=env,
                cwd=project,
                timeout=20,
            )
            print(
                "PASS: session export/import into a separate host-style database, and exact-session resume."
            )
            session_import_regression(root, env, project, ident)
            archive_import_regression(root, env, project, json.loads(exported))
            warm_start_regression(root, env, project)
            print(
                "All app data was temporary. Live Podman isolation remains a separate host-terminal check."
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
