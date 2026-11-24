"""Bounded guest catalog, selection, and transcript on the exact native service."""

from dataclasses import dataclass
from time import monotonic

if __package__:
    from . import (
        web_guest_duplex_exec,
        web_named_studio_catalog as catalog,
        web_native_pair,
        web_studio_conversation as studio,
        web_studio_guest,
        web_studio_guest_binding,
        web_studio_guest_selection,
        web_studio_guest_send,
    )
else:
    import web_guest_duplex_exec
    import web_named_studio_catalog as catalog
    import web_native_pair
    import web_studio_conversation as studio
    import web_studio_guest
    import web_studio_guest_binding
    import web_studio_guest_selection
    import web_studio_guest_send


@dataclass(frozen=True, slots=True)
class Catalog:
    pass


@dataclass(frozen=True, slots=True)
class Select:
    sequence: int
    session_id: str | None


@dataclass(frozen=True, slots=True)
class Transcript:
    session_id: str
    generation: int


def _catalog(peer, identity, directory, hidden):
    before = catalog._snapshot(peer.query, identity, directory)
    if before.info["pid"] != identity.native_pid:
        raise studio.ConversationError()
    value = catalog._project_page(
        peer.query(catalog._list_path(directory)),
        directory,
        before,
        hidden,
    )
    if before != catalog._snapshot(peer.query, identity, directory, closing=True):
        raise studio.ConversationError()
    return value


def _selected(peer, identity, directory, session_id):
    path = "/api/session/" + session_id
    before = studio._snapshot(
        peer.query, identity.endpoint, directory, path, session_id
    )
    if before[0] != identity.native_pid:
        raise studio.ConversationError()
    studio._require_unmoved(peer.query, path + "/message")
    after = studio._snapshot(
        lambda target: peer.query(target, closing=target == path),
        identity.endpoint,
        directory,
        path,
        session_id,
    )
    if after != before:
        raise studio.ConversationError()
    return session_id


def _cookie_secret(binding, cookie):
    name = web_native_pair._cookie_name(binding.authority)
    values = [
        value
        for field in cookie.split(";")
        if (row := field.strip().partition("="))[0] == name
        and row[1] == "="
        and (value := row[2])
    ]
    if len(values) != 1 or not web_native_pair._VALUE.fullmatch(values[0]):
        raise studio.ConversationError()
    return values[0]


def read(
    binding,
    workers,
    worker_use,
    admission,
    use,
    grant,
    selections,
    *,
    operation,
    cookie,
):
    """Run only a fixed read-only operation and report this transport's retirement.

    The route owner must have validated its exact origin, request schema and
    Page/App bearer, and be tracking cancellation on the original use. It
    must independently account for the cookie-verification helper and the
    browser exchange. No installed route is activated here.
    """
    if (
        not web_studio_guest_binding._accepts_worker_registry(binding, workers)
        or type(admission) is not web_studio_guest.GuestAdmission
        or type(selections) is not web_studio_guest_selection.GuestSelections
        or admission._binding is not binding.native.binding
        or selections._admission is not admission
        or type(use) is not web_studio_guest.GuestUse
        or type(grant) is not web_studio_guest.GuestGrant
        or type(operation) not in (Catalog, Select, Transcript)
    ):
        return None, True
    relay = view = value = None
    retired = True
    try:
        if not admission.authorize_native(use, grant, cookie=cookie):
            return None, True
        if type(operation) is Select and operation.session_id is None:
            return selections.change(
                use, sequence=operation.sequence, session_id=None
            ).public_state(), True
        if type(operation) is Transcript:
            view = selections.snapshot(
                use, session_id=operation.session_id, generation=operation.generation
            )
        identity = binding.identity
        owner = binding.owner
        directory = studio._directory(owner.owner.box.container_project(owner.data))
        hidden = (_cookie_secret(binding.native.binding, cookie),)
        deadline = min(use.deadline, monotonic() + studio.HOST_TIMEOUT)

        def check():
            if (
                not workers.current(worker_use)
                or use.cancelled
                or monotonic() >= deadline
            ):
                raise studio.ConversationError()

        def current():
            check()
            if view is None:
                if not admission.native_current(use):
                    raise studio.ConversationError()
            elif selections.observe(use, view) is not view:
                raise studio.ConversationError()
            check()

        def open_peer():
            nonlocal relay
            current()
            relay = web_guest_duplex_exec.GuestDuplexExec(binding, workers, worker_use)
            peer = web_studio_guest_send._Peer(
                relay.start(deadline=deadline),
                binding.native.binding,
                cookie,
                deadline,
                check=check,
                current=current,
                identity=identity,
            )
            peer.open()
            return peer

        if type(operation) is Select:

            def verify(origin, request_use, session_id, _):
                if origin is not binding.native.binding or request_use is not use:
                    raise studio.ConversationError()
                # The authenticated change has already invalidated the old
                # selection before relay startup or native membership I/O.
                return _selected(open_peer(), identity, directory, session_id)

            view = selections.change(
                use,
                sequence=operation.sequence,
                session_id=operation.session_id,
                verify=verify,
            )
            value = view.public_state()
        elif type(operation) is Catalog:
            value = _catalog(open_peer(), identity, directory, hidden)
        else:
            peer = open_peer()
            value = studio._conversation(
                lambda path, limit: peer.query(path, limit=limit, raw=True),
                identity.endpoint,
                directory,
                view.session_id,
                hidden,
            )
        current()
    except Exception:
        value = None
    finally:
        if relay is not None:
            try:
                retired = relay.retire() is True
            except Exception:
                retired = False
    if not retired:
        admission.revoke(binding.native.binding, grant)
    if value is not None and retired:
        try:
            current()
        except Exception:
            value = None
    return (value if retired else None), retired
