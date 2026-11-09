"""Native V1 crash history for the offline V1-to-V2 migration regression.

Only use with a synthetic project and fully private HOME/XDG directories. No
transcript is imported or edited: unfinished assistants come from SIGKILL of
owned native CLI process groups while a loopback provider holds its SSE stream.
"""

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_MODEL = "interrupted-fixture/gpt-interrupted"
_MARKERS = (
    "HOLD interrupted first",
    "DONE after first interruption",
    "HOLD interrupted second",
    "DONE after second interruption",
    "HOLD unfinished tail",
)


class _Provider(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.timeout = 0.1
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.failure = "request"
        self.held = set()
        self.requests = []

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, *_):
        self.failed.set()

    def run(self):
        while not self.stop.is_set():
            self.handle_request()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        stage = "route"
        try:
            assert self.path == "/v1/chat/completions"
            stage = "authorization"
            assert self.headers.get("Authorization") == "Bearer synthetic"
            stage = "body"
            size = int(self.headers["Content-Length"])
            assert 0 < size <= 2_000_000
            body = json.loads(self.rfile.read(size))
            stage = "model"
            assert body["model"] == "gpt-interrupted" and body["stream"] is True
            stage = "messages"
            users = [m["content"] for m in body["messages"] if m["role"] == "user"]
            content = users[-1]
            text = (
                content
                if isinstance(content, str)
                else "".join(part.get("text", "") for part in content)
            )
            stage = "marker"
            # V1 appends native system reminders to the lowered user content.
            markers = [marker for marker in _MARKERS if marker in text]
            assert len(markers) == 1
            marker = markers[0]
            stage = "stream"
            self.server.requests.append(marker)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.chunk({"role": "assistant", "content": "synthetic partial"})
            if marker.startswith("HOLD"):
                self.server.held.add(marker)
                deadline = time.monotonic() + 90
                while not self.server.stop.wait(0.1):
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                return
            self.chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected after killing an owned native client.
        except Exception:  # noqa: BLE001 -- Suppress private protocol diagnostics.
            self.server.failure = stage
            self.server.failed.set()  # Never print a request or raw exception.

    def chunk(self, delta, finish=None):
        item = {
            "id": "chatcmpl-interrupted-fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-interrupted",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if finish:
            item["usage"] = {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            }
        self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
        self.wfile.flush()


def _kill(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "Interrupted V1 fixture: owned process did not stop"
        ) from None


class _Native:
    def __init__(self, env, project, capture, provider):
        self.env, self.project, self.capture, self.provider = (
            env,
            project,
            capture,
            provider,
        )

    @contextmanager
    def command(self, *args):
        # Private regular files cannot deadlock when a CLI writes verbose output.
        with (
            tempfile.TemporaryFile(dir=self.capture) as output,
            tempfile.TemporaryFile(dir=self.capture) as errors,
        ):
            process = subprocess.Popen(
                ["opencode", "--pure", *args],
                env=self.env,
                cwd=self.project,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                start_new_session=True,
            )
            try:
                yield process, output
            finally:
                _kill(process)

    def cli(self, *args, timeout=15):
        with self.command(*args) as (process, output):
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "Interrupted V1 fixture: native command timed out"
                ) from None
            assert process.returncode == 0, (
                "Interrupted V1 fixture: native command failed"
            )
            output.seek(0)
            return output.read()

    def sessions(self):
        rows = json.loads(self.cli("session", "list", "--format", "json") or b"[]")
        return {row["id"] for row in rows}

    def export(self, ident):
        value = json.loads(self.cli("export", ident))
        assert value["info"]["id"] == ident, (
            "Interrupted V1 fixture: export identity mismatch"
        )
        return value["messages"]

    def turn(self, marker, ident=None, count=1):
        before = self.sessions() if ident is None else None
        args = ["run", "--format", "json", "--model", _MODEL]
        args += (
            ["--title", "Interrupted V1 fixture"]
            if ident is None
            else ["--session", ident]
        )
        with self.command(*args, marker) as (process, _):
            if not marker.startswith("HOLD"):
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    raise AssertionError(
                        "Interrupted V1 fixture: completed turn timed out"
                    ) from None
                assert process.returncode == 0, (
                    "Interrupted V1 fixture: completed turn failed"
                )
                return ident
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert not self.provider.failed.is_set(), (
                    "Interrupted V1 fixture: provider failed ("
                    + self.provider.failure
                    + ")"
                )
                assert process.poll() is None, (
                    "Interrupted V1 fixture: held turn exited early"
                )
                if marker in self.provider.held:
                    if ident is None:
                        created = self.sessions() - before
                        assert len(created) == 1, (
                            "Interrupted V1 fixture: session discovery mismatch"
                        )
                        ident = created.pop()
                    messages = self.export(ident)
                    if len(messages) == count * 2:
                        info = messages[-1]["info"]
                        if (
                            info["role"] == "assistant"
                            and info["time"].get("completed") is None
                        ):
                            # The native export, not merely an SSE request, proves
                            # the unfinished assistant was durably created.
                            _kill(process)
                            assert process.returncode == -signal.SIGKILL, (
                                "Interrupted V1 fixture: held turn was not killed"
                            )
                            return ident
                time.sleep(0.05)
            raise AssertionError(
                "Interrupted V1 fixture: persisted held turn timed out"
            )


