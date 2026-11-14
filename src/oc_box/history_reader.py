"""Non-executing V2 history lease, only after a verified full container stop.

The caller proves stop/restart identity, holds the lifecycle lock, supplies one
relative-budget callback, and always stops the container afterwards. This module
does not elect a service, prepare credentials, or certify live-owner quiescence.
It is also copied verbatim to ``python -c``; keep module imports stdlib-only.
"""

import contextlib
import contextvars
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

CURRENT = contextvars.ContextVar("oc_box_history_reader", default=None)
# Exact audited native versions only. 2.2.x retains cancelled reload jobs; 2.3.0
# drains reload jobs but can retain a running shell after producer termination.
# Both profiles require the same non-executing reader and unchanged durable jobs;
# neither is a live-readiness exception or a claim of whole-container acceptance.
SUPPORTED = frozenset(("2.2.6", "2.2.7", "2.3.0"))
MAX_CONTROL = 16384
MAX_PAYLOAD = 131072
PATH = "/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MARKER = "oc-box-history-reader: "
GUARD_CATEGORIES = frozenset(
    (
        "database-missing",
        "database-unsafe",
        "database-invalid",
        "schedule-schema",
        "schedule-present",
        "system-config",
        "unsupported-version",
    )
)
CATEGORIES = GUARD_CATEGORIES | frozenset(
    (
        "deadline",
        "unsupported-version",
        "host-database-path",
        "host-database-probe",
        "startup-payload",
        "startup-timeout",
        "startup-failed",
        "startup-response",
        "cleanup-failed",
        "reader-ended",
        "client-command",
        "reader-reuse",
        "active-response",
        "foreground-active",
        "active-query",
        "reader-source",
    )
)


class Refusal(ValueError):
    """Fixed-category errors only; never include SQLite/native diagnostics."""

    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in CATEGORIES
            else "startup-failed"
        )
        super().__init__(f"History reader refused ({self.category}).")


def _boxed(box, category):
    refusal = Refusal(category)
    error = box.BoxError(str(refusal))
    error.history_reader_category = refusal.category
    return error


def current():
    return CURRENT.get()


def _check_version(output, expected):
    match = re.fullmatch(r"(?:opencode\s+)?v?([12]\.\d+\.\d+)", output.strip())
    if not match or match[1] != expected or expected not in SUPPORTED:
        raise Refusal("unsupported-version")


def _budget(remaining, maximum):
    value = remaining()
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise Refusal("deadline")
    return min(value, maximum)


def _check_database(path, *, allow_missing=False, timeout=1):
    """Inspect scheduler metadata only, without creating or migrating a DB.

    A disabled schedules flag does NOT disable native Schedule.rewake. All rows,
    including terminal/unknown statuses, therefore refuse service construction.
    Missing schemas are not evidence of an empty scheduler. Never use immutable
    SQLite mode here: it can hide committed schedule rows in an existing WAL.
    """
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise Refusal("database-missing") from None
    except OSError:
        raise Refusal("database-unsafe") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_mode & 0o022
    ):
        raise Refusal("database-unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with contextlib.ExitStack() as stack:
            stack.callback(os.close, descriptor)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise Refusal("database-unsafe")
            connection = stack.enter_context(
                contextlib.closing(
                    sqlite3.connect(
                        path.absolute().as_uri() + "?mode=ro", uri=True, timeout=timeout
                    )
                )
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            kind = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'schedule'"
            ).fetchall()
            if kind != [("table",)]:
                raise Refusal("schedule-schema")
            columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info('schedule')")
            }
            required = ("id", "session_id", "status", "last_message_id")
            if any(
                name not in columns or columns[name][2].upper() != "TEXT"
                for name in required
            ):
                raise Refusal("schedule-schema")
            if not columns["id"][5]:
                raise Refusal("schedule-schema")
            if (
                connection.execute('SELECT 1 FROM "schedule" LIMIT 1').fetchone()
                is not None
            ):
                raise Refusal("schedule-present")
            after = path.lstat()
            if (after.st_dev, after.st_ino, after.st_uid) != (
                before.st_dev,
                before.st_ino,
                before.st_uid,
            ) or not stat.S_ISREG(after.st_mode):
                raise Refusal("database-unsafe")
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, Refusal):
            raise
        raise Refusal("database-invalid") from None


