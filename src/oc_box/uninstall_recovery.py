"""Unserved, one-attempt write-ahead fence for a future full uninstall actor.

No recovery anchor is discovered, provisioned, reset, or reconciled here. No
engine, installation, or registry operations are dispatched. The caller must
hold an independently verified installation/store admission and exclusion
through dispatch, verification, and cleanup, not merely hold this file lock.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import stat
import threading

if __package__:
    from . import workspace_uninstall as model
    from .uninstall_review import UninstallRequest
else:
    import workspace_uninstall as model
    from uninstall_review import UninstallRequest


JOURNAL = "full-uninstall-v1.jsonl"
_LINE_LIMIT = 1024
_MAX_BYTES = (1 + 4 * model.MAX_WORKSPACES) * _LINE_LIMIT
_HEX = re.compile(r"[a-f0-9]{64}")
_ENTRY = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


class RecoveryRefusal(ValueError):
    """Fixed refusal; never render paths, proof values, or native replies."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Full uninstall recovery fence refused ({reason}).")


def _fail(reason):
    raise RecoveryRefusal(reason) from None


@dataclass(frozen=True, slots=True)
class ReadyAnchor:
    """Provisioned independently, not captured from a journal at admission.

    The private directory and pristine header have the file stamps described
    by workspace_uninstall._file_stamp. The stamped baseline is never updated
    by this module. Loss of either it or the journal requires reconciliation.
    """

    generation: str
    directory_stamp: tuple[int, ...]
    journal_stamp: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReviewProof:
    """Digests of the verified installation, durable store and exact approval.

    The future profile must supply these, including proof beyond WorkspacePlan
    (notably ownership, consumer exclusion and physical storage bindings).
    EngineScope mount IDs are live observations, not the durable store digest.
    """

    installation: str
    store: str
    approval: str


@dataclass(frozen=True, slots=True, eq=False)
class DispatchReservation:
    """Pass-local identity, not a persisted permission or a dispatch result."""

    scope: model.EngineScope
    resource: model.ResourceRef


def _line(value):
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(raw) >= _LINE_LIMIT:
        _fail("recovery-limit")
    return raw + b"\n"


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _stamp(value, count):
    return (
        type(value) is tuple
        and len(value) == count
        and all(type(part) is int and 0 <= part < 2**64 for part in value)
    )


def _storage_values(path):
    return path.path, path.identity


def _projection(plan):
    try:
        steps = model._execution_steps(plan)
    except model._ExecutionRefusal:
        _fail("invalid-recovery-review")
    if (
        not steps
        or plan.registry_present is not True
        or not model._local_scope(plan.engine)
        or not model._storage_layout(plan.engine)
    ):
        _fail("invalid-recovery-review")
    graph, run, volume = (
        plan.engine.graph_root,
        plan.engine.run_root,
        plan.engine.volume_root,
    )
    scope = _binding_digest(tuple(map(_storage_values, (graph, run, volume))))
    targets = []
    for index, resource in steps:
        workspace = plan.workspaces[index]
        registration = workspace.registration
        if (
            not model._matches(_HEX, registration.fingerprint)
            or not _stamp(registration.record_stamp, 8)
            or not _stamp(registration.directory_stamp, 5)
        ):
            _fail("invalid-recovery-review")
        if resource.kind == "container":
            expected = registration.container_name
            paths = workspace.container.writable_paths
            if (
                type(paths) is not tuple
                or not 1 <= len(paths) <= model.MAX_STORAGE_ALIASES
                or not all(map(model._storage, paths))
            ):
                _fail("invalid-recovery-review")
            created = None
        else:
            expected = registration.home_volume
            home = workspace.home_volume
            created, paths = home.created_at, (home.mountpoint,)
            if not model._valid_created(created) or not model._storage(paths[0]):
                _fail("invalid-recovery-review")
        if type(expected) is not str or resource.name != expected:
            _fail("invalid-recovery-review")
        targets.append(
            (
                index,
                resource,
                {
                    "registration": _binding_digest(
                        (
                            registration.fingerprint,
                            registration.record_stamp,
                            registration.directory_stamp,
                        )
                    ),
                    "backing": _binding_digest(tuple(map(_storage_values, paths))),
                    "created": created,
                },
            )
        )
    return scope, tuple(targets)


