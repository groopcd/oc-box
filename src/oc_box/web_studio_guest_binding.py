"""One binding of the actual Gateway and original owning native run."""

from dataclasses import dataclass, field
from threading import Event

if __package__:
    from . import (
        web_guest_worker_drain,
        web_studio_guest,
        web_studio_guest_native,
        web_studio_guest_owner,
        workspace_service,
    )
else:
    import web_guest_worker_drain
    import web_studio_guest
    import web_studio_guest_native
    import web_studio_guest_owner
    import workspace_service


class GuestBindingError(ValueError):
    def __init__(self):
        super().__init__("Studio guest owner and gateway could not be bound.")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _OriginalBinding:
    gateway: workspace_service.Gateway
    listener: workspace_service._GatewayListenerProof
    owner: web_studio_guest_owner.GuestOwnerProof
    identity: web_studio_guest_owner.GuestOwnerIdentity
    native: web_studio_guest_native.GuestNativeScope
    _retired: Event
    _fenced: Event | None = None

    def _stopping(self):
        return self._fenced is not None and self._fenced.is_set()

    def listener_current(self):
        return (
            self.listener.origin(self.gateway, listening=self._fenced is not None)
            == self.native.binding.origin
        )

    def _observe_listener(self):
        if self._stopping():
            return False
        if self.listener_current():
            return True
        # Deliberate closure may overlap a registered worker's recheck.
        # The acceptor checks physical identity independently before close.
        if not self._stopping():
            self._retired.set()
        return False

    def current(self):
        """Recheck the exact pair in its expected phase."""
        if self._stopping() or self._retired.is_set():
            return None
        try:
            if not self._observe_listener():
                return None
            if not (
                _same_owner(self.gateway, self.owner)
                and self.owner.current() is self.identity
                and _same_owner(self.gateway, self.owner)
            ):
                self._retired.set()
                return None
            if (
                self._observe_listener()
                and not self._stopping()
                and not self._retired.is_set()
            ):
                return self.native.binding
            return None
        except BaseException:
            self._retired.set()
            raise


class _UnservedBinding(_OriginalBinding):
    """Original probe-only handoff; cannot authorize a listening Gateway."""

    __slots__ = ()

    def listener_current(self):
        return (
            self._fenced is None
            and self.listener._probe_only
            and super().listener_current()
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _ManagedBinding(_OriginalBinding):
    """The same exact handoff shape, on a private managed listener only."""

    _workers: web_guest_worker_drain.AcceptedWorkers = field(kw_only=True)

    def accepts_registry(self, workers):
        return self.listener._managed_binding is self and self._workers is workers

    def current(self):
        if self.accepts_registry(self._workers):
            return _OriginalBinding.current(self)
        return None

    def listener_current(self):
        return self.accepts_registry(self._workers) and (
            _OriginalBinding.listener_current(self)
        )

    def lease_current(self):
        """Check the same owning scope after deliberate listener retirement."""
        if (
            not self.accepts_registry(self._workers)
            or not self.owner._in_original_scope()
            or self._retired.is_set()
        ):
            return False
        checked = False
        try:
            checked = (
                _same_owner(self.gateway, self.owner)
                and self.owner.current() is self.identity
                and _same_owner(self.gateway, self.owner)
                and not self._retired.is_set()
            )
            return checked
        finally:
            if not checked:
                self._retired.set()


def _same_owner(gateway, owner):
    return (
        gateway.box is owner.owner.box
        and gateway.connection is owner.owner.connection
        and gateway.data == owner.pinned_data()
    )


def bind_unserved(gateway, listener, owner):
    """Bind one explicitly reserved but non-activatable gateway for testing.

    This exercises the owning handoff, not a route/grant. An attach-only
    connection, an arbitrary URL, or a merely matching dict cannot supply
    the actual held proof. The caller owns the original scopes throughout.
    """
    return _bind(gateway, listener, owner, fenced=None)


def _bind_managed(gateway, listener, owner, *, fenced, workers):
    if (
        type(fenced) is not Event
        or fenced.is_set()
        or type(workers) is not web_guest_worker_drain.AcceptedWorkers
    ):
        raise GuestBindingError()
    return _bind(gateway, listener, owner, fenced=fenced, workers=workers)


def _bind(gateway, listener, owner, *, fenced, workers=None):
    managed = fenced is not None
    if (
        type(gateway) is not workspace_service.Gateway
        or type(listener) is not workspace_service._GatewayListenerProof
        or type(owner) is not web_studio_guest_owner.GuestOwnerProof
        or listener._managed is not managed
        or listener._probe_only is managed
        or (managed and not owner._in_original_scope())
        or not _same_owner(gateway, owner)
        or (origin := listener.origin(gateway, listening=False)) is None
    ):
        raise GuestBindingError()
    identity = owner.current()
    if identity is None or not _same_owner(gateway, owner):
        raise GuestBindingError()
    # Spend the original owner's handoff, not a flag on one reservation.
    # An interrupted or stale first claim cannot be tried on a second socket.
    if (
        not owner.claim_gateway(identity)
        or not _same_owner(gateway, owner)
        or listener.origin(gateway, listening=False) != origin
    ):
        raise GuestBindingError()
    guest = web_studio_guest.GuestBinding(
        identity.workspace_id, identity.generation, identity.container_id, origin
    )
    native = web_studio_guest_native.GuestNativeScope(
        guest, identity.endpoint, identity.native_pid, identity.version
    )
    if not managed:
        return _UnservedBinding(gateway, listener, owner, identity, native, Event())
    binding = _ManagedBinding(
        gateway, listener, owner, identity, native, Event(), fenced, _workers=workers
    )
    listener._managed_binding = binding
    return binding
