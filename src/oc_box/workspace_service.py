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
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import secrets
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

if __package__:
    from . import lifecycle_output, startup_output
else:
    import lifecycle_output
    import startup_output

CONNECTION = contextvars.ContextVar("oc_box_v2_connection")
_DEVICE_LEASE = contextvars.ContextVar("oc_box_device_lease_engine", default=None)
_QUIET_HISTORY_SOURCE = contextvars.ContextVar(
    "oc_box_quiet_history_source", default=None
)
LIFECYCLE_LOCK = contextvars.ContextVar("oc_box_owner_lifecycle_lock", default=None)
_NAMED_ONLY = "named-only"
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


def socket_path(box, data, *, create=True):
    root = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / (
        "oc-box-live-" + str(os.getuid())
    )
    if create:
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


class HistorySource:
    """Pass-local unregistered quiet source, never the post-stop Reader.

    The caller must retain its normal exclusive workspace lifecycle lock.
    2.4 additionally requires a verified stopped origin. Only positively
    identified, clean preflight opt-outs can return to ordinary sync.
    """

    def __init__(
        self,
        box,
        data,
        *,
        stopped_source=False,
        scratch_binding=None,
        engine_spawn=None,
        spawn_admission=None,
    ):
        if __package__:
            from .opencode_v2_io import HISTORY_SOURCE_24
        else:
            from opencode_v2_io import HISTORY_SOURCE_24

        self.box, self.data = box, dict(data)
        release = self.data.get("opencode_version")
        self.exact = type(release) is str and release in HISTORY_SOURCE_24
        self.stopped_source = stopped_source
        self._scratch_binding = scratch_binding
        self._engine_spawn = engine_spawn
        self._spawn_admission = spawn_admission
        self.source_scope = None
        self.child = self.errors = self.error_file = self.token = None
        self.connection = None
        self._source_code = None
        self._lifecycle_lock = None
        self._spawning = False
        self.started = False
        self.fallback = False

    def fail(self, category):
        error = self.box.BoxError(
            "Private history source lease failed (" + category + ")."
        )
        if self.exact:
            error.history_source_category = category
            if category == "cleanup-failed":
                error.history_source_cleanup_failed = True
        return error

    def _wait(self, timeout):
        if not self.exact:
            return self.child.wait(timeout=timeout)
        if __package__:
            from .opencode_v2_io import _wait_exact_child
        else:
            from opencode_v2_io import _wait_exact_child

        # Refuse a synthetic Popen zero (e.g. ECHILD). A previously observed
        # exit is already a startup/protocol failure, never live lease proof.
        if self.child.returncode is None:
            _wait_exact_child(self.child, timeout)
        return self.child.returncode

    def _poll(self):
        if self._scratch_binding is None:
            return self.child.poll()
        # Popen.poll() can cache a synthetic zero on ECHILD. The scoped
        # protocol must carry a real wait status, including on early exit.
        try:
            return self._wait(0)
        except subprocess.TimeoutExpired:
            return None

    def command(self):
        if __package__:
            from . import history_reader
        else:
            import history_reader

        source = (self.box.PACKAGE / "opencode_v2_io.py").read_text()
        if self.exact:
            if __package__:
                from .web_named_native_profile import named_native_worker_source
            else:
                from web_named_native_profile import named_native_worker_source
            self._source_code = source
            source = named_native_worker_source(source)
        return self.box.ContainerCommand(
            [
                "podman",
                "exec",
                "-i",
                "--workdir",
                "/",
                self.data["container"],
                "/usr/bin/env",
                "-i",
                "PATH=" + history_reader.PATH,
                "LANG=C.UTF-8",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-c",
                source,
                "history-source",
                self.box.CONTAINER_OPENCODE,
            ],
            self.box.maintenance_host_env(),
        )

    def read_command(self, data, action):
        """One short-lived read client, never a new service or reused transport."""
        if __package__:
            from .history_reader import PATH
        else:
            from history_reader import PATH
        if (
            not self.exact
            or self.stopped_source is not True
            or self._lifecycle_lock is None
            or LIFECYCLE_LOCK.get() is not self._lifecycle_lock
            or not self._source_code
            or self.connection is None
            or self.connection is not live_connection()
            or action not in ("sessions", "export")
            or any(
                data.get(key) != self.data.get(key)
                for key in ("id", "container", "home", "opencode_version")
            )
            or self.box.container_project(data) != self.box.container_project(self.data)
        ):
            raise self.fail("source")
        if self.child is None or self._poll() is not None:
            raise self.fail("lease-ended")
        # An authenticated endpoint request needs no discovery roots or forwarded
        # settings. Do not launch an environment trampoline or pass credentials as
        # exec environment/argv. The endpoint still arrives only via private stdin.
        return self.box.ContainerCommand(
            [
                "podman",
                "exec",
                "-i",
                "--workdir",
                "/",
                self.data["container"],
                "/usr/bin/env",
                "-i",
                "PATH=" + PATH,
                "LANG=C.UTF-8",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                "-c",
                self._source_code,
                "quiet-history-" + action,
                self.box.CONTAINER_OPENCODE,
            ],
            self.box.maintenance_host_env(),
        )

    def overlap_command(self, data, attempt):
        """One READY/GO export worker, after independently bound scratch admission."""
        if __package__:
            from .history_overlap_io import SourceAttempt
        else:
            from history_overlap_io import SourceAttempt
        if (
            self._scratch_binding is None
            or self.source_scope is None
            or type(attempt) is not SourceAttempt
            or attempt.container_id != self.data["container"]
        ):
            raise self.fail("source")
        command = self.read_command(data, "export")
        # Both texts are trusted launcher resources, not settings sent from
        # the container. The attempt token binds the protocol, not the native
        # endpoint credential (which still arrives only via private stdin).
        launcher = (
            "import sys, types\n"
            "bridge = types.ModuleType('oc_box_overlap_bridge')\n"
            "guest = types.ModuleType('oc_box_overlap_guest')\n"
            "sys.modules[bridge.__name__] = bridge\n"
            "sys.modules[guest.__name__] = guest\n"
            "exec(sys.argv[2], bridge.__dict__)\n"
            "exec(sys.argv[3], guest.__dict__)\n"
            "try:\n"
            "    guest.supervise(sys.argv[1], bridge)\n"
            "except Exception:\n"
            "    sys.exit(2)\n"
        )
        command[-3:] = [
            launcher,
            attempt.token,
            self._source_code,
            (self.box.PACKAGE / "history_overlap_guest.py").read_text(),
        ]
        return command

    def _write(self, payload, deadline):
        descriptor = self.child.stdin.fileno()
        os.set_blocking(descriptor, False)
        offset = 0
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self.fail("startup-timeout")
            if not select.select([], [descriptor], [], min(0.1, remaining))[1]:
                continue
            try:
                count = os.write(descriptor, payload[offset : offset + 4096])
            except BlockingIOError:
                continue
            if count <= 0:
                raise self.fail("startup-failed")
            offset += count

    def _category(self):
        if __package__:
            from . import opencode_v2_io as bridge
        else:
            import opencode_v2_io as bridge

        self.error_file.seek(0)
        lines = self.error_file.read(MAX_CONTROL).decode("utf-8", errors="replace")
        for line in reversed(lines.splitlines()):
            if line.startswith(bridge.HISTORY_SOURCE_MARKER):
                category = line[len(bridge.HISTORY_SOURCE_MARKER) :]
                if category in bridge.HISTORY_SOURCE_ERRORS:
                    return category
        return "startup-failed"

    def _exact_exit(self, code, *, startup_failed, preflight):
        if __package__:
            from . import opencode_v2_io as bridge
        else:
            import opencode_v2_io as bridge

        # A reaped wrapper alone is not acknowledgement: require strict EOF,
        # with neither extra control bytes nor an inherited open pipe.
        if (
            code not in (0, 2)
            or not self.child.stdout
            or not select.select([self.child.stdout], [], [], 0)[0]
            or os.read(self.child.stdout.fileno(), 1)
        ):
            raise self.fail("cleanup-failed")
        self.error_file.seek(0)
        report = self.error_file.read(MAX_CONTROL + 1)
        marker = bridge.HISTORY_SOURCE_MARKER.encode()
        if code == 0:
            if (
                # The scoped guest returns zero only after a normal readiness
                # or fallback path and successful scratch removal. Zero alone
                # after an unrecognized/missing startup record is not proof.
                self._scratch_binding is not None and (startup_failed or report)
            ) or marker + b"cleanup-failed" in report.splitlines():
                raise self.fail("cleanup-failed")
            return None
        if startup_failed and preflight:
            # The trusted guest emits exactly this terminal status and fixed
            # refusal only after unwinding the scratch context (if entered).
            # ScratchCleanupUncertain replaces the original category there.
            for category in bridge.HISTORY_SOURCE_ERRORS - {
                "cleanup-failed",
                "control",
                "lease-ended",
                "lease-timeout",
            }:
                if report == marker + category.encode() + b"\n":
                    return category
        raise self.fail("cleanup-failed")

    def __enter__(self):
        if __package__:
            from . import history_reader, opencode_v2_io as bridge
        else:
            import history_reader
            import opencode_v2_io as bridge

        if self.started:
            raise self.fail("reuse")
        self.started = True
        output = bytearray()
        ready = False
        try:
            self._lifecycle_lock = LIFECYCLE_LOCK.get()
            if self.exact and (
                self.stopped_source is not True or self._lifecycle_lock is None
            ):
                raise self.fail("source-not-stopped")
            if self.exact and signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
                raise self.fail("startup-failed")
            if (
                not isinstance(self.data.get("id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", self.data["id"])
                or not isinstance(self.data.get("container"), str)
                or not re.fullmatch(
                    r"[a-f0-9]{64}"
                    if self.exact
                    else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                    self.data["container"],
                )
                or not isinstance(self.data.get("home"), str)
                or not self.data["home"].startswith("/")
                or os.path.normpath(self.data["home"]) != self.data["home"]
                or "\0" in self.data["home"]
            ):
                raise self.fail("source")
            settings = {
                "guards": (self.box.PACKAGE / "history_reader.py").read_text(),
                "version": self.data["opencode_version"],
                "home": self.data["home"],
            }
            if self._scratch_binding is not None:
                if (
                    not self.exact
                    or type(self._scratch_binding) is not dict
                    or self._scratch_binding.get("home") != settings["home"]
                    or self._scratch_binding.get("version") != settings["version"]
                ):
                    raise self.fail("scratch-unverified")
                settings["scratch_binding"] = self._scratch_binding
                settings["scratch_source"] = (
                    self.box.PACKAGE / "history_volume_scratch.py"
                ).read_text()
            payload = json.dumps(settings).encode() + b"\n"
            if len(payload) > bridge.HISTORY_SOURCE_MAX_PAYLOAD:
                raise self.fail("payload")
            command = self.command()
            self.errors = self.box.captured_output(private=True)
            self.error_file = self.errors.__enter__()
            spawn_options = (
                self._engine_spawn(command) if self._engine_spawn is not None else {}
            )
            with (
                self._spawn_admission()
                if self._spawn_admission is not None
                else contextlib.nullcontext()
            ):
                self._spawning = True
                self.child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self.error_file,
                    start_new_session=True,
                    **spawn_options,
                )
                self._spawning = False
            # Child startup alarm is 45s, leaving time for its native cleanup
            # before this wrapper's independent hard deadline.
            deadline = time.monotonic() + 60
            self._write(payload, deadline)
            while b"\n" not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self.fail("startup-timeout")
                if select.select([self.child.stdout], [], [], min(0.1, remaining))[0]:
                    chunk = os.read(
                        self.child.stdout.fileno(), MAX_CONTROL + 1 - len(output)
                    )
                    if not chunk:
                        self._wait(min(1, remaining))
                        raise self.fail(
                            "startup-failed" if self.exact else self._category()
                        )
                    output.extend(chunk)
                elif self._poll() is not None:
                    raise self.fail(
                        "startup-failed" if self.exact else self._category()
                    )
                if len(output) > MAX_CONTROL:
                    raise self.fail("startup-response")
            line, trailing = output.split(b"\n", 1)
            response = (
                bridge.named_native_json(line) if self.exact else json.loads(line)
            )
            if trailing or not isinstance(response, dict):
                raise self.fail("startup-response")
            if set(response) == {"fallback"}:
                fallbacks = (
                    bridge.HISTORY_SOURCE_24_FALLBACKS
                    if self.exact
                    else bridge.HISTORY_SOURCE_FALLBACKS
                )
                if (
                    type(response["fallback"]) is not str
                    or response["fallback"] not in fallbacks
                ):
                    raise self.fail("startup-response")
                self.shutdown()
                self.fallback = True
                return None
            expected = (
                {"endpoint"}
                if self._scratch_binding is None
                else {"endpoint", "source"}
            )
            if set(response) != expected or set(response["endpoint"]) != {
                "url",
                "password",
            }:
                raise self.fail("startup-response")
            endpoint = validate_endpoint(response["endpoint"])
            if self._scratch_binding is not None:
                source = response["source"]
                if (
                    type(source) is not dict
                    or set(source) != {"pid", "cwd"}
                    or type(source["pid"]) is not int
                    or not 1 <= source["pid"] < 2**31
                    or type(source["cwd"]) is not str
                    or source["cwd"].rsplit("/", 2)[0] != self.data["home"]
                    or not source["cwd"].endswith("/work")
                    or re.fullmatch(
                        r"\.oc-box-quiet-history-[a-f0-9]{32}",
                        source["cwd"].rsplit("/", 2)[-2],
                    )
                    is None
                ):
                    raise self.fail("scratch-unverified")
                # Not authority: host-side acquisition must independently
                # match this PID/cwd to the original engine and volume.
                self.source_scope = source
            ready = True
            if self._poll() is not None:
                raise self.fail("startup-failed" if self.exact else self._category())
            self.connection = {**endpoint, "workspace": self.data["id"]}
            self.token = CONNECTION.set(self.connection)
            return self.connection
        except BaseException as error:
            interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
            refusal = self._shutdown_preserving(
                error,
                startup_failed=not ready if self.exact else not interrupted,
                preflight=not output,
            )
            if interrupted:
                raise
            if (
                refusal is not None
                and getattr(error, "history_source_category", "startup-failed")
                == "startup-failed"
            ):
                raise self.fail(refusal) from None
            if isinstance(error, self.box.BoxError):
                raise
            raise self.fail("startup-failed") from None

    def _shutdown_preserving(self, original, *, startup_failed=False, preflight=False):
        try:
            return self.shutdown(startup_failed=startup_failed, preflight=preflight)
        except BaseException:
            if isinstance(original, (KeyboardInterrupt, SystemExit)):
                # The original interruption remains authoritative, including
                # during startup. The fixed marker records uncertain cleanup
                # without replacing it with a different exception or success.
                self._mark_cleanup_interruption(original)
                raise original from None
            raise

    @staticmethod
    def _mark_cleanup_interruption(error):
        error.history_source_cleanup_failed = True
        if isinstance(error, SystemExit) and (error.code is None or error.code == 0):
            # Preserve identity, never let uncertain cleanup become exit zero.
            error.code = 1

    def shutdown(self, *, startup_failed=False, preflight=False):
        # An interruption during Popen can prevent receiving a handle even
        # though the scoped guest may have been launched.
        failed = (
            self._scratch_binding is not None and self._spawning and self.child is None
        )
        interruption = None
        refusal = None

        def caught(error):
            nonlocal failed, interruption
            failed = True
            if interruption is None and isinstance(
                error, (KeyboardInterrupt, SystemExit)
            ):
                interruption = error

        try:
            if self.child is not None:
                try:
                    if self.child.stdin:
                        try:
                            self.child.stdin.close()
                        except OSError:
                            pass  # A remote refusal can close its input first.
                        except BaseException as error:
                            caught(error)
                    try:
                        code = self._wait(20)
                    except BaseException as error:
                        caught(error)
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        except BaseException as error:
                            caught(error)
                        try:
                            self._wait(5)
                        except BaseException as error:
                            caught(error)
                        code = None
                    if self.exact and not failed:
                        try:
                            refusal = self._exact_exit(
                                code, startup_failed=startup_failed, preflight=preflight
                            )
                        except BaseException as error:
                            caught(error)
                    elif not self.exact:
                        failed = failed or (code != 0 and not startup_failed)
                finally:
                    if self.child.stdout:
                        try:
                            self.child.stdout.close()
                        except BaseException as error:
                            caught(error)
                    self.child = None
        finally:
            self._spawning = False
            if self.token is not None:
                try:
                    CONNECTION.reset(self.token)
                except BaseException as error:
                    caught(error)
                self.token = None
            self.connection = None
            self.source_scope = None
            self._source_code = self._lifecycle_lock = None
            if self.error_file is not None:
                try:
                    self.errors.__exit__(None, None, None)
                except BaseException as error:
                    caught(error)
                self.error_file = None
        if interruption is not None:
            self._mark_cleanup_interruption(interruption)
            raise interruption
        if failed:
            raise self.fail("cleanup-failed")
        return refusal

    def __exit__(self, kind, value, traceback):
        if self.fallback:
            return
        try:
            ended = kind is None and (
                self.child is None
                or self._poll() is not None
                or bool(select.select([self.child.stdout], [], [], 0)[0])
            )
        except BaseException as error:
            self._shutdown_preserving(error)
            raise
        self._shutdown_preserving(value)
        if ended:
            raise self.fail("lease-ended")