def fence_environment(env):
    """New public-only environment; retain native store/record path selectors.

    The input is an already constructed private_host_env, not os.environ. In
    particular do not retain arbitrary OPENCODE_ settings or inline credentials.
    """
    names = (
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "OPENCODE_TEST_HOME",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DB",
        "OPENCODE_DISABLE_CHANNEL_DB",
    )
    result = {name: env[name] for name in names if name in env}
    result.update(
        {
            "OPENCODE_FLAGS": "-schedules",
            "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_FILEWATCHER_DISABLE": "1",
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "snapshots": False,
                    "formatter": False,
                    "update": "disable",
                    "warming": False,
                    "policies": [
                        {"action": action, "resource": "*", "effect": "deny"}
                        for action in ("plugin.use", "provider.use", "tool.use")
                    ],
                }
            ),
        }
    )
    return result


def _check_system_config():
    # Native system policy outranks inline policy and can allow integrations.
    # There is no supported CLI switch to suppress it: refuse, never rewrite or
    # weaken it. Recovery is Linux/rootless-Podman only.
    root = Path("/etc/opencode")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise Refusal("system-config") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise Refusal("system-config")
    try:
        if next(root.iterdir(), None) is not None:
            raise Refusal("system-config")
    except OSError:
        raise Refusal("system-config") from None


def assert_host_safe(box, path, data, env, remaining):
    """Fence a destination before native projection; never query a host service.

    ``debug paths db`` in supported native versions uses Global/databasePath
    only, not service discovery or the database layer. ``path`` is deliberately
    not used to reconstruct private_host_env (which would recurse).
    """
    del path
    try:
        if data.get("opencode_version") not in SUPPORTED:
            raise Refusal("unsupported-version")
        _check_system_config()
        result_env = fence_environment(env)
        command = box.host_command(data, "debug", "paths", "db")
        version = box.run(
            [command[0], "--version"],
            env=result_env,
            capture=True,
            private=True,
            timeout=_budget(remaining, 10),
        )
        _check_version(version.stdout, data["opencode_version"])
        result = box.run(
            command,
            env=result_env,
            capture=True,
            private=True,
            timeout=_budget(remaining, 15),
        )
        _budget(remaining, 15)
        # Remove the CLI's one line terminator, not filename whitespace.
        candidate = result.stdout.removesuffix("\n")
        if (
            not candidate.startswith("/")
            or "\n" in candidate
            or "\0" in candidate
            or len(candidate) > 4096
        ):
            raise Refusal("host-database-path")
        _check_database(candidate, allow_missing=True, timeout=_budget(remaining, 1))
        return result_env
    except Refusal as error:
        raise _boxed(box, error.category) from None
    except (OSError, ValueError, subprocess.SubprocessError, box.BoxError):
        raise _boxed(box, "host-database-probe") from None


@contextlib.contextmanager
def _remote_environment(home):
    """Private empty discovery roots, with only the real source data directory."""
    with tempfile.TemporaryDirectory(prefix="oc-box-history-") as temporary:
        root = Path(temporary)
        for name in ("home", "config", "state", "cache"):
            (root / name).mkdir(mode=0o700)
        env = fence_environment(
            {
                "HOME": str(root / "home"),
                "OPENCODE_TEST_HOME": str(root / "home"),
                "PATH": PATH,
                "LANG": "C.UTF-8",
                "TMPDIR": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": home + "/.local/share",
                "OPENCODE_DB": "oc-box-v2.db",
            }
        )
        yield root, env


