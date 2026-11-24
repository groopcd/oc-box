"""Unserved original-owner evidence for an owning 2.4.1 Studio guest."""

from dataclasses import dataclass
import os
import re
import secrets
import signal
import stat
import subprocess

if __package__:
    from . import (
        opencode_v2_io,
        web_named_native_profile,
        web_named_protocol,
        workspace_control,
        workspace_service,
    )
else:
    import opencode_v2_io
    import web_named_native_profile
    import web_named_protocol
    import workspace_control
    import workspace_service


class GuestOwnerError(ValueError):
    def __init__(self):
        super().__init__("Studio guest original workspace owner could not be verified.")


@dataclass(frozen=True, slots=True, repr=False)
class GuestOwnerIdentity:
    workspace_id: str
    generation: str
    container_id: str
    started_at: str
    endpoint: str
    native_pid: int
    version: str


_VERSION = opencode_v2_io.GUEST_OWNER_VERSION
_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


def _regular_host():
    return (
        os.environ.get("OC_BOX_CONTAINER") != "1"
        and os.getuid() != 0
        and os.getuid() == os.geteuid()
    )


def _ready_frame(raw):
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1 or len(raw) > 4096:
        raise GuestOwnerError()
    value = web_named_native_profile.named_native_json(raw)
    if (
        type(value) is not dict
        or set(value) != {"protocol", "endpoint", "native"}
        or value["protocol"] != opencode_v2_io.GUEST_OWNER_PROTOCOL
        or type(endpoint := value["endpoint"]) is not dict
        or endpoint.keys() != {"url", "password"}
        or type(endpoint["password"]) is not str
        or _TOKEN.fullmatch(endpoint["password"]) is None
    ):
        raise GuestOwnerError()
    port = web_named_native_profile.named_native_port(endpoint["url"])
    url = f"http://127.0.0.1:{port}"
    native = value["native"]
    if (
        endpoint["url"] != url
        or type(native) is not dict
        or native.keys() != {"version", "pid", "urls"}
        or native["version"] != _VERSION
        or native["urls"] != [url]
        or type(pid := native["pid"]) is not int
        or not 0 < pid <= 2**31 - 1
    ):
        raise GuestOwnerError()
    return endpoint, pid


