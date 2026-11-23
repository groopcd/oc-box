"""Real native composer/host-drop integration; NOT host or Podman acceptance.

Run from the checkout root after freezing the production runtime:
TMPDIR=/tmp/opencode python3 -B tests/smoke_host_drop_v2.py
--opencode /path/to/unmodified-opencode

Supports native 2.2.6, 2.2.7 and 2.3.0, one executable per invocation. Fresh
synthetic HOME/XDG directories, a stdio-owned server, a canned loopback provider
and the existing native-preferences PTY observer are used. Production run_tui,
Relay, Export, Transfer and the executable host_drop_io protocol remain real.
Only the Podman client_command boundary becomes local execution of that helper.
A forwarding-only HTTP observer counts native composer admissions; it does not
implement or inject native API operations. Setup creates empty sessions through
the native API; ONLY a separately sent user Enter submits either test prompt.
The controlled pathname emulator selects the Konsole compatibility profile;
the controlled OSC72 emulator selects Kitty. These explicit fixture profiles
are not production capability overrides or proof of a real terminal emulator.

The stage observer renames every synthetic original before returning the real
staged pathname to run_tui. Thus successful native drafts cannot be explained by
the native client's otherwise-shared access to the original synthetic path.
PTY output is never recorded: only fixed-label flags, small terminal-query
parser state and bounded private stage/cleanup observations survive each read.
This tests neither actual screenshot-app gestures/rendering nor isolation from
another user/container, real model support, host desktop clipboard or Podman.
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from oc_box import host_drop, oc_box as box, opencode_compat, opencode_v2_io as bridge
from oc_box import terminal_ui
from smoke_native_preferences_v2 import Client as PreferencesClient
from smoke_session_sync_v2 import bounded, private_environment
from smoke_sharing_v2 import Failure, PNG, nodes, require

SUPPORTED = frozenset(("2.2.6", "2.2.7", "2.3.0"))
MARKER = "SYNTHETIC_HOST_DROP_USER_ENTER"
MAX_HTTP = 2 * 1024 * 1024
SOURCES = (
    "tests/smoke_host_drop_v2.py",
    "tests/smoke_native_preferences_v2.py",
    "tests/smoke_session_sync_v2.py",
    "tests/smoke_sharing_v2.py",
    "src/oc_box/terminal_ui.py",
    "src/oc_box/host_drop.py",
    "src/oc_box/host_drop_input.py",
    "src/oc_box/host_drop_io.py",
    "src/oc_box/sharing.py",
    "src/oc_box/oc_box.py",
    "src/oc_box/opencode_compat.py",
    "src/oc_box/opencode_v2_io.py",
)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def source_identity():
    def git(*args):
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO,
            stderr=subprocess.DEVNULL,
            timeout=10,
            text=True,
        ).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "hashes": {name: digest(REPO / name) for name in SOURCES},
    }


def synthetic_pdf():
    """One deterministic, self-contained blank page, not just a PDF signature."""
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] "
        b"/Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    )
    content = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    cross_reference = len(content)
    content.extend(b"xref\n0 5\n0000000000 65535 f \n")
    for offset in offsets:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n"
        + str(cross_reference).encode()
        + b"\n%%EOF\n"
    )
    return bytes(content)


@contextlib.contextmanager
def canned_provider():
    """No tool calls: retain hashes/flags only, never provider request bodies."""
    state = {"requests": [], "failures": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= MAX_HTTP, "provider-size")
                require(self.path == "/v1/chat/completions", "provider-route")
                body = json.loads(self.rfile.read(size))
                require(len(state["requests"]) < 4, "provider-request-bound")
                # Native may supply definitions even when this synthetic model
                # declares tools:false. A catalog is not a model tool call.
                require(
                    not any(
                        item.get("role") == "tool" for item in body.get("messages", [])
                    ),
                    "unexpected-model-tool-result",
                )
                media = {}
                for item in nodes(body.get("messages", [])):
                    image = item.get("image_url")
                    document = item.get("file")
                    uri = (
                        image.get("url")
                        if isinstance(image, dict)
                        else document.get("file_data")
                        if isinstance(document, dict)
                        else None
                    )
                    if not isinstance(uri, str) or not uri.startswith("data:"):
                        continue
                    header, separator, encoded = uri.partition(",")
                    require(separator and header.endswith(";base64"), "provider-media")
                    content = base64.b64decode(encoded, validate=True)
                    mime = header[5:-7]
                    require(mime not in media, "provider-duplicate-media")
                    media[mime] = hashlib.sha256(content).hexdigest()
                state["requests"].append(
                    {
                        "media": media,
                        "marker": MARKER in json.dumps(body.get("messages", [])),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, reason in (
                    ({"content": "SYNTHETIC_HOST_DROP_COMPLETE"}, None),
                    ({}, "stop"),
                ):
                    event = {
                        "id": "chatcmpl-host-drop",
                        "object": "chat.completion.chunk",
                        "model": "probe",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": reason}
                        ],
                    }
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception as error:
                state["failures"].append(
                    str(error) if isinstance(error, Failure) else type(error).__name__
                )
                with contextlib.suppress(OSError):
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


class NativeHTTPObserver:
    """Forward actual native HTTP/SSE responses, counting admission POSTs only.

    WebSockets are intentionally outside this fixture: client-mcp is off and no
    terminal pane is opened. Encountering an upgrade fails instead of inventing
    a response. Chunked upstream responses are decoded by HTTPConnection and
    close-delimited downstream; native JSON/event bodies are not rewritten.
    """

    def __init__(self, endpoint):
        self.endpoint = urlsplit(endpoint["url"])
        self.admissions, self.failures = [], []
        self.lock = threading.Lock()
        self.connections = set()
        self.handlers = set()
        self.closing = False

    def __enter__(self):
        observer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def forward(self):
                upstream = http.client.HTTPConnection(
                    observer.endpoint.hostname, observer.endpoint.port, timeout=5
                )
                response = stream = None
                with observer.lock:
                    observer.handlers.add(threading.current_thread())
                try:
                    self.connection.settimeout(10)
                    require(not self.headers.get("Upgrade"), "unexpected-websocket")
                    require(
                        not self.headers.get("Transfer-Encoding"), "request-framing"
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    require(0 <= size <= MAX_HTTP, "native-request-size")
                    body = self.rfile.read(size)
                    require(len(body) == size, "native-request-short")
                    if self.command == "POST" and re.fullmatch(
                        r"/api/session/[^/]+/(prompt|inbox|command|shell|synthetic)",
                        urlsplit(self.path).path,
                    ):
                        with observer.lock:
                            observer.admissions.append(urlsplit(self.path).path)
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower() not in ("connection", "host")
                    }
                    headers["Connection"] = "close"
                    upstream.connect()
                    stream = upstream.sock
                    stream.settimeout(60)
                    with observer.lock:
                        observer.connections.add(stream)
                    upstream.request(self.command, self.path, body or None, headers)
                    response = upstream.getresponse()
                    require(response.status != 101, "unexpected-websocket")
                    self.send_response_only(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ("connection", "transfer-encoding"):
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    with observer.lock:
                        if not observer.closing:
                            observer.failures.append(
                                str(error)
                                if isinstance(error, Failure)
                                else type(error).__name__
                            )
                finally:
                    if response is not None:
                        response.close()
                    upstream.close()
                    with observer.lock:
                        observer.connections.discard(stream)
                        observer.handlers.discard(threading.current_thread())

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = forward

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, *_):
                observer.failures.append("native-http-worker")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        return self

    def __exit__(self, *_):
        self.closing = True
        self.server.shutdown()
        with self.lock:
            connections = tuple(self.connections)
            handlers = tuple(self.handlers)
        for stream in connections:
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
            "native-http-stop",
        )


def note(fd, value):
    encoded = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    require(len(encoded) <= 4096, "observation-bound")
    terminal_ui.write_all(fd, encoded)


def relay_child(arguments):
    """Actual production relay in a controlling PTY, with an observed Relay."""
    binary, project, url, session, raw_fd, selection, profile = arguments
    require(profile in ("konsole", "kitty"), "synthetic-terminal-profile")
    fd = int(raw_fd)
    sources = [Path(value) for value in json.loads(selection)]
    fixture = sources[0].parent.parent
    require(
        sources
        and len(sources) <= 2
        and all(path.parent == fixture / "selected" for path in sources),
        "synthetic-source-binding",
    )
    expected = [path.read_bytes() for path in sources]
    original_helper = (REPO / "src/oc_box/host_drop_io.py").read_text()
    environment = dict(os.environ)

    def client_command(_box, _data, connection, command):
        require(
            connection == {"url": url}
            and command[:5] == ["python3", "-I", "-S", "-B", "-c"]
            and command[5] == original_helper
            and len(command) == 7
            and re.fullmatch(r"[0-9a-f]{32}", command[6]) is not None,
            "helper-execution-boundary",
        )
        return box.ContainerCommand([sys.executable, *command[1:]], environment)

    facade = SimpleNamespace(
        PACKAGE=REPO / "src/oc_box",
        APP=fixture / "protected/app",
        state_root=lambda: fixture / "protected/state",
        vault=SimpleNamespace(vault_path=lambda: fixture / "protected/vault/data"),
        workspace_service=SimpleNamespace(client_command=client_command),
    )

    class ObservedRelay(host_drop.Relay):
        """Observation only: all transformations and bytes use production code."""

        def __init__(self):
            super().__init__(facade, {}, {"url": url}, profile=profile)
            self.pastes = self.stages = self.inputs = 0

        def paste(self, payload):
            self.pastes += 1
            return super().paste(payload)

        def stage(self, images):
            require(self.stages == 0, "unexpected-second-stage")
            require(
                [content for _mime, content in images] == expected, "captured-bytes"
            )
            replacement = super().stage(images)
            staged = [Path(value) for value in shlex.split(replacement.decode())]
            require(len(staged) == len(sources), "staged-file-count")
            root = Path(self.transfer.root)
            require(
                stat.S_IMODE(root.stat().st_mode) == 0o700
                and root.stat().st_uid == os.geteuid(),
                "helper-root-privacy",
            )
            for index, (path, original) in enumerate(zip(staged, sources)):
                require(
                    path.parent == root
                    and path.read_bytes() == expected[index]
                    and stat.S_IMODE(path.stat().st_mode) == 0o600
                    and path.stat().st_uid == os.geteuid(),
                    "helper-file-privacy-content",
                )
                original.rename(original.with_name(original.name + ".unavailable"))
                require(not original.exists(), "original-still-readable")
            self.stages += 1
            note(fd, {"kind": "stage", "root": str(root), "count": len(staged)})
            # Native has not yet received these bytes. No file mapping, API
            # attachment injection or fake native parser is involved.
            return replacement

        def input_bytes(self, content):
            result = super().input_bytes(content)
            self.inputs += 1
            note(
                fd,
                {
                    "kind": "input",
                    "inputs": self.inputs,
                    "pastes": self.pastes,
                    "stages": self.stages,
                },
            )
            return result

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    relay = ObservedRelay()
    result = None
    try:
        with relay:
            note(fd, {"kind": "started", "profile": profile})
            result = terminal_ui.run_tui(
                [binary, project, "--server", url, "--session", session],
                env=environment,
                input_fd=0,
                output_fd=1,
                refresh=0.025,
                show_loading=False,
                file_drops=relay,
            )
    finally:
        process = relay.transfer.process
        note(
            fd,
            {
                "kind": "closed",
                "code": result,
                "helper_code": process.poll() if process is not None else None,
                "cleanup": relay.transfer.cleanup_verified,
                "root_missing": not Path(relay.transfer.root).exists(),
                "refused": relay.refused,
            },
        )
        os.close(fd)
    return result


class Client(PreferencesClient):
    """Reuse fixed-label native-preferences parsing and bounded PTY waits."""

    def __init__(self, binary, env, project, endpoint, selected, sources, *, profile):
        import pty

        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        self.observations, send = os.pipe()
        os.set_blocking(self.observations, False)
        self.note_bytes = bytearray()
        self.notes = []
        self.last_input = {"inputs": 0, "pastes": 0, "stages": 0}
        self.query_tail = b""
        self.kitty_id = None
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--relay-child",
            binary,
            str(project),
            endpoint["url"],
            selected,
            str(send),
            json.dumps([str(path) for path in sources]),
            profile,
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=project,
                env={**env, "OPENCODE_PASSWORD": endpoint["password"]},
                stdin=slave,
                stdout=slave,
                stderr=slave,
                pass_fds=(send,),
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            os.close(self.observations)
            raise
        finally:
            os.close(slave)
            os.close(send)
        self.state = "text"
        self.watch("Ask anything", "Synthetic native host drop")

    def feed(self, data):
        # A tiny responder for real OpenTUI startup queries, not a second UI.
        # Preserve only a bounded incomplete ESC sequence between PTY reads.
        self.query_tail += data
        require(len(self.query_tail) <= 131072, "terminal-query-bound")
        query = re.compile(
            rb"\x1b(?:\[(?:[?>]?[0-9;]*[cnu]|\?[0-9;]+\$p|[0-9;]*t)"
            rb"|\](?:10|11);\?(?:\x07|\x1b\\)"
            rb"|\]72;t=q:i=([0-9]+);(?:\x07|\x1b\\))"
        )
        replies = {
            b"\x1b[c": b"\x1b[?6c",
            b"\x1b[0c": b"\x1b[?6c",
            b"\x1b[>c": b"\x1b[>0;0;0c",
            b"\x1b[>0c": b"\x1b[>0;0;0c",
            b"\x1b[5n": b"\x1b[0n",
            b"\x1b[6n": b"\x1b[1;1R",
            b"\x1b[?6n": b"\x1b[?1;1R",
            b"\x1b[?u": b"\x1b[?0u",
            b"\x1b[18t": b"\x1b[8;40;140t",
            b"\x1b[14t": b"\x1b[4;640;1120t",
            b"\x1b[16t": b"\x1b[6;16;8t",
        }
        consumed = 0
        for match in query.finditer(self.query_tail):
            raw = match.group()
            response = replies.get(raw)
            if match.group(1):
                self.kitty_id = match.group(1)
            elif raw.startswith(b"\x1b]10;"):
                response = b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
            elif raw.startswith(b"\x1b]11;"):
                response = b"\x1b]11;rgb:0000/0000/0000\x1b\\"
            elif raw.startswith(b"\x1b[?") and raw.endswith(b"$p"):
                response = raw[:-2] + b";2$y"
            if response:
                terminal_ui.write_all(self.master, response)
            consumed = match.end()
        remainder = self.query_tail[consumed:]
        escape = remainder.rfind(b"\x1b")
        self.query_tail = remainder[escape:] if escape >= 0 else b""
        if len(self.query_tail) > 1024:
            self.query_tail = b""
        super().feed(data)

    def read_notes(self):
        while select.select([self.observations], [], [], 0)[0]:
            content = os.read(self.observations, 4096)
            if not content:
                break
            self.note_bytes.extend(content)
            require(len(self.note_bytes) <= 8192, "observation-bound")
            while b"\n" in self.note_bytes:
                raw, _, self.note_bytes = self.note_bytes.partition(b"\n")
                value = json.loads(raw)
                if value["kind"] == "input":
                    self.last_input = value
                else:
                    require(len(self.notes) < 6, "observation-count")
                    self.notes.append(value)

    def drain(self, duration):
        super().drain(duration)
        self.read_notes()

    def split(self, content):
        cuts = sorted(
            {1, 2, 4, 6, len(content) // 2, len(content) - 5, len(content) - 1}
        )
        previous = 0
        for cut in [*cuts, len(content)]:
            if previous < cut:
                self.send(content[previous:cut], 0.035)
            previous = cut

    def close(self):
        result = None
        try:
            if self.process.poll() is None:
                self.send(b"\x1b")
                self.send(b"\x03")
                # On a failing case the first Ctrl+C may only clear a draft.
                if self.process.poll() is None:
                    self.send(b"\x03")
                deadline = time.monotonic() + 8
                while self.process.poll() is None and time.monotonic() < deadline:
                    self.drain(0.1)
            result = self.process.poll()
        finally:
            try:
                if self.process.poll() is None:
                    # The outer relay owns a separately sessioned native child.
                    # Unwind production run_tui/Relay before any last-resort kill
                    # of this fixture's controller, rather than orphaning native.
                    self.process.send_signal(signal.SIGALRM)
                    deadline = time.monotonic() + 45
                    while self.process.poll() is None and time.monotonic() < deadline:
                        with contextlib.suppress(Failure, OSError):
                            self.drain(0.1)
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
                self.read_notes()
            finally:
                os.close(self.master)
                os.close(self.observations)
                self.tail = ""
                self.query_tail = b""
        require(result == 0, "native-tui-clean-exit")


def media_bytes(native, endpoint, item):
    """Read native inline or content-addressed media, never the source pathname."""
    if "data" in item:
        return base64.b64decode(item["data"], validate=True)
    uri = item.get("uri", "")
    if uri.startswith("data:"):
        return base64.b64decode(uri.partition(",")[2], validate=True)
    require(re.fullmatch(r"media:[0-9a-f]{64}", uri) is not None, "native-media-shape")
    endpoint_url = urlsplit(endpoint["url"])
    connection = http.client.HTTPConnection(
        endpoint_url.hostname, endpoint_url.port, timeout=5
    )
    try:
        authorization = base64.b64encode(
            ("opencode:" + endpoint["password"]).encode()
        ).decode()
        connection.request(
            "GET",
            "/api/experimental/media/" + uri[6:],
            headers={"Authorization": "Basic " + authorization},
        )
        response = connection.getresponse()
        content = response.read(host_drop.MAX_FILE + 1)
        require(
            response.status == 200
            and len(content) <= host_drop.MAX_FILE
            and hashlib.sha256(content).hexdigest() == uri[6:],
            "native-media-read",
        )
        return content
    finally:
        connection.close()


def exercise_case(binary, env, native, provider, project, root, *, structured):
    root.mkdir()
    selected = root / "selected"
    selected.mkdir(mode=0o700)
    originals = [selected / "Synthetic Screenshot.png"]
    originals[0].write_bytes(base64.b64decode(PNG))
    if not structured:
        originals.append(selected / "Synthetic document.pdf")
        originals[1].write_bytes(synthetic_pdf())
    expected_mimes = {"image/png"} | (set() if structured else {"application/pdf"})
    session = native(
        "POST",
        "/api/session",
        {
            "location": {"directory": str(project)},
            "title": "Synthetic native host drop",
            "agent": "build",
            "model": {"providerID": "fixture", "id": "probe"},
        },
    )["data"]["id"]
    before_provider = len(provider["requests"])
    profile = "kitty" if structured else "konsole"
    with NativeHTTPObserver(native.endpoint) as http:
        client = Client(
            binary,
            env,
            project,
            {**native.endpoint, "url": http.url},
            session,
            originals,
            profile=profile,
        )
        completed = False
        try:
            client.ready()
            client.wait(
                lambda: any(
                    n["kind"] == "started" and n.get("profile") == profile
                    for n in client.notes
                ),
                "profile-bound relay",
            )
            client.watch("[Image 1]", "[PDF 1]")
            if structured:
                client.wait(lambda: client.kitty_id is not None, "drop query")
                ident = client.kitty_id

                def packet(fields, data=b""):
                    return (
                        b"\x1b]72;" + fields + b":i=" + ident + b";" + data + b"\x1b\\"
                    )

                client.split(packet(b"t=q"))
                client.split(packet(b"t=M", b"image/png"))
                encoded = base64.b64encode(base64.b64decode(PNG))
                client.split(packet(b"t=r:x=1", encoded))
                client.split(packet(b"t=r:x=1"))
            else:
                require(client.kitty_id is None, "konsole-unexpected-drop-query")
                client.split(
                    b"\x1b[200~"
                    + shlex.join(map(str, originals)).encode()
                    + b"\x1b[201~"
                )
            client.wait(
                lambda: (
                    client.labels["[Image 1]"]
                    and (structured or client.labels["[PDF 1]"])
                ),
                "native image/PDF composer draft",
            )
            stages = [item for item in client.notes if item["kind"] == "stage"]
            require(
                len(stages) == 1
                and stages[0]["count"] == len(originals)
                and all(not path.exists() for path in originals)
                and native("GET", "/api/session/" + session + "/context")["data"] == []
                and native("GET", "/api/session/" + session + "/inbox")["data"] == []
                and not http.admissions
                and len(provider["requests"]) == before_provider,
                "draft-was-not-local-before-user-enter",
            )
            # Run reply-injection observations only after staging has made the
            # original unavailable to native. Otherwise this same-filesystem
            # fixture lets native's own paste parser read a nested original
            # pathname, unlike the real container boundary, contaminating its
            # composer independently of the correctly opaque host parser.
            before = client.last_input.copy()
            fake_paste = (
                b"\x1b[200~" + shlex.quote(str(originals[0])).encode() + b"\x1b[201~"
            )
            for introducer in (b"\x1b]777;", b"\x1bP1$r"):
                client.split(introducer + fake_paste + b"\x1b\\")
            client.wait(
                lambda: client.last_input["inputs"] > before["inputs"], "reply delivery"
            )
            require(
                client.last_input["pastes"] == before["pastes"]
                and client.last_input["stages"] == before["stages"] == 1
                and all(not path.exists() for path in originals)
                and not http.admissions
                and len(provider["requests"]) == before_provider,
                "terminal-reply-triggered-host-selection",
            )
            # This text and this separate Enter are the ONLY prompt submission
            # actions. No direct prompt/inbox API call exists in this fixture.
            client.send(MARKER.encode())
            require(not http.admissions, "text-insertion-submitted")
            client.send(b"\r")

            def admitted():
                require(
                    not provider["failures"] and not http.failures, "native-transport"
                )
                context = native("GET", "/api/session/" + session + "/context")["data"]
                messages = native(
                    "GET", "/api/session/" + session + "/message?order=asc&limit=100"
                )["data"]
                return (
                    any(
                        item.get("type") == "user" and MARKER in item.get("text", "")
                        for item in context
                    )
                    and any(
                        item.get("type") == "assistant"
                        and item.get("time", {}).get("completed")
                        for item in messages
                    )
                    and session not in native("GET", "/api/session/active")["data"]
                    and len(provider["requests"]) > before_provider
                )

            client.wait(admitted, "user Enter/native provider completion", timeout=30)
            context = native("GET", "/api/session/" + session + "/context")["data"]
            users = [
                item
                for item in context
                if item.get("type") == "user" and MARKER in item.get("text", "")
            ]
            require(
                len(users) == 1 and len(http.admissions) == 1, "native-admission-count"
            )
            files = users[0].get("files", [])
            messages = native(
                "GET", "/api/session/" + session + "/message?order=asc&limit=100"
            )["data"]
            require(
                not any(item.get("type") == "tool" for item in nodes(messages)),
                "unexpected-native-tool-execution",
            )
            require(
                len(files) == len(originals)
                and {item.get("mime") for item in files} == expected_mimes,
                "native-context-attachments",
            )
            preserved = {}
            for item in files:
                content = media_bytes(native, native.endpoint, item)
                require(
                    host_drop.mime_type(content) == item["mime"], "native-media-content"
                )
                if item["mime"] == "application/pdf":
                    require(content == synthetic_pdf(), "native-pdf-byte-preservation")
                preserved[item["mime"]] = hashlib.sha256(content).hexdigest()
            requests = provider["requests"][before_provider:]
            require(
                len(requests) == 1
                and requests[0]["marker"]
                and requests[0]["media"] == preserved,
                "native-provider-image-pdf-preservation",
            )
            exported = native(
                "GET", "/api/experimental/session/" + session + "/export"
            )["data"]["messages"]
            require(
                any(
                    item.get("id") == users[0]["id"] and item.get("files") == files
                    for item in exported
                ),
                "native-export-attachments",
            )
            completed = True
        except BaseException:
            print(
                "OBSERVATION "
                + json.dumps(
                    {
                        "structured": structured,
                        "labels": client.labels,
                        "pastes": client.last_input["pastes"],
                        "stages": client.last_input["stages"],
                        "admissions": len(http.admissions),
                        "provider_failures": provider["failures"],
                        "http_failures": http.failures,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            raise
        finally:
            client.close()
        if completed:
            closed = [item for item in client.notes if item["kind"] == "closed"]
            require(
                len(closed) == 1
                and closed[0]
                == {
                    "kind": "closed",
                    "code": 0,
                    "helper_code": 0,
                    "cleanup": True,
                    "root_missing": True,
                    "refused": 0,
                }
                and not Path(stages[0]["root"]).exists()
                and (structured or client.kitty_id is None)
                and not http.failures,
                "native-helper-cleanup",
            )
    print(
        "PASS: "
        + ("OSC72 PNG" if structured else "split pathname PNG+PDF")
        + "; real native draft before prompt POST; originals unavailable; "
        "separate user Enter; context/provider/export media; helper cleanup.",
        flush=True,
    )


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release in SUPPORTED, "native-version")
    print("NATIVE version=" + release, flush=True)
    config_directory = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    config_directory.mkdir(exist_ok=True)
    (config_directory / "cli.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/v2/cli.json",
                "animations": False,
                "highlights": False,
                "tabs": {"mode": "off"},
                "theme": {"name": "opencode", "mode": "dark"},
                "attention": {"notifications": False, "sound": False},
                "prompt": {"editor": False, "image_preview": False},
                "session": {"permissions": "prompt", "image_preview": False},
                "experimental": {"client-mcp": False},
            }
        )
    )
    with canned_provider() as (port, provider):
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        config.update(
            {
                "model": "fixture/probe",
                "warming": False,
                "lsp": False,
                "providers": {
                    "fixture": {
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {
                            "baseURL": f"http://127.0.0.1:{port}/v1",
                            "apiKey": "synthetic-only",
                        },
                        "models": {
                            "probe": {
                                "name": "Synthetic host drop",
                                "limit": {"context": 32000, "output": 2000},
                                "capabilities": {
                                    "tools": False,
                                    "input": ["text", "image", "pdf"],
                                    "output": ["text"],
                                },
                            }
                        },
                    }
                },
            }
        )
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        previous = Path.cwd()
        exit_info = {}
        os.chdir(project)
        try:
            from unittest.mock import patch

            # Environment replacement is the established isolated native-server
            # harness, not a mock of any server/client/API implementation.
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary, exit_info=exit_info) as native,
            ):
                require(
                    native("GET", "/api/credential")["data"] == []
                    and bridge.session_inventory(native, str(project)) == [],
                    "private-native-state",
                )
                for structured, name in ((False, "paths"), (True, "osc72")):
                    exercise_case(
                        binary,
                        env,
                        native,
                        provider,
                        project,
                        root / name,
                        structured=structured,
                    )
                require(
                    not provider["failures"]
                    and len(provider["requests"]) == 2
                    and native("GET", "/api/session/active")["data"] == {}
                    and native("GET", "/api/credential")["data"] == [],
                    "native-final-state",
                )
        finally:
            os.chdir(previous)
        require(exit_info == {"code": 0, "forced": False}, "native-owner-clean-exit")
    return release


def main():
    if sys.argv[1:2] == ["--relay-child"]:

        def child_timeout(*_):
            raise Failure("relay-child-timeout")

        signal.signal(signal.SIGALRM, child_timeout)
        signal.alarm(90)
        try:
            return relay_child(sys.argv[2:])
        except BaseException:
            # Never print a traceback containing source paths, HTTP credentials
            # or bytes. The parent fails with its bounded fixed-label timeout.
            return 1
        finally:
            signal.alarm(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    arguments = parser.parse_args()

    def timed_out(*_):
        raise Failure("fixture-timeout")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(180)
    try:
        binary = arguments.opencode.resolve()
        identity, native_hash = source_identity(), digest(binary)
        print(
            f"SOURCE commit={identity['commit']} dirty={str(identity['dirty']).lower()}",
            flush=True,
        )
        print("NATIVE sha256=" + native_hash, flush=True)
        for name, value in identity["hashes"].items():
            print(f"SOURCE sha256 {name}={value}", flush=True)
        directory = Path(tempfile.mkdtemp(prefix="oc-box-host-drop-native-")).resolve()
        directory.chmod(0o700)
        print("PRIVATE fixture_root=" + str(directory), flush=True)
        # Retain this private synthetic store on success and failure. Report
        # logs/hashes are useful even when an oracle fails after real admission.
        release = exercise(str(binary), directory)
        require(
            source_identity() == identity and digest(binary) == native_hash,
            "tested-source-changed",
        )
        print(
            "Native " + release + " scripted synthetic PTY integration only: "
            "NOT host screenshot gestures/rendering, interactive UI, desktop "
            "clipboard, Podman isolation/lifecycle or real-provider acceptance.",
            flush=True,
        )
        return 0
    except Failure as error:
        print("FAIL: native host drop (" + str(error) + ").", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("FAIL: native host drop (unexpected).", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    sys.exit(main())
