"""Bounded offline Build permission checks against unmodified OpenCode V2.

Usage: python3 -B tests/smoke_build_permissions_v2.py --opencode /path/to/opencode

Every home, config, database, project and service is private temporary fixture
state. The only provider is a loopback synthetic server; no shared native
service, real credentials, host workspace or Podman lifecycle is used. Passing
these checks establishes native service behavior, not live host acceptance.
"""

import argparse
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import oc_box as box
import opencode_compat as compat
import opencode_v2_io as bridge

ACTION = "ocbox_permission_probe"
BUILD_PLUGIN = "oc-box.build-permissions"
TOOL_PLUGIN = "oc-box.fixture-permission-tool"
DISABLED_PLUGIN = "oc-box.fixture-unrelated-disabled"
MARKER = "BUILD_PERMISSION_TOOL_COMPLETED"
TERMINAL_EVENTS = {
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
}
ASK_RULES = [
    {"action": "*", "resource": "*", "effect": "ask"},
    {"action": ACTION, "resource": "session-denied", "effect": "deny"},
]
AGENT_RULES = [
    {"action": ACTION, "resource": "*", "effect": "ask"},
    {"action": ACTION, "resource": "agent-denied", "effect": "deny"},
]

TOOL_SOURCE = """export default {
  id: "oc-box.fixture-permission-tool",
  async setup(ctx) {
    await ctx.tool.transform(editor => {
      editor.add({
        name: "ocbox_permission_probe",
        description: "Exercise a synthetic permission assertion without an agent field.",
        options: { codemode: false },
        input: {
          type: "object", properties: { resource: { type: "string" } },
          required: ["resource"], additionalProperties: false,
        },
        execute: async (input, context) => {
          await ctx.permission.assert({
            sessionID: context.sessionID,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "ocbox_permission_probe", resources: [input.resource],
          })
          return { content: "BUILD_PERMISSION_TOOL_COMPLETED" }
        },
      })
    })
  },
}
"""


def bounded(command, env, cwd, timeout=60):
    """Always deliver stdin EOF; terminate the entire private process group."""
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
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
        raise AssertionError(
            "Native permission fixture timed out: " + output[-2000:] + errors[-2000:]
        ) from None
    assert process.returncode == 0, output[-2000:] + errors[-2000:]
    return output


def environment(root):
    # Positive allowlist: never inherit provider keys, auth content, CLI
    # autoaccept settings, native service registration or host configuration.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root / "home"),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        directory = root / name.lower()
        directory.mkdir(parents=True)
        env["XDG_" + name + "_HOME"] = str(directory)
    Path(env["HOME"]).mkdir()
    return env


@contextlib.contextmanager
def private_server(binary, env, project):
    previous = Path.cwd()
    try:
        os.chdir(project)
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as request:
            yield request
    finally:
        os.chdir(previous)


def write_plugin(directory, source):
    directory.mkdir(parents=True)
    (directory / "package.json").write_text('{"type":"module","main":"index.js"}\n')
    (directory / "index.js").write_text(source)


def location_query(project):
    return "?directory=" + quote(str(project), safe="")


def session_info(request, ident):
    return request("GET", "/api/session/" + ident)["data"]


def create_session(request, project, *, agent=None, parent=None, permissions=None):
    value = {"location": {"directory": str(project)}}
    if agent is not None:
        value["agent"] = agent
    if parent is not None:
        value["parentID"] = parent
    if permissions is not None:
        value["permissions"] = permissions
    return request("POST", "/api/session", value)["data"]


def evaluate(request, ident, expected, label, *, agent=None, resource="ask-target"):
    """Exercise the synchronous native evaluator; reject every pending ask."""
    path = "/api/session/" + ident + "/permission"
    assert request("GET", path)["data"] == [], label + ": stale pending request"
    payload = {"action": ACTION, "resources": [resource]}
    if agent is not None:
        payload["agent"] = agent
    result = request("POST", path, payload)["data"]
    assert isinstance(result.get("id"), str), (label, result)
    pending = request("GET", path)["data"]
    # Clean up even an unexpected ask. Never use once/always or client autoaccept.
    try:
        assert result["effect"] == expected, (label, expected, result)
        if expected == "ask":
            assert len(pending) == 1 and pending[0]["id"] == result["id"], (
                label,
                pending,
            )
            assert pending[0]["sessionID"] == ident, (label, pending)
        else:
            assert pending == [], (label, pending)
    finally:
        for item in pending:
            request("POST", path + "/" + item["id"] + "/reply", {"decision": "reject"})
    assert request("GET", path)["data"] == [], label + ": rejected ask remained"


