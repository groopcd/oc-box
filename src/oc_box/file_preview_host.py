"""Bounded artifact transfer, armed by a fresh Ctrl-click on the host input."""

import base64
import binascii
import fcntl
import os
import re
import secrets
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass

PREFIX = b"\x1b]778;oc-box-open:v1:"
_GRANT_PREFIX = b"\x1b]778;oc-box-click:v1:"
_PREFIXES = (PREFIX, _GRANT_PREFIX)
_FRAME_START = re.compile(rb"\x1b\]778;oc-box-(?:open|click):v1:")
_MOUSE = re.compile(
    rb"\x1b\[<((?:0|[1-9][0-9]{0,2}));([1-9][0-9]{0,4});([1-9][0-9]{0,4})([Mm])"
)
_KINDS = frozenset((b"png", b"txt", b"md", b"html"))
MAX_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 4096
REQUEST_TIMEOUT = 15.0
FRAME_TIMEOUT = 2.0
PENDING_TIMEOUT = 2.0
STARTUP_TIMEOUT = 12.0
_CLICK_TIMEOUT = 2.0
_ID_BYTES = 32
_ID = re.compile(rb"[0-9a-f]{32}")
_LENGTH = re.compile(rb"[1-9][0-9]*")
_TERMINATOR = re.compile(rb"\x07|\x1b\\")
_MAX_BODY = _ID_BYTES + len(b":data:") + 4 * ((CHUNK_BYTES + 2) // 3)

_BASENAME = re.compile(r"artifact-[0-9a-f]{32}\.(?:png|txt|md|html)")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RETENTION_SECONDS = 24 * 60 * 60
_MAX_FILES = 128
_MAX_STORAGE_BYTES = 64 * 1024 * 1024
_OPENER = "/usr/bin/xdg-open"
_MIME = "/usr/bin/xdg-mime"
_OPEN_TIMEOUT = 5.0
_DESKTOP_ENV = frozenset(
    (
        "HOME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CURRENT_DESKTOP",
        "DESKTOP_SESSION",
        "XDG_SESSION_TYPE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_DIRS",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    )
)


class PreviewError(ValueError):
    """Fixed categories only, never paths, payloads or executable diagnostics."""

    def __init__(self, message, *, reason=None):
        super().__init__(message)
        self.reason = reason


_OPEN_FAILURES = frozenset(
    (
        "mount-recheck-unverified",
        "artifact-rejected",
        "desktop-session-unavailable",
        "desktop-tools-unavailable",
        "runtime-staging-unverified",
        "runtime-staging-untrusted",
        "runtime-staging-overlaps-mount",
        "runtime-staging-busy",
        "runtime-staging-limit",
        "runtime-staging-write-failed",
        "mime-query-failed",
        "mime-query-timeout",
        "mime-query-empty",
        "mime-type-rejected",
        "opener-timeout",
        "opener-failed",
        "xdg-open-unavailable-or-untrusted",
        "xdg-mime-unavailable-or-untrusted",
    )
)


def safe_open_failure(error):
    if (
        isinstance(error, PreviewError)
        and isinstance(error.reason, str)
        and error.reason in _OPEN_FAILURES
    ):
        return error.reason
    return "host-open-failed"


@dataclass(frozen=True)
class InactiveMountSource:
    """A configured mount of a container verified to be inactive."""

    path: str


class RelayError(PreviewError):
    """The PTY's frame boundary is unverified; do not bypass this relay."""


class Relay:
    """Issue a single-use grant from host input; never infer it from PTY output."""

    def __init__(self, *, clock=None):
        self._clock = time.monotonic if clock is None else clock
        self._startup_deadline = self._clock() + STARTUP_TIMEOUT
        self._startup = None
        self._tail = b""
        self._body = None
        self._body_deadline = 0.0
        self._body_esc = False
        self._echo = False
        self._failed = False
        self._press = None
        self._grant = None
        self._active_id = None
        self._active_size = 0
        self._active_kind = None
        self._active_deadline = 0.0
        self._payload = bytearray()
        self._pending = None
        self._pending_deadline = 0.0

    def _discard_active(self):
        self._active_id = None
        self._active_kind = None
        self._payload.clear()

    def _fail(self):
        self._failed = True
        self._body = None
        self._tail = b""
        self._pending = None
        self._grant = self._press = None
        self._discard_active()
        raise RelayError("Host artifact transfer lost the PTY frame boundary.")

    def _expire(self, now):
        if self._failed or (self._body is not None and now >= self._body_deadline):
            self._fail()
        if self._active_id is not None and now >= self._active_deadline:
            self._discard_active()
        if self._pending is not None and now >= self._pending_deadline:
            self._pending = None
        if self._grant is not None and now >= self._grant[1]:
            self._grant = None

    def mouse(self, packet):
        """Forward mouse bytes; attach a nonce to a matched host Ctrl-click."""
        if self._failed or self._startup != b"ready":
            return packet
        event = _MOUSE.fullmatch(packet)
        previous, self._press = self._press, None
        if event is None:
            return packet
        code, x, y, edge = event.groups()
        now = self._clock()
        if code == b"16" and edge == b"M":
            self._press = (x, y, now + _CLICK_TIMEOUT)
            return packet
        if (
            code != b"16"
            or edge != b"m"
            or previous is None
            or (x, y) != previous[:2]
            or now >= previous[2]
        ):
            return packet
        # Another deliberate gesture supersedes unread bytes from the old one.
        self._discard_active()
        self._pending = None
        grant = secrets.token_hex(16).encode("ascii")
        self._grant = grant, now + REQUEST_TIMEOUT
        return packet + _GRANT_PREFIX + grant + b":" + x + b":" + y + b"\x07"

    def break_press(self):
        """Intervening host input other than the matching release cancels a press."""
        self._press = None

    def _receive(self, now, *, valid_end):
        if self._echo:
            return
        body = bytes(self._body)
        if body in (b"ready", b"unavailable-notified"):
            # Evidence of client compatibility only. Terminal output never
            # authorizes a transfer or fills in either half of a host click.
            if valid_end and self._startup is None and now < self._startup_deadline:
                self._startup = body
            return
        request_id, sep, message = body.partition(b":")
        if not _ID.fullmatch(request_id):
            return
        if not (valid_end and sep):
            if request_id == self._active_id:
                self._discard_active()
            return

        kind, sep, payload = message.partition(b":")
        if kind == b"begin":
            if request_id == self._active_id:
                self._discard_active()
                return
            format_, _, length = payload.partition(b":")
            if self._grant is None or request_id != self._grant[0]:
                return
            self._grant = None
            limit = MAX_BYTES if format_ == b"png" else 256 * 1024
            if (
                not sep
                or format_ not in _KINDS
                or len(length) > len(str(limit))
                or not _LENGTH.fullmatch(length)
                or (size := int(length)) > limit
            ):
                return
            if self._active_id is None and self._pending is None:
                self._active_id = request_id
                self._active_size = size
                self._active_kind = format_.decode("ascii")
                self._active_deadline = now + REQUEST_TIMEOUT
            return

        if request_id != self._active_id:
            return
        if kind == b"data" and sep:
            try:
                chunk = base64.b64decode(payload, validate=True)
            except binascii.Error:
                chunk = b""
            if (
                len(chunk) == min(CHUNK_BYTES, self._active_size - len(self._payload))
                and chunk
                and base64.b64encode(chunk) == payload
            ):
                self._payload.extend(chunk)
                return
        elif kind == b"end" and not sep:
            if len(self._payload) == self._active_size:
                self._pending = (self._active_kind, bytes(self._payload))
                self._pending_deadline = now + PENDING_TIMEOUT
        self._discard_active()

    def output_bytes(self, data: bytes) -> bytes:
        """Pass native bytes through while retaining bounded split-frame state."""
        now = self._clock()
        self._expire(now)
        if self._tail:
            data = self._tail + data
            self._tail = b""
        visible = bytearray()
        view = memoryview(data)
        cursor = 0
        while cursor < len(data):
            if self._body is None:
                match = _FRAME_START.search(data, cursor)
                if match is None:
                    # Only a suffix that could be part of the exact sentinel
                    # must wait for the following PTY read.
                    for size in range(
                        min(max(map(len, _PREFIXES)) - 1, len(data) - cursor), 0, -1
                    ):
                        if any(
                            size < len(prefix) and data.endswith(prefix[:size])
                            for prefix in _PREFIXES
                        ):
                            self._tail = data[-size:]
                            break
                    visible.extend(view[cursor : len(data) - len(self._tail)])
                    break
                visible.extend(view[cursor : match.start()])
                cursor = match.end()
                self._body = bytearray()
                self._echo = match.group() == _GRANT_PREFIX
                self._body_deadline = now + FRAME_TIMEOUT
                self._body_esc = False
                continue

            if self._body_esc:
                self._body_esc = False
                if data[cursor] == ord("\\"):
                    # ST flushes an invalid OSC, including split ESC/backslash.
                    self._receive(now, valid_end=False)
                    self._body = None
                    cursor += 1
                    continue
                if len(self._body) >= _MAX_BODY:
                    self._fail()
                self._body.append(0x1B)
            # Bound the terminator search as well as the stored bytes. Permit
            # one extra byte at the boundary for an ST split across reads.
            end = min(len(data), cursor + _MAX_BODY - len(self._body) + 2)
            ending = _TERMINATOR.search(data, cursor, end)
            stop = len(data) if ending is None else ending.start()
            if ending is None and data[-1] == 0x1B:
                self._body_esc = True
                stop -= 1
            if len(self._body) + stop - cursor > _MAX_BODY:
                self._fail()
            self._body.extend(view[cursor:stop])
            if ending is None:
                break
            self._receive(now, valid_end=ending.group() == b"\x07")
            self._body = None
            cursor = ending.end()
        return bytes(visible)

    def finish_output(self) -> bytes:
        """Flush unmatched ordinary bytes; refuse an unclosed reserved frame."""
        self._expire(self._clock())
        if self._body is not None:
            self._fail()
        self._discard_active()
        trailing, self._tail = self._tail, b""
        return trailing

    def check(self) -> None:
        """Expire stale state on idle ticks; raise on an unclosed timed-out OSC."""
        self._expire(self._clock())

    def startup_unverified(self):
        """Report missing native readiness after restoring the host terminal."""
        return self._startup is None

    def take(self) -> tuple[str, bytes] | None:
        """Consume one complete transfer; no staging or process work here."""
        self.check()
        if self._body is not None or self._tail:
            return None
        pending, self._pending = self._pending, None
        return pending


def _prepare(kind, data):
    if __package__:
        from .file_open_formats import MIME_TYPES, prepare
    else:
        from file_open_formats import MIME_TYPES, prepare

    return prepare(kind, data), MIME_TYPES[kind]


def _private_directory(info, uid):
    return (
        stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == uid
    )


def _storage_root():
    path = os.environ.get("XDG_RUNTIME_DIR", "")
    if (
        not os.path.isabs(path)
        or path.startswith("//")
        or path == "/"
        or os.path.realpath(path) != path
    ):
        raise PreviewError(
            "Host PNG preview requires a private desktop runtime directory."
        )
    return path


def _storage_name(uid):
    # Do not adopt or discard v1 copies. That spool did not distinguish
    # pre-dispatch refusals from files a viewer may still be consuming.
    return f"oc-box-open-v2-{uid}"


def _desktop_env():
    env = {key: value for key, value in os.environ.items() if key in _DESKTOP_ENV}
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    # xdg-utils uses these host session markers to select KDE helpers.
    # Without the version it can take the obsolete kfile/kfmclient path.
    version = os.environ.get("KDE_SESSION_VERSION")
    if version in ("4", "5", "6"):
        env["KDE_SESSION_VERSION"] = version
    if os.environ.get("KDE_FULL_SESSION") == "true":
        env["KDE_FULL_SESSION"] = "true"
    env["PATH"] = "/usr/bin:/bin"
    return env


def _trusted_exec(path):
    try:
        info = os.stat(path)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022
            and os.access(path, os.X_OK)
        )
    except OSError:
        return False


