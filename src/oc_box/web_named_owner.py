"""Unserved named HTTPS composition over one already-verified private attach.

The host lease holder supplies the exact post-reply attach stream and a
read-only view of that owner. This module does not acquire or extend leases,
discover services, open listeners, or provide a namespace connector.
"""

import os
import re
import select
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock

if __package__:
    from . import (
        web_admission,
        web_app_ingress,
        web_local_origins,
        web_named_local,
        web_named_relay,
        web_native_pair,
        workspace_service,
    )
else:
    import web_admission
    import web_app_ingress
    import web_local_origins
    import web_named_local
    import web_named_relay
    import web_native_pair
    import workspace_service


class OwnerLeaseError(ValueError):
    """Fixed refusal without paths, identities, or subprocess diagnostics."""

    def __init__(self):
        super().__init__("Named workspace owner lease could not be verified.")


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """Verified owner generation, independent of optional UI-reporting labels."""

    workspace_id: str
    generation: str
    container_id: str
    version: str
    endpoint: str

    def __post_init__(self):
        if (
            any(
                type(value) is not str
                or len(value) != size
                or re.fullmatch(r"[a-f0-9]+", value) is None
                for value, size in (
                    (self.workspace_id, 32),
                    (self.generation, 32),
                    (self.container_id, 64),
                )
            )
            or type(self.version) is not str
            or self.version != web_native_pair._VERSION
        ):
            raise OwnerLeaseError()
        try:
            web_native_pair._port(self.endpoint)
        except web_native_pair.NativePairError:
            raise OwnerLeaseError() from None


def connect_workspace_native(box, identity, *, track, check, deadline):
    """Explicit managed connector for a *verified* lease, not an owner lookup."""
    if type(identity) is not OwnerIdentity:
        raise OwnerLeaseError()
    # The registration carries the request's check and deadline. It owns the
    # socket and exact process before any bytes or external work can start.
    track.start_workspace_relay(
        box, container_id=identity.container_id, endpoint=identity.endpoint
    )


def _absolute(path):
    return (
        type(path) is str
        and 0 < len(path) <= 4096
        and path.startswith("/")
        and not path.startswith("//")
        and "\0" not in path
        and os.path.normpath(path) == path
    )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class VerifiedOwnerLease:
    """Explicit trusted-host contract, not verification by construction.

    The producer must have verified the existing same-user owner, immutable
    container and exact running generation before constructing this handle.
    ``control`` is the original connected attach socket held by the producer,
    not a path, duplicate, or new probe. ``observe`` is a bounded, thread-safe,
    read-only recheck of that same owner and container run, including native
    readiness and lifecycle fences; it returns None on any uncertainty. No
    refresh, reconnect, follow-by-name, or bare PID/socket probe is sufficient.

    ``connect_native(identity, *, track, check, deadline)`` must obey the
    NamedNativeRelay connector contract in the pinned container namespace.
    A verified producer can explicitly bind ``connect_workspace_native`` to
    the launcher module; there is no automatic connector or owner discovery.
    ``project`` and ``home`` are verified *container* paths. The only secret
    is the existing ephemeral native service control password for pairing,
    never a browser password or vault value.
    """

    identity: OwnerIdentity
    control: socket.socket
    project: str
    home: str
    native_password: str
    observe: Callable[[], OwnerIdentity | None]
    connect_native: Callable

    def __post_init__(self):
        if (
            type(self.identity) is not OwnerIdentity
            or type(self.control) is not socket.socket
            or self.control.family != socket.AF_UNIX
            or self.control.type != socket.SOCK_STREAM
            or not _absolute(self.project)
            or not _absolute(self.home)
            or type(self.native_password) is not str
            or not callable(self.observe)
            or not callable(self.connect_native)
        ):
            raise OwnerLeaseError()
        try:
            workspace_service.validate_endpoint(
                {"url": self.identity.endpoint, "password": self.native_password}
            )
        except ValueError:
            raise OwnerLeaseError() from None


def _descriptor(control):
    fd = control.fileno()
    info = os.fstat(fd)
    return fd, info.st_dev, info.st_ino


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class BoundNamedApp:
    sign_in: web_named_local.LocalNamedSignIn
    ingress: web_app_ingress.NamedAppIngress
    relay: web_named_relay.NamedNativeRelay


