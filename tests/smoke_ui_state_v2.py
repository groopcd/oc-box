"""Provider-free actual native TUI UI-state reporting, not host acceptance.

python3 -B tests/smoke_ui_state_v2.py --opencode /path/to/unmodified-opencode

Only fresh synthetic HOME/XDG directories and a stdio-owned server are used.
The installed public-SDK package, native TUI/router/tabs, production client
environment bridge and stdin read helper stay real. Only Podman execution is
substituted with local execution. A forwarding-only HTTP observer counts native
session mutation attempts; it implements no native API and injects no routes.
No inference, host history/config, private browser storage or Podman is used.
The fixture-only second public-SDK plugin drives ui.tabs.focus and app.exit
through its own RPC event, never through session mutation APIs or helper mocks.
"""

import argparse
import contextlib
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import secrets
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oc_box import native_tabs
from oc_box import oc_box as box, opencode_compat, opencode_v2_io as bridge
from oc_box import ui_state, workspace_service as service
from smoke_native_preferences_v2 import Client as PreferencesClient
from smoke_session_sync_v2 import bounded, private_environment
from smoke_sharing_v2 import Failure, require

SOURCES = (
    "src/oc_box/native_tabs.py",
    "src/oc_box/ui_state.py",
    *("src/oc_box/" + name for name in ui_state.RESOURCES),
    "src/oc_box/oc_box.py",
    "src/oc_box/workspace_service.py",
    "src/oc_box/opencode_v2_io.py",
    "src/oc_box/installation.py",
    "pyproject.toml",
    "tests/smoke_ui_state_v2.py",
    "tests/smoke_native_preferences_v2.py",
    "tests/smoke_session_sync_v2.py",
    "tests/test_ui_state.py",
    "tests/ui_state_fixture.mjs",
    "tests/test_packaging.py",
    "tools/check_package.py",
    "docs/ui-state.md",
)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for content in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(content)
    return value.hexdigest()


def identity():
    def git(*args):
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "hashes": {name: digest(REPO / name) for name in SOURCES},
    }


CONTROL_RPC = """
import { Rpc } from "@opencode/plugin/rpc"
const schema = {"~standard": {version: 1, vendor: "synthetic-ui-control",
  validate(value) { return {value} }}}
export const Control = Rpc.define({id: "synthetic.ui-control",
  methods: {send: {input: schema, output: schema}},
  events: {action: {schema}}})
"""
CONTROL_SERVER = """
import { Plugin } from "@opencode/plugin"
import { Control } from "./rpc.js"
export default Plugin.define({id: "synthetic.ui-control", async setup(ctx) {
  const registration = await ctx.rpc.register(Control, {
    send: async (value) => { await registration.events.emit("action", value); return {accepted:true} }
  })
  return () => registration.dispose()
}})
"""
CONTROL_TUI = """
import { Plugin } from "@opencode/plugin/tui"
import { Control } from "./rpc.js"
export default Plugin.define({id:"synthetic.ui-control.tui", setup(ctx) {
  return ctx.client.rpc(Control).events.on("action", (event) => {
    if (event.data.client !== process.env.OC_BOX_UI_STATE_CLIENT) return
    if (event.data.action === "select") ctx.ui.tabs.focus(event.data.sessionID)
    if (event.data.action === "blur") ctx.renderer.emit("blur")
    if (event.data.action === "exit") ctx.keymap.dispatch("app.exit")
  })
}})
"""