def _opener_available():
    return _trusted_exec(_OPENER) and _trusted_exec(_MIME)


def _ancestors(path):
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _identity(path):
    info = os.stat(path)
    return info.st_dev, info.st_ino


def _resolve_mount_source(path, *, missing_ok):
    """Resolve an existing source or project an absent suffix from a real parent."""
    missing = []
    while True:
        try:
            existing = os.path.realpath(path, strict=True)
        except FileNotFoundError:
            if not missing_ok:
                raise
            # A dangling symlink is not an absent path component. Do not
            # guess where it may lead (or swallow a permission failure).
            try:
                os.lstat(path)
            except FileNotFoundError:
                pass
            else:
                raise
            parent, component = os.path.split(path)
            if not component or parent == path:
                raise
            missing.append(component)
            path = parent
        else:
            suffix = tuple(reversed(missing))
            return (
                os.path.join(existing, *suffix) if suffix else existing,
                existing,
                suffix,
            )


def _spool_not_mounted(parent, root, name, uid, host_mounts):
    root_info = os.fstat(parent)
    if not _private_directory(root_info, uid) or (
        root_info.st_dev,
        root_info.st_ino,
    ) != _identity(root):
        return False
    spool = os.path.join(root, name)
    spool_identity = None
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not _private_directory(info, uid):
            return False
        spool_identity = info.st_dev, info.st_ino

    def overlaps(path):
        return os.path.commonpath((spool, path)) in (spool, path)

    spool_parents = {}
    for path in _ancestors(root):
        spool_parents.setdefault(_identity(path), []).append(path)
    for source in host_mounts:
        inactive = isinstance(source, InactiveMountSource)
        path = os.fspath(source.path if inactive else source)
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            return False
        if overlaps(path):
            return False
        resolved, existing, suffix = _resolve_mount_source(path, missing_ok=inactive)
        if overlaps(resolved):
            return False
        # If the closest existing parent is a bind alias for a staging
        # ancestor, test the missing suffix at that ancestor too. A missing
        # sibling outside the spool is not the same as a missing spool path.
        for alias in spool_parents.get(_identity(existing), ()):
            if overlaps(os.path.join(alias, *suffix) if suffix else alias):
                return False
        if spool_identity is not None and any(
            _identity(path) == spool_identity for path in _ancestors(existing)
        ):
            return False
    return True