@contextlib.contextmanager
def history_source(
    box,
    data,
    *,
    stopped_source=False,
    scratch_binding=None,
    engine_spawn=None,
    spawn_admission=None,
):
    """Lease an ordinary source; 2.4 requires the caller's verified pre-start stop."""
    if __package__:
        from . import history_reader, history_recovery, opencode_v2_io
    else:
        import history_reader
        import history_recovery
        import opencode_v2_io

    live = live_connection()
    version = data.get("opencode_version")
    if (
        live is not None
        or history_reader.current() is not None
        or history_recovery.FENCE.get() is not None
        or type(version) is not str
        or version not in opencode_v2_io.HISTORY_SOURCE_SUPPORTED
    ):
        if (
            scratch_binding is not None
            or engine_spawn is not None
            or spawn_admission is not None
        ):
            raise box.BoxError(
                "Private history source lease failed (scratch-unverified)."
            )
        yield live
        return
    if version in opencode_v2_io.HISTORY_SOURCE_24:
        safeguard = getattr(box, "runtime_safeguard", None)
        if stopped_source is not True or (
            safeguard is not None and safeguard.current() is not None
        ):
            if (
                scratch_binding is not None
                or engine_spawn is not None
                or spawn_admission is not None
            ):
                raise box.BoxError(
                    "Private history source lease failed (scratch-unverified)."
                )
            yield None
            return
    source = HistorySource(
        box,
        data,
        stopped_source=stopped_source,
        scratch_binding=scratch_binding,
        engine_spawn=engine_spawn,
        spawn_admission=spawn_admission,
    )
    with source as connection:
        token = _QUIET_HISTORY_SOURCE.set(
            source if source.exact and connection is not None else None
        )
        try:
            yield connection
        finally:
            _QUIET_HISTORY_SOURCE.reset(token)