def _binding_digest(values):
    # A validated typed projection, not a serialization of inventory, paths
    # from native command output, or raw workspace metadata into the journal.
    return _digest(json.dumps(values, separators=(",", ":")).encode("ascii"))


def _journal_stamp(fd, mount):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_BYTES
        or model._mount_id(fd) != mount
    ):
        _fail("unsafe-recovery-anchor")
    return model._file_stamp(info)


@contextmanager
def _errors():
    try:
        yield
    except (OSError, model.WorkspacePlanError):
        _fail("recovery-io-unverified")


def _check(callback, reason, *args):
    try:
        valid = callback(*args)
    except Exception:
        _fail(reason)
    if valid is not True:
        _fail(reason)


@contextmanager
def open_fence(
    directory_fd, anchor, request, reviewed, proof, *, check_admission, revalidate
):
    """Borrow the authority's already-bound directory; never use a pathname.

    Admission must resolve the one independently provisioned ReadyAnchor for
    this authority, not a new journal per plan or a fallback directory. Its
    exact private v1 header is ``{"generation":"...","mode":"full","version":1}``
    plus LF. ``check_admission(anchor, proof)`` must recheck the anchor's named
    binding and still-held authority/exclusion. ``revalidate(proof, expected,
    retired_ids)`` must compare the complete live proof (not just the expected
    WorkspacePlan) and refuse retired container IDs anywhere in the inventory.
    Both accept only exact True. The frozen ReviewProof never changes.
    A pristine journal is a prerequisite, never approval. Do not fork, replay
    a reservation, reuse invalidated approval, or let a handle escape the
    admitted lock/exclusion scope. Use the handle on the opening thread only.
    """
    if (
        type(request) is not UninstallRequest
        or request.full is not True
        or request.dry_run is not False
        or type(proof) is not ReviewProof
        or type(anchor) is not ReadyAnchor
        or type(directory_fd) is not int
        or directory_fd < 0
        or not callable(check_admission)
        or not callable(revalidate)
        or not all(
            model._matches(_HEX, value)
            for value in (
                anchor.generation,
                proof.installation,
                proof.store,
                proof.approval,
            )
        )
        or not _stamp(anchor.directory_stamp, 8)
        or not _stamp(anchor.journal_stamp, 8)
    ):
        _fail("invalid-recovery-admission")
    scope, targets = _projection(reviewed)
    with _errors():
        directory = os.open(".", model._DIR, dir_fd=directory_fd)
        try:
            info = os.fstat(directory)
            if (
                info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or model._file_stamp(info) != anchor.directory_stamp
            ):
                _fail("unsafe-recovery-anchor")
            mount = model._mount_id(directory)
            entry = os.open(JOURNAL, _ENTRY, dir_fd=directory)
            try:
                # Do not open a substituted special file for I/O.
                if _journal_stamp(entry, mount) != anchor.journal_stamp:
                    _fail("recovery-not-pristine")
                fd = os.open(
                    f"/proc/self/fd/{entry}",
                    os.O_RDWR | os.O_APPEND | os.O_NOATIME | os.O_CLOEXEC,
                )
            finally:
                os.close(entry)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _fail("recovery-in-use")
                attempt = _Attempt(
                    directory,
                    fd,
                    mount,
                    anchor,
                    reviewed,
                    scope,
                    targets,
                    proof,
                    check_admission,
                    revalidate,
                )
                attempt._check_file()
                header = _line(
                    {"generation": anchor.generation, "mode": "full", "version": 1}
                )
                if os.pread(fd, len(header) + 1, 0) != header:
                    _fail("recovery-not-pristine")
                attempt._check()
                try:
                    yield attempt
                finally:
                    attempt._stopped = True
            finally:
                os.close(fd)
        finally:
            os.close(directory)