def readiness_reason(*, host_mounts=None) -> str | None:
    """Read-only gate: verify a private runtime directory and desktop opener.

    Supply inspected host mount sources before admitting workspace proposals.
    Compare canonical paths and ancestor identities to detect bind aliases.
    """
    if _desktop_env() is None:
        return "desktop-session-unavailable"
    if not _trusted_exec(_OPENER):
        return "xdg-open-unavailable-or-untrusted"
    if not _trusted_exec(_MIME):
        return "xdg-mime-unavailable-or-untrusted"
    try:
        uid = os.geteuid()
        root = _storage_root()
        parent = os.open(root, _DIRECTORY_FLAGS)
        try:
            if not _spool_not_mounted(parent, root, _storage_name(uid), uid, ()):
                return "runtime-staging-untrusted"
            if host_mounts is not None and not _spool_not_mounted(
                parent, root, _storage_name(uid), uid, host_mounts
            ):
                return "runtime-staging-overlaps-mount"
        finally:
            os.close(parent)
    except (OSError, TypeError, ValueError):
        return "runtime-staging-unverified"
    return None


def ready(*, host_mounts=()) -> bool:
    return readiness_reason(host_mounts=host_mounts) is None


def _private_file(info, uid):
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == uid
        and info.st_nlink == 1
    )