def agents_snapshot(request, project):
    return {
        item["id"]: {key: item[key] for key in ("id", "mode", "hidden", "permissions")}
        for item in request("GET", "/api/agent" + location_query(project))["data"]
    }


def assert_plugins(request, project, *, generated):
    # As in smoke_v2.py and the production plugins readiness probe, force the
    # native location's integration/plugin initialization before reading state.
    request("GET", "/api/model/default" + location_query(project))
    plugins = request("GET", "/api/plugin" + location_query(project))["data"]
    indexed = {item["id"]: item for item in plugins}
    if generated:
        assert (
            indexed.get(BUILD_PLUGIN, {}).get("state", {}).get("status") == "active"
        ), plugins
        assert (
            indexed.get(TOOL_PLUGIN, {}).get("state", {}).get("status") == "active"
        ), plugins
        assert (
            indexed.get(DISABLED_PLUGIN, {}).get("state", {}).get("status") != "active"
        ), plugins
    else:
        assert BUILD_PLUGIN not in indexed, plugins
    assert not [item for item in plugins if item["state"]["status"] == "failed"], (
        plugins
    )


def assert_preserved(request, project, sessions, preserved_agents):
    for name, original in sessions.items():
        current = session_info(request, original["id"])
        assert current.get("permissions") == original.get("permissions"), (
            name,
            "session permission rules changed",
            current,
        )
        assert current.get("agent") == original.get("agent"), (
            name,
            "stored agent changed during evaluation",
            current,
        )
        assert current.get("parentID") == original.get("parentID"), (name, current)
    agents = agents_snapshot(request, project)
    for name, original in preserved_agents.items():
        assert agents[name] == original, (name, "non-Build agent changed", agents[name])
    assert (
        request("GET", "/api/permission/request" + location_query(project))["data"]
        == []
    )
    assert (
        request("GET", "/api/permission/saved" + location_query(project))["data"] == []
    )
    assert_plugins(request, project, generated=True)


