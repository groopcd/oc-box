"""Live V2 workspace leases, authenticated same-user attach and loopback relay.

The host controller holds only an ephemeral server password, never vault/provider
values. A private Unix socket lets same-user clients join without taking the
lifecycle lock. Browser bytes traverse ordinary Podman exec pipes; no container
port, host shared service, project bind, or external network listener is added.
"""

import argparse
import base64
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

CONNECTION = contextvars.ContextVar("oc_box_v2_connection")
MAX_CONTROL = 16384
PROXY = """import os,select,socket,sys
s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=10); s.settimeout(None)
readers=[s,sys.stdin.buffer]
while readers:
    ready,_,_=select.select(readers,[],[],1)
    for item in ready:
        if item is s:
            data=s.recv(65536)
            if not data: sys.exit(0)
            sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
        else:
            data=os.read(0,65536)
            if not data:
                readers.remove(sys.stdin.buffer); s.shutdown(socket.SHUT_WR)
            else: s.sendall(data)
"""


def socket_path(box, data):
    root = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / (
        "oc-box-live-" + str(os.getuid())
    )
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise box.BoxError(
            "Private oc-box live-client runtime directory has unsafe ownership or mode."
        )
    digest = hashlib.sha256(
        (str(box.state_root()) + ":" + data["id"]).encode()
    ).hexdigest()[:24]
    address = root / (digest + ".sock")
    if len(os.fsencode(address)) >= 108:
        raise box.BoxError(
            "The private runtime directory path is too long for a Unix socket; set a shorter XDG_RUNTIME_DIR."
        )
    return address


def live_connection():
    return CONNECTION.get(None)


def controller_running(box, data):
    address = socket_path(box, data)
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(0.5)
        try:
            stream.connect(str(address))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
    return True


def validate_endpoint(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid private workspace service response.")
    parsed = urlparse(value.get("url", ""))
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path not in ("", "/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid private workspace service endpoint.")
    if (
        not isinstance(value.get("password"), str)
        or not 32 <= len(value["password"]) <= 256
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value["password"])
    ):
        raise ValueError("Invalid private workspace service credential.")
    return value


def read_control(stream):
    content = bytearray()
    while b"\n" not in content:
        chunk = stream.recv(min(4096, MAX_CONTROL - len(content)))
        if not chunk or len(content) + len(chunk) >= MAX_CONTROL:
            raise ValueError("Incomplete or oversized workspace control response.")
        content.extend(chunk)
    value = json.loads(content.split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("Invalid workspace control response.")
    return value


def validate_workspace(box, path, data):
    project = box.validate_project(data["project"])
    selected, _ = box.find_workspace(project)
    if (
        selected != path
        or not box.workspace_identity.same_project(data, project)
        or data.get("restore_incomplete")
    ):
        raise box.BoxError("Workspace identity changed; refusing live session access.")
    info = box.inspect_owned(data, env=box.maintenance_host_env())
    if not info or not info["State"].get("Running") or info["State"].get("Paused"):
        raise box.BoxError(
            "Workspace is not running; start it before attaching a client."
        )
    if not any(
        mount.get("Source") == data["project"]
        and mount.get("Destination") == box.container_project(data)
        for mount in info.get("Mounts", [])
    ):
        raise box.BoxError(
            "Container workspace mount does not match the host registry."
        )


def client_command(box, data, connection, arguments, *, interactive=False):
    """No host pass_env, auth refresh, profiles, or vault unlock on attach."""
    env = box.maintenance_host_env()
    env["OPENCODE_PASSWORD"] = connection["password"]
    command = ["podman", "exec", "-i"]
    if interactive and sys.stdin.isatty() and sys.stdout.isatty():
        command.append("-t")
    command.extend(
        [
            "--workdir",
            box.container_project(data),
            "--env",
            "OPENCODE_PASSWORD",
            data["container"],
        ]
    )
    names = {
        "HOME": data["home"],
        "PWD": box.container_project(data),
        "PATH": "/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "SHELL": "/bin/bash",
        "OC_BOX_CONTAINER": "1",
        "XDG_CONFIG_HOME": data["home"] + "/.config",
        "XDG_STATE_HOME": data["home"] + "/.local/state",
        "XDG_DATA_HOME": data["home"] + "/.local/share",
        "XDG_CACHE_HOME": data["home"] + "/.cache",
        "OPENCODE_DB": "oc-box-v2.db",
    }
    term = os.environ.get("TERM", "xterm-256color")
    names["TERM"] = (
        term
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}", term)
        else "xterm-256color"
    )
    bridge = "import os,sys,json; env=json.loads(sys.argv[1]); env['OPENCODE_PASSWORD']=os.environ['OPENCODE_PASSWORD']; os.execvpe(sys.argv[2],sys.argv[2:],env)"
    command.extend(["python3", "-c", bridge, json.dumps(names), *arguments])
    return box.ContainerCommand(command, env)


