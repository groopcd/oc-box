"""Bounded offline tests against a separately installed, unmodified OpenCode 2.x.

Usage: python3 -B tests/smoke_v2.py --opencode /path/to/v2/opencode
No host registry, credentials, providers, or managed service is touched.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
from urllib.parse import quote
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import oc_box as box
import opencode_compat as compat
import opencode_v2_io as bridge
import sync_config


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


def main(binary):
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
        (source / "opencode.json").write_text(
            json.dumps(
                {
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
            "opencode_version": release,
        }
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}):
            payload = box.config_archive(data)
        sync_config.sync_config(env["HOME"], payload, release)
        config = Path(env["HOME"]) / ".config/opencode"
        env["XDG_CONFIG_HOME"] = str(config.parent)
        env["OPENCODE_CONFIG"] = str(config / "oc-box-profile.json")
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

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                tools = {
                    tool["function"]["name"]: tool for tool in body.get("tools", [])
                }
                completed = sum(
                    message.get("role") == "tool"
                    for message in body.get("messages", [])
                )
                delta, finish = {"content": "Smoke complete"}, "stop"
                if tools and completed < 4:
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
                                "command": 'test "$OC_BOX_SYNTHETIC_TOKEN" = synthetic-v2-secret && printf ENV_OK'
                            },
                        )
                    else:
                        name, arguments = (
                            "execute",
                            {"code": "return await tools.fixture.ping({})"},
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
                }
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
            output = cli(
                "run",
                "--model",
                "fixture/gpt-smoke",
                "Exercise native write/read and shell tools.",
            )
            assert target.is_file(), output
            assert target.read_text() == "HELLO FORMATTER\n", output
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
            print(
                "PASS: V2 native tools, formatter, MCP initialization and server/shell environment inheritance.",
                flush=True,
            )
            listed = json.loads(cli("session", "list", "--format", "json"))
            ident = listed[0]["id"]
            exported = json.loads(cli("export", ident))
            box.validate_session_export(exported, ident)
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
            assert (
                json.loads(
                    bounded(
                        ["opencode", "--pure", "export", "ses_legacy_fixture"],
                        legacy_env,
                        project,
                    )
                )["messages"][0]["parts"][0]["text"]
                == "Legacy text"
            )
            print(
                "PASS: real V1-to-V2 snapshot migration with unchanged V1 database and downgrade export.",
                flush=True,
            )
            assert not (config / "service.json").exists()
            assert not (Path(env["XDG_STATE_HOME"]) / "opencode/service.json").exists()
        finally:
            model.shutdown()
            model.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    main(parser.parse_args().opencode)