class NamedOwnerBinding:
    """One named app lifetime, sharing the same fence through pairing and relay."""

    def __init__(self, box, *, origins, lease):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(origins) is not web_local_origins.LocalOrigins
            or type(lease) is not VerifiedOwnerLease
        ):
            raise OwnerLeaseError()
        self._box = box
        self._origins = origins
        self._lease = lease
        identity = lease.identity
        self._binding = web_admission.WebBinding(
            identity.workspace_id, identity.generation, origins.workspace.value
        )
        self._closed = Event()
        self._lock = Lock()
        self._app = None
        try:
            control = lease.control
            control.getpeername()  # A bound/listening socket is not an attach.
            _, uid, _ = struct.unpack(
                "3i", control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            self._control = _descriptor(control)
        except (OSError, ValueError, struct.error):
            raise OwnerLeaseError() from None
        if uid != os.getuid() or not self._owner_current(self.binding):
            raise OwnerLeaseError()

    @property
    def binding(self):
        return self._binding

    def _quiet_control(self):
        control = self._lease.control
        if _descriptor(control) != self._control:
            return False
        poll = select.poll()
        poll.register(self._control[0], select.POLLIN | select.POLLRDHUP)
        # A completed attach never sends another reply. EOF, error or even
        # unexpected data is loss, without reading from a borrowed socket.
        return not poll.poll(0) and _descriptor(control) == self._control

    def _owner_current(self, binding):
        if (
            type(binding) is not web_admission.WebBinding
            or binding != self.binding
            or self._closed.is_set()
        ):
            return False
        try:
            if self._quiet_control():
                actual = self._lease.observe()
                if (
                    type(actual) is OwnerIdentity
                    and actual == self._lease.identity
                    and self._quiet_control()
                ):
                    return not self._closed.is_set()
        except Exception:  # noqa: BLE001 - any check error permanently fences this binding.
            pass
        self._closed.set()
        return False

    def _pair_native(self, binding, origin):
        if origin != self._origins.workspace or not self._owner_current(binding):
            raise web_native_pair.NativePairError()
        lease = self._lease
        identity = lease.identity
        # Project is the verified internal workdir. Never use the registry's
        # mutable container name or forward any ambient/vault credentials.
        return web_native_pair.pair_cookie(
            self._box,
            {
                "id": identity.workspace_id,
                "container": identity.container_id,
                "opencode_version": identity.version,
                "project": lease.project,
                "home": lease.home,
            },
            {
                "workspace": identity.workspace_id,
                "version": identity.version,
                "url": identity.endpoint,
                "password": lease.native_password,
            },
            origin=self._origins.workspace,
            binding=self.binding,
            owner_current=self._owner_current,
        )

    def _connect_native(self, endpoint, *, track, check, deadline):
        identity = self._lease.identity
        if endpoint != identity.endpoint:
            raise OwnerLeaseError()
        # The relay supplies its cancellation, lease and deadline check and a
        # track-before-connect hook for the unique stream. Never dial the
        # private port in the host namespace.
        check()
        result = self._lease.connect_native(
            identity, track=track, check=check, deadline=deadline
        )
        check()
        return result

    def compose(self, *, flow, claim_bootstrap):
        """Bind one app to the same guard, pairing worker and pinned connector."""
        if not callable(claim_bootstrap) or not self._owner_current(self.binding):
            raise OwnerLeaseError()
        with self._lock:
            if self._closed.is_set() or self._app is not None:
                raise OwnerLeaseError()
            sign_in = web_named_local.LocalNamedSignIn(
                origins=self._origins,
                binding=self.binding,
                flow=flow,
                owner_current=self._owner_current,
                pair_native=self._pair_native,
            )
            ingress = web_app_ingress.NamedAppIngress(
                sign_in,
                endpoint=self._lease.identity.endpoint,
                claim_bootstrap=claim_bootstrap,
            )
            relay = web_named_relay.NamedNativeRelay(
                ingress, connect_native=self._connect_native
            )
            self._app = BoundNamedApp(sign_in, ingress, relay)
            return self._app

    def close(self):
        """Fence first and cancel admission; caller drains before releasing attach."""
        self._closed.set()
        with self._lock:
            app = self._app
        if app is not None:
            app.ingress.close()