def evaluator_matrix(
    request, project, sessions, preserved_agents, phase, default="build"
):
    assert_plugins(request, project, generated=True)
    build = sessions["build"]["id"]
    unspecified = sessions["unspecified"]["id"]
    bare = sessions["bare-build"]["id"]
    for label, ident, agent, effect in (
        ("Build explicit", build, "build", "allow"),
        ("Build stored", build, None, "allow"),
        (
            "unset stored agent/native default",
            unspecified,
            None,
            "allow" if default == "build" else "ask",
        ),
        ("explicit Build overrides default", unspecified, "build", "allow"),
        ("Plan explicit in Build", build, "plan", "ask"),
        ("custom subagent explicit in Build", build, "fixture-child", "ask"),
        ("custom primary explicit in Build", build, "fixture-review", "ask"),
        # Native session rules can override a missing agent's fallback deny.
        # Such an existing ask must remain ask, never acquire Build privilege.
        ("unknown explicit in Build ask session", build, "fixture-unknown", "ask"),
        ("Plan stored", sessions["plan"]["id"], None, "ask"),
        ("Plan child of Build", sessions["plan-child"]["id"], None, "ask"),
        ("Plan child explicit", sessions["plan-child"]["id"], "plan", "ask"),
        ("custom child of Build", sessions["custom-child"]["id"], None, "ask"),
        (
            "custom child explicit",
            sessions["custom-child"]["id"],
            "fixture-child",
            "ask",
        ),
        ("unknown explicit/native missing-agent deny", bare, "fixture-unknown", "deny"),
        (
            "unknown stored/native missing-agent deny",
            sessions["unknown"]["id"],
            None,
            "deny",
        ),
    ):
        evaluate(request, ident, effect, phase + ": " + label, agent=agent)
    for agent in (None, "build", "plan", "fixture-child", "fixture-unknown"):
        evaluate(
            request,
            build,
            "deny",
            phase + ": explicit session denial " + str(agent),
            agent=agent,
            resource="session-denied",
        )
    for name, agent in (
        ("bare-build", "build"),
        ("bare-plan", "plan"),
        ("bare-child", "fixture-child"),
    ):
        for explicit in (None, agent):
            evaluate(
                request,
                sessions[name]["id"],
                # The existing final Build wildcard intentionally overrides
                # ordinary copied Build rules; it is not a mandatory denial.
                "allow" if agent == "build" else "deny",
                phase + ": configured agent denial " + name,
                agent=explicit,
                resource="agent-denied",
            )
    for agent in (None, "build", "plan", "fixture-child"):
        evaluate(
            request,
            build,
            "deny",
            phase + ": mandatory policy denial " + str(agent),
            agent=agent,
            resource="policy-denied",
        )
    # Read the current session every time, not a cached identity. Explicit
    # request identity still wins over an intentionally switched stored agent.
    try:
        for selected in ("plan", "fixture-child"):
            request("POST", "/api/session/" + build + "/agent", {"agent": selected})
            evaluate(request, build, "ask", phase + ": switched stored " + selected)
            evaluate(
                request,
                build,
                "allow",
                phase + ": explicit Build in stored " + selected,
                agent="build",
            )
    finally:
        request("POST", "/api/session/" + build + "/agent", {"agent": "build"})
    evaluate(request, build, "allow", phase + ": stored Build restored")
    assert_preserved(request, project, sessions, preserved_agents)
    print(
        "PASS: native evaluator "
        + phase
        + "; Build-only asks, child/explicit-agent boundaries and retained denies/rules.",
        flush=True,
    )


def import_sessions(request, project, sessions):
    exports = {
        name: request("GET", "/api/experimental/session/" + value["id"] + "/export")[
            "data"
        ]
        for name, value in sessions.items()
    }
    assert MARKER in json.dumps(exports["native-tool"]["messages"]), (
        "Import fixture must retain a nonempty completed native-tool transcript"
    )
    # Exported parent relationships are retained. Delete roots only (native
    # removal recursively deletes children), then import parents before children.
    for value in sessions.values():
        if "parentID" not in value:
            request("DELETE", "/api/session/" + value["id"])
    for name, original in sessions.items():
        transcript = exports[name]
        imported = request(
            "POST",
            "/api/experimental/session/import",
            {**transcript, "location": {"directory": str(project)}},
        )["data"]
        assert imported["id"] == original["id"], (name, imported)
        copied = request(
            "GET", "/api/experimental/session/" + imported["id"] + "/export"
        )["data"]
        assert copied["messages"] == transcript["messages"], (
            name,
            "native transcript changed",
        )


class EventProbe:
    """Observe native events from connected handshake through terminal execution."""

    def __init__(self, endpoint, session):
        self.endpoint = endpoint
        self.session = session
        self.ready = threading.Event()
        self.done = threading.Event()
        self.events = []
        self.errors = []
        self.worker = threading.Thread(target=self.read, daemon=True)

    def read(self):
        auth = base64.b64encode(
            ("opencode:" + self.endpoint["password"]).encode()
        ).decode()
        request = Request(
            self.endpoint["url"] + "/api/event",
            headers={"Authorization": "Basic " + auth, "Accept": "text/event-stream"},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=70) as response:
                data = []
                for raw in response:
                    line = raw.decode().rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data = []
                        self.events.append(event)
                        if event["type"] == "server.connected":
                            self.ready.set()
                        if (
                            event["type"] in TERMINAL_EVENTS
                            and event["data"]["sessionID"] == self.session
                        ):
                            break
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            self.done.set()

    def start(self):
        self.worker.start()
        assert self.ready.wait(timeout=10), (
            "Native event stream did not connect",
            self.errors,
        )

    def finish(self):
        assert self.done.wait(timeout=10), (
            "Native event stream did not observe terminal execution",
            [event["type"] for event in self.events],
        )
        self.worker.join(timeout=2)
        assert not self.errors, self.errors
        assert any(
            item["type"] in TERMINAL_EVENTS
            and item["data"]["sessionID"] == self.session
            for item in self.events
        ), "Missing event-stream terminal barrier"


