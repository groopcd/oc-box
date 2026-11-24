"""Unwired host-private routing fences for foreground Studio lifetimes."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
import fcntl
import hashlib
import math
import os
from pathlib import Path
import secrets
import struct
from threading import Event, get_ident
import time

if __package__:
    from . import studio_registry, workspace_control as control, workspace_service
else:
    import studio_registry
    import workspace_control as control
    import workspace_service


_NAME = "named-stop.lock"
_MAGIC = b"OCBNAM1\n"
_STUDIO_NAME = "studio-stop.lock"
_STUDIO_MAGIC = b"OCBSTU1\n"
# Version, directory and gate identities, generation, state/intent byte.
_FRAME = struct.Struct(">8sQQQQ32sB")
_SIZE = _FRAME.size + 32
_EMPTY = (bytes(32), 0)
_DIRECT_IN_PROGRESS = (_EMPTY[0], 2)
_CATEGORIES = frozenset(
    {
        "invalid-request",
        "unsafe-storage",
        "not-enrolled",
        "already-enrolled",
        "invalid-state",
        "busy",
        "owner-lock",
        "armed",
        "direct-unverified",
        "wrong-lifetime",
        "completion-unverified",
    }
)


class FenceError(ValueError):
    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in _CATEGORIES
            else "unsafe-storage"
        )
        super().__init__(f"Named workspace stop routing refused ({self.category}).")


class StopRoute(Enum):
    DIRECT = "direct"  # The caller's ordinary stop callback ran under the fence.
    PENDING = "pending"  # Intent only, not a drain or a successful stop.


@contextmanager
def _errors():
    try:
        yield
    except FenceError as exc:
        raise exc from None
    except control.ControlError as exc:
        raise FenceError(
            "busy" if exc.category == "control-timeout" else "unsafe-storage"
        ) from None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise FenceError("unsafe-storage") from None


def _path(path):
    try:
        path = Path(path)
        if (
            path.anchor != "/"
            or path.name != "workspace.json"
            or ".." in path.parts
            or len(os.fsencode(path)) > 4096
            or "\0" in str(path)
        ):
            raise ValueError
        return path
    except (TypeError, ValueError, OSError):
        raise FenceError("invalid-request") from None


@dataclass(frozen=True, slots=True, repr=False)
class _StudioScope:
    state: Path
    workspace: studio_registry.StudioWorkspace
    seal: bytes

    @property
    def path(self):
        return (
            self.state
            / studio_registry.NAMESPACE
            / self.workspace.id
            / studio_registry.RECORD_NAME
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _AttachWitness:
    scope: _StudioScope
    gate: tuple[int, int, int, int]
    generation: bytes
    _retired: Event = field(default_factory=Event, init=False)


def _studio_seal(directory, name, workspace):
    studio_registry._owned(os.fstat(directory), private=0o700)
    raw, stamp = studio_registry._read(
        directory, name, studio_registry._MAX_RECORD, private=0o600
    )
    project, _ = studio_registry._record(raw, workspace.id, studio=True)
    if project != workspace.project:
        raise FenceError("wrong-lifetime")
    # Bind the internal record, not a requester-provided path, project
    # label, or native PID. Include descriptor and immutable record stamps
    # so replacing the bytes with an identical copy cannot adopt a fence.
    return hashlib.sha256(
        repr((_identity(directory), stamp)).encode("ascii") + b"\0" + raw
    ).digest()


def studio_scope(state_root, workspace_id):
    """Select the disjoint read-only Studio registration; not provisioning."""
    with _errors():
        state = studio_registry._path(state_root)
        workspace = studio_registry.select(state, workspace_id=workspace_id)
        path = (
            state
            / studio_registry.NAMESPACE
            / workspace.id
            / studio_registry.RECORD_NAME
        )
        with studio_registry._absolute_directory(path.parent) as directory:
            scope = _StudioScope(
                state, workspace, _studio_seal(directory, path.name, workspace)
            )
            if studio_registry.select(state, workspace_id=workspace_id) != workspace:
                raise FenceError("wrong-lifetime")
            return scope


@contextmanager
def _directory(path, scope):
    if scope is None:
        with control._workspace_directory(path) as directory:
            yield directory
        return
    if (
        studio_registry.select(scope.state, workspace_id=scope.workspace.id)
        != scope.workspace
    ):
        raise FenceError("wrong-lifetime")
    with studio_registry._absolute_directory(path.parent) as directory:
        if _studio_seal(directory, path.name, scope.workspace) != scope.seal:
            raise FenceError("wrong-lifetime")
        yield directory


def _deadline(timeout):
    if (
        type(timeout) not in (int, float)
        or not 0 <= timeout <= control.MAX_TIMEOUT
        or not math.isfinite(timeout)
    ):
        raise FenceError("invalid-request")
    return time.monotonic() + timeout


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _require_flock(fd, category):
    """Verify this open description holds our kernel lock, without acquiring it."""
    device, inode = _identity(fd)
    identity = f"{os.major(device):02x}:{os.minor(device):02x}:{inode}".encode()
    pid = str(os.getpid()).encode()
    with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096 or not any(
        fields[:1] == [b"lock:"]
        and fields[2:6] == [b"FLOCK", b"ADVISORY", b"WRITE", pid]
        and fields[6:] == [identity, b"0", b"EOF"]
        for fields in (line.split() for line in raw.splitlines())
    ):
        raise FenceError(category)


class _OwnerLock:
    def __init__(self, path, directory, stack):
        self.held = workspace_service.LIFECYCLE_LOCK.get()
        if type(self.held) is not tuple or len(self.held) != 2 or self.held[0] != path:
            raise FenceError("owner-lock")
        self.thread = get_ident()
        self.original = self.held[1].fileno()
        self.pin = os.dup(self.original)
        stack.callback(os.close, self.pin)
        self.check(directory)

    def check(self, directory):
        if (
            workspace_service.LIFECYCLE_LOCK.get() is not self.held
            or get_ident() != self.thread
            or self.held[1].fileno() != self.original
            or _identity(self.original) != _identity(self.pin)
        ):
            raise FenceError("owner-lock")
        _require_flock(self.original, "owner-lock")
        _require_flock(self.pin, "owner-lock")
        control._same_file(directory, "lock", self.pin, private=False)


class _Gate:
    def __init__(self, path, directory, fd, owner, scope):
        self.path, self.directory, self.fd, self.owner = path, directory, fd, owner
        self.identity = (*_identity(directory), *_identity(fd))
        self.scope = scope
        self.name = _NAME if scope is None else _STUDIO_NAME
        self.magic = _MAGIC if scope is None else _STUDIO_MAGIC

    def digest(self, frame):
        binding = b"" if self.scope is None else self.scope.seal
        return hashlib.sha256(os.fsencode(self.path) + b"\0" + binding + frame).digest()

    def check(self):
        with _directory(self.path, self.scope) as current:
            if (*_identity(current), *_identity(self.fd)) != self.identity:
                raise FenceError("unsafe-storage")
            control._same_file(current, self.name, self.fd)
            if self.owner is not None:
                self.owner.check(current)

    @contextmanager
    def locked(self, deadline):
        acquired = False
        try:
            with _errors():
                control._flock(self.fd, deadline)
                acquired = True
                self.check()
                _require_flock(self.fd, "unsafe-storage")
            yield
        finally:
            with _errors():
                try:
                    if acquired:
                        self.check()
                        _require_flock(self.fd, "unsafe-storage")
                finally:
                    # Also cover interruption after the kernel grants the
                    # lock but before Python observes successful acquisition.
                    fcntl.flock(self.fd, fcntl.LOCK_UN)

    def read(self):
        raw = os.pread(self.fd, _SIZE + 1, 0)
        if len(raw) != _SIZE or self.digest(raw[:-32]) != raw[-32:]:
            raise FenceError("invalid-state")
        magic, *identity, generation, status = _FRAME.unpack(raw[:-32])
        if (
            magic != self.magic
            or tuple(identity) != self.identity
            or status not in ((0, 2) if generation == _EMPTY[0] else (0, 1))
        ):
            raise FenceError("invalid-state")
        return generation, status

    def sync(self):
        os.fsync(self.fd)
        os.fsync(self.directory)

    def publish(self, state, *, previous):
        self.check()
        existing = None if os.fstat(self.fd).st_size == 0 else self.read()
        if existing != previous:
            raise FenceError("invalid-state")
        frame = _FRAME.pack(self.magic, *self.identity, *state)
        raw = frame + self.digest(frame)
        # This inode IS the lock: never replace it. A torn frame is not
        # admissible; no reader repairs it or authorizes direct stop.
        if os.pwrite(self.fd, raw, 0) != len(raw):
            raise FenceError("invalid-state")
        self.sync()
        self.check()
        if self.read() != state:
            raise FenceError("invalid-state")

    def unarmed(self):
        state = self.read()
        if state == _DIRECT_IN_PROGRESS:
            raise FenceError("direct-unverified")
        if state != _EMPTY:
            raise FenceError("armed")


@contextmanager
def _gate(target, *, owning=False, fresh=False):
    scope = target if type(target) is _StudioScope else None
    path = scope.path if scope is not None else _path(target)
    name = _NAME if scope is None else _STUDIO_NAME
    stack = ExitStack()
    try:
        with _errors():
            directory = stack.enter_context(_directory(path, scope))
            owner = _OwnerLock(path, directory, stack) if owning else None
            try:
                fd = control._open_file(directory, name, write=True, create=fresh)
            except FileNotFoundError:
                raise FenceError("not-enrolled") from None
            except FileExistsError:
                raise FenceError("already-enrolled") from None
            stack.callback(os.close, fd)
            gate = _Gate(path, directory, fd, owner, scope)
        yield gate
    finally:
        with _errors():
            stack.close()


def enroll_new(path, *, timeout=control.DEFAULT_TIMEOUT):
    """Explicitly enroll a caller-proven never-enrolled workspace under its lock.

    Missing state alone is not that proof. Stop and ordinary readers never
    enroll. An existing incomplete, unsafe or armed entry is never recreated.
    """
    deadline = _deadline(timeout)
    with _gate(path, owning=True, fresh=True) as gate, gate.locked(deadline), _errors():
        gate.publish(_EMPTY, previous=None)


class _Lifetime:
    def __init__(self, gate, generation):
        self._gate, self._generation = gate, generation
        self._active = True

    @contextmanager
    def _current(self, timeout):
        deadline = _deadline(timeout)
        if not self._active:
            raise FenceError("wrong-lifetime")
        gate = self._gate
        with _errors():
            # Reject a different foreground context before touching the shared
            # open file description (flock would otherwise be reentrant).
            gate.check()
        with gate.locked(deadline), _errors():
            state = gate.read()
            if state[0] != self._generation:
                raise FenceError("wrong-lifetime")
            yield state

    def stop_requested(self, *, timeout=control.DEFAULT_TIMEOUT):
        """Check intent for this exact lifetime from its owning foreground."""
        with self._current(timeout) as state:
            return bool(state[1])

    def complete(self, *, drained, stopped, timeout=control.DEFAULT_TIMEOUT):
        """Retire only after the caller has verified drain and same-run stop."""
        if drained is not True or stopped is not True:
            raise FenceError("completion-unverified")
        with self._current(timeout) as state:
            self._gate.publish(_EMPTY, previous=state)
        self._active = False


@contextmanager
def arm(path, *, timeout=control.DEFAULT_TIMEOUT):
    """Arm durably while holding the real lifecycle lock; never clear on exit."""
    deadline = _deadline(timeout)
    with _gate(path, owning=True) as gate:
        with gate.locked(deadline), _errors():
            gate.unarmed()
            generation = secrets.token_bytes(32)
            if generation == _EMPTY[0]:
                raise FenceError("invalid-state")
            gate.publish((generation, 0), previous=_EMPTY)
        lifetime = _Lifetime(gate, generation)
        try:
            yield lifetime
        finally:
            lifetime._active = False
            with _errors():
                gate.check()


def require_unarmed(path, *, timeout=control.DEFAULT_TIMEOUT):
    """Check under the lifecycle lock before other workspace mutations."""
    deadline = _deadline(timeout)
    with _gate(path, owning=True) as gate, gate.locked(deadline), _errors():
        gate.unarmed()
        gate.sync()


def observe_attach(scope, *, timeout=control.DEFAULT_TIMEOUT):
    """Observe exactly one armed Studio lifetime with no stop intent.

    This read is not owner or stream attestation and cannot acquire a client
    lease, start an owner, or adopt legacy state. Retain the original witness
    across the later counted exchange; do not resolve a second candidate.
    """
    if type(scope) is not _StudioScope:
        raise FenceError("invalid-request")
    deadline = _deadline(timeout)
    with _gate(scope) as gate, gate.locked(deadline), _errors():
        generation, status = gate.read()
        if generation == _EMPTY[0] or status != 0:
            raise FenceError("wrong-lifetime")
        return _AttachWitness(scope, gate.identity, generation)


def attach_current(witness, *, timeout=control.DEFAULT_TIMEOUT):
    """Recheck only the captured arm; a refreshed observation is not a retry."""
    if (
        type(witness) is not _AttachWitness
        or type(witness.scope) is not _StudioScope
        or type(witness.generation) is not bytes
        or len(witness.generation) != len(_EMPTY[0])
        or witness.generation == _EMPTY[0]
        or witness._retired.is_set()
    ):
        return False
    matched = False
    try:
        deadline = _deadline(timeout)
        with _gate(witness.scope) as gate, gate.locked(deadline), _errors():
            same = gate.identity == witness.gate and gate.read() == (
                witness.generation,
                0,
            )
        matched = same and not witness._retired.is_set()
        return matched
    except FenceError:
        return False
    finally:
        if not matched:
            witness._retired.set()


def route_stop(path, *, direct_stop, timeout=control.DEFAULT_TIMEOUT):
    """Run the entire ordinary stop under the gate, or publish named intent only.

    The callback must not re-enter this fence or wait for the lifecycle lock.
    It returns True only when no stop work can still execute. Failure, an
    unknown result or process death leaves a durable direct-stop refusal.
    A pending result never invokes the callback or attests container state.
    """
    if not callable(direct_stop):
        raise FenceError("invalid-request")
    deadline = _deadline(timeout)
    with _gate(path) as gate, gate.locked(deadline):
        with _errors():
            state = gate.read()
            if state == _DIRECT_IN_PROGRESS:
                raise FenceError("direct-unverified")
            if state != _EMPTY:
                if not state[1]:
                    gate.publish((state[0], 1), previous=state)
                else:
                    gate.sync()
                return StopRoute.PENDING
            gate.publish(_DIRECT_IN_PROGRESS, previous=_EMPTY)
        if direct_stop() is not True:
            raise FenceError("direct-unverified")
        with _errors():
            gate.publish(_EMPTY, previous=_DIRECT_IN_PROGRESS)
        return StopRoute.DIRECT