def _cleanup(fd, uid, incoming_bytes):
    oldest = time.time() - _RETENTION_SECONDS
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= _MAX_FILES or not _BASENAME.fullmatch(entry.name):
                raise PreviewError(
                    "Host PNG preview storage is not trusted.",
                    reason="runtime-staging-untrusted",
                )
            info = entry.stat(follow_symlinks=False)
            if not _private_file(info, uid) or info.st_size > MAX_BYTES:
                raise PreviewError(
                    "Host PNG preview storage is not trusted.",
                    reason="runtime-staging-untrusted",
                )
            names.append((entry.name, info.st_mtime, info.st_size))
    remaining = 0
    retained_bytes = 0
    for name, modified, size in names:
        if modified < oldest:
            os.unlink(name, dir_fd=fd)
        else:
            remaining += 1
            retained_bytes += size
    if remaining >= _MAX_FILES or retained_bytes + incoming_bytes > _MAX_STORAGE_BYTES:
        raise PreviewError(
            "Host PNG preview limit reached; try again later.",
            reason="runtime-staging-limit",
        )


@contextmanager
def _storage(host_mounts, incoming_bytes):
    uid = os.geteuid()
    name = _storage_name(uid)
    root = _storage_root()
    parent = os.open(root, _DIRECTORY_FLAGS)
    try:
        if not _spool_not_mounted(parent, root, name, uid, host_mounts):
            raise PreviewError("Host PNG preview storage is not trusted.")
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            if not _private_directory(os.fstat(fd), uid):
                raise PreviewError("Host PNG preview storage is not trusted.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PreviewError(
                    "Host artifact staging is busy.", reason="runtime-staging-busy"
                ) from None
            _cleanup(fd, uid, incoming_bytes)
            yield fd, os.path.join(root, name), uid
        finally:
            os.close(fd)
    finally:
        os.close(parent)