class Observer:
    """Forward real JSON/SSE; retain fixed route classifications, never bodies."""

    def __init__(self, endpoint):
        self.endpoint = urlsplit(endpoint["url"])
        self.bad_posts = self.releases = 0
        self.reports = 0
        self.report_statuses = []
        self.report_shapes = []
        self.errors = []
        self.lock = threading.Lock()
        self.streams, self.handlers = set(), set()
        self.closing = False

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def forward(self):
                upstream = http.client.HTTPConnection(
                    owner.endpoint.hostname, owner.endpoint.port, timeout=5
                )
                response = stream = None
                with owner.lock:
                    owner.handlers.add(threading.current_thread())
                try:
                    require(not self.headers.get("Upgrade"), "unexpected-websocket")
                    size = int(self.headers.get("Content-Length", "0"))
                    require(0 <= size <= 1024 * 1024, "request-size")
                    body = self.rfile.read(size)
                    route = urlsplit(self.path).path
                    if self.command == "POST":
                        with owner.lock:
                            if re.fullmatch(
                                r"/api/session/[^/]+/(prompt|inbox|command|shell|synthetic|move)",
                                route,
                            ):
                                owner.bad_posts += 1
                            if route == ui_state.RPC + "release":
                                owner.releases += 1
                            if route == ui_state.RPC + "report":
                                owner.reports += 1
                                try:
                                    value = json.loads(body).get("input", {})
                                    owner.report_shapes.append(
                                        {
                                            "keys": sorted(value),
                                            "types": {
                                                key: type(item).__name__
                                                for key, item in value.items()
                                            },
                                            "routeType": value.get("routeType"),
                                            "seq": value.get("seq"),
                                            "client_valid": ui_state._nonce(
                                                value.get("client")
                                            ),
                                            "generation_valid": ui_state._nonce(
                                                value.get("generation")
                                            ),
                                            "tabs_count": len(value.get("tabs", [])),
                                            "focus": value.get("focus"),
                                        }
                                    )
                                except Exception:
                                    owner.report_shapes.append({"invalid": True})
                                owner.report_shapes = owner.report_shapes[-4:]
                    upstream.connect()
                    stream = upstream.sock
                    stream.settimeout(60)
                    with owner.lock:
                        owner.streams.add(stream)
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower() not in ("connection", "host")
                    }
                    headers["Connection"] = "close"
                    upstream.request(self.command, self.path, body or None, headers)
                    response = upstream.getresponse()
                    if route == ui_state.RPC + "report":
                        owner.report_statuses.append(response.status)
                        owner.report_statuses = owner.report_statuses[-16:]
                    require(response.status != 101, "unexpected-websocket")
                    self.send_response_only(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ("connection", "transfer-encoding"):
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while content := response.read1(65536):
                        self.wfile.write(content)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    if not owner.closing:
                        owner.errors.append("http-observer")
                finally:
                    if response is not None:
                        response.close()
                    upstream.close()
                    with owner.lock:
                        owner.streams.discard(stream)
                        owner.handlers.discard(threading.current_thread())

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = forward

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, *_):
                owner.errors.append("http-worker")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        return self

    def __exit__(self, *_):
        self.closing = True
        self.server.shutdown()
        with self.lock:
            streams, handlers = tuple(self.streams), tuple(self.handlers)
        for stream in streams:
            with contextlib.suppress(OSError):
                stream.shutdown(2)
            stream.close()
        self.server.server_close()
        self.worker.join(5)
        deadline = time.monotonic() + 5
        for handler in handlers:
            handler.join(max(0, deadline - time.monotonic()))
        require(
            not self.worker.is_alive()
            and all(not handler.is_alive() for handler in handlers),
            "http-observer-stop",
        )


