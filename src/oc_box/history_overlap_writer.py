"""One create or readback request per admitted, direct host native child.

The caller owns the destination lock, durable attempt, native database/path
preflight and independent readback. This is not a shared service client or a
substitute for any of those checks.
"""

import base64
import contextlib
import io
import json
import math
import os
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import threading
import time

if __package__:
    from . import history_reader, runtime_safeguard
    from .history_overlap_archive import MAX_ARCHIVE_BYTES, SealedCandidate
    from .web_named_native_profile import named_native_json, named_native_port
else:
    import history_reader
    import runtime_safeguard
    from history_overlap_archive import MAX_ARCHIVE_BYTES, SealedCandidate
    from web_named_native_profile import named_native_json, named_native_port


# The create/conflict semantics are qualified on these exact stock ELFs, not
# inferred from the version string or the presence of the experimental route.
_STOCK = {
    "2.4.0": "7915c1cf09fac2fa475dc5f2e008486579da1c181a439699e2097ac3866f1da3",
    "2.4.1": "ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152",
}
_IMPORT = "/api/experimental/session/import"
_EXPORT = "/api/experimental/session/{target}/export"
_SERVER_INFO = "/api/info"
_MIGRATION = "/api/experimental/migration/v1"
_ROOTS = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "config",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_RUNTIME_DIR": "runtime",
    "TMPDIR": "tmp",
}
_MAX_HEADER = 8192
_MAX_REPLY = 16384
_MAX_PROC_ROWS = 32768
_EOF_GRACE = 5
_KILL_GRACE = 2
_CATEGORIES = frozenset(
    (
        "admission",
        "binding",
        "candidate",
        "cleanup",
        "conflict",
        "deadline",
        "identity",
        "protocol",
        "readback",
        "readiness",
        "unacknowledged",
        "unsupported",
    )
)


class WriterRefusal(ValueError):
    """Fixed refusal only, with mutation and retirement uncertainty separate."""

    def __init__(
        self,
        category,
        *,
        operation="create",
        attempted=False,
        uncertain=False,
        cleanup=False,
    ):
        assert category in _CATEGORIES
        assert operation in ("create", "readback")
        super().__init__(f"Host history {operation} refused ({category}).")
        self.category = category
        self.history_create_attempted = attempted
        self.history_write_uncertain = uncertain
        self.history_destination_cleanup_failed = cleanup


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WriterRefusal("deadline")
    return remaining


def _absolute(path):
    path = os.fspath(path)
    if (
        type(path) is not str
        or not path.startswith("/")
        or path.startswith("//")
        or len(os.fsencode(path)) > 4096
        or not path.isprintable()
        or os.path.normpath(path) != path
    ):
        raise WriterRefusal("binding")
    return path


