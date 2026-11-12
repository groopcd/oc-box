"""Private stop intent and bounded admission, independent of lifecycle locks.

Only an owning launch or explicit stop may call :func:`capture` or
:func:`request_stop`: these are the only initializers. A stop publishes its new
opaque token *before* its caller waits for :func:`admission`. An admission holder
can safely call either marker operation: the short marker-writer lock is distinct
and no marker operation acquires admission. Readers never create or repair state.

The fixed files contain only a version, random public tokens, a persistent
uncertainty flag and a fixed ready byte; they are not credentials, workspace
identities, or activity records. Failed initialization remains failed closed.
No operation sweeps abandoned temp files, unlinks a lock, or repairs a missing or
unsafe marker. Uncertainty refuses automatic-recovery baselines, not admission
for explicit lifecycle work. Only an explicit verified transition completion may
clear that fence; capture and stop intent never clear it.
"""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time


_DIRECTORY = "control"
_MARKER = "history-stop.json"
_ADMISSION = "admission.lock"
_WRITER = "marker.lock"
_READY = b"1\n"
_MAX_MARKER_BYTES = 256
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_WRITER_TIMEOUT = 1.0
_POLL = 0.01
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 60.0
_CATEGORIES = frozenset(
    {
        "invalid-path",
        "invalid-timeout",
        "invalid-baseline",
        "missing-control",
        "unsafe-control",
        "invalid-marker",
        "incomplete-control",
        "uncertain-control",
        "control-timeout",
        "control-unavailable",
    }
)


class ControlError(ValueError):
    """A fixed, secret-free refusal; ``category`` never contains caller data."""

    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in _CATEGORIES
            else "control-unavailable"
        )
        super().__init__(f"Workspace control failed ({self.category}).")


@contextmanager
def _errors():
    try:
        yield
    except ControlError as exc:
        raise exc from None
    except (OSError, ValueError, TypeError, OverflowError):
        raise ControlError("control-unavailable") from None


def _directory_info(fd, *, private):
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (
            stat.S_IMODE(info.st_mode) != 0o700
            if private
            else bool(stat.S_IMODE(info.st_mode) & 0o022)
        )
    ):
        raise ControlError("unsafe-control")


def _file_info(fd, *, private=True):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or (
            stat.S_IMODE(info.st_mode) != 0o600
            if private
            else bool(stat.S_IMODE(info.st_mode) & 0o022)
        )
    ):
        raise ControlError("unsafe-control")
    return info


def _same_file(directory, name, fd):
    info = _file_info(fd)
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        raise ControlError("missing-control") from None
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise ControlError("unsafe-control")


def _open_file(directory, name, *, write=False, create=False, private=True):
    flags = (
        (os.O_RDWR if write else os.O_RDONLY)
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        if create:
            os.fchmod(fd, 0o600)
        _file_info(fd, private=private)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _control(path, *, create):
    path = Path(path).absolute()
    if path.name != "workspace.json" or ".." in path.parts:
        raise ControlError("invalid-path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        # Public ancestors such as /tmp and /home need not be owned/private.
        # Open each component without following links; validate the workspace
        # registry parent, accepting its legitimate owned legacy 0755 mode.
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _directory_info(fd, private=False)
        registry = _open_file(fd, path.name, private=False)
        os.close(registry)
        created = False
        if create:
            try:
                os.mkdir(_DIRECTORY, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
        try:
            child = os.open(_DIRECTORY, flags, dir_fd=fd)
        except FileNotFoundError:
            raise ControlError("missing-control") from None
        os.close(fd)
        fd = child
        _directory_info(fd, private=True)
        yield fd, created
    finally:
        os.close(fd)


def _pause(deadline, category="control-timeout"):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControlError(category)
    time.sleep(min(_POLL, remaining))


def _flock(fd, deadline):
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, InterruptedError):
            _pause(deadline)


@contextmanager
def _writer(directory):
    deadline = time.monotonic() + _WRITER_TIMEOUT
    while True:
        try:
            fd = _open_file(directory, _WRITER, write=True)
        except FileNotFoundError:
            # The directory creator alone may create the stable lock. Waiting
            # here covers first-use races without repairing an interrupted init.
            _pause(deadline, "incomplete-control")
            continue
        try:
            _flock(fd, deadline)
            _same_file(directory, _WRITER, fd)
            ready = os.read(fd, len(_READY) + 1)
            if ready == _READY:
                yield
                return
            if ready:
                raise ControlError("unsafe-control")
        finally:
            os.close(fd)
        # A newly created lock can be observed just before its creator locks it.
        # Release it while waiting, so the creator can finish initialization.
        _pause(deadline, "incomplete-control")


def _marker_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ControlError("invalid-marker")
        value[key] = item
    return value


def _read_marker(directory):
    try:
        fd = _open_file(directory, _MARKER)
    except FileNotFoundError:
        raise ControlError("missing-control") from None
    try:
        if os.fstat(fd).st_size > _MAX_MARKER_BYTES:
            raise ControlError("invalid-marker")
        raw = os.read(fd, _MAX_MARKER_BYTES + 1)
        if len(raw) > _MAX_MARKER_BYTES:
            raise ControlError("invalid-marker")
        try:
            value = json.loads(raw.decode("ascii"), object_pairs_hook=_marker_object)
        except (UnicodeError, ValueError):
            raise ControlError("invalid-marker") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "token", "uncertain"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["token"], str)
            or not _TOKEN.fullmatch(value["token"])
            or type(value["uncertain"]) is not bool
        ):
            raise ControlError("invalid-marker")
        return value
    finally:
        os.close(fd)


