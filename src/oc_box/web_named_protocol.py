"""Unserved, first-mode owner control for the named workspace lease.

The capability and lifecycle lock are local to the already opted-in owning
launcher. This is not a cross-launcher login protocol or a namespace connector.
"""

import contextlib
from datetime import datetime
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from threading import Event
from time import monotonic

if __package__:
    from . import web_named_owner, web_native_pair, workspace_control, workspace_service
else:
    import web_named_owner
    import web_native_pair
    import workspace_control
    import workspace_service


_PROTOCOL = "oc-box.named-owner/1"
_ACTION = "named-attach"
_LIMIT = 4096
_TIMEOUT = 4
_INSPECT_TIMEOUT = 2
_INSPECT_LIMIT = 1024 * 1024
_HEX32 = re.compile(r"[a-f0-9]{32}")
_HEX64 = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_STARTED = re.compile(
    r"(?:19[7-9][0-9]|[2-9][0-9]{3})-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])"
)
_REQUEST_FIELDS = {"action", "protocol", "workspace", "nonce"}
_PROOF = "proof"
OwnerLeaseError = web_named_owner.OwnerLeaseError


def _match(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _unique(items):
    value = dict(items)
    if len(value) != len(items):
        raise OwnerLeaseError()
    return value


def _read(stream):
    """One bounded frame and no observable surplus, under an absolute deadline."""
    deadline = monotonic() + _TIMEOUT
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - monotonic()
        if remaining <= 0 or len(data) >= _LIMIT:
            raise OwnerLeaseError()
        stream.settimeout(remaining)
        chunk = stream.recv(_LIMIT - len(data))
        if not chunk:
            raise OwnerLeaseError()
        data.extend(chunk)
    if data.find(b"\n") != len(data) - 1 or monotonic() >= deadline:
        raise OwnerLeaseError()
    value = json.loads(data, object_pairs_hook=_unique)
    if type(value) is not dict:
        raise OwnerLeaseError()
    return value


def _running_instance(box, data, target):
    """Read the real owned running instance, never retry by mutable name."""
    with box.captured_output(private=True) as output:
        result = box.podman(
            "container",
            "inspect",
            target,
            private=True,
            check=False,
            timeout=_INSPECT_TIMEOUT,
            env=box.maintenance_host_env(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode:
            raise OwnerLeaseError()
        output.seek(0)
        payload = output.read(_INSPECT_LIMIT + 1)
    if not 0 < len(payload) <= _INSPECT_LIMIT:
        raise OwnerLeaseError()
    entries = json.loads(payload, object_pairs_hook=_unique)
    if type(entries) is not list or len(entries) != 1 or type(entries[0]) is not dict:
        raise OwnerLeaseError()
    info = entries[0]
    container = info.get("Id")
    state = info["State"]
    started = state.get("StartedAt")
    labels = info["Config"]["Labels"]
    host = info["HostConfig"]
    network = host.get("NetworkMode")
    mounts = info["Mounts"]
    if (
        not _match(_HEX64, container)
        or labels.get(box.LABEL) != data["id"]
        or state.get("Status") != "running"
        or state.get("Running") is not True
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or not _match(_STARTED, started)
        or info.get("Pod") not in (None, "")
        or type(network) is not str
        or network.split(":", 1)[0]
        not in ("pasta", "slirp4netns", "bridge", "private", "none")
        or host.get("PortBindings") not in (None, {})
        or type(mounts) is not list
        or any(type(mount) is not dict for mount in mounts)
    ):
        raise OwnerLeaseError()
    # Validate the calendar without normalizing away Podman's nanoseconds.
    datetime.fromisoformat(started[:19])
    project = [
        mount
        for mount in mounts
        if mount.get("Destination") == box.container_project(data)
    ]
    home = [mount for mount in mounts if mount.get("Destination") == data["home"]]
    if (
        len(project) != 1
        or project[0].get("Type") != "bind"
        or project[0].get("Source") != data["project"]
        or len(home) != 1
        or home[0].get("Type") != "volume"
        or home[0].get("Name") != data["home_volume"]
    ):
        raise OwnerLeaseError()
    return container, started


class NamedControl:
    """One owner-generated capability, one claim, no mode conversion or retry."""

    def __init__(self, owner, stop_token):
        self.owner = owner
        self.box, self.path, self.data = owner.box, owner.path, dict(owner.data)
        self.stop_token = stop_token
        held = workspace_service.LIFECYCLE_LOCK.get()
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or held is None
            or held[0] != self.path
            or not _match(_HEX32, self.data.get("id"))
            or not _match(_HEX64, stop_token)
            or not _match(_NAME, self.data.get("container"))
            or not web_native_pair.named_native_supports(
                self.data.get("opencode_version")
            )
            or self.data.get("ready") is not True
            or self.data.get("podman_args")
        ):
            raise OwnerLeaseError()
        self._lock_file = held[1]
        self.generation = secrets.token_hex(16)
        self._key = secrets.token_bytes(32)
        self._retired = Event()
        self._claimed = False
        self._active = None
        self._child = None
        self.identity = None
        self._password = None
        if not self._stop_current():
            raise OwnerLeaseError()
        workspace_service._validate_workspace_identity(self.box, self.path, self.data)
        self._run = _running_instance(self.box, self.data, self.data["container"])
        if not self._stop_current():
            raise OwnerLeaseError()

    def _stop_current(self):
        try:
            descriptor = os.fstat(self._lock_file.fileno())
            current = self.path.with_name("lock").lstat()
            return (
                stat.S_ISREG(current.st_mode)
                and current.st_uid == os.getuid()
                and not (current.st_mode & 0o022)
                and (current.st_dev, current.st_ino)
                == (descriptor.st_dev, descriptor.st_ino)
                and workspace_control.unchanged(self.path, self.stop_token)
            )
        except (OSError, ValueError):
            return False

    def pinned_data(self):
        return {**self.data, "container": self._run[0]}

    def worker_arguments(self, source, binary):
        return [
            "python3",
            "-c",
            web_native_pair.named_native_worker_source(source),
            "named-lease",
            binary,
            self.data["opencode_version"],
        ]

    def run_current(self):
        if self._retired.is_set() or not self._stop_current():
            return False
        try:
            workspace_service._validate_workspace_identity(
                self.box, self.path, self.data
            )
            return (
                _running_instance(self.box, self.data, self._run[0]) == self._run
                and self._stop_current()
                and not self._retired.is_set()
            )
        except (
            self.box.BoxError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,
        ):
            return False

    def seal(self, connection):
        if not self.run_current() or self.owner.child.poll() is not None:
            raise OwnerLeaseError()
        if set(connection) != {"url", "password"}:
            raise OwnerLeaseError()
        self.identity = web_named_owner.OwnerIdentity(
            self.data["id"],
            self.generation,
            self._run[0],
            self.data["opencode_version"],
            connection["url"],
        )
        self._password = connection["password"]
        self._child = self.owner.child

    def _mac(self, direction, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
        return hmac.digest(self._key, direction + b"\0" + encoded, "sha256").hex()

    def _payload(self, frame, direction):
        proof = frame.pop(_PROOF, None)
        if not _match(_HEX64, proof) or not hmac.compare_digest(
            proof, self._mac(direction, frame)
        ):
            raise OwnerLeaseError()
        return frame

    def _send(self, stream, direction, payload):
        frame = json.dumps({**payload, _PROOF: self._mac(direction, payload)}).encode(
            "ascii"
        )
        if len(frame) >= _LIMIT:
            raise OwnerLeaseError()
        stream.settimeout(_TIMEOUT)
        stream.sendall(frame + b"\n")

    def _reply(self, nonce):
        identity = self.identity
        return {
            "protocol": _PROTOCOL,
            "nonce": nonce,
            "workspace": identity.workspace_id,
            "generation": identity.generation,
            "container": identity.container_id,
            "started_at": self._run[1],
            "version": identity.version,
            "url": identity.endpoint,
            "password": self._password,
        }

    def _live(self):
        # Called under the owner's lock. This is the original monitored native
        # lease worker, not a PID lookup or a substitute for run/stop checks.
        return (
            not self._retired.is_set()
            and self.owner.ready
            and not self.owner.closing
            and self.owner._named_control is self
            and self._child is not None
            and self.owner.child is self._child
            and self._child.poll() is None
        )

    def _current(self, nonce):
        with self.owner.condition:
            return self._active == nonce and self._live()

    def serve(self, stream):
        """Entered only after Owner.handle verified kernel same-user credentials."""
        try:
            request = self._payload(_read(stream), b"request")
            nonce = request.get("nonce")
            if (
                request.keys() != _REQUEST_FIELDS
                or request["action"] != _ACTION
                or request["protocol"] != _PROTOCOL
                or request["workspace"] != self.data["id"]
                or not _match(_HEX64, nonce)
            ):
                return
            with self.owner.condition:
                if self._claimed or not self._live():
                    return
                # Reserve before any I/O. Once claimed, even uncertain reply or
                # early disconnect requires a new owner; never reopen in place.
                self._claimed = True
                self._active = nonce
                self.owner.clients += 1
            try:
                if not self.run_current() or not self._current(nonce):
                    return
                self._send(stream, b"reply", self._reply(nonce))
                stream.settimeout(0.25)
                while self._current(nonce) and self._stop_current():
                    try:
                        stream.recv(1)  # EOF or ANY extra input ends this lease.
                        break
                    except TimeoutError:
                        pass
            finally:
                self._retired.set()
                with self.owner.condition:
                    self._active = None
                    self.owner.clients -= 1
                    self.owner.condition.notify_all()
        except (OSError, ValueError, RecursionError):
            pass

    def exchange(self, stream, peer_pid):
        # Capability authentication plus the kernel same-UID check in
        # _attach_control. No separate daemon/PID is trusted to impersonate
        # the opted-in, in-process owner.
        if peer_pid != os.getpid():
            raise OwnerLeaseError()
        nonce = secrets.token_hex(32)
        self._send(
            stream,
            b"request",
            {
                "action": _ACTION,
                "protocol": _PROTOCOL,
                "workspace": self.data["id"],
                "nonce": nonce,
            },
        )
        reply = self._payload(_read(stream), b"reply")
        if reply != self._reply(nonce) or not self._current(nonce):
            raise OwnerLeaseError()
        return reply

    def lease(self, attached, connector):
        nonce = attached.connection["nonce"]
        stream = attached.stream
        descriptor = web_named_owner._descriptor(stream)

        def observe():
            try:
                if (
                    self._current(nonce)
                    and web_named_owner._quiet_control(stream, descriptor)
                    and self.run_current()
                    and web_named_owner._quiet_control(stream, descriptor)
                    and self._current(nonce)
                ):
                    return self.identity
            except (OSError, ValueError):
                pass
            self._retired.set()
            return None

        if observe() is None:
            raise OwnerLeaseError()
        return web_named_owner.VerifiedOwnerLease(
            self.identity,
            stream,
            self.box.container_project(self.data),
            self.data["home"],
            self._password,
            observe,
            connector,
        )


@contextlib.contextmanager
def verified_owner_lease(owner, *, connect_native):
    """Produce a verified lease for this trusted caller's already running owner.

    There is no default connector. The trusted caller closes admission and
    drains workers before exiting this context. No Podman relay is spawned here.
    """
    if (
        type(owner) is not workspace_service.Owner
        or type(owner._named_control) is not NamedControl
        or not callable(connect_native)
    ):
        raise OwnerLeaseError()
    control = owner._named_control
    with contextlib.ExitStack() as stack:
        try:
            attached = stack.enter_context(
                workspace_service._attach_control(
                    owner.box,
                    owner.path,
                    owner.data,
                    named=control,
                )
            )
            lease = control.lease(attached, connect_native)
        except (
            owner.box.BoxError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RecursionError,
        ):
            raise OwnerLeaseError() from None
        yield lease