def quiet_history_source():
    return _QUIET_HISTORY_SOURCE.get()


def controller_running(box, data, *, create_runtime=True):
    try:
        address = socket_path(box, data, create=create_runtime)
    except FileNotFoundError:
        if not create_runtime:
            return False
        raise
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
    if type(value.get("image_preview", False)) is not bool:
        raise ValueError("Invalid private workspace image-preview setting.")
    if "ui_state" in value:
        if __package__:
            from . import ui_state
        else:
            import ui_state
        ui_state._metadata(value)
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


def require_session(box, connection):
    """Session-bound clients must not invent a target for a native home view."""
    ident = connection.get("session")
    if not box.valid_session_id(ident):
        raise box.BoxError(
            "No open native session is selected. Open one in OpenCode or start with --new."
        )
    return ident


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


def _validate_workspace_identity(box, path, data):
    project = box.validate_project(data["project"])
    selected, _ = box.find_workspace(project)
    if (
        selected != path
        or not box.workspace_identity.same_project(data, project)
        or data.get("restore_incomplete")
    ):
        raise box.BoxError("Workspace identity changed; refusing live session access.")


def validate_workspace(box, path, data, *, engine_env=None):
    _validate_workspace_identity(box, path, data)
    info = box.inspect_owned(
        data,
        **(
            {"local_only": True, "env": engine_env}
            if engine_env is not None
            else {"env": box.maintenance_host_env()}
        ),
    )
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


def _borrowed_device_engine(data, container):
    binding = _DEVICE_LEASE.get()
    if binding is not None and binding[:2] == (data["id"], container):
        return binding[2]
    return None


@contextlib.contextmanager
def _device_engine_scope(data, container, environment):
    if environment is None:
        yield
        return
    token = _DEVICE_LEASE.set((data["id"], container, environment.copy()))
    try:
        yield
    finally:
        _DEVICE_LEASE.reset(token)


