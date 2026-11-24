"""One unserved binding of the actual gateway and original owning native run."""

from dataclasses import dataclass
from threading import Event

if __package__:
    from . import (
        web_studio_guest,
        web_studio_guest_native,
        web_studio_guest_owner,
        workspace_service,
    )
else:
    import web_studio_guest
    import web_studio_guest_native
    import web_studio_guest_owner
    import workspace_service


class GuestBindingError(ValueError):
    def __init__(self):
        super().__init__("Studio guest owner and gateway could not be bound.")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _UnservedBinding:
    gateway: workspace_service.Gateway
    listener: workspace_service._GatewayListenerProof
    owner: web_studio_guest_owner.GuestOwnerProof
    identity: web_studio_guest_owner.GuestOwnerIdentity
    native: web_studio_guest_native.GuestNativeScope
    _retired: Event

    def current(self):
        """Recheck the exact pair; this is not a live request observer."""
        binding = self.native.binding
        if self._retired.is_set():
            return None
        checked = False
        try:
            if (
                _local_match(self.gateway, self.listener, self.owner)
                and self.owner.current() is self.identity
                and _local_match(self.gateway, self.listener, self.owner)
                and self.listener.origin(self.gateway, listening=False)
                == binding.origin
                and not self._retired.is_set()
            ):
                checked = True
                return binding
            return None
        finally:
            if not checked:
                self._retired.set()


def _local_match(gateway, listener, owner):
    return (
        listener.is_unserved_probe(gateway)
        and gateway.box is owner.owner.box
        and gateway.connection is owner.owner.connection
        and gateway.data == owner.pinned_data()
    )


def bind_unserved(gateway, listener, owner):
    """Bind one explicitly reserved but non-activatable gateway for testing.

    This exercises the owning handoff, not a route/grant. An attach-only
    connection, an arbitrary URL, or a merely matching dict cannot supply
    the actual held proof. The caller owns the original scopes throughout.
    """
    if (
        type(gateway) is not workspace_service.Gateway
        or type(listener) is not workspace_service._GatewayListenerProof
        or type(owner) is not web_studio_guest_owner.GuestOwnerProof
        or not _local_match(gateway, listener, owner)
    ):
        raise GuestBindingError()
    identity = owner.current()
    if identity is None or not _local_match(gateway, listener, owner):
        raise GuestBindingError()
    origin = listener.origin(gateway, listening=False)
    if origin is None:
        raise GuestBindingError()
    with gateway.lock:
        if gateway._guest_preview_bound or not _local_match(gateway, listener, owner):
            raise GuestBindingError()
        gateway._guest_preview_bound = True
    binding = web_studio_guest.GuestBinding(
        identity.workspace_id, identity.generation, identity.container_id, origin
    )
    native = web_studio_guest_native.GuestNativeScope(
        binding, identity.endpoint, identity.native_pid, identity.version
    )
    return _UnservedBinding(gateway, listener, owner, identity, native, Event())