@contextmanager
def _stage(kind, data, host_mounts):
    with _storage(host_mounts, len(data)) as (directory, path, uid):
        name = f"artifact-{secrets.token_hex(16)}.{kind}"
        fd = os.open(name, _FILE_FLAGS, mode=0o600, dir_fd=directory)
        handed_off = False

        def retain_for_viewer():
            nonlocal handed_off
            handed_off = True

        try:
            try:
                if not _private_file(os.fstat(fd), uid):
                    raise PreviewError("Host PNG preview storage is not trusted.")
                pending = memoryview(data)
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise OSError("preview-write")
                    pending = pending[written:]
            finally:
                os.close(fd)
            yield os.path.join(path, name), retain_for_viewer
        finally:
            if not handed_off:
                os.unlink(name, dir_fd=directory)


def _verify_mime(path, types, env):
    try:
        # Do not dispatch a file that the desktop classifies as a script,
        # desktop launcher, executable or another unexpected type.
        mime = subprocess.run(
            (_MIME, "query", "filetype", path),
            env=env,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            timeout=_OPEN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PreviewError(
            "Host MIME check did not report completion.", reason="mime-query-timeout"
        ) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise PreviewError(
            "Host MIME check failed.", reason="mime-query-failed"
        ) from None
    if mime.returncode:
        raise PreviewError("Host MIME check failed.", reason="mime-query-failed")
    if not mime.stdout.strip():
        raise PreviewError(
            "The desktop MIME helper returned no file type.",
            reason="mime-query-empty",
        )
    if len(mime.stdout) > 256 or mime.stdout.strip() not in {
        value.encode("ascii") for value in types
    }:
        raise PreviewError(
            "Host artifact has an unexpected desktop file type.",
            reason="mime-type-rejected",
        )


def open_artifact(kind: str, data: bytes, *, host_mounts=()) -> None:
    """Only call for a complete click-authorized request; open a private copy.

    The launcher does not reap a file within a day of staging it, even when
    opener status is uncertain. A later approved open can reap expired files
    across client exits. Runtime-directory teardown may clear them sooner, and
    a viewer may retain its own copy.
    """
    if (
        type(data) is not bytes
        or kind not in ("png", "txt", "md", "html")
        or not 0 < len(data) <= MAX_BYTES
    ):
        raise PreviewError("Host artifact refused.", reason="artifact-rejected")
    try:
        data, types = _prepare(kind, data)
    except ValueError:
        raise PreviewError(
            "Host artifact refused.", reason="artifact-rejected"
        ) from None

    env = _desktop_env()
    if env is None:
        raise PreviewError(
            "Host PNG preview requires a desktop session.",
            reason="desktop-session-unavailable",
        )
    if not _opener_available():
        raise PreviewError(
            "Host PNG preview opener is unavailable.",
            reason="desktop-tools-unavailable",
        )
    try:
        host_mounts = tuple(host_mounts)
    except TypeError:
        raise PreviewError("Host PNG preview storage is not trusted.") from None
    reason = readiness_reason(host_mounts=host_mounts)
    if reason is not None:
        raise PreviewError("Host PNG preview storage is not trusted.", reason=reason)
    try:
        with _stage(kind, data, host_mounts) as (path, retain_for_viewer):
            _verify_mime(path, types, env)
            # From this point an interrupted or nonzero opener can still
            # have started a viewer. Keep the copy for the normal lifetime.
            retain_for_viewer()
    except OSError:
        raise PreviewError(
            "Host PNG preview storage is unavailable.",
            reason="runtime-staging-write-failed",
        ) from None
    try:
        result = subprocess.run(
            (_OPENER, path),
            env=env,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
            timeout=_OPEN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PreviewError(
            "Host PNG preview opener did not report completion.",
            reason="opener-timeout",
        ) from None
    except PreviewError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise PreviewError(
            "Host PNG preview opener is unavailable.", reason="opener-failed"
        ) from None
    if result.returncode != 0:
        raise PreviewError("Host PNG preview opener failed.", reason="opener-failed")