def _check(messages, markers, unfinished):
    assert len(messages) == len(markers) * 2, (
        "Interrupted V1 fixture: message count mismatch"
    )
    for index, marker in enumerate(markers):
        user, assistant = messages[index * 2 : index * 2 + 2]
        info = assistant["info"]
        assert user["info"]["role"] == "user" and info["role"] == "assistant", (
            "Interrupted V1 fixture: message order mismatch"
        )
        assert (
            sum(
                marker in part["text"]
                for part in user["parts"]
                if part["type"] == "text"
            )
            == 1
        ), "Interrupted V1 fixture: user turn mismatch"
        assert info["parentID"] == user["info"]["id"] and not info.get("error"), (
            "Interrupted V1 fixture: assistant parent or error mismatch"
        )
        if index in unfinished:
            assert info["time"].get("completed") is None and not info.get("finish"), (
                "Interrupted V1 fixture: interruption was settled"
            )
        else:
            assert (
                info["time"].get("completed") is not None
                and info.get("finish") == "stop"
            ), "Interrupted V1 fixture: later turn did not finish"


def create_interrupted_history(env, project):
    """Return (superseded ID, tail ID, original native messages keyed by ID).

    The caller owns the private V1 environment and its resulting database. This
    helper changes only a copy of the environment and temporary capture files;
    the returned messages come directly from supported native V1 exports.
    """
    provider = _Provider()
    worker = threading.Thread(target=provider.run, name="interrupted-v1-provider")
    worker.start()
    try:
        settings = {
            "snapshot": False,
            "formatter": False,
            "lsp": False,
            "autoupdate": False,
            "model": _MODEL,
            "small_model": _MODEL,
            "enabled_providers": ["interrupted-fixture"],
            "provider": {
                "interrupted-fixture": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Synthetic interrupted fixture",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{provider.server_port}/v1",
                        "apiKey": "synthetic",
                    },
                    "models": {
                        "gpt-interrupted": {
                            "name": "Synthetic interrupted fixture",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                }
            },
        }
        scoped = {
            **env,
            "OPENCODE_CONFIG_CONTENT": json.dumps(settings),
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }
        temporary = scoped.get("TMPDIR") or (
            "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        )
        with tempfile.TemporaryDirectory(
            prefix="oc-box-interrupted-v1-", dir=temporary
        ) as capture:
            native = _Native(scoped, project, capture, provider)
            assert native.cli("--version").decode().strip() == "1.19.5", (
                "Interrupted V1 fixture: native V1 1.19.5 is required"
            )
            superseded = native.turn(_MARKERS[0])
            native.turn(_MARKERS[1], superseded)
            native.turn(_MARKERS[2], superseded, count=3)
            native.turn(_MARKERS[3], superseded)
            tail = native.turn(_MARKERS[4])
            assert tail != superseded, "Interrupted V1 fixture: sessions were reused"
            originals = {ident: native.export(ident) for ident in (superseded, tail)}
            _check(originals[superseded], _MARKERS[:4], {0, 2})
            _check(originals[tail], _MARKERS[4:], {0})
            assert not provider.failed.is_set() and provider.requests == list(
                _MARKERS
            ), "Interrupted V1 fixture: unexpected synthetic provider requests"
            return superseded, tail, originals
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 -- Native errors must not reach public logs.
        raise AssertionError(
            "Interrupted V1 fixture: private native setup or protocol failed"
        ) from None
    finally:
        provider.stop.set()
        worker.join(timeout=5)
        provider.server_close()
        assert not worker.is_alive(), "Interrupted V1 fixture: provider did not stop"
