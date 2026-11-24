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


class HistorySource:
    """Pass-local unregistered quiet source, never the post-stop Reader.

    The caller must retain its normal exclusive workspace lifecycle lock.
    Unsupported/incompatible sources opt out *before* native service creation.
    Startup/cleanup uncertainty is fatal, never an instruction to replay sync.
    """

    def __init__(self, box, data):
        self.box, self.data = box, dict(data)
        self.child = self.errors = self.error_file = self.token = None
        self.connection = None
        self.started = False
        self.fallback = False

    def fail(self, category):
        return self.box.BoxError(
            "Private history source lease failed (" + category + ")."
        )

    def command(self):
        if __package__:
            from . import history_reader
        else:
            import history_reader

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
                (self.box.PACKAGE / "opencode_v2_io.py").read_text(),
                "history-source",
                self.box.CONTAINER_OPENCODE,
            ],
            self.box.maintenance_host_env(),
        )

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

    def __enter__(self):
        if __package__:
            from . import history_reader, opencode_v2_io as bridge
        else:
            import history_reader
            import opencode_v2_io as bridge

        if self.started:
            raise self.fail("reuse")
        self.started = True
        try:
            if (
                not isinstance(self.data.get("id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", self.data["id"])
                or not isinstance(self.data.get("container"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.data["container"]
                )
                or not isinstance(self.data.get("home"), str)
                or not self.data["home"].startswith("/")
                or os.path.normpath(self.data["home"]) != self.data["home"]
                or "\0" in self.data["home"]
            ):
                raise self.fail("source")
            payload = (
                json.dumps(
                    {
                        "guards": (self.box.PACKAGE / "history_reader.py").read_text(),
                        "version": self.data["opencode_version"],
                        "home": self.data["home"],
                    }
                ).encode()
                + b"\n"
            )
            if len(payload) > bridge.HISTORY_SOURCE_MAX_PAYLOAD:
                raise self.fail("payload")
            command = self.command()
            self.errors = self.box.captured_output(private=True)
            self.error_file = self.errors.__enter__()
            self.child = subprocess.Popen(
                command,
                env=command.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.error_file,
                start_new_session=True,
            )
            # Child startup alarm is 45s, leaving time for its native cleanup
            # before this wrapper's independent hard deadline.
            deadline = time.monotonic() + 60
            self._write(payload, deadline)
            output = bytearray()
            while b"\n" not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self.fail("startup-timeout")
                if select.select([self.child.stdout], [], [], min(0.1, remaining))[0]:
                    chunk = os.read(
                        self.child.stdout.fileno(), MAX_CONTROL + 1 - len(output)
                    )
                    if not chunk:
                        self.child.wait(timeout=min(1, remaining))
                        raise self.fail(self._category())
                    output.extend(chunk)
                elif self.child.poll() is not None:
                    raise self.fail(self._category())
                if len(output) > MAX_CONTROL:
                    raise self.fail("startup-response")
            line, trailing = output.split(b"\n", 1)
            response = json.loads(line)
            if trailing or not isinstance(response, dict):
                raise self.fail("startup-response")
            if set(response) == {"fallback"}:
                if response["fallback"] not in bridge.HISTORY_SOURCE_FALLBACKS:
                    raise self.fail("startup-response")
                self.shutdown()
                self.fallback = True
                return None
            if set(response) != {"endpoint"} or set(response["endpoint"]) != {
                "url",
                "password",
            }:
                raise self.fail("startup-response")
            endpoint = validate_endpoint(response["endpoint"])
            if self.child.poll() is not None:
                raise self.fail(self._category())
            self.connection = {**endpoint, "workspace": self.data["id"]}
            self.token = CONNECTION.set(self.connection)
            return self.connection
        except BaseException as error:
            self._shutdown_preserving(
                error,
                startup_failed=not isinstance(error, (KeyboardInterrupt, SystemExit)),
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit, self.box.BoxError)):
                raise
            raise self.fail("startup-failed") from None

    def _shutdown_preserving(self, original, *, startup_failed=False):
        try:
            self.shutdown(startup_failed=startup_failed)
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

    def shutdown(self, *, startup_failed=False):
        failed = False
        interruption = None

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
                        code = self.child.wait(timeout=20)
                    except BaseException as error:
                        caught(error)
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        except BaseException as error:
                            caught(error)
                        try:
                            self.child.wait(timeout=5)
                        except BaseException as error:
                            caught(error)
                        code = None
                    failed = failed or (code != 0 and not startup_failed)
                finally:
                    if self.child.stdout:
                        try:
                            self.child.stdout.close()
                        except BaseException as error:
                            caught(error)
                    self.child = None
        finally:
            if self.token is not None:
                try:
                    CONNECTION.reset(self.token)
                except BaseException as error:
                    caught(error)
                self.token = None
            self.connection = None
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

    def __exit__(self, kind, value, traceback):
        if self.fallback:
            return
        try:
            ended = kind is None and (
                self.child is None
                or self.child.poll() is not None
                or bool(select.select([self.child.stdout], [], [], 0)[0])
            )
        except BaseException as error:
            self._shutdown_preserving(error)
            raise
        self._shutdown_preserving(value)
        if ended:
            raise self.fail("lease-ended")


@contextlib.contextmanager
def history_source(box, data):
    """Optional manual-sync optimization; existing owners/recovery stay intact."""
    if __package__:
        from . import history_reader, history_recovery, opencode_v2_io
    else:
        import history_reader
        import history_recovery
        import opencode_v2_io

    live = live_connection()
    if (
        live is not None
        or history_reader.current() is not None
        or history_recovery.FENCE.get() is not None
        or data.get("opencode_version") not in opencode_v2_io.HISTORY_SOURCE_SUPPORTED
    ):
        yield live
        return
    with HistorySource(box, data) as connection:
        yield connection


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
):
    """No host pass_env, auth refresh, profiles, or vault unlock on attach."""
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
    env = box.maintenance_host_env()
    env["OPENCODE_PASSWORD"] = connection["password"]
    command = ["podman", "exec", "-i"]
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
        if file_preview:
            if __package__:
                from . import file_preview as preview, file_preview_host
            else:
                import file_preview as preview
                import file_preview_host
            release = data.get("opencode_version")
            verified = (
                preview.client_binding(box, data, connection)
                if preview.supports(release)
                and preview.client_incompatibility(release) is None
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
                        box, data, container, refusals=refusals
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
                        sources = preview.host_export_mounts(box, data, container)
                        if sources is None:
                            raise file_preview_host.PreviewError(
                                "host-mounts-unverified",
                                reason="mount-recheck-unverified",
                            )
                        file_preview_host.open_artifact(
                            kind, candidate, host_mounts=sources
                        )

                if preview_host_open is None:
                    startup_output.line(
                        f"oc-box: Experimental host opening is off ({reason}).",
                        flush=True,
                    )

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
    command.extend(["python3", "-I", "-S", "-c", bridge, json.dumps(names), *arguments])
    result = box.ContainerCommand(command, env)
    if guarded:
        result.guarded_spawn = True
    if preview_host_open is not None:
        result.preview_host_open = preview_host_open
    return result


def tab_inventory(box, data, connection, *, timeout=4):
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
    Stable native 2.3.x may route retained shell/subagent jobs to a guarded
    unregistered post-stop reader; older/unknown versions retain shell-only hints.
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
            or job.get("kind") not in ("shell", "subagent", "image")
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
            kind: sum(job["kind"] == kind for job in jobs)
            for kind in ("shell", "subagent", "image")
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
            if profile == "2.3" and active and all(job["kind"] in kinds for job in jobs)
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
    def __init__(self, box, path, data, *, image_preview=False, ui_bridge=False):
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
        safeguard = getattr(box, "runtime_safeguard", None)
        guarded = safeguard.current() if safeguard else None
        try:
            with guarded.spawn(box) if guarded else contextlib.nullcontext():
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
            if guarded and self.connection.get("version") != guarded.record.version:
                raise box.BoxError(
                    "Private owner readiness did not match the guarded OpenCode release."
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
            with history_recovery.budget(4):
                snapshot = native_tabs.inspect(
                    self.box, self.data, connection=self.connection
                )
                if snapshot.status == "invalid" or (
                    snapshot.status == "valid" and not snapshot.tabs
                ):
                    return None
                inventory = tab_inventory(self.box, self.data, self.connection)
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
            or "session" not in connection
            or (
                connection["session"] is not None
                and not box.valid_session_id(connection["session"])
            )
        ):
            raise box.BoxError(
                "Live service does not match this workspace/version/session."
            )
        safeguard = getattr(box, "runtime_safeguard", None)
        if safeguard:
            safeguard.bind_client(box, path, data, connection)
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
        if version not in web_attachments.SUPPORTED:
            raise box.BoxError(
                "The browser attachment adapter requires native OpenCode 2.2.6 or 2.3.0."
            )
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
        help="opt in to the visible browser File-drop adapter (native 2.2.6/2.3.0 only; no automatic Send)",
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