def _environment(root, data_home, version, *, database=None, disable_channel_db=None):
    root, data_home = _absolute(root), _absolute(data_home)
    env = {
        **{key: os.path.join(root, name) for key, name in _ROOTS.items()},
        "OPENCODE_TEST_HOME": os.path.join(root, "home"),
        "OPENCODE_CONFIG_DIR": os.path.join(root, "config", "opencode"),
        "XDG_DATA_HOME": data_home,
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    if database is not None:
        if type(database) is not str or (
            not re.fullmatch(r"[A-Za-z0-9._-]{1,255}\.db", database)
            and _absolute(database) != database
        ):
            raise WriterRefusal("binding")
        env["OPENCODE_DB"] = database
    if disable_channel_db is not None:
        if disable_channel_db not in ("0", "1"):
            raise WriterRefusal("binding")
        env["OPENCODE_DISABLE_CHANNEL_DB"] = disable_channel_db
    fenced = history_reader.fence_environment(env, version=version)
    # This is the normal host archive store, not a read-only recovery
    # projection. Keep native search indexing available for a new import.
    fenced["OPENCODE_FLAGS"] = "-machines"
    return fenced


def writer_environment(
    root, data_home, version, *, database=None, disable_channel_db=None
):
    """Create isolated discovery roots in the caller's fresh private directory.

    The caller must preflight the same env, executable and cwd before use.
    Only the explicitly selected native data store is retained across writers.
    """
    try:
        env = _environment(
            root,
            data_home,
            version,
            database=database,
            disable_channel_db=disable_channel_db,
        )
        with history_reader._native_directory(
            root, time.monotonic() + 1, "database-unsafe"
        ) as fd:
            if fd is None or os.fstat(fd).st_mode & 0o077:
                raise WriterRefusal("binding")
            for name in _ROOTS.values():
                os.mkdir(name, 0o700, dir_fd=fd)
            home = os.open(
                "home",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            try:
                os.mkdir(".local", 0o700, dir_fd=home)
            finally:
                os.close(home)
        return env
    except (OSError, TypeError, history_reader.Refusal):
        raise WriterRefusal("binding") from None


def _checked_environment(env, version, deadline):
    if type(env) is not dict:
        raise WriterRefusal("binding")
    # Recover only a candidate spelling for the common scratch parent. The
    # closed environment must agree, and the filesystem walk below must prove
    # the parent and each child without following links.
    scratch, temp_name = os.path.split(_absolute(env.get("TMPDIR")))
    if temp_name != _ROOTS["TMPDIR"]:
        raise WriterRefusal("binding")
    checked = _environment(
        scratch,
        env.get("XDG_DATA_HOME"),
        version,
        database=env.get("OPENCODE_DB"),
        disable_channel_db=env.get("OPENCODE_DISABLE_CHANNEL_DB"),
    )
    if checked != env:
        raise WriterRefusal("binding")
    with history_reader._native_directory(scratch, deadline, "database-unsafe") as root:
        if root is None or os.fstat(root).st_mode & 0o077:
            raise WriterRefusal("binding")
        for name in _ROOTS.values():
            row = os.stat(name, dir_fd=root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(row.st_mode)
                or row.st_uid != os.getuid()
                or row.st_mode & 0o077
            ):
                raise WriterRefusal("binding")
        home = os.open(
            "home",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root,
        )
        try:
            fallback = os.stat(".local", dir_fd=home, follow_symlinks=False)
            if (
                not stat.S_ISDIR(fallback.st_mode)
                or fallback.st_uid != os.getuid()
                or fallback.st_mode & 0o077
            ):
                raise WriterRefusal("binding")
        finally:
            os.close(home)
    history_reader._check_system_config()
    return checked


def _target_id(target):
    return (
        type(target) is str
        and re.fullmatch(r"ses[A-Za-z0-9_-]{1,253}", target) is not None
    )


def _body(candidate, target, cwd, deadline):
    if (
        type(candidate) is not SealedCandidate
        or not _target_id(target)
        or not 1 <= candidate.size <= MAX_ARCHIVE_BYTES
    ):
        raise WriterRefusal("candidate")
    try:
        with io.BytesIO() as staged:
            candidate.write_to(staged)
            archive = named_native_json(staged.getvalue())
        if (
            type(archive) is not dict
            or type(info := archive.get("info")) is not dict
            or info.get("id") != target
            or info.keys() & {"parentID", "permission", "permissions", "fork", "revert"}
            or type(messages := archive.get("messages")) is not list
        ):
            raise WriterRefusal("candidate")
        payload = {"info": info, "messages": messages, "location": {"directory": cwd}}
        encoder = json.JSONEncoder(
            allow_nan=False, ensure_ascii=False, separators=(",", ":")
        )
        body = bytearray()
        for part in encoder.iterencode(payload):
            encoded = part.encode()
            if len(encoded) > MAX_ARCHIVE_BYTES - len(body):
                raise WriterRefusal("candidate")
            body.extend(encoded)
        _remaining(deadline)
        return body
    except WriterRefusal:
        raise
    except (OSError, ValueError, RecursionError):
        raise WriterRefusal("candidate") from None


def _same_dir(row):
    return (row.st_dev, row.st_ino, row.st_uid, row.st_mode)


def _readable(*fds, timeout=0):
    poll = select.poll()
    for fd in fds:
        poll.register(fd, select.POLLIN)
    return {fd for fd, _ in poll.poll(math.ceil(timeout * 1000))}


def _exact_wait(child, pidfd, deadline):
    if not _readable(pidfd, timeout=max(0, deadline - time.monotonic())):
        return False
    # Popen.wait/poll can synthesize a return code after ChildProcessError.
    # Positive proof requires an actual waitid result for this direct child.
    event = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
    if event is None or event.si_pid != child.pid:
        return False
    if event.si_code == os.CLD_EXITED:
        child.returncode = event.si_status
    elif event.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        child.returncode = -event.si_status
    else:
        return False
    return True


@contextlib.contextmanager
def _defer_sigint():
    """Keep Ctrl-C from escaping between Popen and ownership, or during reap."""
    handler = signal.getsignal(signal.SIGINT)
    pending = []

    def remember(number, frame):
        if not pending:
            pending.append(frame)

    def deliver():
        if pending:
            handler(signal.SIGINT, pending.pop())

    if callable(handler):
        signal.signal(signal.SIGINT, remember)
    try:
        yield deliver
    finally:
        if callable(handler):
            signal.signal(signal.SIGINT, handler)


class _Child:
    def __init__(self):
        self.process = None
        self.pidfd = None

    def start(self, host, env, cwd, fd, deadline):
        _remaining(deadline)
        if runtime_safeguard._signature(os.fstat(host.fd)) != host.identity:
            raise WriterRefusal("identity")
        binary = f"/proc/{os.getpid()}/fd/{host.fd}"
        self.process = subprocess.Popen(
            [
                binary,
                "serve",
                "--stdio",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                "--log-level",
                "none",
            ],
            cwd=f"/proc/{os.getpid()}/fd/{fd}",
            env={**env, "PWD": cwd},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
            umask=0o077,
        )
        self.pidfd = os.pidfd_open(self.process.pid)
        os.set_blocking(self.process.stdout.fileno(), False)

    def _alive(self):
        return self.pidfd is not None and not _readable(self.pidfd)

    def check(self, host, path, fd):
        proc = f"/proc/{self.process.pid}"
        if (
            not self._alive()
            or self.process.pid != os.getsid(self.process.pid)
            or self.process.pid != os.getpgid(self.process.pid)
            or runtime_safeguard._signature(os.fstat(host.fd)) != host.identity
            or runtime_safeguard._signature(os.stat(proc + "/exe")) != host.identity
            or os.readlink(proc + "/cwd") != path
            or _same_dir(os.stat(proc + "/cwd")) != _same_dir(os.fstat(fd))
            or _same_dir(os.stat(path, follow_symlinks=False))
            != _same_dir(os.fstat(fd))
        ):
            raise WriterRefusal("identity")

    def port(self, deadline):
        output = bytearray()
        stdout = self.process.stdout.fileno()
        while b"\n" not in output:
            ready = _readable(stdout, self.pidfd, timeout=_remaining(deadline))
            if stdout not in ready or self.pidfd in ready:
                raise WriterRefusal("readiness")
            chunk = os.read(stdout, 4097 - len(output))
            if not chunk or len(output) + len(chunk) > 4096:
                raise WriterRefusal("readiness")
            output.extend(chunk)
        try:
            if not output.endswith(b"\n") or output.count(b"\n") != 1:
                raise ValueError
            value = named_native_json(output[:-1])
            if type(value) is not dict or value.keys() != {"url"}:
                raise ValueError
            port = named_native_port(value["url"])
            if value["url"] != f"http://127.0.0.1:{port}":
                raise ValueError
        except (ValueError, RecursionError):
            raise WriterRefusal("readiness") from None
        return port

    def own_socket(self, port, peer_port, deadline):
        """Bind the listener/accepted peer to this child, not just its TCP port."""
        proc = f"/proc/{self.process.pid}"
        local = f"0100007F:{port:04X}".encode()
        peer = f"0100007F:{peer_port:04X}".encode() if peer_port else b"00000000:0000"
        state = b"01" if peer_port else b"0A"
        matches = []
        with open(proc + "/net/tcp", "rb") as table:
            table.readline(513)
            for count in range(_MAX_PROC_ROWS + 1):
                _remaining(deadline)
                line = table.readline(513)
                if not line:
                    break
                if count == _MAX_PROC_ROWS or len(line) > 512:
                    raise WriterRefusal("identity")
                row = line.split()
                if len(row) >= 10 and row[1:4] == [local, peer, state]:
                    matches.append(row[9])
        if len(matches) != 1 or not matches[0].isdigit() or matches[0] == b"0":
            raise WriterRefusal("identity")
        target = b"socket:[" + matches[0] + b"]"
        with os.scandir(os.fsencode(proc + "/fd")) as entries:
            for count, entry in enumerate(entries):
                _remaining(deadline)
                if count >= 1024:
                    break
                try:
                    if os.readlink(entry.path) == target and self._alive():
                        return
                except FileNotFoundError:
                    continue
        raise WriterRefusal("identity")

    def retire(self):
        if self.process is None:
            return True
        p = self.process
        try:
            alive = self._alive()
            p.stdin.close()
            # If opening the pidfd failed during startup, try only to recover
            # owned cleanup, never to turn that failure into a successful write.
            if self.pidfd is None:
                self.pidfd = os.pidfd_open(p.pid)
            if not _exact_wait(p, self.pidfd, time.monotonic() + _EOF_GRACE):
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                _exact_wait(p, self.pidfd, time.monotonic() + _KILL_GRACE)
                return False
            if not alive or p.returncode != 0:
                return False
            # EOF with no extra byte on the direct control channel. Never claim
            # that cleanup is proven from the numeric exit status alone.
            if not _readable(p.stdout.fileno()) or os.read(p.stdout.fileno(), 1):
                return False
            try:
                os.killpg(p.pid, 0)
            except ProcessLookupError:
                return True
            return False
        except OSError:
            return False
        finally:
            p.stdin.close()
            p.stdout.close()
            if self.pidfd is not None:
                os.close(self.pidfd)
                self.pidfd = None


class _Exchange:
    """Fixed HTTP/1.1 wire; one connection, never proxy, redirect, or retry."""

    def __init__(self, stream, port, password, deadline):
        self.stream, self.port, self.deadline = stream, port, deadline
        self.authorization = b"Basic " + base64.b64encode(
            b"opencode:" + password.encode()
        )

    def send(self, data):
        self.stream.settimeout(_remaining(self.deadline))
        self.stream.sendall(data)
        _remaining(self.deadline)

    def header(
        self, method, path, *, authenticated=True, content_length=None, close=False
    ):
        header = (
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Connection: {'close' if method == 'POST' or close else 'keep-alive'}\r\n"
        ).encode()
        if authenticated:
            header += b"Authorization: " + self.authorization + b"\r\n"
        if content_length is not None:
            header += f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n".encode()
        return header + b"\r\n"

    def _read(self, size):
        self.stream.settimeout(_remaining(self.deadline))
        return self.stream.recv(size)

    def receive(self, *, close=False, max_body=_MAX_REPLY):
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = self._read(_MAX_HEADER - len(head))
            if not chunk:
                raise WriterRefusal("protocol")
            head.extend(chunk)
        raw, body = head.split(b"\r\n\r\n", 1)
        status_line, *lines = raw.split(b"\r\n")
        match = re.fullmatch(rb"HTTP/1\.1 ([1-5][0-9]{2}) [\x20-\x7e]+", status_line)
        headers = {}
        for line in lines:
            parsed = re.fullmatch(rb"([A-Za-z0-9-]+):[ \t]*([\x20-\x7e]+)", line)
            if not parsed:
                raise WriterRefusal("protocol")
            name, value = parsed[1].lower(), parsed[2]
            if name in headers:
                raise WriterRefusal("protocol")
            headers[name] = value
        length = headers.get(b"content-length", b"")
        if (
            not match
            or headers.keys()
            & {
                b"transfer-encoding",
                b"content-encoding",
                b"location",
                b"set-cookie",
                b"set-cookie2",
            }
            or headers.get(b"content-type") != b"application/json"
            or headers.get(b"connection", b"").lower()
            != (b"close" if close else b"keep-alive")
            or re.fullmatch(rb"[1-9][0-9]{0,7}", length) is None
            or not len(body) <= int(length) <= max_body
        ):
            raise WriterRefusal("protocol")
        size = int(length)
        while len(body) < size:
            chunk = self._read(min(65536, size - len(body)))
            if not chunk:
                raise WriterRefusal("protocol")
            body.extend(chunk)
        if close and self._read(1):
            raise WriterRefusal("protocol")
        try:
            value = named_native_json(body)
        except (ValueError, RecursionError):
            raise WriterRefusal("protocol") from None
        return int(match[1]), headers, value

    def get(self, path, *, authenticated=True):
        self.send(self.header("GET", path, authenticated=authenticated))
        return self.receive()


def _ready(exchange, child, version, tmpdir):
    code, _, info = exchange.get(_SERVER_INFO)
    if (
        code != 200
        or type(info) is not dict
        or info.keys()
        != {"version", "pid", "hostname", "urls", "paths", "capabilities"}
        or info["version"] != version
        or type(info["pid"]) is not int
        or info["pid"] != child.process.pid
        or info["hostname"] != socket.gethostname()
        or info["urls"] != [f"http://127.0.0.1:{exchange.port}"]
        or info["paths"] != {"tmp": tmpdir + "/opencode"}
        or type(info["capabilities"]) is not dict
        or info["capabilities"].keys() != {"persistentPty"}
        or info["capabilities"]["persistentPty"] is not True
    ):
        raise WriterRefusal("identity")
    code, _, migration = exchange.get(_MIGRATION)
    if code != 200 or migration != {"status": "completed"}:
        raise WriterRefusal("readiness")


def supports(host, version):
    """Eligibility only; this does not prove a destination or admit an operation."""
    return (
        type(host) is runtime_safeguard.HostBinary
        and type(version) is str
        and version in _STOCK
        and host.digest == _STOCK[version]
        and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
        and hasattr(os, "P_PIDFD")
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal")
        and threading.current_thread() is threading.main_thread()
    )


def _readback_projection(code, envelope, target):
    if (
        code != 200
        or type(envelope) is not dict
        or envelope.keys() != {"data"}
        or type(data := envelope["data"]) is not dict
        or data.keys() != {"info", "messages"}
        or type(info := data["info"]) is not dict
        or info.get("id") != target
        or type(data["messages"]) is not list
    ):
        raise WriterRefusal("readback")
    return data


def create_once(host, version, env, cwd, candidate, target, *, gate, deadline):
    """Return only the exact newly created ID, after positive child retirement.

    ``gate()`` is an admission context manager holding the workspace stop/intent
    gate through spawn and separately through dispatch of the one POST header.
    The caller must keep its durable attempt on *every* refusal/interruption
    and perform independent readback before publishing an archive/checkpoint.
    ``deadline`` is an absolute monotonic deadline; cleanup gets its own budget.
    """
    return _native_once(
        host,
        version,
        env,
        cwd,
        target,
        gate=gate,
        deadline=deadline,
        operation="create",
        candidate=candidate,
    )


def readback_once(host, version, env, cwd, target, *, gate, deadline):
    """Return one exact-ID export from a new native child after clean retirement.

    The caller supplies the same preflighted environment and cwd, separately
    admits spawn and dispatch, and still owns full projection comparison and
    the previous create's durable attempt. This operation never sends a POST.
    """
    return _native_once(
        host,
        version,
        env,
        cwd,
        target,
        gate=gate,
        deadline=deadline,
        operation="readback",
    )


def _native_once(
    host, version, env, cwd, target, *, gate, deadline, operation, candidate=None
):
    if not supports(host, version):
        raise WriterRefusal("unsupported", operation=operation)
    if (
        type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or not callable(gate)
    ):
        raise WriterRefusal("binding", operation=operation)
    deadline = min(deadline, time.monotonic() + 60)
    create = operation == "create"
    child = _Child()
    clean = False
    attempted = conflict = False
    error = None
    category = "binding"
    try:
        _remaining(deadline)
        cwd = _absolute(cwd)
        env = _checked_environment(env, version, deadline)
        if create:
            payload = _body(candidate, target, cwd, deadline)
        elif not _target_id(target):
            raise WriterRefusal("binding")
        password = secrets.token_urlsafe(32)
        with history_reader._native_directory(cwd, deadline, "database-unsafe") as fd:
            if fd is None:
                raise WriterRefusal("binding")
            category = "admission"
            with gate():
                with _defer_sigint() as pending:
                    try:
                        child.start(
                            host,
                            {**env, "OPENCODE_PASSWORD": password},
                            cwd,
                            fd,
                            deadline,
                        )
                    finally:
                        pending()
            category = "readiness"
            port = child.port(deadline)
            child.check(host, cwd, fd)
            child.own_socket(port, None, deadline)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
                stream.settimeout(_remaining(deadline))
                stream.connect(("127.0.0.1", port))
                exchange = _Exchange(stream, port, password, deadline)
                code, headers, denied = exchange.get(_SERVER_INFO, authenticated=False)
                if (
                    code != 401
                    or headers.get(b"www-authenticate") != b'Basic realm="Secure Area"'
                    or denied
                    != {
                        "_tag": "UnauthorizedError",
                        "message": "Authentication required",
                    }
                ):
                    raise WriterRefusal("identity")
                peer = stream.getsockname()[1]
                child.own_socket(port, peer, deadline)
                _ready(exchange, child, version, env["TMPDIR"])
                category = "admission"
                dispatched = False
                with gate():
                    child.check(host, cwd, fd)
                    child.own_socket(port, peer, deadline)
                    if create:
                        header = exchange.header(
                            "POST", _IMPORT, content_length=len(payload)
                        )
                        attempted = True  # even a failed send may transfer some bytes
                        category = "unacknowledged"
                    else:
                        header = exchange.header(
                            "GET", _EXPORT.format(target=target), close=True
                        )
                        category = "readback"
                    exchange.send(header)
                    dispatched = True
                if not dispatched:
                    raise WriterRefusal(category)
                if create:
                    exchange.send(payload)
                    code, _, value = exchange.receive(close=True)
                    if code == 409 and value == {
                        "_tag": "ConflictError",
                        "message": "Session already exists: " + target,
                        "resource": target,
                    }:
                        conflict = True
                        raise WriterRefusal("conflict")
                    if (
                        code != 200
                        or type(value) is not dict
                        or value.keys() != {"data"}
                        or type(data := value["data"]) is not dict
                        or data.get("id") != target
                        or data.get("location") != {"directory": cwd}
                    ):
                        raise WriterRefusal("unacknowledged")
                    result = target
                else:
                    code, _, value = exchange.receive(
                        close=True, max_body=MAX_ARCHIVE_BYTES
                    )
                    result = _readback_projection(code, value, target)
                child.check(host, cwd, fd)
                _remaining(deadline)
    except (KeyboardInterrupt, SystemExit) as exc:
        error = exc
    except WriterRefusal as exc:
        category, error = exc.category, True
    except Exception:
        # Native/network/guard exception text can carry paths or transcript
        # fragments. Only the closed phase/category can cross this boundary.
        error = True
    finally:
        try:
            with _defer_sigint() as pending:
                clean = child.retire()
            pending()
        except (KeyboardInterrupt, SystemExit) as exc:
            error = exc
        except Exception:
            clean = False
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        error.history_create_attempted = attempted
        error.history_write_uncertain = attempted and (not conflict or not clean)
        error.history_destination_cleanup_failed = not clean
        raise error from None
    if error is not None or not clean:
        raise WriterRefusal(
            category if error is not None else "cleanup",
            operation=operation,
            attempted=attempted,
            uncertain=attempted and (not conflict or not clean),
            cleanup=not clean,
        ) from None
    return result