class _Attempt:
    def __init__(
        self,
        directory,
        fd,
        mount,
        anchor,
        reviewed,
        scope,
        targets,
        proof,
        check,
        revalidate,
    ):
        self._directory, self._fd, self._mount = directory, fd, mount
        self._anchor, self._stamp = anchor, anchor.journal_stamp
        self._expected, self._targets = reviewed, targets
        self._scope, self._proof, self._check_admission = scope, proof, check
        self._revalidate, self._retired = revalidate, frozenset()
        self._position, self._pending, self._stopped = 0, None, False
        self._pid = os.getpid()
        self._thread = threading.get_ident()

    def _check_file(self):
        if model._file_stamp(os.fstat(self._directory)) != self._anchor.directory_stamp:
            _fail("recovery-anchor-changed")
        entry = os.open(JOURNAL, _ENTRY, dir_fd=self._directory)
        try:
            if (
                _journal_stamp(entry, self._mount) != self._stamp
                or _journal_stamp(self._fd, self._mount) != self._stamp
            ):
                _fail("recovery-anchor-changed")
        finally:
            os.close(entry)

    def _check(self):
        _check(
            self._check_admission, "recovery-admission-lost", self._anchor, self._proof
        )
        self._check_file()

    def _check_plan(self, expected, retired):
        self._check()
        _check(
            self._revalidate, "recovery-plan-unverified", self._proof, expected, retired
        )
        self._check()

    def _append(self, raw):
        self._check_file()
        size = os.fstat(self._fd).st_size
        if size + len(raw) > _MAX_BYTES:
            _fail("recovery-limit")
        pending = raw
        while pending:
            count = os.write(self._fd, pending)
            if count <= 0:
                _fail("recovery-io-unverified")
            pending = pending[count:]
        stamp = _journal_stamp(self._fd, self._mount)
        if stamp[5] != size + len(raw):
            _fail("recovery-anchor-changed")
        # A partial append or an fsync failure is not repaired or truncated.
        os.fsync(self._fd)
        os.fsync(self._directory)
        if os.pread(self._fd, len(raw) + 1, size) != raw:
            _fail("recovery-anchor-changed")
        self._stamp = stamp
        self._check_file()

    def _enter_step(self):
        if (
            self._stopped
            or os.getpid() != self._pid
            or threading.get_ident() != self._thread
        ):
            _fail("recovery-attempt-closed")
        # Success is the only path that can permit another step on this handle.
        self._stopped = True

    def reserve(self, resource):
        """Return once, only after the current resource's pending row is durable."""
        self._enter_step()
        if (
            self._pending is not None
            or self._position >= len(self._targets)
            or not model._command_targets(resource)
            or resource != self._targets[self._position][1]
        ):
            _fail("recovery-step-unavailable")
        _, target, generation = self._targets[self._position]
        with _errors():
            self._check_plan(self._expected, self._retired)
            raw = _line(
                {
                    **generation,
                    "installation": self._proof.installation,
                    "store": self._proof.store,
                    "approval": self._proof.approval,
                    "scope": self._scope,
                    "step": self._position,
                    "resource": {
                        "workspace_id": target.workspace_id,
                        "kind": target.kind,
                        "name": target.name,
                        "container_id": target.container_id,
                    },
                    "status": "pending",
                }
            )
            self._append(raw)
            self._check()
        ticket = DispatchReservation(self._expected.engine, target)
        self._pending = ticket, _digest(raw)
        self._stopped = False
        return ticket

    def confirm(self, ticket, *, ack, absent):
        """Only native ACK, independent same-scope absence and recheck can advance.

        ``absent`` receives the exact EngineScope and ResourceRef. The next
        expected plan differs only by this removal. Neither absence nor
        revalidation is attempted for unconfirmed or not-dispatched replies.
        """
        self._enter_step()
        if self._pending is None or ticket is not self._pending[0]:
            _fail("recovery-step-unavailable")
        if ack is not model.RemovalAck.ACKNOWLEDGED:
            _fail("recovery-removal-unconfirmed")
        index, resource, _ = self._targets[self._position]
        expected = model._without_resource(self._expected, index, resource.kind)
        retired = self._retired
        if resource.container_id is not None:
            retired = retired.union((resource.container_id,))
        with _errors():
            self._check()
            _check(absent, "recovery-absence-unverified", ticket.scope, ticket.resource)
            self._check_plan(expected, retired)
            self._append(
                _line(
                    {
                        "pending": self._pending[1],
                        "status": "confirmed",
                        "step": self._position,
                    }
                )
            )
            self._check()
        self._expected, self._retired = expected, retired
        self._pending = None
        self._position += 1
        self._stopped = False
        return model.ResourceOutcome(ticket.resource, model.ResourceState.CONFIRMED)