def _validate_locks(directory):
    for name in (_WRITER, _ADMISSION):
        try:
            fd = _open_file(directory, name)
        except FileNotFoundError:
            raise ControlError("missing-control") from None
        try:
            if name == _WRITER:
                if os.read(fd, len(_READY) + 1) != _READY:
                    raise ControlError("incomplete-control")
            elif os.fstat(fd).st_size != 0:
                raise ControlError("unsafe-control")
        finally:
            os.close(fd)


def _write_all(fd, content):
    while content:
        written = os.write(fd, content)
        if written <= 0:
            raise ControlError("control-unavailable")
        content = content[written:]


def _publish(directory, token, *, uncertain=False, previous=None):
    name = ".history-stop." + secrets.token_hex(16) + ".tmp"
    # Keep permission/metadata setup inside the cleanup scope too. Lock creation
    # deliberately never unlinks on failure, but a newly created temp is ours.
    fd = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
        | os.O_NONBLOCK,
        0o600,
        dir_fd=directory,
    )
    try:
        os.fchmod(fd, 0o600)
        _file_info(fd)
        content = (
            json.dumps(
                {"version": 1, "token": token, "uncertain": uncertain},
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        _write_all(fd, content)
        os.fsync(fd)
        _same_file(directory, name, fd)
        if previous is None:
            try:
                os.stat(_MARKER, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ControlError("unsafe-control")
        elif _read_marker(directory) != previous:
            raise ControlError("unsafe-control")
        os.replace(name, _MARKER, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        # Remove only this writer's still-identical temp, including cancellation.
        # Never sweep a crashed writer's temp or touch a substituted pathname.
        failed = sys.exc_info()[0] is not None
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            own = os.fstat(fd)
            if (
                stat.S_ISREG(current.st_mode)
                and current.st_nlink == 1
                and (current.st_dev, current.st_ino) == (own.st_dev, own.st_ino)
            ):
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            if not failed:
                raise
        finally:
            try:
                os.close(fd)
            except OSError:
                if not failed:
                    raise


def _initialize(directory):
    # Only the successful mkdir caller comes here. Incomplete existing state is
    # never initialized again, and both locks use O_EXCL with no truncation.
    fd = _open_file(directory, _WRITER, write=True, create=True)
    try:
        _flock(fd, time.monotonic() + _WRITER_TIMEOUT)
        lock = _open_file(directory, _ADMISSION, write=True, create=True)
        os.close(lock)
        try:
            os.stat(_MARKER, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ControlError("unsafe-control")
        token = secrets.token_hex(32)
        _publish(directory, token)
        _write_all(fd, _READY)
        os.fsync(fd)
        os.fsync(directory)
        return token
    finally:
        os.close(fd)


def capture(path):
    """Return the opaque baseline, initializing only previously absent control.

    Existing valid intent is preserved, including a stop racing initial launch.
    A missing marker or persistent uncertainty is a refusal, not reset. Callers
    must not turn this automatic-recovery refusal into a ban on explicit launch.
    """
    with _errors(), _control(path, create=True) as (directory, created):
        if created:
            return _initialize(directory)
        with _writer(directory):
            _validate_locks(directory)
            return _certain(_read_marker(directory))


def request_stop(path):
    """Publish and return a fresh opaque token without acquiring admission."""
    with _errors(), _control(path, create=True) as (directory, created):
        if created:
            return _initialize(directory)
        with _writer(directory):
            _validate_locks(directory)
            previous = _read_marker(directory)
            token = secrets.token_hex(32)
            if token == previous["token"]:
                raise ControlError("control-unavailable")
            _publish(
                directory,
                token,
                uncertain=previous["uncertain"],
                previous=previous,
            )
            return token


def _certain(marker):
    if marker["uncertain"]:
        raise ControlError("uncertain-control")
    return marker["token"]


def mark_uncertain(path):
    """Reserve the persistent uncertainty fence *before* invoking a start command.

    The caller holds admission and must not start if this write fails. Control
    must already exist. Repeated calls preserve the marker/token exactly. Any
    start exception, interruption, timeout or unverifiable result leaves the
    fence set: do not call finish_transition for those outcomes.
    """
    with _errors(), _control(path, create=False) as (directory, _):
        with _writer(directory):
            _validate_locks(directory)
            previous = _read_marker(directory)
            if not previous["uncertain"]:
                _publish(
                    directory, previous["token"], uncertain=True, previous=previous
                )


def finish_transition(path):
    """Clear only the fence after a caller-verified successful start transition.

    Internal lifecycle use only, while admission remains held: the start command
    must have returned zero and inspection of the pinned container must have
    verified it running. This helper cannot establish those external facts. It
    preserves the *latest* stop token, including intent published during start.
    """
    with _errors(), _control(path, create=False) as (directory, _):
        with _writer(directory):
            _validate_locks(directory)
            previous = _read_marker(directory)
            if previous["uncertain"]:
                _publish(
                    directory, previous["token"], uncertain=False, previous=previous
                )


def require_certain(path):
    """Read and validate only; refuse persistent uncertainty before stop success."""
    with _errors(), _control(path, create=False) as (directory, _):
        _validate_locks(directory)
        _certain(_read_marker(directory))


def unchanged(path, baseline):
    """Read/validate only; return false for changed intent, refuse unsafe state."""
    with _errors():
        if not isinstance(baseline, str) or not _TOKEN.fullmatch(baseline):
            raise ControlError("invalid-baseline")
        with _control(path, create=False) as (directory, _):
            _validate_locks(directory)
            return _certain(_read_marker(directory)) == baseline


def _acquire_admission(path, timeout):
    with _errors():
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 <= timeout <= MAX_TIMEOUT
            or not math.isfinite(timeout)
        ):
            raise ControlError("invalid-timeout")
        deadline = time.monotonic() + timeout
        with _control(path, create=False) as (directory, _):
            # No lock order inversion: this readiness check never takes writer.
            _validate_locks(directory)
            _read_marker(directory)
            try:
                fd = _open_file(directory, _ADMISSION, write=True)
            except FileNotFoundError:
                raise ControlError("missing-control") from None
            try:
                if os.fstat(fd).st_size != 0:
                    raise ControlError("unsafe-control")
                _flock(fd, deadline)
                _same_file(directory, _ADMISSION, fd)
                return fd
            except BaseException:
                os.close(fd)
                raise


@contextmanager
def admission(path, timeout=DEFAULT_TIMEOUT):
    """Hold the stable exclusive admission lock for at most ``timeout`` waiting.

    ``timeout`` is finite, numeric, and in [0, MAX_TIMEOUT] seconds. Zero attempts
    an immediate nonblocking acquisition. Caller work inside the context is not
    timed or interrupted, and caller exceptions are preserved. This function
    never initializes control state.
    """
    fd = _acquire_admission(path, timeout)
    try:
        yield
    finally:
        with _errors():
            os.close(fd)