@contextlib.contextmanager
def synthetic_provider():
    state = {"requests": 0, "tool_calls": 0, "completed": 0, "errors": []}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions", self.path
                length = int(self.headers["Content-Length"])
                assert 0 < length < 2_000_000, "Synthetic request size exceeded"
                body = json.loads(self.rfile.read(length))
                state["requests"] += 1
                assert state["requests"] <= 12, "Synthetic inference was not bounded"
                tools = {tool["function"]["name"] for tool in body.get("tools", [])}
                completed = [
                    message
                    for message in body.get("messages", [])
                    if message.get("role") == "tool"
                ]
                delta, finish = (
                    {"content": "Synthetic permission fixture finished."},
                    "stop",
                )
                if completed:
                    assert any(
                        MARKER in json.dumps(message.get("content"))
                        for message in completed
                    ), "Native tool did not complete"
                    state["completed"] += 1
                elif tools:
                    assert ACTION in tools, (
                        "Fixture native tool was not advertised: " + repr(sorted(tools))
                    )
                    state["tool_calls"] += 1
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_permission_fixture",
                                "type": "function",
                                "function": {
                                    "name": ACTION,
                                    "arguments": json.dumps(
                                        {"resource": "native-tool-ask"}
                                    ),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for choice in (
                    {"index": 0, "delta": delta, "finish_reason": None},
                    {"index": 0, "delta": {}, "finish_reason": finish},
                ):
                    chunk = {
                        "id": "chatcmpl-permission-fixture",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [choice],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception as error:
                state["errors"].append(str(error))
                self.send_error(400, "Synthetic fixture contract failed")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def actual_tool(request, binary, env, project, state, phase):
    session = create_session(request, project, agent="build", permissions=ASK_RULES)
    ident = session["id"]
    before_calls, before_completed = state["tool_calls"], state["completed"]
    observer = EventProbe(request.endpoint, ident)
    observer.start()
    command = [
        binary,
        "run",
        "--server",
        request.endpoint["url"],
        "--session",
        ident,
        "--agent",
        "build",
        "--model",
        "fixture/gpt-smoke",
        "Exercise the synthetic permission fixture tool exactly once.",
    ]
    assert "--auto" not in command
    try:
        bounded(
            command, {**env, "OPENCODE_PASSWORD": request.endpoint["password"]}, project
        )
    except AssertionError as error:
        raise AssertionError(
            str(error) + "; synthetic provider errors: " + repr(state["errors"])
        ) from None
    observer.finish()
    assert not state["errors"], state["errors"]
    assert state["tool_calls"] == before_calls + 1, state
    assert state["completed"] == before_completed + 1, state
    asked = [
        event
        for event in observer.events
        if event["type"] == "permission.asked" and event["data"]["sessionID"] == ident
    ]
    assert asked == [], (phase, "Build tool emitted permission.asked", asked)
    assert request("GET", "/api/session/" + ident + "/permission")["data"] == []
    assert session_info(request, ident).get("permissions") == session["permissions"]
    exported = request("GET", "/api/experimental/session/" + ident + "/export")["data"]
    completed = [
        part
        for message in exported["messages"]
        if message["type"] == "assistant"
        for part in message["content"]
        if part.get("type") == "tool"
        and part["name"] == ACTION
        and part["state"]["status"] == "completed"
    ]
    assert len(completed) == 1 and MARKER in json.dumps(completed[0]["state"]), exported
    print(
        "PASS: native Build tool "
        + phase
        + " asserted without agent; completed marker, zero pending/permission.asked, no --auto.",
        flush=True,
    )
    return session


def main(binary):
    binary = str(Path(binary).resolve(strict=True))
    temporary_parent = Path("/tmp/opencode")
    # CI may not pre-create the preferred private temporary parent.
    temporary_parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="oc-box-build-permissions-v2-", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        env = environment(root / "container")
        release = compat.version(bounded([binary, "--version"], env, project))
        assert release.startswith("2."), (
            "An unmodified OpenCode V2 executable is required: " + release
        )
        bounded(["git", "init", "--quiet"], env, project)
        config = Path(env["HOME"]) / ".config/opencode"
        config.mkdir(parents=True)
        env["XDG_CONFIG_HOME"] = str(config.parent)
        own = config / "plugins/oc-box-build-permissions"
        fixture = config / "plugins/fixture-permission-tool"
        disabled = config / "plugins/fixture-unrelated-disabled"
        write_plugin(own, box.container_build_plugin(release))
        write_plugin(fixture, TOOL_SOURCE)
        write_plugin(
            disabled, 'export default { id: "' + DISABLED_PLUGIN + '", setup() {} }\n'
        )
        agent_config = {
            name: {"mode": mode, "permissions": AGENT_RULES}
            for name, mode in (
                ("build", "primary"),
                ("plan", "primary"),
                ("fixture-child", "subagent"),
                ("fixture-review", "primary"),
                ("fixture-hidden", "primary"),
            )
        }
        agent_config["fixture-hidden"]["hidden"] = True
        settings = {
            "$schema": "https://opencode.ai/config.json",
            "default_agent": "build",
            "snapshots": False,
            "formatter": False,
            "update": "disable",
            "agents": agent_config,
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "fixture", "effect": "allow"},
            ],
        }
        (config / "opencode.json").write_text(json.dumps(settings))
        profile = config / "oc-box-profile.json"
        profile.write_text(
            json.dumps({"plugins": [str(own), str(fixture), str(disabled)]})
        )
        env["OPENCODE_CONFIG"] = str(profile)
        # Disable IDs rather than paths: native V2 removals operate on plugin IDs.
        project_config = project / "opencode.json"
        project_config.write_text(
            json.dumps({"plugins": ["-" + BUILD_PLUGIN, "-" + DISABLED_PLUGIN]})
        )
        final = box.container_build_config(release, home=env["HOME"])
        targets = [
            entry["package"] if isinstance(entry, dict) else entry
            for entry in final.get("plugins", [])
        ]
        assert targets == [str(own)], (
            "Final overlay must re-enable only its own generated target",
            final,
        )
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(final)

        # A plain-host control uses another isolated home/database and no
        # generated profile/content/plugin, not the user's real host instance.
        control_project = root / "host-control-project"
        control_project.mkdir()
        control_env = environment(root / "host-control")
        control_config = Path(control_env["XDG_CONFIG_HOME"]) / "opencode"
        control_config.mkdir()
        (control_config / "opencode.json").write_text(json.dumps(settings))
        with private_server(binary, control_env, control_project) as request:
            control_agents = agents_snapshot(request, control_project)
            assert_plugins(request, control_project, generated=False)
            control = create_session(
                request, control_project, agent="build", permissions=ASK_RULES
            )
            for agent in (None, "build"):
                evaluate(
                    request, control["id"], "ask", "normal host control", agent=agent
                )
        print(
            "PASS: isolated normal-host control still asks; no generated plugin or global client autoaccept.",
            flush=True,
        )

        removed = {key: value for key, value in final.items() if key != "plugins"}
        removal_env = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(removed)}
        with private_server(binary, removal_env, project) as request:
            assert_plugins(request, project, generated=False)
            removal_control = create_session(
                request, project, agent="build", permissions=ASK_RULES
            )
            evaluate(
                request,
                removal_control["id"],
                "ask",
                "project-removal negative control",
                agent="build",
            )
            request("DELETE", "/api/session/" + removal_control["id"])
        print(
            "PASS: project removal disables the helper without its final private re-enable entry.",
            flush=True,
        )

        with synthetic_provider() as (port, state):
            final["model"] = "fixture/gpt-smoke"
            final["policies"] = [
                {
                    "action": "tool.use",
                    "resource": ACTION + ":policy-denied",
                    "effect": "deny",
                }
            ]
            final["providers"] = {
                "fixture": {
                    "package": "aisdk:@ai-sdk/openai-compatible",
                    "settings": {
                        "baseURL": f"http://127.0.0.1:{port}/v1",
                        "apiKey": "synthetic-fixture-key",
                    },
                    "models": {
                        "gpt-smoke": {
                            "name": "Permission fixture",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                }
            }
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(final)
            with private_server(binary, env, project) as request:
                agents = agents_snapshot(request, project)
                assert_plugins(request, project, generated=True)
                preserved = {
                    # Native Plan has a HOME-relative scratch-file allowance.
                    # Compare the same rule with its private control home
                    # relocated, not the unrelated absolute fixture paths.
                    name: json.loads(
                        json.dumps(control_agents[name]).replace(
                            control_env["HOME"], env["HOME"]
                        )
                    )
                    for name in (
                        "plan",
                        "fixture-child",
                        "fixture-review",
                        "fixture-hidden",
                    )
                }
                assert {name: agents[name] for name in preserved} == preserved, (
                    "Generated config modified non-Build rules",
                    {
                        name: {"control": preserved[name], "generated": agents[name]}
                        for name in preserved
                        if agents[name] != preserved[name]
                    },
                )
                selectable = [
                    item
                    for item in request("GET", "/api/agent" + location_query(project))[
                        "data"
                    ]
                    if item["mode"] != "subagent" and not item["hidden"]
                ]
                assert selectable[0]["id"] == "build", selectable
                sessions = {}
                for name, agent, rules in (
                    ("build", "build", ASK_RULES),
                    ("unspecified", None, ASK_RULES),
                    ("plan", "plan", ASK_RULES),
                    ("bare-build", "build", []),
                    ("bare-plan", "plan", []),
                    ("bare-child", "fixture-child", []),
                    ("unknown", "fixture-unknown", []),
                ):
                    sessions[name] = create_session(
                        request, project, agent=agent, permissions=rules
                    )
                assert "agent" not in sessions["unspecified"], (
                    "Fixture must cover absent stored agent"
                )
                for name, agent in (
                    ("plan-child", "plan"),
                    ("custom-child", "fixture-child"),
                ):
                    sessions[name] = create_session(
                        request, project, agent=agent, parent=sessions["build"]["id"]
                    )
                    assert sessions[name]["permissions"] == ASK_RULES, (
                        "Native child did not inherit asks"
                    )
                evaluator_matrix(request, project, sessions, preserved, "fresh")
                sessions["native-tool"] = actual_tool(
                    request, binary, env, project, state, "fresh"
                )
                import_sessions(request, project, sessions)
                evaluator_matrix(
                    request,
                    project,
                    sessions,
                    preserved,
                    "after native transcript import",
                )
                request("POST", "/api/location/reload")
                evaluator_matrix(
                    request, project, sessions, preserved, "after location reload"
                )
                actual_tool(
                    request, binary, env, project, state, "after location reload"
                )
                # Keep stored identity absent and switch only the native default.
                # This catches cached Build identity and wrong list ordering.
                project_config.write_text(
                    json.dumps(
                        {
                            "plugins": ["-" + BUILD_PLUGIN, "-" + DISABLED_PLUGIN],
                            "default_agent": "plan",
                        }
                    )
                )
                request("POST", "/api/location/reload")
                selectable = [
                    item
                    for item in request("GET", "/api/agent" + location_query(project))[
                        "data"
                    ]
                    if item["mode"] != "subagent" and not item["hidden"]
                ]
                assert selectable[0]["id"] == "plan", selectable
                evaluator_matrix(
                    request,
                    project,
                    sessions,
                    preserved,
                    "after default Plan reload",
                    default="plan",
                )
        print(
            "PASS: native V2 Build permission fixture (offline service only; no live Podman/host acceptance).",
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opencode",
        required=True,
        help="Separately installed unmodified OpenCode V2 executable",
    )
    main(parser.parse_args().opencode)