class LocalTransport:
    """Use production private client bootstrap; substitute only Podman exec."""

    def __init__(self, binary, env, project, connection):
        self.env, self.project, self.connection = env, project, connection
        self.data = {
            "id": "a" * 32,
            "home": env["HOME"],
            "project": str(project),
            "container": "synthetic-ui-no-podman",
            "opencode_version": connection["version"],
        }
        self.box = SimpleNamespace(
            BoxError=box.BoxError,
            ContainerCommand=box.ContainerCommand,
            CONTAINER_OPENCODE=binary,
            PACKAGE=REPO / "src/oc_box",
            container_project=lambda _data: str(project),
            maintenance_host_env=lambda: dict(env),
            run=self.run,
        )
        self.box.workspace_service = SimpleNamespace(
            client_command=self.command,
            validate_endpoint=service.validate_endpoint,
            api=lambda *args, **kwargs: service.api(self.box, *args[1:], **kwargs),
        )

    def command(self, _box, data, connection, arguments, **options):
        command = service.client_command(
            self.box, data, connection, arguments, **options
        )
        boundary = command.index(self.data["container"])
        require(command[:3] == ["podman", "exec", "-i"], "podman-boundary")
        return box.ContainerCommand(command[boundary + 1 :], command.environment)

    def run(self, command, **options):
        # service.api uses its module's production client_command directly;
        # substitute that same outer Podman boundary here as well.
        if command and command[0] == "podman":
            require(command[:3] == ["podman", "exec", "-i"], "podman-boundary")
            boundary = command.index(self.data["container"])
            command = box.ContainerCommand(command[boundary + 1 :], command.environment)
        if options.get("input") is None:
            options["stdin"] = subprocess.DEVNULL
        result = box.run(command, cwd=self.project, env=command.environment, **options)
        if options.get("input") is not None:
            try:
                value = json.loads(result.stdout)
                shape = {
                    "status": value.get("status"),
                    "reason": value.get("reason"),
                    "source": value.get("source"),
                    "client_valid": ui_state._nonce(value.get("client")),
                    "tabs_type": type(value.get("tabs")).__name__,
                    "keys": sorted(value),
                }
            except Exception:
                shape = None
            self.last_helper = {
                "code": result.returncode,
                "shape": shape,
                "error_classes": re.findall(
                    r"(?m)^([A-Za-z]+Error):", result.stderr or ""
                ),
                "file_undefined": "__file__" in (result.stderr or ""),
            }
        return result

    def snapshot(self):
        self.last_snapshot = ui_state.read_snapshot(
            self.box, self.data, self.connection
        )
        return self.last_snapshot


