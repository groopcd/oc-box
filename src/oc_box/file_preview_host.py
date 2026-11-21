"""Untrusted PTY image proposals and the separate, consent-only host opener."""

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

PREFIX = b"\x1b]778;oc-box-png:v1:"
MAX_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 4096
REQUEST_TIMEOUT = 15.0
FRAME_TIMEOUT = 2.0
PENDING_TIMEOUT = 45.0
_ID_BYTES = 32
_MAX_IDS = 4096
_ID = re.compile(rb"[0-9a-f]{32}")
_LENGTH = re.compile(rb"[1-9][0-9]*")
_TERMINATOR = re.compile(rb"\x07|\x1b\\")
_MAX_BODY = _ID_BYTES + len(b":data:") + 4 * ((CHUNK_BYTES + 2) // 3)

_BASENAME = re.compile(r"preview-[0-9a-f]{32}\.png")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RETENTION_SECONDS = 24 * 60 * 60
_MAX_FILES = 8
_OPENER = "/usr/bin/xdg-open"
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


class RelayError(PreviewError):
    """The PTY's frame boundary is unverified; do not bypass this relay."""


class Relay:
    """Strip exact reserved OSC frames, offering at most one untrusted proposal.

    A proposal is only immutable bytes, not validated PNG or user approval.
    Neither parsing nor consuming a proposal touches files, processes or input.
    """

    def __init__(self, *, clock=None):
        self._clock = time.monotonic if clock is None else clock
        self._tail = b""
        self._body = None
        self._body_deadline = 0.0
        self._body_esc = False
        self._failed = False
        self._seen = set()
        self._active_id = None
        self._active_size = 0
        self._active_deadline = 0.0
        self._payload = bytearray()
        self._pending = None
        self._pending_deadline = 0.0

    def _discard_active(self):
        self._active_id = None
        self._payload.clear()

    def _fail(self):
        self._failed = True
        self._body = None
        self._tail = b""
        self._pending = None
        self._discard_active()
        raise RelayError("Host PNG preview lost the PTY frame boundary.")

    def _expire(self, now):
        if self._failed or (self._body is not None and now >= self._body_deadline):
            self._fail()
        if self._active_id is not None and now >= self._active_deadline:
            self._discard_active()
        if self._pending is not None and now >= self._pending_deadline:
            self._pending = None

    def _receive(self, now, *, valid_end):
        request_id, sep, message = bytes(self._body).partition(b":")
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
            if (
                not sep
                or len(payload) > len(str(MAX_BYTES))
                or not _LENGTH.fullmatch(payload)
                or (size := int(payload)) > MAX_BYTES
                or request_id in self._seen
                or len(self._seen) >= _MAX_IDS
            ):
                return
            # Retire IDs even for concurrent proposals. Never make a stale
            # stream valid again within this TUI relay by evicting earlier IDs.
            self._seen.add(request_id)
            if self._active_id is None and self._pending is None:
                self._active_id = request_id
                self._active_size = size
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
                self._pending = bytes(self._payload)
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
                start = data.find(PREFIX, cursor)
                if start < 0:
                    # Only a suffix that could be part of the exact sentinel
                    # must wait for the following PTY read.
                    for size in range(min(len(PREFIX) - 1, len(data) - cursor), 0, -1):
                        if data.endswith(PREFIX[:size]):
                            self._tail = data[-size:]
                            break
                    visible.extend(view[cursor : len(data) - len(self._tail)])
                    break
                visible.extend(view[cursor:start])
                cursor = start + len(PREFIX)
                self._body = bytearray()
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

    def take(self) -> bytes | None:
        """Consume the pending proposal once, with no side effects on the host."""
        self.check()
        if self._body is not None or self._tail:
            return None
        pending, self._pending = self._pending, None
        return pending


def _validate_canonical(data):
    if __package__:
        from .file_preview_png import validate_canonical
    else:
        from file_preview_png import validate_canonical

    validate_canonical(data)


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
    return f"oc-box-png-{uid}"


def _desktop_env():
    env = {key: value for key, value in os.environ.items() if key in _DESKTOP_ENV}
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    env["PATH"] = "/usr/bin:/bin"
    return env


def _opener_available():
    try:
        info = os.stat(_OPENER)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022
            and os.access(_OPENER, os.X_OK)
        )
    except OSError:
        return False


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

    spool_parents = {_identity(path) for path in _ancestors(root)}
    for source in host_mounts:
        if not os.path.isabs(source):
            return False
        source = os.path.realpath(source)
        if os.path.commonpath((spool, source)) in (spool, source):
            return False
        if _identity(source) in spool_parents:
            return False
        if spool_identity is not None and any(
            _identity(path) == spool_identity for path in _ancestors(source)
        ):
            return False
    return True