def _remote(argv):
    action, home, *arguments = argv
    password = os.environ.get("OPENCODE_PASSWORD") if action == "command" else None
    # Clear create-time container secrets even from this helper's environment.
    os.environ.clear()
    os.environ.update({"PATH": PATH, "LANG": "C.UTF-8"})
    if action == "lease":
        payload = sys.stdin.buffer.readline(MAX_PAYLOAD + 1)
        if len(payload) > MAX_PAYLOAD or not payload.endswith(b"\n"):
            raise Refusal("startup-payload")
        settings = json.loads(payload)
        _check_system_config()
        _check_database(Path(home) / ".local/share/opencode/oc-box-v2.db")
    elif action != "command":
        raise Refusal("startup-payload")
    with _remote_environment(home) as (root, env):
        os.chdir(root)
        os.environ.clear()
        os.environ.update(env)
        if password is not None:
            os.environ["OPENCODE_PASSWORD"] = password
        if action == "command":
            # Keep the temporary discovery roots alive until the client exits.
            return subprocess.call(arguments, env=os.environ)
        if settings.get("version") not in SUPPORTED:
            raise Refusal("unsupported-version")
        version = subprocess.run(
            [arguments[0], "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if version.returncode != 0:
            raise Refusal("unsupported-version")
        _check_version(version.stdout, settings["version"])
        namespace = {"__name__": "oc_box_history_bridge"}
        exec(settings["bridge"], namespace)
        # Reuse the public bridge unchanged: it owns/reaps the native child.
        exit_info = {}
        with namespace["server"](arguments[0], exit_info=exit_info) as request:
            print(json.dumps(request.endpoint), flush=True)
            while request.alive():
                if select.select([sys.stdin], [], [], 0.1)[0] and not os.read(0, 4096):
                    break
        if exit_info != {"code": 0, "forced": False}:
            raise Refusal("cleanup-failed")
    return 0


class Reader:
    """One unregistered private native lease; never retry the same instance."""

    def __init__(self, box, data, *, remaining):
        self.box, self.data, self.remaining = box, dict(data), remaining
        self.child = self.errors = self.error_file = None
        self.connection = None
        self.tokens = []
        self.started = False

    def command(self, arguments):
        if self.connection is None:
            raise _boxed(self.box, "reader-ended")
        args = list(arguments)
        if self.connection is not None:
            if args and args[0] == self.box.CONTAINER_OPENCODE:
                if any(
                    value in args for value in ("--standalone", "serve", "service")
                ) or any(value.startswith("--server=") for value in args):
                    raise _boxed(self.box, "client-command")
                if "--server" not in args:
                    args.extend(["--server", self.connection["url"]])
                elif args.count("--server") != 1 or args[
                    args.index("--server") + 1 : args.index("--server") + 2
                ] != [self.connection["url"]]:
                    raise _boxed(self.box, "client-command")
                if args not in (
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/session/active",
                        "--server",
                        self.connection["url"],
                    ],
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/experimental/job",
                        "--server",
                        self.connection["url"],
                    ],
                    [
                        self.box.CONTAINER_OPENCODE,
                        "session",
                        "list",
                        "--server",
                        self.connection["url"],
                    ],
                ):
                    raise _boxed(self.box, "client-command")
            elif (
                len(args) != 5
                or args[:2] != ["python3", "-c"]
                or args[2] != (self.box.PACKAGE / "opencode_v2_io.py").read_text()
                or args[3] not in ("sessions", "export")
                or args[4] != self.box.CONTAINER_OPENCODE
            ):
                raise _boxed(self.box, "client-command")
        return self._helper_command("command", args)

    def _container_command(self, arguments):
        env = self.box.maintenance_host_env()
        command = ["podman", "exec", "-i", "--workdir", "/"]
        if self.connection is not None:
            env = {**env, "OPENCODE_PASSWORD": self.connection["password"]}
            command.extend(["--env", "OPENCODE_PASSWORD"])
        # env -i removes create-time secrets; only our ephemeral control token
        # is reintroduced by name, never in argv or saved config.
        clean = ["/usr/bin/env", "-i", "PATH=" + PATH, "LANG=C.UTF-8"]
        if self.connection is not None:
            # Read the exec-injected token before resetting the environment,
            # with Python's environment/site hooks disabled, then pass it in memory.
            bootstrap = "import os,sys; p=os.environ['OPENCODE_PASSWORD']; e={'PATH':sys.argv[1],'LANG':'C.UTF-8','OPENCODE_PASSWORD':p}; os.execvpe(sys.argv[2],sys.argv[2:],e)"
            prefix = ["/usr/bin/python3", "-I", "-S", "-c", bootstrap, PATH]
        else:
            prefix = clean
        command.extend([self.data["container"], *prefix, *arguments])
        return self.box.ContainerCommand(command, env)

    def _helper_command(self, action, arguments):
        return self._container_command(
            [
                "python3",
                "-I",
                "-S",
                "-c",
                (self.box.PACKAGE / "history_reader.py").read_text(),
                action,
                self.data["home"],
                *arguments,
            ]
        )

    def _startup_category(self):
        self.error_file.seek(0)
        lines = (
            self.error_file.read(MAX_CONTROL)
            .decode("utf-8", errors="replace")
            .splitlines()
        )
        return next(
            (
                line[len(MARKER) :]
                for line in reversed(lines)
                if line.startswith(MARKER) and line[len(MARKER) :] in GUARD_CATEGORIES
            ),
            "startup-failed",
        )

    def _write_payload(self, payload, deadline):
        descriptor = self.child.stdin.fileno()
        os.set_blocking(descriptor, False)
        offset = 0
        while offset < len(payload):
            budget = min(_budget(self.remaining, 45), deadline - time.monotonic())
            if budget <= 0:
                raise Refusal("startup-timeout")
            if not select.select([], [descriptor], [], min(0.1, budget))[1]:
                continue
            try:
                written = os.write(descriptor, payload[offset : offset + 4096])
            except BlockingIOError:
                continue
            if written <= 0:
                raise Refusal("startup-failed")
            offset += written

    def __enter__(self):
        # Host-only imports must stay here: the remote __main__ is stdlib-only.
        if __package__:
            from . import workspace_service, history_recovery
        else:
            import workspace_service
            import history_recovery

        if self.started or CURRENT.get() is not None:
            raise _boxed(self.box, "reader-reuse")
        try:
            self.started = True
            if self.data.get("opencode_version") not in SUPPORTED:
                raise Refusal("unsupported-version")
            if (
                not isinstance(self.data.get("container"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", self.data["container"])
                or not isinstance(self.data.get("home"), str)
                or not self.data["home"].startswith("/")
            ):
                raise Refusal("reader-source")
            deadline = time.monotonic() + _budget(self.remaining, 45)
            command = self._helper_command("lease", [self.box.CONTAINER_OPENCODE])
            self.errors = self.box.captured_output(private=True)
            self.error_file = self.errors.__enter__()
            with history_recovery.admit():
                self.child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self.error_file,
                    start_new_session=True,
                )
            payload = (
                json.dumps(
                    {
                        "bridge": (self.box.PACKAGE / "opencode_v2_io.py").read_text(),
                        "version": self.data["opencode_version"],
                    }
                ).encode()
                + b"\n"
            )
            if len(payload) > MAX_PAYLOAD:
                raise Refusal("startup-payload")
            self._write_payload(payload, deadline)
            output = bytearray()
            while b"\n" not in output:
                budget = min(_budget(self.remaining, 45), deadline - time.monotonic())
                if budget <= 0:
                    raise Refusal("startup-timeout")
                if self.child.poll() is not None:
                    raise Refusal(self._startup_category())
                if select.select([self.child.stdout], [], [], min(0.1, budget))[0]:
                    chunk = os.read(
                        self.child.stdout.fileno(), MAX_CONTROL + 1 - len(output)
                    )
                    if not chunk:
                        # EOF can precede waitpid's visible exit status. Reap
                        # boundedly before reading the fixed guard marker, never
                        # replace a source schedule refusal with a race-dependent
                        # generic startup error.
                        self.child.wait(timeout=min(1, budget))
                        raise Refusal(self._startup_category())
                    output.extend(chunk)
                if len(output) > MAX_CONTROL:
                    raise Refusal("startup-response")
            line, trailing = output.split(b"\n", 1)
            if trailing.strip():
                raise Refusal("startup-response")
            endpoint = workspace_service.validate_endpoint(json.loads(line))
            self.connection = {
                "url": endpoint["url"],
                "password": endpoint["password"],
                "workspace": self.data["id"],
            }
            self.tokens.append(
                (
                    workspace_service.CONNECTION,
                    workspace_service.CONNECTION.set(self.connection),
                )
            )
            self.tokens.append((CURRENT, CURRENT.set(self)))
            self.assert_idle()
            return self
        except BaseException as error:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            if isinstance(
                error,
                (
                    KeyboardInterrupt,
                    SystemExit,
                    history_recovery.RecoveryError,
                    history_recovery.workspace_control.ControlError,
                ),
            ):
                raise
            category = (
                error.category if isinstance(error, Refusal) else "startup-failed"
            )
            raise _boxed(self.box, category) from None

    def assert_idle(self):
        """Explicit authenticated foreground query; never inspect historical jobs."""
        try:
            if self.connection is None or self.child.poll() is not None:
                raise Refusal("reader-ended")
            # The lease has exactly one control record and no streaming output.
            if select.select([self.child.stdout], [], [], 0)[0]:
                raise Refusal("startup-response")
            result = self.box.run(
                self.command(
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/session/active",
                        "--server",
                        self.connection["url"],
                    ],
                ),
                capture=True,
                private=True,
                timeout=_budget(self.remaining, 15),
            )
            _budget(self.remaining, 15)
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or not isinstance(
                response.get("data"), dict
            ):
                raise Refusal("active-response")
            if response["data"]:
                raise Refusal("foreground-active")
        except Refusal as error:
            raise _boxed(self.box, error.category) from None
        except (OSError, ValueError, subprocess.SubprocessError, self.box.BoxError):
            raise _boxed(self.box, "active-query") from None

    def shutdown(self):
        # Cleanup has its own finite allowance even after the caller's deadline.
        # Killing the wrapper is not proof of container stop; the caller owns it.
        try:
            if self.child is not None:
                try:
                    if self.child.stdin:
                        # A remote refusal can close its input before we do.
                        # A broken control pipe must not skip reaping.
                        with contextlib.suppress(OSError):
                            self.child.stdin.close()
                    try:
                        code = self.child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        self.child.wait(timeout=5)
                        raise _boxed(self.box, "cleanup-failed") from None
                    if code != 0:
                        raise _boxed(self.box, "cleanup-failed")
                finally:
                    if self.child.stdout:
                        self.child.stdout.close()
                    self.child = None
        except (OSError, subprocess.SubprocessError):
            raise _boxed(self.box, "cleanup-failed") from None
        finally:
            for variable, token in reversed(self.tokens):
                variable.reset(token)
            self.tokens.clear()
            self.connection = None
            if self.error_file is not None:
                self.errors.__exit__(None, None, None)
                self.error_file = None

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            return
        try:
            self.assert_idle()
        except BaseException:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            raise
        self.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(_remote(sys.argv[1:]))
    except Refusal as error:
        category = (
            error.category if error.category in GUARD_CATEGORIES else "startup-failed"
        )
        print(MARKER + category, file=sys.stderr)
        sys.exit(2)
    except Exception:
        print(MARKER + "startup-failed", file=sys.stderr)
        sys.exit(1)
