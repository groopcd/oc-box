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
import math
import os
from pathlib import Path
import re
import select
import selectors
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
RELAY_BUFFER = 65536
RELAY_STOP_TIMEOUT = 3.0
# Keep the container transport self-contained: it runs without package imports.
# Each direction has one bounded queue; stdout EOF and stdin EOF are independent.
PROXY = """import os,selectors,socket,sys
limit=65536
with socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=10) as s, selectors.DefaultSelector() as selector:
    s.setblocking(False)
    os.set_blocking(0,False); os.set_blocking(1,False)
    incoming=bytearray(); outgoing=bytearray()
    stdin_open=server_open=True
    write_closed=stdout_closed=False
    while True:
        if not stdin_open and not incoming and not write_closed:
            s.shutdown(socket.SHUT_WR); write_closed=True
        if not server_open and not outgoing and not stdout_closed:
            os.close(1); stdout_closed=True
        if write_closed and stdout_closed:
            break
        for key in list(selector.get_map().values()):
            selector.unregister(key.fd)
        events=0
        if server_open and len(outgoing)<limit: events|=selectors.EVENT_READ
        if incoming: events|=selectors.EVENT_WRITE
        if events: selector.register(s,events)
        if stdin_open and len(incoming)<limit: selector.register(0,selectors.EVENT_READ)
        if outgoing: selector.register(1,selectors.EVENT_WRITE)
        for key,events in selector.select(.2):
            try:
                if key.fd==0:
                    data=os.read(0,limit-len(incoming))
                    if data: incoming.extend(data)
                    else: stdin_open=False
                elif key.fd==1:
                    count=os.write(1,outgoing)
                    if not count: raise BrokenPipeError()
                    del outgoing[:count]
                else:
                    if events & selectors.EVENT_READ:
                        data=s.recv(limit-len(outgoing))
                        if data: outgoing.extend(data)
                        else: server_open=False
                    if events & selectors.EVENT_WRITE:
                        count=s.send(incoming)
                        if not count: raise BrokenPipeError()
                        del incoming[:count]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                if key.fd!=s.fileno(): raise
                stdin_open=False; incoming.clear(); write_closed=True
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


def api(box, data, connection, method, target, value=None, *, timeout=60):
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
        timeout=timeout,
    )
    # Native api prints nothing for successful HTTP 204 replies (permission
    # rejection, session deletion, reload, etc.). Match the private HTTP bridge's
    # None convention. run still checks command failure; nonempty malformed JSON
    # must remain an error rather than being mistaken for a no-content success.
    return json.loads(result.stdout) if result.stdout.strip() else None


def wait_history(box, data, connection, *, timeout=30, interval=0.2):
    """Boundedly wait for this private server's drains and job notices to settle.

    Native active sessions cover foreground drains, not background tools. The
    job inventory also retains finished jobs until their completion notices are
    admitted, which can wake foreground execution again. Two quiet pairs avoid
    accepting a single observation across that handoff. This does not certify
    transcript completeness: user shell fibers and unsettled projected messages
    need a separate check in the export path. Persistent PTYs are not activity.
    """

    def fail(category):
        raise box.BoxError(f"OpenCode 2.x history wait failed ({category}).") from None

    if (
        not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval <= 0
    ):
        fail("invalid-wait-options")
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            fail("activity-timeout")
        return budget

    def request(target):
        budget = remaining()
        try:
            result = api(
                box,
                data,
                connection,
                "GET",
                target,
                timeout=min(60, budget),
            )
        except (box.BoxError, OSError, ValueError, subprocess.SubprocessError):
            # Never include native errors: they can contain commands, provider
            # values, or private response bodies. Deadline expiry stays distinct.
            remaining()
            fail("activity-api-failed")
        remaining()
        return result

    quiet = 0
    while True:
        response = request("/api/session/active")
        active = response.get("data") if isinstance(response, dict) else None
        if not isinstance(active, dict) or any(
            not box.valid_session_id(ident)
            or not isinstance(state, dict)
            or state.get("type") != "running"
            for ident, state in active.items()
        ):
            fail("invalid-active-response")

        response = request("/api/experimental/job")
        jobs = response.get("data") if isinstance(response, dict) else None
        if not isinstance(jobs, list) or any(
            not isinstance(job, dict)
            or not isinstance(job.get("id"), str)
            or not job["id"]
            or not box.valid_session_id(job.get("sessionID"))
            or job.get("kind") not in ("shell", "subagent", "image")
            or job.get("status") not in ("running", "completed", "error", "cancelled")
            for job in jobs
        ):
            fail("invalid-jobs-response")
        quiet = quiet + 1 if not active and not jobs else 0
        if quiet == 2:
            return
        time.sleep(min(interval, remaining()))


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
        self.bound_identity = None
        self.thread = None
        self.handlers = set()
        self.streams = set()
        self.errors = None
        self.errors_entered = False
        self.error_file = None
        self.token = None

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
        try:
            self.errors = box.captured_output(private=True)
            self.error_file = self.errors.__enter__()
            self.errors_entered = True
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
            info = self.address.lstat()
            self.bound_identity = info.st_dev, info.st_ino
            self.address.chmod(0o600)
            self.listener.listen(16)
            self.listener.settimeout(0.2)
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
            return self
        except BaseException as primary:
            self.shutdown(primary=primary)
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
            with self.condition:
                if self.closing:
                    stream.close()
                    return
                self.handlers = {
                    thread for thread in self.handlers if thread.is_alive()
                }
                thread = threading.Thread(
                    target=self.handle, args=(stream,), daemon=True
                )
                self.handlers.add(thread)
                self.streams.add(stream)
                try:
                    thread.start()
                except RuntimeError:
                    self.handlers.discard(thread)
                    self.streams.discard(stream)
                    stream.close()

    def handle(self, stream):
        child = self.child
        if child is None:
            stream.close()
            with self.condition:
                self.streams.discard(stream)
            return
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
                    while not self.closing and child.poll() is None:
                        try:
                            if not stream.recv(1):
                                break
                        except socket.timeout:
                            pass
            except (OSError, ValueError):
                pass
            finally:
                with self.condition:
                    self.streams.discard(stream)
                    if held:
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

    def shutdown(self, *, primary=None):
        """Release independent acquisitions even when another cleanup fails.

        Keep failed acquisitions retryable, but never reset a used token, re-exit
        an output context, join an unstarted thread, or signal a reaped child on
        a subsequent call. Diagnostics contain fixed resource categories only.
        """
        failures = []

        def attempt(category, action):
            try:
                action()
                return True
            except BaseException:
                failures.append(category)
                return False

        with self.condition:
            self.closing = True
            self.ready = False
        if self.listener is not None:
            attempt("listener-close", self.listener.close)
        deadline = time.monotonic() + 2
        if self.thread is not None and self.thread.ident is not None:
            attempt("listener-join", lambda: self.thread.join(timeout=2))
            if self.thread.is_alive():
                failures.append("listener-active")
        with self.condition:
            streams, handlers = tuple(self.streams), tuple(self.handlers)
        for stream in streams:
            # shutdown interrupts read_control; close is a separate release even
            # if shutdown fails (for example after peer EOF).
            def interrupt(stream=stream):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            attempt("client-shutdown", interrupt)
            attempt("client-close", stream.close)
        for thread in handlers:
            attempt(
                "client-join",
                lambda thread=thread: thread.join(
                    timeout=max(0, deadline - time.monotonic())
                ),
            )
            if thread.is_alive():
                failures.append("client-active")

        def remove_socket():
            try:
                info = self.address.lstat()
            except FileNotFoundError:
                self.bound = False
                return
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != self.bound_identity
            ):
                raise self.box.BoxError("Private workspace controller path changed.")
            self.address.unlink()
            self.bound = False

        if self.bound:
            attempt("socket-remove", remove_socket)
        child = self.child
        if child is not None:
            input_closed = True
            if child.stdin is not None:
                input_closed = attempt("child-input", child.stdin.close)
            try:
                child.wait(timeout=10 if input_closed else 0)
            except subprocess.TimeoutExpired:
                pass
            except BaseException:
                failures.append("child-wait")

            def kill_group():
                try:
                    # The group may outlive its leader. This is only the private
                    # child session created by this Owner, never a shared service.
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            attempt("child-stop", kill_group)
            reaped = attempt("child-reap", lambda: child.wait(timeout=5))
            if child.stdout is not None:
                attempt("child-output", child.stdout.close)
            if reaped:
                self.child = None
        if self.token is not None:
            if attempt("connection-reset", lambda: CONNECTION.reset(self.token)):
                self.token = None
                self.connection = None
        if self.errors_entered:
            self.errors_entered = False
            attempt("output-context", lambda: self.errors.__exit__(None, None, None))
        if self.error_file is not None:
            # Also close directly if an output context failed before releasing it.
            attempt("output-close", self.error_file.close)
        self.errors = None
        if failures:
            message = (
                "Private workspace service cleanup failed ("
                + ", ".join(sorted(set(failures)))
                + "); private output was not logged."
            )
            if primary is not None:
                try:
                    print("oc-box: " + message, file=sys.stderr)
                except OSError:
                    pass
            else:
                raise self.box.BoxError(message) from None

    def __exit__(self, _kind, primary, _traceback):
        self.shutdown(primary=primary)


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
        self.streams = set()
        self.workers = set()
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(32)
        self.stop_deadline = None
        self.cleanup_failed = False

    def __enter__(self):
        self.listener = socket.socket()
        try:
            self.listener.bind(("127.0.0.1", self.port))
            self.listener.listen(32)
            self.listener.settimeout(0.2)
            self.url = "http://127.0.0.1:" + str(self.listener.getsockname()[1])
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise
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
            with self.lock:
                if self.closed.is_set():
                    stream.close()
                    self.capacity.release()
                    return
                self.workers = {worker for worker in self.workers if worker.is_alive()}
                worker = threading.Thread(
                    target=self.forward, args=(stream,), daemon=True
                )
                self.streams.add(stream)
                self.workers.add(worker)
                try:
                    worker.start()
                except RuntimeError:
                    self.workers.discard(worker)
                    self.streams.discard(stream)
                    stream.close()
                    self.capacity.release()

    def forward(self, stream):
        child = None
        with stream:
            try:
                # Read only bounded request headers before creating a relay.
                # Gateway cancellation shuts down this socket to interrupt recv.
                stream.settimeout(15)
                headers = bytearray()
                while b"\r\n\r\n" not in headers:
                    if len(headers) == RELAY_BUFFER:
                        return
                    data = stream.recv(min(4096, RELAY_BUFFER - len(headers)))
                    if not data:
                        return
                    headers.extend(data)
                if self.closed.is_set():
                    return
                port = urlparse(self.connection["url"]).port
                command = relay_command(self.box, self.data, port)
                child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    bufsize=0,
                )
                assert child.stdin is not None and child.stdout is not None
                with self.lock:
                    self.children.add(child)
                self.pump(stream, child, headers)
            except (OSError, ValueError):
                pass
            finally:
                if child:
                    self.finish_child(child)
                with self.lock:
                    self.children.discard(child)
                    self.streams.discard(stream)
                    self.capacity.release()

    def pump(self, stream, child, headers):
        """Two 64-KiB queues, independent EOF, and no blocking directional write.

        Pairing alone stages at most 64 KiB of response headers/body before the
        delimiter; the rewritten header adds only the validated session route.
        Staged bytes drain before more backend reads. Bodies are never rewritten.
        """
        assert child.stdin is not None and child.stdout is not None
        stream.setblocking(False)
        os.set_blocking(child.stdin.fileno(), False)
        os.set_blocking(child.stdout.fileno(), False)
        incoming, outgoing = headers, bytearray()
        response = bytearray()
        pairing = headers.startswith(b"GET /auth/connect/")
        input_open = output_open = True
        write_closed = response_closed = False
        with selectors.DefaultSelector() as selector:
            while not self.closed.is_set():
                if not pairing and response:
                    count = min(RELAY_BUFFER - len(outgoing), len(response))
                    outgoing.extend(response[:count])
                    del response[:count]
                if not input_open and not incoming and not write_closed:
                    child.stdin.close()
                    write_closed = True
                if (
                    not output_open
                    and not outgoing
                    and not response
                    and not response_closed
                ):
                    stream.shutdown(socket.SHUT_WR)
                    response_closed = True
                if write_closed and response_closed:
                    # stdout EOF can precede the proxy draining its stdin pipe
                    # into a half-closed backend. Do not terminate that proxy
                    # while the final upload is still in flight. Cancellation
                    # remains active and owns the same bounded cleanup path.
                    if child.poll() is not None:
                        return
                    self.closed.wait(0.05)
                    continue
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fd)
                events = 0
                if input_open and len(incoming) < RELAY_BUFFER:
                    events |= selectors.EVENT_READ
                if outgoing:
                    events |= selectors.EVENT_WRITE
                if events:
                    selector.register(stream, events)
                if incoming:
                    selector.register(child.stdin, selectors.EVENT_WRITE)
                if output_open and (
                    pairing or (not response and len(outgoing) < RELAY_BUFFER)
                ):
                    selector.register(child.stdout, selectors.EVENT_READ)
                for key, events in selector.select(0.2):
                    try:
                        if key.fileobj is stream:
                            if events & selectors.EVENT_READ:
                                data = stream.recv(RELAY_BUFFER - len(incoming))
                                if data:
                                    incoming.extend(data)
                                else:
                                    input_open = False
                            if events & selectors.EVENT_WRITE:
                                count = stream.send(outgoing)
                                if not count:
                                    return
                                del outgoing[:count]
                        elif key.fileobj is child.stdin:
                            count = os.write(child.stdin.fileno(), incoming)
                            if not count:
                                return
                            del incoming[:count]
                        else:
                            room = RELAY_BUFFER - len(response if pairing else outgoing)
                            data = os.read(child.stdout.fileno(), room)
                            if not data:
                                if pairing:
                                    raise ValueError(
                                        "Incomplete browser pairing headers."
                                    )
                                output_open = False
                            elif pairing:
                                response.extend(data)
                                boundary = response.find(b"\r\n\r\n")
                                if boundary < 0:
                                    if len(response) == RELAY_BUFFER:
                                        raise ValueError(
                                            "Oversized browser pairing headers."
                                        )
                                    continue
                                header = re.sub(
                                    rb"(?im)^location: /\r$",
                                    b"location: "
                                    + self.session_route().encode()
                                    + b"\r",
                                    bytes(response[: boundary + 2]),
                                    count=1,
                                )
                                response[: boundary + 2] = header
                                pairing = False
                            else:
                                outgoing.extend(data)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        if key.fileobj is not child.stdin:
                            raise
                        input_open = False
                        incoming.clear()
                        child.stdin.close()
                        write_closed = True

    def signal_child(self, child, sig):
        # Every relay was started in its own session. Signal its owned group even
        # if the leader exited, so an inherited pipe cannot keep a helper alive.
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            self.cleanup_failed = True

    def finish_child(self, child):
        deadline = self.stop_deadline or time.monotonic() + RELAY_STOP_TIMEOUT
        self.signal_child(child, signal.SIGTERM)
        try:
            child.wait(timeout=min(1, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            self.cleanup_failed = True
        self.signal_child(child, signal.SIGKILL)
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            self.cleanup_failed = True
        for pipe in (child.stdin, child.stdout):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    self.cleanup_failed = True

    def session_route(self):
        encoded = base64.urlsafe_b64encode(self.url.encode()).decode().rstrip("=")
        return "/server/" + encoded + "/session/" + self.connection["session"]

    def __exit__(self, _kind, primary, _traceback):
        with self.lock:
            if self.stop_deadline is None:
                self.stop_deadline = time.monotonic() + RELAY_STOP_TIMEOUT
            self.closed.set()
        self.listener.close()
        self.thread.join(timeout=max(0, self.stop_deadline - time.monotonic()))
        # Workers are registered before start/Popen. A late Popen result therefore
        # cannot escape the common deadline or be silently added after shutdown.
        while True:
            with self.lock:
                streams = tuple(self.streams)
                children = tuple(self.children)
                self.workers = {worker for worker in self.workers if worker.is_alive()}
                workers = tuple(self.workers)
            for stream in streams:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            for child in children:
                self.signal_child(child, signal.SIGTERM)
            if not workers:
                break
            remaining = self.stop_deadline - time.monotonic()
            if remaining <= 0:
                for child in children:
                    self.signal_child(child, signal.SIGKILL)
                self.cleanup_failed = True
                break
            for worker in workers:
                worker.join(
                    timeout=min(0.05, max(0, self.stop_deadline - time.monotonic()))
                )
        if self.cleanup_failed:
            message = (
                "Private browser relay cleanup failed; private output was not logged."
            )
            if primary is not None:
                print("oc-box: " + message, file=sys.stderr)
            else:
                raise self.box.BoxError(message) from None


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