def ready(*, host_mounts=()) -> bool:
    """Read-only gate: verify a private runtime directory and desktop opener.

    Supply inspected host mount sources before admitting workspace proposals.
    Compare canonical paths and ancestor identities to detect bind aliases.
    """
    if _desktop_env() is None or not _opener_available():
        return False
    try:
        uid = os.geteuid()
        root = _storage_root()
        parent = os.open(root, _DIRECTORY_FLAGS)
        try:
            return _spool_not_mounted(
                parent, root, _storage_name(uid), uid, host_mounts
            )
        finally:
            os.close(parent)
    except (OSError, TypeError, ValueError):
        return False


def _private_file(info, uid):
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == uid
        and info.st_nlink == 1
    )


def _cleanup(fd, uid):
    oldest = time.time() - _RETENTION_SECONDS
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= _MAX_FILES or not _BASENAME.fullmatch(entry.name):
                raise PreviewError("Host PNG preview storage is not trusted.")
            info = entry.stat(follow_symlinks=False)
            if not _private_file(info, uid) or info.st_size > MAX_BYTES:
                raise PreviewError("Host PNG preview storage is not trusted.")
            names.append((entry.name, info.st_mtime))
    remaining = 0
    for name, modified in names:
        if modified < oldest:
            os.unlink(name, dir_fd=fd)
        else:
            remaining += 1
    if remaining >= _MAX_FILES:
        raise PreviewError("Host PNG preview limit reached; try again later.")


@contextmanager
def _storage(host_mounts):
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
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _cleanup(fd, uid)
            yield fd, os.path.join(root, name), uid
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _stage_png(data, host_mounts):
    with _storage(host_mounts) as (directory, path, uid):
        name = f"preview-{secrets.token_hex(16)}.png"
        fd = os.open(name, _FILE_FLAGS, mode=0o600, dir_fd=directory)
        complete = False
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
            complete = True
        finally:
            if not complete:
                os.unlink(name, dir_fd=directory)
        return os.path.join(path, name)


def open_png(data: bytes, *, host_mounts=()) -> None:
    """Only call after host consent; validate and open the exact approved bytes.

    The launcher does not reap a file within a day of staging it, even when
    opener status is uncertain. A later approved open can reap expired files
    across client exits. Runtime-directory teardown may clear them sooner, and
    a viewer may retain its own copy.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
        raise PreviewError("Host PNG preview refused: invalid canonical PNG.")
    try:
        _validate_canonical(data)
    except ValueError:
        raise PreviewError("Host PNG preview refused: invalid canonical PNG.") from None

    env = _desktop_env()
    if env is None:
        raise PreviewError("Host PNG preview requires a desktop session.")
    if not _opener_available():
        raise PreviewError("Host PNG preview opener is unavailable.")
    try:
        host_mounts = tuple(host_mounts)
    except TypeError:
        raise PreviewError("Host PNG preview storage is not trusted.") from None
    if not ready(host_mounts=host_mounts):
        raise PreviewError("Host PNG preview storage is not trusted.")
    try:
        path = _stage_png(data, host_mounts)
    except OSError:
        raise PreviewError("Host PNG preview storage is unavailable.") from None
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
            "Host PNG preview opener did not report completion."
        ) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise PreviewError("Host PNG preview opener is unavailable.") from None
    if result.returncode != 0:
        raise PreviewError("Host PNG preview opener failed.")