def api(box, data, connection, method, target, value=None):
    args = [
        box.CONTAINER_OPENCODE,
        "api",
        method.lower(),
        target,
        "--server",
        connection["url"],
    ]
    if value is not None:
        args.extend(["--data", json.dumps(value)])
    result = box.run(
        client_command(box, data, connection, args),
        capture=True,
        private=True,
        timeout=60,
    )
    # Native api prints nothing for successful HTTP 204 replies (permission
    # rejection, session deletion, reload, etc.). Match the private HTTP bridge's
    # None convention. run still checks command failure; nonempty malformed JSON
    # must remain an error rather than being mistaken for a no-content success.
    return json.loads(result.stdout) if result.stdout.strip() else None


class Owner:
    def __init__(self, box, path, data):
        self.box, self.path, self.data = box, path, data
        self.address = socket_path(box, data)
        self.clients = 0
        self.condition = threading.Condition()
        self.ready = False
        self.closing = False
        self.session = None
        self.child = None
        self.listener = None
        self.bound = False

    def __enter__(self):
        box = self.box
        # The caller owns the lifecycle lock. A refused connection is stale;
        # a live controller must never be replaced by a new server.
        if self.address.exists():
            info = self.address.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise box.BoxError(
                    "Unexpected file at the private workspace controller path; inspect it before launching."
                )
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(self.address))
                except (ConnectionRefusedError, FileNotFoundError):
                    self.address.unlink(missing_ok=True)
                else:
                    raise box.BoxError(
                        "A private workspace service already owns this workspace."
                    )
        script = (box.PACKAGE / "opencode_v2_io.py").read_text()
        command = box.exec_command(
            self.data, ["python3", "-c", script, "lease", box.CONTAINER_OPENCODE]
        )
        self.errors = box.captured_output(private=True)
        self.error_file = self.errors.__enter__()
        try:
            self.child = subprocess.Popen(
                command,
                env=command.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.error_file,
                start_new_session=True,
            )
            assert self.child.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + 180
            while b"\n" not in output:
                if self.child.poll() is not None or time.monotonic() > deadline:
                    raise box.BoxError(
                        "Private workspace service failed to start; private output was not logged."
                    )
                if select.select([self.child.stdout], [], [], 0.1)[0]:
                    output.extend(os.read(self.child.stdout.fileno(), 4096))
                if len(output) > MAX_CONTROL:
                    raise box.BoxError(
                        "Invalid private workspace service readiness response."
                    )
            self.connection = validate_endpoint(json.loads(output.split(b"\n", 1)[0]))
            self.connection["workspace"] = self.data["id"]
            self.token = CONNECTION.set(self.connection)
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(str(self.address))
            self.bound = True
            self.address.chmod(0o600)
            self.listener.listen(16)
            self.listener.settimeout(0.2)
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.shutdown()
            raise

    def publish(self, session):
        if not self.box.valid_session_id(session):
            raise self.box.BoxError("Invalid live workspace session ID.")
        with self.condition:
            self.session, self.ready = session, True

    def serve(self):
        assert self.listener is not None
        while not self.closing:
            try:
                stream, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(stream,), daemon=True).start()

    def handle(self, stream):
        assert self.child is not None
        held = False
        with stream:
            try:
                _, uid, _ = struct.unpack(
                    "3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    return
                stream.settimeout(5)
                request = read_control(stream)
                with self.condition:
                    if (
                        self.closing
                        or not self.ready
                        or request.get("workspace") != self.data["id"]
                    ):
                        stream.sendall(
                            b'{"error":"Workspace service is not ready; retry after startup."}\n'
                        )
                        return
                    if request.get("action") not in ("info", "attach"):
                        return
                    if request["action"] == "attach":
                        self.clients += 1
                        held = True
                stream.sendall(
                    (
                        json.dumps(
                            {
                                **self.connection,
                                "session": self.session,
                                "version": self.data["opencode_version"],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                if held:
                    stream.settimeout(0.5)
                    while not self.closing and self.child.poll() is None:
                        try:
                            if not stream.recv(1):
                                break
                        except socket.timeout:
                            pass
            except (OSError, ValueError):
                pass
            finally:
                if held:
                    with self.condition:
                        self.clients -= 1
                        self.condition.notify_all()

    def wait_clients(self):
        assert self.child is not None
        with self.condition:
            if self.clients:
                print(
                    "oc-box: Attached web/terminal clients are still active; waiting before history sync and container stop. Close those clients or use 'oc-box stop'.",
                    flush=True,
                )
            while self.clients and self.child.poll() is None:
                self.condition.wait(timeout=0.2)
            self.closing = True

    def shutdown(self):
        self.closing = True
        if self.listener:
            self.listener.close()
            if hasattr(self, "thread"):
                self.thread.join(timeout=2)
            if self.bound:
                self.address.unlink(missing_ok=True)
        if self.child:
            if self.child.stdin:
                self.child.stdin.close()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=5)
            if self.child.stdout:
                self.child.stdout.close()
        if hasattr(self, "token"):
            CONNECTION.reset(self.token)
        if hasattr(self, "errors"):
            self.errors.__exit__(None, None, None)

    def __exit__(self, *_):
        self.shutdown()


@contextlib.contextmanager
def attach(box, path, data):
    validate_workspace(box, path, data)
    stream = socket.socket(socket.AF_UNIX)
    try:
        stream.settimeout(5)
        stream.connect(str(socket_path(box, data)))
        _, uid, _ = struct.unpack(
            "3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != os.getuid():
            raise box.BoxError("Private workspace controller belongs to another user.")
        stream.sendall(
            (json.dumps({"action": "attach", "workspace": data["id"]}) + "\n").encode()
        )
        connection = read_control(stream)
        if connection.get("error"):
            raise box.BoxError(connection["error"])
        validate_endpoint(connection)
        if (
            connection.get("workspace") != data["id"]
            or connection.get("version") != data.get("opencode_version")
            or not box.valid_session_id(connection.get("session"))
        ):
            raise box.BoxError(
                "Live service does not match this workspace/version/session."
            )
    except (OSError, ValueError) as exc:
        stream.close()
        raise box.BoxError(
            "No ready live OpenCode 2.x workspace service. Start oc-box or 'oc-box web' first; no credentials were refreshed."
        ) from exc
    except BaseException:
        stream.close()
        raise
    try:
        yield connection
    finally:
        stream.close()


def relay_command(box, data, port):
    return box.ContainerCommand(
        [
            "podman",
            "exec",
            "-i",
            data["container"],
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "python3",
            "-c",
            PROXY,
            str(port),
        ],
        box.maintenance_host_env(),
    )


class Gateway:
    def __init__(self, box, data, connection, port=0):
        self.box, self.data, self.connection, self.port = box, data, connection, port
        self.closed = threading.Event()
        self.children = set()
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(32)

    def __enter__(self):
        self.listener = socket.socket()
        try:
            self.listener.bind(("127.0.0.1", self.port))
            self.listener.listen(32)
            self.listener.settimeout(0.2)
        except BaseException:
            self.listener.close()
            raise
        self.url = "http://127.0.0.1:" + str(self.listener.getsockname()[1])
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def serve(self):
        while not self.closed.is_set():
            try:
                stream, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.capacity.acquire(blocking=False):
                stream.close()
                continue
            threading.Thread(target=self.forward, args=(stream,), daemon=True).start()

    def forward(self, stream):
        child = None
        with stream:
            try:
                port = urlparse(self.connection["url"]).port
                command = relay_command(self.box, self.data, port)
                child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                assert child.stdin is not None and child.stdout is not None
                with self.lock:
                    self.children.add(child)
                stream.settimeout(15)
                headers = bytearray()
                while b"\r\n\r\n" not in headers and len(headers) < 65536:
                    data = stream.recv(4096)
                    if not data:
                        return
                    headers.extend(data)
                if b"\r\n\r\n" not in headers:
                    return
                pairing = bytes(headers).startswith(b"GET /auth/connect/")
                child.stdin.write(headers)
                child.stdin.flush()
                stream.settimeout(None)
                readers = [stream, child.stdout]
                response = bytearray()
                while not self.closed.is_set():
                    ready, _, _ = select.select(readers, [], [], 0.2)
                    for item in ready:
                        if item is stream:
                            data = stream.recv(65536)
                            if not data:
                                return
                            child.stdin.write(data)
                            child.stdin.flush()
                        else:
                            data = os.read(child.stdout.fileno(), 65536)
                            if not data:
                                return
                            if pairing:
                                response.extend(data)
                                if b"\r\n\r\n" not in response:
                                    if len(response) > 65536:
                                        return
                                    continue
                                route = self.session_route()
                                data = re.sub(
                                    rb"(?im)^location: /\r$",
                                    b"location: " + route.encode() + b"\r",
                                    bytes(response),
                                    count=1,
                                )
                                pairing = False
                            stream.sendall(data)
            except (OSError, ValueError):
                pass
            finally:
                if child:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=3)
                    for pipe in (child.stdin, child.stdout):
                        if pipe:
                            pipe.close()
                    with self.lock:
                        self.children.discard(child)
                self.capacity.release()

    def session_route(self):
        encoded = base64.urlsafe_b64encode(self.url.encode()).decode().rstrip("=")
        return "/server/" + encoded + "/session/" + self.connection["session"]

    def __exit__(self, *_):
        self.closed.set()
        self.listener.close()
        self.thread.join(timeout=2)
        with self.lock:
            children = tuple(self.children)
        for child in children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


def web_session(box, data, connection, args):
    with Gateway(box, data, connection, args.port) as gateway:
        pair = api(box, data, connection, "POST", "/api/pair")
        if not isinstance(pair.get("code"), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", pair["code"]
        ):
            raise box.BoxError("Invalid native OpenCode browser pairing response.")
        link = gateway.url + "/auth/connect/" + pair["code"]
        print(
            "oc-box: OpenCode web session "
            + connection["session"]
            + "\n"
            + link
            + "\nThis one-use link grants access to this workspace service. Keep this command open; Ctrl-C closes web access.",
            flush=True,
        )
        if not args.no_open:
            # Browser activation is explicit, but must not inherit decrypted
            # vault values or provider tokens from the launching host shell.
            clean = box.maintenance_host_env()
            for key in (
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XAUTHORITY",
                "XDG_SESSION_TYPE",
                "XDG_CURRENT_DESKTOP",
            ):
                if key in os.environ:
                    clean[key] = os.environ[key]
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import sys,webbrowser; webbrowser.open(sys.stdin.read())",
                ],
                input=link,
                text=True,
                env=clean,
                check=False,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        try:
            opener = build_opener(ProxyHandler({}))
            authorization = (
                "Basic "
                + base64.b64encode(
                    ("opencode:" + connection["password"]).encode()
                ).decode()
            )
            while True:
                # A stopped container causes the authenticated health check to
                # fail; do not leave a host listener after explicit stop.
                time.sleep(2)
                request = Request(
                    gateway.url + "/api/info", headers={"Authorization": authorization}
                )
                with opener.open(request, timeout=10) as response:
                    response.read()
        except KeyboardInterrupt:
            return 0


def preflight_port(port):
    """Fail before an owning web launch prompts/unlocks or changes resources."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def web_parser():
    result = argparse.ArgumentParser(
        prog="oc-box web",
        description="Open the native web UI on the private V2 workspace service; attach to a running session or own a new foreground lease.",
    )
    group = result.add_mutually_exclusive_group()
    group.add_argument(
        "--project",
        type=Path,
        help="host project directory (default: current directory)",
    )
    group.add_argument("--workspace", help="existing workspace ID")
    result.add_argument(
        "--port",
        type=int,
        default=0,
        help="host loopback browser relay port; default selects a free port",
    )
    result.add_argument(
        "--no-open",
        action="store_true",
        help="print the pairing link without opening a browser",
    )
    result.add_argument(
        "--no-vault",
        action="store_true",
        help="owning V2 launch only: skip vault use once; refused when attaching",
    )
    return result