class Client(PreferencesClient):
    def __init__(self, transport, selected, observer, *, owner):
        binary = transport.box.CONTAINER_OPENCODE
        connection = {**transport.connection, "url": observer.url}
        command = transport.command(
            transport.box,
            transport.data,
            connection,
            [
                binary,
                str(transport.project),
                "--server",
                observer.url,
                "--session",
                selected,
            ],
            interactive=True,
            ui_owner=owner,
        )
        names = json.loads(command[5])
        self.client_id = names["OC_BOX_UI_STATE_CLIENT"]
        self.transport = transport
        self.metadata_bound = (
            ui_state._nonce(names.get("OC_BOX_UI_STATE_CLIENT"))
            and names.get("OC_BOX_UI_STATE_GENERATION")
            == connection["ui_state"]["generation"]
        )
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        wrapper = (
            "import fcntl,termios,os,sys;"
            "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
            "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
        )
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-c", wrapper, *command],
                cwd=transport.project,
                env=command.environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.state = "text"
        self.watch("Ask anything", "Connect a provider", "UIProbe")

    def control(self, native, action, session=None):
        body = {"client": self.client_id, "action": action}
        if session is not None:
            body["sessionID"] = session
        native(
            "POST",
            "/api/rpc/synthetic.ui-control/send?location%5Bdirectory%5D="
            + quote(str(self.transport.project), safe=""),
            {"input": body},
        )

    def close(self):
        try:
            if self.process.poll() is None:
                self.control(self.native, "exit")
                self.wait(
                    lambda: self.process.poll() is not None,
                    "native public app.exit",
                    timeout=8,
                )
            require(self.process.wait(timeout=5) == 0, "native-client-clean-exit")
        finally:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            os.close(self.master)
            self.tail = ""


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    home = Path(env["HOME"])
    for kind, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + kind + "_HOME"] = str(home / relative)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(native_tabs.supports_web_tabs(release), "native-version")
    print("NATIVE version=" + release, flush=True)
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    package = config / "plugins/oc-box-ui-state"
    package.mkdir(parents=True)
    for name, content in ui_state.package_sources(REPO / "src/oc_box").items():
        (package / name).write_bytes(content)
    driver = config / "plugins/synthetic-ui-control"
    driver.mkdir()
    for name, text in {
        "index.js": CONTROL_SERVER,
        "tui.js": CONTROL_TUI,
        "rpc.js": CONTROL_RPC,
        "package.json": '{"name":"synthetic-ui-control","type":"module","exports":{".":"./index.js","./tui":"./tui.js","./rpc":"./rpc.js"}}',
    }.items():
        (driver / name).write_text(text)
    (config / "cli.json").write_text(
        json.dumps(
            {
                "tabs": {"mode": "on", "scope": "cwd", "layout": "horizontal"},
                "animations": False,
                "highlights": False,
                "theme": {"name": "opencode", "mode": "dark"},
                "attention": {"notifications": False, "sound": False},
                "experimental": {"client-mcp": False},
                "prompt": {"editor": False},
            }
        )
    )
    config_value = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    config_value.update(
        {
            "plugins": [str(package), str(driver)],
            "policies": [],
            "model": "fixture/probe",
            "providers": {
                "fixture": {
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {
                        "baseURL": "http://127.0.0.1:9/v1",
                        "apiKey": "synthetic-only",
                    },
                    "models": {
                        "probe": {
                            "name": "Synthetic UI metadata",
                            "limit": {"context": 32000, "output": 1000},
                            "capabilities": {
                                "tools": False,
                                "input": ["text"],
                                "output": ["text"],
                            },
                        }
                    },
                }
            },
        }
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config_value)
    previous = Path.cwd()
    os.chdir(project)
    exit_info = {}
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            bridge.server(binary, exit_info=exit_info) as native,
        ):
            connection = {
                **native.endpoint,
                "version": release,
                "workspace": "a" * 32,
                "ui_state": {
                    "available": True,
                    "generation": secrets.token_hex(16),
                    "ownerClient": secrets.token_hex(16),
                },
            }
            transport = LocalTransport(binary, env, project, connection)
            ready = ui_state.available(transport.box, transport.data, connection)
            if not ready:
                plugins = native("GET", "/api/plugin")["data"]
                summaries = []
                for plugin in plugins:
                    if (
                        "ui-state" not in str(plugin.get("source", {}))
                        and "ui-state" not in str(plugin.get("id", ""))
                        and plugin.get("state", {}).get("status") != "failed"
                    ):
                        continue
                    error = str(plugin.get("state", {}).get("error", ""))
                    summaries.append(
                        {
                            "id": plugin.get("id"),
                            "status": plugin.get("state", {}).get("status"),
                            "error_size": len(error),
                            "error_terms": [
                                term
                                for term in (
                                    "crypto",
                                    "Rpc",
                                    "schema",
                                    "export",
                                    "module",
                                    "input",
                                    "register",
                                    "Promise",
                                    "TypeError",
                                    "ReferenceError",
                                )
                                if term in error
                            ],
                        }
                    )
                print("PLUGIN_DIAGNOSTIC " + json.dumps(summaries), flush=True)
                try:
                    ping = native("POST", ui_state._target("ping", str(project)), {})
                    print(
                        "PING_DIAGNOSTIC "
                        + json.dumps({"http_ok": ping == {"output": {"protocol": 1}}}),
                        flush=True,
                    )
                except Exception as error:
                    print(
                        "PING_DIAGNOSTIC "
                        + json.dumps({"exception_type": type(error).__name__}),
                        flush=True,
                    )
            require(ready, "public-rpc-ready")
            empty = transport.snapshot()
            print(
                "READER_DIAGNOSTIC "
                + json.dumps(
                    {
                        "status": empty.status,
                        "reason": empty.reason,
                    }
                ),
                flush=True,
            )
            if empty.status == "invalid":
                try:
                    native(
                        "POST",
                        ui_state._target("snapshot", str(project)),
                        {
                            "input": {
                                key: connection["ui_state"][key]
                                for key in ("generation", "ownerClient")
                            },
                        },
                    )
                except Exception as error:
                    try:
                        detail = json.loads(error.read())
                    except Exception:
                        detail = {}
                    message = str(detail.get("message", ""))
                    print(
                        "RPC_INPUT_DIAGNOSTIC "
                        + json.dumps(
                            {
                                "kind": detail.get("type"),
                                "terms": [
                                    term
                                    for term in (
                                        "generation",
                                        "ownerClient",
                                        "input",
                                        "undefined",
                                        "string",
                                        "object",
                                        "required",
                                        "32",
                                        "additional",
                                        "Missing",
                                        "Expected",
                                    )
                                    if term in message
                                ],
                            }
                        ),
                        flush=True,
                    )
            require(
                empty == ui_state.Snapshot("missing", reason="absent"),
                "empty-reader-ready",
            )

            def create(title, parent=None):
                payload = {
                    "location": {"directory": str(project)},
                    "title": title,
                    "agent": "build",
                    "model": {"providerID": "fixture", "id": "probe"},
                }
                if parent:
                    payload["parentID"] = parent
                return native("POST", "/api/session", payload)["data"]["id"]

            first, second = create("UIProbeA"), create("UIProbeB")
            child = create("UIProbeChild", first)
            read = "ses_ui_probe_read"
            native(
                "POST",
                "/api/experimental/session/import",
                {
                    "info": {
                        "id": read,
                        "projectID": "global",
                        "title": "UIProbeRead",
                        "agent": "build",
                        "model": {"providerID": "fixture", "id": "probe"},
                        "cost": 0,
                        "tokens": {
                            "input": 0,
                            "output": 0,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                        "time": {"created": 1, "updated": 2, "idle": 2, "viewed": 2},
                        "location": {"directory": str(project)},
                    },
                    "messages": [
                        {
                            "id": "msg_ui_probe_read",
                            "type": "user",
                            "text": "SYNTHETIC_RETAINED_HISTORY",
                            "time": {"created": 1},
                        }
                    ],
                    "location": {"directory": str(project)},
                },
            )
            info = native("GET", "/api/session/" + read)["data"]
            require(
                info["time"].get("idle") == info["time"].get("viewed") == 2,
                "already-read-source",
            )
            identifiers = (first, second, child, read)
            before = {
                ident: native("GET", "/api/experimental/session/" + ident + "/export")[
                    "data"
                ]
                for ident in identifiers
            }
            clients = []
            with Observer(native.endpoint) as observer:
                try:
                    owner = Client(transport, child, observer, owner=True)
                    owner.native = native
                    clients.append(owner)
                    owner.ready()
                    owner.wait(
                        lambda: transport.snapshot().selected == child,
                        "public SDK exact routed child",
                    )
                    snapshot = transport.snapshot()
                    require(
                        snapshot.activeRoot == first and snapshot.tabs == (first,),
                        "root-child-report",
                    )
                    owner.control(native, "select", second)
                    owner.wait(
                        lambda: transport.snapshot().selected == second,
                        "empty unread-free route",
                    )
                    owner.control(native, "select", read)
                    owner.wait(
                        lambda: transport.snapshot().selected == read,
                        "already-read route",
                    )
                    require(
                        transport.snapshot().tabs == (first, second, read),
                        "ordered-real-tabs",
                    )
                    owner.control(native, "blur")
                    owner.drain(0.3)
                    raw = native(
                        "POST",
                        ui_state._target("snapshot", str(project)),
                        {
                            "input": {
                                key: connection["ui_state"][key]
                                for key in ("generation", "ownerClient")
                            },
                        },
                    )["output"]
                    require(raw.get("focus") == "blurred", "native-renderer-blur")
                    require(
                        transport.snapshot().source == "owner-tui",
                        "owner-blurred-selection",
                    )
                    alternate = Client(transport, second, observer, owner=False)
                    alternate.native = native
                    clients.append(alternate)
                    alternate.ready()
                    another = Client(transport, first, observer, owner=False)
                    another.native = native
                    clients.append(another)
                    another.ready()
                    require(
                        transport.snapshot().selected == read,
                        "owner-preferred-over-alternates",
                    )
                    owner.close()
                    clients.remove(owner)
                    alternate.wait(
                        lambda: transport.snapshot().reason == "ambiguous",
                        "multiple-client-refusal",
                    )
                    another.close()
                    clients.remove(another)
                    alternate.wait(
                        lambda: transport.snapshot().source == "single-tui",
                        "single-client-fallback",
                    )
                    require(
                        transport.snapshot().selected == second,
                        "single-client-exact-route",
                    )
                    old_epoch = native(
                        "POST",
                        ui_state._target("snapshot", str(project)),
                        {
                            "input": {
                                key: connection["ui_state"][key]
                                for key in ("generation", "ownerClient")
                            },
                        },
                    )["output"]["epoch"]
                    native("POST", "/api/location/reload")
                    alternate.wait(
                        lambda: transport.snapshot().selected == second,
                        "reload repopulates live route",
                    )
                    new_epoch = native(
                        "POST",
                        ui_state._target("snapshot", str(project)),
                        {
                            "input": {
                                key: connection["ui_state"][key]
                                for key in ("generation", "ownerClient")
                            },
                        },
                    )["output"]["epoch"]
                    require(
                        new_epoch != old_epoch, "reload-generation-invalidates-memory"
                    )
                    os.kill(alternate.process.pid, signal.SIGSTOP)
                    try:
                        deadline = time.monotonic() + 10
                        while transport.snapshot().status != "missing":
                            require(
                                time.monotonic() < deadline, "server-receipt-expiry"
                            )
                            time.sleep(0.1)
                    finally:
                        os.kill(alternate.process.pid, signal.SIGCONT)
                    alternate.wait(
                        lambda: transport.snapshot().selected == second,
                        "heartbeat-restores-presence",
                    )
                    alternate.close()
                    clients.remove(alternate)
                    deadline = time.monotonic() + 10
                    while transport.snapshot().status != "missing":
                        require(time.monotonic() < deadline, "final-release")
                        time.sleep(0.1)
                    require(observer.releases >= 3, "public-plugin-release")
                    require(
                        observer.bad_posts == 0 and not observer.errors,
                        "no-native-session-mutations",
                    )
                except BaseException:
                    observed = getattr(transport, "last_snapshot", None)
                    print(
                        "TUI_DIAGNOSTIC "
                        + json.dumps(
                            {
                                "snapshot_status": observed.status
                                if observed
                                else None,
                                "snapshot_reason": observed.reason
                                if observed
                                else None,
                                "snapshot_source": observed.source
                                if observed
                                else None,
                                "tabs_count": len(observed.tabs) if observed else None,
                                "bad_posts": observer.bad_posts,
                                "http_errors": observer.errors,
                                "helper": getattr(transport, "last_helper", None),
                                "reports": observer.reports,
                                "report_statuses": observer.report_statuses,
                                "report_shapes": observer.report_shapes,
                                "metadata_bound": owner.metadata_bound,
                            }
                        ),
                        flush=True,
                    )
                    raise
                finally:
                    for client in reversed(clients):
                        client.close()
            after = {
                ident: native("GET", "/api/experimental/session/" + ident + "/export")[
                    "data"
                ]
                for ident in identifiers
            }
            require(before == after, "transcripts-and-native-metadata-unchanged")
            require(
                native("GET", "/api/session/active")["data"] == {},
                "no-execution-activity",
            )
            require(
                native("GET", "/api/credential")["data"] == [], "no-real-credentials"
            )
    finally:
        os.chdir(previous)
    require(exit_info == {"code": 0, "forced": False}, "stdio-server-clean-exit")
    print(
        "PASS: real public-SDK TUI exact child/root and all ordered tabs; no-idle/"
        "already-read switches; owner blur/preference; ambiguity/single fallback; "
        "reload/expiry/heartbeat/release; zero prompt/move POSTs; unchanged history.",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    args = parser.parse_args()

    def expired(*_):
        raise Failure("fixture-timeout")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(180)
    try:
        binary = args.opencode.resolve()
        source, native_hash = identity(), digest(binary)
        print("SOURCE " + json.dumps(source, sort_keys=True), flush=True)
        print("NATIVE sha256=" + native_hash, flush=True)
        root = Path(tempfile.mkdtemp(prefix="oc-box-ui-state-native-")).resolve()
        root.chmod(0o700)
        print("PRIVATE fixture_root=" + str(root), flush=True)
        exercise(str(binary), root)
        require(
            identity() == source and digest(binary) == native_hash,
            "tested-source-changed",
        )
        print(
            "Native synthetic integration only; NOT host/Podman/GUI or security acceptance.",
            flush=True,
        )
        return 0
    except Failure as error:
        print("FAIL: native UI state (" + str(error) + ").", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt) as error:
        print(
            "DIAGNOSTIC "
            + json.dumps(
                {
                    "type": type(error).__name__,
                    "frames": [
                        {
                            "file": Path(frame.filename).name,
                            "line": frame.lineno,
                            "function": frame.name,
                        }
                        for frame in traceback.extract_tb(error.__traceback__)[-6:]
                    ],
                }
            ),
            flush=True,
        )
        print("FAIL: native UI state (unexpected).", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    sys.exit(main())