class GuestOwnerProof:
    """Host-held original owning scope, not a controller or attach protocol.

    Prepared before spawning the private worker and sealed once from that
    worker's private pipe. No gateway, lease grant, or shutdown authority.
    """

    def __init__(self, owner, stop_token):
        if type(owner) is not workspace_service.Owner:
            raise GuestOwnerError()
        held = workspace_service.LIFECYCLE_LOCK.get()
        data = dict(owner.data)
        if (
            not _regular_host()
            or owner.child is not None
            or owner.ready
            or owner.bound
            or owner.listener is not None
            or owner._named_stop_token is not None
            or type(held) is not tuple
            or len(held) != 2
            or held[0] != owner.path
            or not web_named_protocol._match(web_named_protocol._HEX32, data.get("id"))
            or not web_named_protocol._match(web_named_protocol._HEX64, stop_token)
            or not web_named_protocol._match(
                web_named_protocol._NAME, data.get("container")
            )
            or data.get("opencode_version") != _VERSION
            or data.get("ready") is not True
            or data.get("podman_args")
            or data.get("devices")
        ):
            raise GuestOwnerError()
        self.owner, self.data, self.stop_token = owner, data, stop_token
        self._lock_file = held[1]
        self._lock_fd = self._lock_file.fileno()
        info = os.fstat(self._lock_fd)
        self._lock_identity = info.st_dev, info.st_ino
        self._identity = self._child = self._connection = None
        self._retired = False
        self._gateway_claimed = False
        self._generation = secrets.token_hex(16)
        self._engine_env = owner.box.maintenance_host_env()
        self._check_local()
        self._run = web_named_protocol._running_instance(
            owner.box, data, data["container"], env=self._engine_env
        )
        self._check_local()

    def _check_local(self):
        path = self.owner.path
        info = path.with_name("lock").lstat()
        device, inode = self._lock_identity
        if (
            self._retired
            or self._lock_file.fileno() != self._lock_fd
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (info.st_dev, info.st_ino) != self._lock_identity
        ):
            raise GuestOwnerError()
        pinned = os.fstat(self._lock_fd)
        if (pinned.st_dev, pinned.st_ino) != self._lock_identity:
            raise GuestOwnerError()
        with open(f"/proc/self/fdinfo/{self._lock_fd}", "rb") as stream:
            raw = stream.read(4097)
        pid = str(os.getpid()).encode()
        identity = f"{os.major(device):02x}:{os.minor(device):02x}:{inode}".encode()
        if (
            len(raw) > 4096
            or not any(
                row[:1] == [b"lock:"]
                and row[2:]
                == [b"FLOCK", b"ADVISORY", b"WRITE", pid, identity, b"0", b"EOF"]
                for row in (line.split() for line in raw.splitlines())
            )
            or not workspace_control.unchanged(path, self.stop_token)
        ):
            raise GuestOwnerError()
        box = self.owner.box
        if box.maintenance_host_env() != self._engine_env:
            raise GuestOwnerError()
        workspace_service._validate_workspace_identity(box, path, self.data)
        selected, current = box.find_workspace(None, self.data["id"])
        if selected != path or current != self.data or self.owner.data != self.data:
            raise GuestOwnerError()

    def pinned_data(self):
        return {**self.data, "container": self._run[0]}

    def _in_original_scope(self):
        held = workspace_service.LIFECYCLE_LOCK.get()
        return (
            type(held) is tuple
            and len(held) == 2
            and held[0] == self.owner.path
            and held[1] is self._lock_file
        )

    def worker_command(self, source):
        box = self.owner.box
        return box.exec_command(
            self.pinned_data(),
            self.worker_arguments(source, box.CONTAINER_OPENCODE),
            isolated_owner_env=self._engine_env,
        )

    def worker_arguments(self, source, binary):
        return [
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            "-c",
            web_named_native_profile.named_native_worker_source(source),
            "guest-proof-lease",
            binary,
            _VERSION,
        ]

    def claim_gateway(self, identity):
        with self.owner.condition:
            if (
                self._retired
                or self._gateway_claimed
                or identity is None
                or identity is not self._identity
                or not self.owner.ready
                or self.owner.closing
                or self.owner.child is not self._child
            ):
                return False
            self._gateway_claimed = True
            return True

    def _check_run(self):
        self._check_local()
        if (
            web_named_protocol._running_instance(
                self.owner.box, self.data, self._run[0], env=self._engine_env
            )
            != self._run
        ):
            raise GuestOwnerError()
        self._check_local()

    def before_spawn(self):
        if self._child is not None or self.owner.child is not None:
            raise GuestOwnerError()
        self._check_run()

    def seal(self, raw):
        child = self.owner.child
        if self._identity is not None or child is None or child.poll() is not None:
            raise GuestOwnerError()
        endpoint, pid = _ready_frame(raw)
        self._check_run()
        if self.owner.child is not child or child.poll() is not None:
            raise GuestOwnerError()
        self._child, self._connection = child, endpoint
        self._identity = GuestOwnerIdentity(
            self.data["id"],
            self._generation,
            *self._run,
            endpoint["url"],
            pid,
            _VERSION,
        )
        return endpoint

    def before_publish(self):
        verified = False
        try:
            if not self._live(ready=False):
                raise GuestOwnerError()
            self._check_run()
            if not self._live(ready=False):
                raise GuestOwnerError()
            verified = True
        finally:
            if not verified:
                self.retire()

    def _live(self, *, ready=True):
        with self.owner.condition:
            return (
                not self._retired
                and self._identity is not None
                and self.owner._guest_proof is self
                and self.owner.ready is ready
                and not self.owner.closing
                and self.owner.child is self._child
                and self.owner.connection is self._connection
                and self._connection["url"] == self._identity.endpoint
                and self._child.poll() is None
            )

    def current(self):
        """Return only the original generation, with sticky refusal on loss."""
        observed = None
        try:
            if self._live():
                self._check_run()
                if self._live():
                    observed = self._identity
        except (
            self.owner.box.BoxError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            subprocess.SubprocessError,
        ):
            pass
        finally:
            # Also fence an interrupted check. A success requires the same
            # generation to survive the complete observation.
            if observed is None:
                self.retire()
        return observed

    def retire(self):
        with self.owner.condition:
            self._retired = True

    def retire_worker(self, child):
        """Require exact host exec retirement; do not compensate with a kill."""
        if (
            (self._child is not None and child is not self._child)
            or child.returncode is not None
            or child.stdin is None
            or child.stdin.closed
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        ):
            raise GuestOwnerError()
        child.stdin.close()
        opencode_v2_io._wait_exact_child(child, 10)
        if child.returncode != 0:
            raise GuestOwnerError()