def client_command(
    box,
    data,
    connection,
    arguments,
    *,
    interactive=False,
    force_tty=False,
    ui_owner=False,
    file_preview=False,
    activity_timeline=False,
    inline_timestamps=False,
    engine_env=None,
):
    """No host pass_env, auth refresh, profiles, or vault unlock on attach.

    An explicit engine environment comes from this process's verified owner,
    not from a connection reply or a rendering worker's ambient environment.
    """
    safeguard = getattr(box, "runtime_safeguard", None)
    guarded = safeguard.current() if safeguard else None
    guarded_container = (
        guarded.record.container if guarded else connection.get("_runtime_container")
    )
    guarded_digest = (
        guarded.record.digest if guarded else connection.get("_runtime_digest")
    )
    image_preview = connection.get("image_preview", False)
    if type(image_preview) is not bool:
        raise box.BoxError("Invalid private workspace image-preview setting.")
    active_engine = getattr(guarded, "engine_env", None)
    if active_engine is None:
        active_engine = _borrowed_device_engine(data, guarded_container)
    if engine_env is not None and (
        not guarded_container
        or not guarded_digest
        or (active_engine is not None and engine_env != active_engine)
    ):
        raise box.BoxError("Private workspace client lost its local engine binding.")
    pinned = active_engine if active_engine is not None else engine_env
    if data.get("devices") and guarded_container and pinned is None:
        raise box.BoxError(
            "Private workspace client lost its local device engine binding."
        )
    # Keep a providerless engine snapshot for worker calls; the client token
    # belongs only to the native subprocess, not to host mount inspection.
    pinned = pinned.copy() if pinned is not None else None
    env = pinned.copy() if pinned is not None else box.maintenance_host_env()
    env["OPENCODE_PASSWORD"] = connection["password"]
    command = [
        "podman",
        *(("--remote=false",) if pinned is not None else ()),
        "exec",
        "-i",
    ]
    # An embedded client has its own local PTY even when the dashboard's
    # Python stdio isn't a TTY (e.g. headless terminal protocol fixtures).
    if force_tty or (interactive and sys.stdin.isatty() and sys.stdout.isatty()):
        command.append("-t")
    command.extend(
        [
            "--workdir",
            box.container_project(data),
            "--env",
            "OPENCODE_PASSWORD",
            guarded_container or data["container"],
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
    preview_host_open = None
    if (
        interactive
        and len(arguments) >= 2
        and arguments[:2] == [box.CONTAINER_OPENCODE, box.container_project(data)]
        and "--server" in arguments
        and arguments[arguments.index("--server") + 1 :]
        and arguments[arguments.index("--server") + 1] == connection["url"]
    ):
        if __package__:
            from . import ui_state
        else:
            import ui_state
        try:
            binding = ui_state._metadata(connection)
        except ui_state.StateError:
            raise box.BoxError("Invalid private workspace UI-state setting.") from None
        if binding is not None:
            # NONSECRET labels, not native identity or caller attestation. Do
            # not overwrite cli.json plugins to supply per-client options.
            names["OC_BOX_UI_STATE_CLIENT"] = (
                binding["ownerClient"] if ui_owner else secrets.token_hex(16)
            )
            names["OC_BOX_UI_STATE_GENERATION"] = binding["generation"]
        if ui_owner and activity_timeline:
            # The panel only needs local TUI capabilities. UI-state reporting
            # may be unavailable even when the native client can load the panel.
            names["OC_BOX_ACTIVITY_TIMELINE"] = "1"
            startup_output.line(
                "oc-box: Activity timeline requested (look for Open activity timeline in Ctrl+P).",
                flush=True,
            )
        if ui_owner and inline_timestamps:
            names["OC_BOX_INLINE_TIMESTAMPS"] = "1"
            startup_output.line(
                "oc-box: Inline timestamps requested (matched transcript rows only).",
                flush=True,
            )
        if file_preview:
            if __package__:
                from . import file_preview as preview, file_preview_host
            else:
                import file_preview as preview
                import file_preview_host
            release = data.get("opencode_version")
            reason = (
                "native-version-unverified"
                if release is None
                else preview.client_incompatibility(release)
            )
            if reason is not None:
                preview.report_unavailable(reason)
            engine_options = {"engine_env": pinned} if pinned is not None else {}
            verified = (
                preview.client_binding(box, data, connection, **engine_options)
                if reason is None
                else None
            )
            if verified is not None:
                # Use the inspected immutable container ID, not a mutable name.
                container, mounts = verified
                if guarded_container and container != guarded_container:
                    raise box.BoxError(
                        "The live file-open client no longer matches the guarded container."
                    )
                reason = file_preview_host.readiness_reason()
                if reason is None:
                    refusals = []
                    sources = preview.host_export_mounts(
                        box, data, container, refusals=refusals, **engine_options
                    )
                    reason = (
                        (refusals[0] if refusals else "mount-inventory-unverified")
                        if sources is None
                        else file_preview_host.readiness_reason(host_mounts=sources)
                    )
                if reason is None:
                    command[-1] = container
                    names[preview.ROOTS] = json.dumps(mounts)

                    def preview_host_open(kind, candidate):
                        sources = preview.host_export_mounts(
                            box, data, container, **engine_options
                        )
                        if sources is None:
                            raise file_preview_host.PreviewError(
                                "host-mounts-unverified",
                                reason="mount-recheck-unverified",
                            )
                        file_preview_host.open_artifact(
                            kind, candidate, host_mounts=sources
                        )

                if preview_host_open is None:
                    preview.report_unavailable(reason)

    if (
        image_preview
        and interactive
        and arguments
        and arguments[0] == box.CONTAINER_OPENCODE
    ):
        # This is deliberately not the host CLI snapshot or saved cli.json.
        # Native UI writes to unrelated preferences must retain their base image
        # values. Additional TUI clients inherit only this owner's boolean.
        names["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps(
            {
                "prompt": {"image_preview": True},
                "session": {"image_preview": True},
            }
        )
    term = os.environ.get("TERM", "xterm-256color")
    names["TERM"] = (
        term
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}", term)
        else "xterm-256color"
    )
    bridge = "import os,sys,json; env=json.loads(sys.argv[1]); env['OPENCODE_PASSWORD']=os.environ['OPENCODE_PASSWORD']; os.execvpe(sys.argv[2],sys.argv[2:],env)"
    if guarded_digest and safeguard:
        arguments = safeguard.bound_command(arguments, guarded_digest)
    interpreter = (
        safeguard.SYSTEM_HELPER
        if guarded_digest and safeguard
        else ("python3", "-I", "-S")
    )
    command.extend([*interpreter, "-c", bridge, json.dumps(names), *arguments])
    result = box.ContainerCommand(command, env)
    if guarded:
        result.guarded_spawn = True
    if preview_host_open is not None:
        result.preview_host_open = preview_host_open
    return result


def tab_inventory(box, data, connection, *, timeout=4, engine_env=None):
    """Read admission metadata through the existing providerless client only."""
    if connection.get("workspace") != data["id"]:
        raise box.BoxError("Native tab inventory does not match the workspace.")
    validate_endpoint(connection)
    command = client_command(
        box,
        data,
        connection,
        [
            "python3",
            "-I",
            "-S",
            "-B",
            "-c",
            (box.PACKAGE / "opencode_v2_io.py").read_text(),
            "sessions",
            box.CONTAINER_OPENCODE,
        ],
        **({"engine_env": engine_env} if engine_env is not None else {}),
    )
    result = box.run(
        command,
        input=json.dumps(
            {
                "directory": box.container_project(data),
                "endpoint": {key: connection[key] for key in ("url", "password")},
            }
        ),
        capture=True,
        private=True,
        timeout=timeout,
    )
    inventory = json.loads(result.stdout)
    if not isinstance(inventory, list):
        raise box.BoxError("Invalid native tab inventory.")
    return inventory


def web_session_catalog(box, data, connection, members, *, timeout=4):
    """Explicit chooser only: same private service, membership + native GET.

    Unlike tab_inventory this permits already-open worktree/moved/archived/child
    IDs. It must not be used by automatic owner selection or history sync.
    """
    if connection.get("workspace") != data["id"]:
        raise box.BoxError("Web session catalog does not match the workspace.")
    validate_endpoint(connection)
    command = client_command(
        box,
        data,
        connection,
        [
            "python3",
            "-I",
            "-S",
            "-B",
            "-c",
            (box.PACKAGE / "opencode_v2_io.py").read_text(),
            "web-sessions",
            box.CONTAINER_OPENCODE,
        ],
    )
    result = box.run(
        command,
        input=json.dumps(
            {
                "members": list(members),
                "endpoint": {key: connection[key] for key in ("url", "password")},
            }
        ),
        capture=True,
        private=True,
        timeout=timeout,
    )
    inventory = json.loads(result.stdout)
    if not isinstance(inventory, list):
        raise box.BoxError("Invalid private web session catalog.")
    return inventory


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


def confirm_guarded_owner(box, data, connection, expected, deadline):
    """A fresh lease frame carries credentials, not the native release."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise box.BoxError("Guarded OpenCode owner readiness expired.")
    info = api(box, data, connection, "GET", "/api/info", timeout=min(remaining, 10))
    if time.monotonic() >= deadline:
        raise box.BoxError("Guarded OpenCode owner readiness expired.")
    if not isinstance(info, dict) or info.get("version") != expected:
        raise box.BoxError(
            "Private owner readiness did not match the guarded OpenCode release."
        )


def wait_history(box, data, connection, *, timeout=30, interval=0.2):
    """Boundedly wait for this private server's drains and job notices to settle.

    Native active sessions cover foreground drains, not background tools. The
    job inventory also retains finished jobs until their completion notices are
    admitted, which can wake foreground execution again. Two quiet pairs avoid
    accepting a single observation across that handoff. This does not certify
    transcript completeness: user shell fibers and unsettled projected messages
    need a separate check in the export path. Persistent PTYs are not activity.
    Timeout diagnostics contain the phase, the last fully validated status/kind
    counts and aggregate timings of existing queries, never native identifiers,
    commands, responses or errors. Timings include native CLI transport overhead;
    they do not distinguish live jobs from retained native records.
    Audited native subagent profiles may route retained shell/subagent jobs to
    a guarded unregistered reader; older/unknown versions retain shell-only hints.
    """
    if __package__:
        from . import history_reader
    else:
        import history_reader

    phase = "foreground-query"
    observation = "unavailable"
    kind_observation = "unavailable"
    last_counts = {}
    profile = history_reader.snapshot_profile(data.get("opencode_version"))
    kinds = history_reader.SNAPSHOT_JOB_KINDS.get(profile, frozenset(("shell",)))
    recognized_kinds = ("shell", "subagent", "image")
    if profile == history_reader.SNAPSHOT_24:
        # Native recognizes these, but the history reader must not resume them.
        recognized_kinds += ("video", "codemode")
    insufficient = (
        "insufficient-job-observations"
        if "subagent" in kinds
        else "insufficient-shell-observations"
    )
    job_pairs = 0
    foreground_pairs = 0
    foreground_observed = False
    compatibility_reason = None
    recovery_reason = insufficient
    complete_pairs = 0
    last_complete = None
    last_query = "unavailable"
    last_query_budget = None
    last_query_outcome = "unavailable"
    queries = {
        name: {"count": 0, "total": 0.0, "max": 0.0}
        for name in ("foreground-query", "background-query")
    }

    def timing_diagnostic():
        def count(value):
            return str(value) if value < 10000 else "9999+"

        def duration(value):
            if value is None:
                return "unavailable"
            return f"{max(0, value):.3f}s" if value < 10000 else "9999+s"

        summary = "; ".join(
            f"{name}-queries={count(query['count'])}, "
            f"total={duration(query['total'])}, max={duration(query['max'])}"
            for name in ("foreground", "background")
            for query in (queries[name + "-query"],)
        )
        age = None if last_complete is None else time.monotonic() - last_complete
        metrics = {
            "complete_pairs": min(complete_pairs, 9999),
            **last_counts,
        }
        for name in ("foreground", "background"):
            query = queries[name + "-query"]
            metrics[name + "_queries"] = min(query["count"], 9999)
            for aggregate in ("total", "max"):
                metrics[name + "_" + aggregate + "_ms"] = int(
                    max(0, min(query[aggregate], 9999)) * 1000
                )
        for name, value in (
            ("remaining_ms", last_query_budget),
            ("last_complete_age_ms", age),
        ):
            if value is not None:
                metrics[name] = int(max(0, min(value, 9999)) * 1000)
        lifecycle_output.record(
            "history.wait",
            level="debug",
            stage=phase,
            reason="timeout",
            outcome="failure",
            metrics=metrics,
        )
        return (
            f"History wait timing: complete-pairs={count(complete_pairs)}; {summary}; "
            f"last-query={last_query}, remaining-at-start={duration(last_query_budget)}, "
            f"outcome={last_query_outcome}; last-complete-age={duration(age)}."
        )

    def record_query(started, outcome):
        nonlocal last_query_outcome
        elapsed = max(0, time.monotonic() - started)
        query = queries[last_query]
        query["count"] += 1
        query["total"] += elapsed
        query["max"] = max(query["max"], elapsed)
        last_query_outcome = outcome

    def fail(category):
        if category == "activity-timeout":
            diagnostic = (
                f"History wait timeout: phase={phase}; "
                f"last complete job kinds: {kind_observation}; "
                f"last complete observation: {observation}."
            )
            timing = timing_diagnostic()
            # These strings contain only the fixed query phases and validated
            # counts above, never a native payload or an exception's text.
            lifecycle_output.fixed_detail("debug", diagnostic)
            lifecycle_output.fixed_detail("debug", timing)
            error = box.HistoryWaitTimeout(
                diagnostic,
                recovery_eligible=job_pairs >= 2 or foreground_pairs >= 2,
                recovery_reason=recovery_reason,
                timing_diagnostic=timing,
                interrupted_foreground=foreground_observed,
            )
            # Keep the deadline and routing diagnostics; a native response
            # decode failure is still a known compatibility refusal.
            if compatibility_reason is not None:
                error.history_compatibility_category = compatibility_reason
            raise error from None
        error = box.BoxError(f"OpenCode 2.x history wait failed ({category}).")
        error.interrupted_foreground = foreground_observed
        error.history_retryable = (
            category == "activity-api-failed" and compatibility_reason is None
        )
        if category in ("invalid-active-response", "invalid-jobs-response"):
            error.history_compatibility_category = category
        elif compatibility_reason is not None:
            error.history_compatibility_category = compatibility_reason
        lifecycle_output.record(
            "history.wait",
            level="debug",
            stage=phase,
            reason=(
                "schema"
                if getattr(error, "history_compatibility_category", None) is not None
                else "transport"
                if category == "activity-api-failed"
                else "refused"
            ),
            outcome="failure",
        )
        raise error from None

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

    def active_items(response):
        value = response.get("data") if isinstance(response, dict) else None
        if not isinstance(value, dict) or any(
            not box.valid_session_id(ident)
            or not isinstance(state, dict)
            or state.get("type") != "running"
            for ident, state in value.items()
        ):
            return None
        return value

    def job_items(response):
        value = response.get("data") if isinstance(response, dict) else None
        if not isinstance(value, list) or any(
            not isinstance(job, dict)
            or not isinstance(job.get("id"), str)
            or not job["id"]
            or not box.valid_session_id(job.get("sessionID"))
            or job.get("kind") not in recognized_kinds
            or job.get("status") not in ("running", "completed", "error", "cancelled")
            for job in value
        ):
            return None
        return value

    def request(target):
        nonlocal phase, job_pairs, foreground_pairs, recovery_reason
        nonlocal last_query, last_query_budget
        nonlocal foreground_observed, compatibility_reason
        budget = remaining()
        phase = (
            "foreground-query"
            if target == "/api/session/active"
            else "background-query"
        )
        last_query, last_query_budget = phase, budget
        started = time.monotonic()
        try:
            result = api(
                box,
                data,
                connection,
                "GET",
                target,
                timeout=min(60, budget),
            )
        except (box.BoxError, OSError, ValueError, subprocess.SubprocessError) as error:
            # Never include native errors: they can contain commands, provider
            # values, or private response bodies. Preserve the legacy public
            # deadline category, but a known non-timeout failure invalidates the
            # optional snapshot hint even when the clock also expired. run()
            # retains subprocess.TimeoutExpired as the private BoxError cause.
            timed_out = isinstance(error, subprocess.TimeoutExpired) or isinstance(
                error.__cause__, subprocess.TimeoutExpired
            )
            record_query(started, "subprocess-timeout" if timed_out else "failed")
            if isinstance(error, ValueError):
                compatibility_reason = (
                    "invalid-active-response"
                    if target == "/api/session/active"
                    else "invalid-jobs-response"
                )
            if not timed_out:
                job_pairs = 0
                foreground_pairs = 0
                recovery_reason = "activity-api-failed"
            remaining()
            fail("activity-api-failed")
        record_query(started, "returned")
        value = (
            active_items(result)
            if target == "/api/session/active"
            else job_items(result)
        )
        if target == "/api/session/active" and value is not None:
            # Keep execution outcome separate from authorization counters. A
            # later API error cannot prove previously observed work completed.
            foreground_observed = bool(value)
        if value is None or (
            not value
            if target == "/api/session/active"
            else any(job["kind"] not in kinds for job in value)
        ):
            # A foreground-interruption hint also needs consecutive complete
            # observations, and must not survive contradictory partial replies.
            foreground_pairs = 0
        if value is None or (
            bool(value)
            if target == "/api/session/active"
            else not value or any(job["kind"] not in kinds for job in value)
        ):
            # A late successful response can contradict the last complete pair.
            # Validate/reset BEFORE checking the deadline; it must never leave a
            # stale routing hint behind. A real request timeout cannot manufacture
            # a qualifying pair, but does not erase prior validated observations.
            job_pairs = 0
            if value is None:
                recovery_reason = (
                    "invalid-active-response"
                    if target == "/api/session/active"
                    else "invalid-jobs-response"
                )
            elif target == "/api/session/active":
                recovery_reason = "foreground-active"
            else:
                recovery_reason = "jobs-empty" if not value else "non-shell-jobs"
        remaining()
        return value

    quiet = 0
    while True:
        active = request("/api/session/active")
        if active is None:
            fail("invalid-active-response")

        jobs = request("/api/experimental/job")
        if jobs is None:
            fail("invalid-jobs-response")
        # A terminal job can still have an unconsumed completion notice. Native
        # reload can also retain cancelled shell jobs indefinitely. Neither age
        # nor status proves it safe to ignore one: report counts, do not waive it.
        counts = {"foreground": len(active)}
        for status in ("running", "completed", "error", "cancelled"):
            counts["jobs-" + status] = sum(job["status"] == status for job in jobs)
        observation = ", ".join(
            f"{name}={value if value < 10000 else '9999+'}"
            for name, value in counts.items()
        )
        kind_counts = {
            kind: sum(job["kind"] == kind for job in jobs) for kind in recognized_kinds
        }
        kind_observation = ", ".join(
            f"{kind}={count if count < 10000 else '9999+'}"
            for kind, count in kind_counts.items()
        )
        last_counts = {
            name.replace("-", "_"): min(count, 9999)
            for name, count in (counts | kind_counts).items()
        }
        complete_pairs += 1
        last_complete = time.monotonic()
        # This requests only a post-STOP snapshot, never a live readiness waiver.
        # Durable shell rows can hydrate as running on the next owner. The
        # ordinary timeout already stops genuine pending work too; recovery must
        # preserve that failure/interruption result, irrespective of job status.
        job_pairs = (
            job_pairs + 1
            if not active and jobs and all(job["kind"] in kinds for job in jobs)
            else 0
        )
        foreground_pairs = (
            foreground_pairs + 1
            if profile in history_reader.SUBAGENT_SNAPSHOT_PROFILES
            and active
            and all(job["kind"] in kinds for job in jobs)
            else 0
        )
        if job_pairs:
            recovery_reason = insufficient
        quiet = quiet + 1 if not active and not jobs else 0
        if quiet == 2:
            return
        phase = "poll-delay"
        time.sleep(min(interval, remaining()))


class Owner:
    def __init__(
        self,
        box,
        path,
        data,
        *,
        image_preview=False,
        ui_bridge=False,
        named_stop_token=None,
        guest_stop_token=None,
    ):
        if type(image_preview) is not bool:
            raise box.BoxError("Invalid private workspace image-preview setting.")
        self.box, self.path, self.data = box, path, data
        self.image_preview = image_preview
        self.ui_bridge = ui_bridge is True
        self.address = socket_path(box, data)
        self.clients = 0
        self.condition = threading.Condition()
        self.ready = False
        self.closing = False
        self.session = None
        self.child = None
        self.listener = None
        self.bound = False
        self._named_stop_token = named_stop_token
        self._named_control = None
        self._guest_stop_token = guest_stop_token
        self._guest_proof = None
        self._engine_env = None

    def __enter__(self):
        box = self.box
        # The caller owns the lifecycle lock. A refused connection is stale;
        # a live controller must never be replaced by a new server.
        named = self._named_stop_token is not None
        guest = self._guest_stop_token is not None
        safeguard = getattr(box, "runtime_safeguard", None)
        guarded = safeguard.current() if safeguard else None
        pinned = getattr(guarded, "engine_env", None)
        # Request threads do not inherit Admission. Keep only the verified
        # Podman environment on this owner, never in the lease reply.
        self._engine_env = pinned.copy() if pinned is not None else None
        if named and guarded:
            # These startup envelopes were verified separately. Do not waive
            # either exact readiness check to let one masquerade as the other.
            raise box.BoxError(
                "Named HTTPS cannot use the guarded workspace owner contract yet."
            )
        if guest and (named or guarded or self._guest_proof is not None):
            raise box.BoxError("This workspace owner cannot supply Studio guest proof.")
        live = live_connection()
        if guest and (
            self.address.exists()
            or self.address.is_symlink()
            or (live is not None and live.get("workspace") == self.data["id"])
        ):
            raise box.BoxError(
                "Studio guest proof cannot replace a workspace controller."
            )
        if (named and (self.address.exists() or self.address.is_symlink())) or (
            live is not None
            and live.get("workspace") == self.data["id"]
            and (named or live.get("web_mode") == _NAMED_ONLY)
        ):
            raise box.BoxError(
                "Named workspace mode cannot replace an existing controller."
            )
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
        execution_data = self.data
        worker = ["python3", "-c", script, "lease", box.CONTAINER_OPENCODE]
        if named:
            if __package__:
                from . import web_named_protocol
            else:
                import web_named_protocol
            try:
                if self.bound or self._named_control is not None:
                    raise web_named_protocol.OwnerLeaseError()
                self._named_control = web_named_protocol.NamedControl(
                    self, self._named_stop_token
                )
                execution_data = self._named_control.pinned_data()
                worker = self._named_control.worker_arguments(
                    script, box.CONTAINER_OPENCODE
                )
            except (
                box.BoxError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                RecursionError,
            ):
                raise box.BoxError(
                    "Named workspace owner could not be verified."
                ) from None
        if guest:
            if __package__:
                from . import web_studio_guest_owner, workspace_control
            else:
                import web_studio_guest_owner
                import workspace_control
            try:
                self._guest_proof = web_studio_guest_owner.GuestOwnerProof(
                    self, self._guest_stop_token
                )
                command = self._guest_proof.worker_command(script)
            except (
                box.BoxError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                subprocess.SubprocessError,
                RecursionError,
            ):
                raise box.BoxError(
                    "Studio guest original workspace owner could not be verified."
                ) from None
        else:
            command = box.exec_command(execution_data, worker)
        self.errors = box.captured_output(private=True)
        self.error_file = self.errors.__enter__()
        try:
            with (
                workspace_control.admission(self.path)
                if guest
                else guarded.spawn(box)
                if guarded
                else contextlib.nullcontext()
            ):
                if guest:
                    self._guest_proof.before_spawn()
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
                    chunk = os.read(self.child.stdout.fileno(), 4096)
                    if guest and not chunk:
                        raise box.BoxError(
                            "Studio guest original workspace owner could not be verified."
                        )
                    output.extend(chunk)
                if len(output) > (4096 if guest else MAX_CONTROL):
                    raise box.BoxError(
                        "Invalid private workspace service readiness response."
                    )
            if named:
                try:
                    if not output.endswith(b"\n") or output.count(b"\n") != 1:
                        raise web_named_protocol.OwnerLeaseError()
                    self.connection = validate_endpoint(
                        json.loads(output, object_pairs_hook=web_named_protocol._unique)
                    )
                    self._named_control.seal(self.connection)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    RecursionError,
                ):
                    raise box.BoxError(
                        "Named workspace owner could not be verified."
                    ) from None
                self.connection["web_mode"] = _NAMED_ONLY
            elif guest:
                try:
                    if time.monotonic() >= deadline:
                        raise web_studio_guest_owner.GuestOwnerError()
                    self.connection = self._guest_proof.seal(bytes(output))
                    if time.monotonic() >= deadline:
                        raise web_studio_guest_owner.GuestOwnerError()
                except (
                    box.BoxError,
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    subprocess.SubprocessError,
                    RecursionError,
                ):
                    raise box.BoxError(
                        "Studio guest original workspace owner could not be verified."
                    ) from None
            else:
                self.connection = validate_endpoint(
                    json.loads(output.split(b"\n", 1)[0])
                )
            if guarded:
                confirm_guarded_owner(
                    box, self.data, self.connection, guarded.record.version, deadline
                )
            self.connection["workspace"] = self.data["id"]
            self.connection["image_preview"] = self.image_preview
            if guarded:
                self.connection["_runtime_container"] = guarded.record.container
                self.connection["_runtime_digest"] = guarded.record.digest
            if __package__:
                from . import ui_state
            else:
                import ui_state
            self.connection["ui_state"] = (
                {
                    "available": True,
                    "generation": secrets.token_hex(16),
                    "ownerClient": secrets.token_hex(16),
                }
                if self.ui_bridge
                and ui_state.available(box, self.data, self.connection)
                else {"available": False}
            )
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
        if session is not None and not self.box.valid_session_id(session):
            raise self.box.BoxError("Invalid live workspace session ID.")
        safeguard = getattr(self.box, "runtime_safeguard", None)
        guarded = safeguard.current() if safeguard else None
        if self._guest_proof is not None:
            try:
                self._guest_proof.before_publish()
            except (
                self.box.BoxError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                subprocess.SubprocessError,
                RecursionError,
            ):
                raise self.box.BoxError(
                    "Studio guest original workspace owner could not be verified."
                ) from None
        with (
            guarded.spawn(self.box) if guarded else contextlib.nullcontext(),
            self.condition,
        ):
            self.session, self.ready = session, True
            if guarded:
                guarded.launching = False

    def default_session(self):
        """Recheck persisted open membership without inventing an active tab."""
        if __package__:
            from . import history_recovery, native_tabs
        else:
            import history_recovery
            import native_tabs

        with self.condition:
            previous = self.session
        if self.data.get("opencode_version") not in native_tabs.AUTO_RESUME_RELEASES:
            return previous
        try:
            engine_options = (
                {"engine_env": self._engine_env} if self._engine_env is not None else {}
            )
            with history_recovery.budget(4):
                snapshot = native_tabs.inspect(
                    self.box, self.data, connection=self.connection, **engine_options
                )
                if snapshot.status == "invalid" or (
                    snapshot.status == "valid" and not snapshot.tabs
                ):
                    return None
                inventory = tab_inventory(
                    self.box, self.data, self.connection, **engine_options
                )
                return native_tabs.revalidate(
                    snapshot,
                    inventory,
                    previous,
                    directory=self.box.container_project(self.data),
                )
        except (
            self.box.BoxError,
            native_tabs.TabError,
            history_recovery.RecoveryError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ):
            return None

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
                if self._named_control is not None:
                    # Indistinguishable legacy attach/info clients cannot
                    # receive the private credential from a named-only owner.
                    self._named_control.serve(stream)
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
                    # Even an info refresh owns a temporary lease across its
                    # metadata I/O. Only attach retains it after the reply.
                    self.clients += 1
                    held = True
                selected = self.default_session()
                with self.condition:
                    if self.closing or self.child.poll() is not None:
                        stream.sendall(
                            b'{"error":"Workspace service is closing; retry after startup."}\n'
                        )
                        return
                    self.session = selected
                stream.sendall(
                    (
                        json.dumps(
                            {
                                **self.connection,
                                "session": selected,
                                "version": self.data["opencode_version"],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                if request["action"] == "attach":
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
                if lifecycle_output.concise():
                    lifecycle_output.record(
                        "lifecycle.notice",
                        level="info",
                        stage="shutdown",
                        reason="holder-active",
                    )
                    startup_output.line(
                        "oc-box: Waiting for attached clients. Close them to finish shutdown, or use 'oc-box stop'.",
                        flush=True,
                    )
                else:
                    startup_output.line(
                        "oc-box: Attached web/terminal/file-sharing clients are still active; waiting before history sync and container stop. Close those clients or use 'oc-box stop'.",
                        flush=True,
                    )
            while self.clients and self.child.poll() is None:
                self.condition.wait(timeout=0.2)
            self.closing = True

    def shutdown(self):
        self.closing = True
        guest_retired = True
        if self._guest_proof is not None:
            self._guest_proof.retire()
        if self.listener:
            self.listener.close()
            if hasattr(self, "thread"):
                self.thread.join(timeout=2)
            if self.bound:
                self.address.unlink(missing_ok=True)
        if self.child:
            if self._guest_proof is not None:
                guest_retired = False
                try:
                    self._guest_proof.retire_worker(self.child)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                else:
                    guest_retired = True
            else:
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
        if not guest_retired:
            raise self.box.BoxError(
                "Studio guest native owner retirement could not be verified."
            )

    def __exit__(self, *_):
        self.shutdown()


@dataclass(frozen=True, slots=True, repr=False)
class AttachedControl:
    """Trusted borrower view of one same-user attach, not owner attestation."""

    connection: dict
    stream: socket.socket
    peer_pid: int


@contextlib.contextmanager
def _attach_control(box, path, data, *, named=None):
    """Hold the original connected stream, never acquire a second lease.

    Borrowers must not close it, read additional replies, or treat a same-user
    PID and connected descriptor as proof of the native owner's generation.
    """
    safeguard = getattr(box, "runtime_safeguard", None)
    engine_env = None
    if named is None:
        if safeguard and data.get("devices"):
            engine_env = safeguard.client_engine(box, path, data)
        validate_workspace(
            box,
            path,
            data,
            **({"engine_env": engine_env} if engine_env is not None else {}),
        )
    stream = socket.socket(socket.AF_UNIX)
    try:
        stream.settimeout(5)
        stream.connect(
            str(socket_path(box, data) if named is None else named.owner.address)
        )
        peer_pid, uid, _ = struct.unpack(
            "3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != os.getuid():
            raise box.BoxError("Private workspace controller belongs to another user.")
        if named is not None:
            connection = named.exchange(stream, peer_pid)
        else:
            connection = _legacy_attachment(box, data, stream)
            if safeguard:
                engine_env = safeguard.bind_client(
                    box,
                    path,
                    data,
                    connection,
                    **({"engine_env": engine_env} if engine_env is not None else {}),
                )
    except (OSError, ValueError) as exc:
        stream.close()
        if named is not None:
            raise
        raise box.BoxError(
            "No ready live OpenCode 2.x workspace service. Start oc-box or 'oc-box web' first; no credentials were refreshed."
        ) from exc
    except BaseException:
        stream.close()
        raise
    try:
        with _device_engine_scope(
            data, connection.get("_runtime_container"), engine_env
        ):
            yield AttachedControl(connection, stream, peer_pid)
    finally:
        stream.close()


def _legacy_attachment(box, data, stream):
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
        or "session" not in connection
        or (
            connection["session"] is not None
            and not box.valid_session_id(connection["session"])
        )
    ):
        raise box.BoxError(
            "Live service does not match this workspace/version/session."
        )
    return connection


@contextlib.contextmanager
def attach(box, path, data):
    with _attach_control(box, path, data) as attached:
        yield attached.connection


def relay_command(box, data, port, *, engine_env=None, container=None):
    return box.ContainerCommand(
        [
            "podman",
            *(("--remote=false",) if engine_env is not None else ()),
            "exec",
            "-i",
            container or data["container"],
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "python3",
            "-c",
            PROXY,
            str(port),
        ],
        engine_env.copy() if engine_env is not None else box.maintenance_host_env(),
    )


class NativeResponseHeaders:
    """Bounded initial HTTP headers, then opaque streaming bytes.

    Gateway classifies only the first request on a socket. Final non-upgrade
    replies therefore forbid browser connection reuse, including after pairing.
    This is not body framing, pipelining support or a per-request dispatcher.
    """

    LIMIT = 65536

    def __init__(self, pairing_route=None):
        self.pending = bytearray()
        self.header_bytes = 0
        self.complete = False
        self.pairing_route = pairing_route

    def feed(self, data):
        if self.complete:
            return data
        # Production feeds at most one 64-KiB pipe read. Body bytes sharing the
        # final header read are immediately emitted, never accumulated further.
        self.pending.extend(data)
        output = bytearray()
        while not self.complete:
            boundary = self.pending.find(b"\r\n\r\n")
            if boundary < 0:
                if self.header_bytes + len(self.pending) > self.LIMIT:
                    raise ValueError("Native HTTP response headers exceed the limit.")
                break
            boundary += 4
            self.header_bytes += boundary
            if self.header_bytes > self.LIMIT:
                raise ValueError("Native HTTP response headers exceed the limit.")
            head = bytes(self.pending[:boundary])
            del self.pending[:boundary]
            fields = head[:-4].split(b"\r\n")
            status = re.fullmatch(
                rb"HTTP/1\.[01] ([1-9][0-9]{2})(?: [^\r\n]*)?", fields[0]
            )
            if status is None:
                raise ValueError("Invalid native HTTP response status.")
            status = int(status[1])
            if 100 <= status < 200:
                output.extend(head)
                self.complete = status == 101
            else:
                rewritten = [fields[0]]
                paired = False
                for field in fields[1:]:
                    name, separator, value = field.partition(b":")
                    if not separator:
                        raise ValueError("Invalid native HTTP response header.")
                    name = name.lower()
                    if name in (b"connection", b"keep-alive"):
                        continue
                    if (
                        self.pairing_route is not None
                        and not paired
                        and name == b"location"
                        and value.strip() == b"/"
                    ):
                        field = b"Location: " + self.pairing_route().encode()
                        paired = True
                    rewritten.append(field)
                rewritten.append(b"Connection: close")
                output.extend(b"\r\n".join(rewritten) + b"\r\n\r\n")
                self.complete = True
            if self.complete:
                output.extend(self.pending)
                self.pending.clear()
        return bytes(output)


class _GatewayListenerError(ValueError):
    def __init__(self):
        super().__init__(
            "The original loopback gateway listener could not be verified."
        )


def _gateway_listener_snapshot(listener, *, accepting):
    """Read kernel facts about this exact TCP socket, not a reported URL."""
    if type(listener) is not socket.socket:
        raise _GatewayListenerError()
    identity = _gateway_socket_identity(listener)
    if (
        listener.get_inheritable()
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN) != socket.AF_INET
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_PROTOCOL)
        != socket.IPPROTO_TCP
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT)
        or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        != int(accepting)
    ):
        raise _GatewayListenerError()
    host, port = listener.getsockname()
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise _GatewayListenerError()
    try:
        listener.getpeername()
    except OSError as error:
        if error.errno != errno.ENOTCONN:
            raise
    else:
        raise _GatewayListenerError()
    if _gateway_socket_identity(listener) != identity:
        raise _GatewayListenerError()
    return identity, (host, port)


def _gateway_socket_identity(listener):
    fd = listener.fileno()
    info = os.fstat(fd)
    if not stat.S_ISSOCK(info.st_mode):
        raise _GatewayListenerError()
    return fd, info.st_dev, info.st_ino


class _GatewayListenerProof:
    """Listener evidence only; never an owner lease or worker drain verdict."""

    def __init__(self, gateway, *, probe_only, managed=False):
        if gateway.closed.is_set() or gateway._listener_proof is not None:
            raise _GatewayListenerError()
        self._gateway = gateway
        self._listener = gateway.listener
        self._identity, self._address = _gateway_listener_snapshot(
            self._listener, accepting=False
        )
        self._lock = threading.Lock()
        self._probe_only = probe_only
        self._managed = managed
        self._managed_binding = None
        self._listening = False
        self._retired = False

    def _matches(self, gateway, *, accepting):
        if gateway.closed.is_set() or gateway.listener is not self._listener:
            return False
        try:
            return (
                _gateway_listener_snapshot(self._listener, accepting=accepting)
                == (self._identity, self._address)
                and not gateway.closed.is_set()
                and gateway.listener is self._listener
            )
        except (AttributeError, OSError, ValueError):
            return False

    def _mark_listening(self, gateway):
        # Called by the foreground startup path, after its own listen() and
        # before starting the accept thread. An early external listen fails the
        # separate unserved check before the transition is attempted.
        with self._lock:
            if (
                gateway is not self._gateway
                or self._probe_only
                or self._retired
                or self._listening
                or not self._matches(gateway, accepting=True)
            ):
                raise _GatewayListenerError()
            self._listening = True

    def origin(self, gateway, *, listening):
        """Return the pinned origin for this phase, or None; never rebind."""
        if (
            gateway is not self._gateway
            or gateway._listener_proof is not self
            or type(listening) is not bool
        ):
            return None
        with self._lock:
            if self._retired:
                return None
            if not self._matches(gateway, accepting=self._listening):
                self._retired = True
                return None
            if listening != self._listening:
                return None
            host, port = self._address
            return f"http://{host}:{port}"

    def is_unserved_probe(self, gateway):
        return (
            gateway is self._gateway
            and self._probe_only
            and self.origin(gateway, listening=False) is not None
        )

    def _close_original(self):
        # Never close a different socket that replaced the descriptor.
        # This confirms only the local close, not acceptor or worker drain.
        with self._lock:
            self._retired = True
            if self._listener.fileno() < 0:
                return False
            try:
                original = _gateway_socket_identity(self._listener) == self._identity
            except (OSError, ValueError):
                original = False
            if original:
                self._listener.close()
                return self._listener.fileno() < 0
            # Fail closed without leaving a Python finalizer that could
            # later close a descriptor now owned by a different socket.
            self._listener.detach()
            return False


class Gateway:
    # Trusted first-party extension hook; never populated by browser input.
    shell_addon_scripts = ()

    def __init__(
        self,
        box,
        data,
        connection,
        port=0,
        *,
        attachments=False,
        shell_addon_scripts=None,
        selection_snapshot_reader=None,
    ):
        self.box, self.data, self.connection, self.port = box, data, connection, port
        self.attachments = attachments
        if shell_addon_scripts is not None:
            self.shell_addon_scripts = shell_addon_scripts
        self.selection_snapshot_reader = selection_snapshot_reader
        self.closed = threading.Event()
        self.children = set()
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(32)
        self.listener = None
        self._listener_proof = None
        self._device_relay = {}
        container = (
            connection.get("_runtime_container")
            if isinstance(connection, dict)
            else None
        )
        safeguard = getattr(box, "runtime_safeguard", None)
        guarded = safeguard.current() if safeguard and container is not None else None
        environment = getattr(guarded, "engine_env", None)
        if container is not None and (
            environment is not None or (isinstance(data, dict) and data.get("devices"))
        ):
            if environment is None:
                environment = _borrowed_device_engine(data, container)
            if environment is None:
                raise box.BoxError(
                    "Private workspace relay lost its local engine binding."
                )
            self._device_relay = {
                "engine_env": environment.copy(),
                "container": container,
            }

    def _reserve_listener(self, *, probe_only=False, managed=False):
        """Bind the actual gateway socket, not the disposable port preflight."""
        if self.listener is not None or self.closed.is_set():
            raise _GatewayListenerError()
        listener = socket.socket()
        try:
            self.listener = listener
            listener.bind(("127.0.0.1", self.port))
            try:
                self._listener_proof = _GatewayListenerProof(
                    self, probe_only=probe_only, managed=managed
                )
            except (AttributeError, OSError, ValueError):
                raise _GatewayListenerError() from None
        except BaseException:
            listener.close()
            raise
        return self._listener_proof

    @contextlib.contextmanager
    def _listener_preflight(self):
        """Borrow the real reservation for a private, unserved-only check."""
        proof = self._reserve_listener(probe_only=True)
        try:
            yield proof
        finally:
            self.closed.set()
            proof._close_original()

    def _start_listener(self, proof):
        """Activate that same reservation; retain the legacy accept behavior."""
        if (
            type(proof) is not _GatewayListenerProof
            or proof is not self._listener_proof
            or proof._probe_only
            or proof._managed
        ):
            raise _GatewayListenerError()
        listener = proof._listener
        try:
            if proof.origin(self, listening=False) is None:
                raise _GatewayListenerError()
            listener.listen(32)
            listener.settimeout(0.2)
            proof._mark_listening(self)
            self.url = proof.origin(self, listening=True)
            if self.url is None:
                raise _GatewayListenerError()
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.closed.set()
            proof._close_original()
            raise

    def __enter__(self):
        self._start_listener(self._reserve_listener())
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
        with (
            _device_engine_scope(
                self.data,
                self._device_relay.get("container"),
                self._device_relay.get("engine_env"),
            ),
            stream,
        ):
            try:
                port = urlparse(self.connection["url"]).port
                command = relay_command(self.box, self.data, port, **self._device_relay)
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
                if self.attachments:
                    if __package__:
                        from . import web_attachments
                    else:
                        import web_attachments
                    if web_attachments.matches(headers):
                        web_attachments.serve(stream, headers, child, self.url)
                        return
                if __package__:
                    from . import web_sessions
                else:
                    import web_sessions
                if web_sessions.respond(self, stream, child, bytes(headers)):
                    return
                response = NativeResponseHeaders(
                    self.session_route
                    if bytes(headers).startswith(b"GET /auth/connect/")
                    else None
                )
                child.stdin.write(headers)
                child.stdin.flush()
                stream.settimeout(None)
                readers = [stream, child.stdout]
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
                            stream.sendall(response.feed(data))
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
                            # A peer can close during browser navigation while
                            # stdin still holds buffered bytes. A failed flush
                            # on close must not leak a connection slot.
                            with contextlib.suppress(BrokenPipeError):
                                pipe.close()
                    with self.lock:
                        self.children.discard(child)
                self.capacity.release()

    def session_route(self):
        # Native browser tabs are client-local, not the native TUI's open tabs.
        if __package__:
            from . import web_sessions
        else:
            import web_sessions
        return web_sessions.INDEX

    def native_session_route(self, ident):
        """A selected native row, never the default chooser/entry route."""
        if not self.box.valid_session_id(ident):
            raise self.box.BoxError("Invalid native web session ID.")
        if __package__:
            from . import web_sessions
        else:
            import web_sessions
        return web_sessions.native_session_route(self.url, ident)

    def __exit__(self, *_):
        self.closed.set()
        self._listener_proof._close_original()
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
    live = live_connection()
    if connection.get("web_mode") == _NAMED_ONLY or (
        live is not None
        and live.get("workspace") == data["id"]
        and live.get("web_mode") == _NAMED_ONLY
    ):
        raise box.BoxError(
            "The one-use link is unavailable on a named-only workspace owner."
        )
    if connection.get("_runtime_container"):
        data = {**data, "container": connection["_runtime_container"]}
    if __package__:
        from . import ui_state
    else:
        import ui_state
    options = {"selection_snapshot_reader": ui_state.read_snapshot}
    if getattr(args, "attachments", False):
        if __package__:
            from . import web_attachments
        else:
            import web_attachments
        version = connection.get("version")
        if not web_attachments.supports_release(version):
            raise box.BoxError(web_attachments.UNVERIFIED_RELEASE)
        info = api(box, data, connection, "GET", "/api/info")
        if not isinstance(info, dict) or info.get("version") != version:
            raise box.BoxError(
                "Browser attachment adapter native version verification failed."
            )
        options["attachments"] = True
        options["shell_addon_scripts"] = (web_attachments.SCRIPT,)
    override = getattr(args, "web_session", None)
    if override is not None:
        if __package__:
            from . import web_sessions
        else:
            import web_sessions
        web_sessions.explicit_session(box, data, connection, override)
        connection = {**connection, "web_session_override": override}
    with Gateway(box, data, connection, args.port, **options) as gateway:
        pair = api(box, data, connection, "POST", "/api/pair")
        if not isinstance(pair.get("code"), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", pair["code"]
        ):
            raise box.BoxError("Invalid native OpenCode browser pairing response.")
        link = gateway.url + "/auth/connect/" + pair["code"]
        print(
            "oc-box: OpenCode web workspace Studio\n"
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
    if __package__:
        from .log_config import add_launch_arguments
    else:
        from log_config import add_launch_arguments

    result = argparse.ArgumentParser(
        prog="oc-box web",
        description="Open Studio around the native web UI on the private V2 workspace service; attach to a running session or own a new foreground lease.",
        epilog=(
            "Run on the host with OpenCode 2.x. Keep the command open while using "
            "the loopback browser relay. Sign in with the native one-use link."
        ),
    )
    group = result.add_mutually_exclusive_group()
    group.add_argument(
        "--project",
        type=Path,
        help="host project directory (default: current directory)",
    )
    group.add_argument("--workspace", metavar="ID", help="existing workspace ID")
    result.add_argument(
        "--session",
        metavar="ID",
        help="explicit existing private-service conversation ID; overrides the launcher selection",
    )
    result.add_argument(
        "--port",
        type=int,
        metavar="N",
        default=0,
        help="host loopback browser relay port, 1..65535; 0 (default) selects a free port",
    )
    result.add_argument(
        "--no-open",
        action="store_true",
        help="print the pairing link without opening a browser; keep the relay running",
    )
    result.add_argument(
        "--attachments",
        action="store_true",
        help="opt in to browser File attachments on tested native releases only; no automatic Send",
    )
    # Keep scripts using the previous explicit opt-in working. There is only
    # one public browser shell now; this compatibility flag selects nothing.
    result.add_argument(
        "--workspace-layout",
        action="store_true",
        help="compatibility flag; Studio is now the only browser workspace layout on launch",
    )
    result.add_argument(
        "--no-vault",
        action="store_true",
        help="owning V2 launch only: skip vault use once; refused when attaching",
    )
    add_launch_arguments(result)
    return result
