"""Bounded, read-only host profiles for a separately encrypted backup member.

Snapshots map (scope, relative name) to exact file bytes, or None for a
directory. No source paths, permission bits or ownership are serialized.
The encoding is plaintext and must stay in memory until encrypted.
"""

import base64
import binascii
import json
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

if __package__:
    from . import log_config, vault
    from .backup_crypto import MAX_SMALL, BackupError
else:
    import log_config
    import vault
    from backup_crypto import MAX_SMALL, BackupError


OC_BOX_CONFIG = "oc-box-config"
VAULT = "vault"
OPENCODE_CONFIG = "opencode-config"
OPENCODE_AUTH = "opencode-auth"
_FIXED = {
    OC_BOX_CONFIG: frozenset({"config.json"}),
    VAULT: frozenset({"vault.json"}),
    OPENCODE_AUTH: frozenset({"auth.json", "mcp-auth.json"}),
}
_SCOPES = (*_FIXED, OPENCODE_CONFIG)
_SKIP_DIRS = frozenset({".git", ".cache", "node_modules"})
_DATABASE_NAMES = frozenset(
    name + suffix
    for name in ("opencode.db", "oc-box-v2.db")
    for suffix in ("", "-wal", "-shm", "-journal")
)
_FORMAT = "oc-box-host"
_VERSION = 1
MAX_PAYLOAD = MAX_SMALL - 64 * 1024  # Leave room for the enclosing age envelope.
MAX_PROFILE_BYTES = 800 * 1024
MAX_FILES = 128
MAX_DIRECTORIES = 64
MAX_SCAN_ENTRIES = 384  # Includes the untraversed cache directories themselves.
MAX_DEPTH = 12
MAX_PATH_BYTES = 512
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_UNSAFE = "Host backup source is unsafe, unreadable, changed, or exceeds its limits."
_INVALID = "Host backup payload is invalid or exceeds its limits."
_BAD_ROOTS = "Host backup locations are invalid or overlap."


class HostPayloadError(BackupError):
    """Fixed diagnostic; never includes paths, metadata or content."""


def destinations():
    """Return current host scope directories; these are not write authority."""
    try:
        config_dir = log_config._config_directory()
        data_home = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
        )
        vault_file = vault.vault_path()
        if (
            data_home.anchor != "/"
            or ".." in data_home.parts
            or vault_file.name != "vault.json"
            or vault_file.parent.parent.parent != data_home
        ):
            raise ValueError
        roots = {
            OC_BOX_CONFIG: config_dir,
            VAULT: vault_file.parent,
            OPENCODE_CONFIG: config_dir.with_name("opencode"),
            OPENCODE_AUTH: data_home / "opencode",
        }
        for scope, path in roots.items():
            if any(
                path.is_relative_to(other) or other.is_relative_to(path)
                for key, other in roots.items()
                if key != scope
            ):
                raise ValueError
        return roots
    except (OSError, RuntimeError, ValueError):
        raise HostPayloadError(_BAD_ROOTS) from None


def mount_id(fd):
    """Identify the pinned mount, including bind mounts with identical st_dev."""
    try:
        with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
            raw = stream.read(4097)
        if len(raw) <= 4096:
            for line in raw.splitlines():
                if line.startswith(b"mnt_id:\t"):
                    value = line[len(b"mnt_id:\t") :]
                    if 0 < len(value) <= 20 and value.isdigit():
                        return int(value)
    except OSError:
        pass
    raise HostPayloadError(_UNSAFE) from None


def _check_directory(info, *, leaf, private=False):
    uid = os.getuid()
    if not stat.S_ISDIR(info.st_mode):
        raise HostPayloadError(_UNSAFE)
    if leaf:
        if info.st_uid != uid or info.st_mode & (0o077 if private else 0o022):
            raise HostPayloadError(_UNSAFE)
    elif info.st_uid not in (0, uid) or (
        info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
    ):
        raise HostPayloadError(_UNSAFE)


def _open_scope_directory(path, anchor, *, private):
    if path.anchor != "/" or ".." in path.parts or anchor not in path.parents:
        raise HostPayloadError(_BAD_ROOTS)
    fd = os.open("/", _DIR_FLAGS)
    try:
        _check_directory(os.fstat(fd), leaf=False)
        anchor_mount = mount_id(fd) if anchor == Path("/") else None
        for depth, name in enumerate(path.parts[1:], 2):
            try:
                opened = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = opened
            _check_directory(
                os.fstat(fd), leaf=depth == len(path.parts), private=private
            )
            if depth == len(anchor.parts):
                anchor_mount = mount_id(fd)
            elif depth > len(anchor.parts) and mount_id(fd) != anchor_mount:
                raise HostPayloadError(_UNSAFE)
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def open_scope(scope, *, roots=None):
    """Yield an existing pinned scope FD (or None), without writing anything.

    The selected XDG base may be on its own filesystem, but scope directories
    and contents may not cross a mount below that base.
    """
    if scope not in _SCOPES:
        raise HostPayloadError(_BAD_ROOTS)
    root = (destinations() if roots is None else roots)[scope]
    anchor = root.parent.parent if scope == VAULT else root.parent
    try:
        fd = _open_scope_directory(root, anchor, private=scope == VAULT)
    except OSError:
        raise HostPayloadError(_UNSAFE) from None
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _relative_parts(name):
    if type(name) is not str or len(name) > MAX_PATH_BYTES:
        raise HostPayloadError(_INVALID)
    if name == "":
        return ()
    parts = tuple(name.split("/"))
    if (
        len(parts) > MAX_DEPTH
        or len(name.encode("utf-8")) > MAX_PATH_BYTES
        or "\\" in name
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
        or any(
            part in ("", ".", "..")
            or part in _SKIP_DIRS
            or part in _DATABASE_NAMES
            or len(part.encode("utf-8")) > 255
            for part in parts
        )
    ):
        raise HostPayloadError(_INVALID)
    return parts


def _entry_parts(scope, name):
    if type(scope) is not str or type(name) is not str:
        raise HostPayloadError(_INVALID)
    if scope == OPENCODE_CONFIG:
        return _relative_parts(name)
    if scope not in _FIXED or name not in _FIXED[scope]:
        raise HostPayloadError(_INVALID)
    return (name,)


class _Entries:
    def __init__(self):
        self.values = {}
        self.profile_bytes = 0
        self.files = 0
        self.directories = 0

    def available(self, scope):
        if scope == VAULT:
            return vault.MAX_FILE
        remaining = MAX_PROFILE_BYTES - self.profile_bytes
        return (
            min(remaining, log_config._CONFIG_LIMIT)
            if scope == OC_BOX_CONFIG
            else remaining
        )

    def add(self, scope, name, value):
        parts = _entry_parts(scope, name)
        key = (scope, name)
        if key in self.values:
            raise HostPayloadError(_INVALID)
        if value is None:
            if scope != OPENCODE_CONFIG or self.directories >= MAX_DIRECTORIES:
                raise HostPayloadError(_INVALID)
            self.directories += 1
        else:
            if (
                type(value) is not bytes
                or not parts
                or self.files >= MAX_FILES
                or len(value) > self.available(scope)
            ):
                raise HostPayloadError(_INVALID)
            self.files += 1
            if scope != VAULT:
                self.profile_bytes += len(value)
        self.values[key] = value

    def finish(self):
        missing = object()
        for scope, name in self.values:
            if scope == OPENCODE_CONFIG and name:
                parent = name.rpartition("/")[0]
                if self.values.get((scope, parent), missing) is not None:
                    raise HostPayloadError(_INVALID)
        try:
            if (OC_BOX_CONFIG, "config.json") in self.values:
                log_config._parse_config(self.values[OC_BOX_CONFIG, "config.json"])
            if (VAULT, "vault.json") in self.values:
                vault.parse_envelope(self.values[VAULT, "vault.json"])
        except (TypeError, ValueError, RecursionError, vault.VaultError):
            raise HostPayloadError(_INVALID) from None
        return self.values


def encode(snapshot):
    """Validate and serialize a snapshot; return plaintext bytes, not an archive."""
    if type(snapshot) is not dict or len(snapshot) > MAX_FILES + MAX_DIRECTORIES:
        raise HostPayloadError(_INVALID)
    entries = _Entries()
    try:
        for key, value in snapshot.items():
            if type(key) is not tuple or len(key) != 2:
                raise HostPayloadError(_INVALID)
            entries.add(*key, value)
        entries.finish()
        rows = [
            [
                scope,
                name,
                None if raw is None else base64.b64encode(raw).decode("ascii"),
            ]
            for (scope, name), raw in sorted(entries.values.items())
        ]
        raw = json.dumps(
            {"format": _FORMAT, "version": _VERSION, "entries": rows},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, RecursionError):
        raise HostPayloadError(_INVALID) from None
    if len(raw) > MAX_PAYLOAD:
        raise HostPayloadError(_INVALID)
    return raw


def decode(raw):
    """Validate the full payload before returning any entries for restore."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PAYLOAD:
        raise HostPayloadError(_INVALID)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=log_config._object,
            parse_constant=log_config._constant,
        )
        if (
            type(payload) is not dict
            or set(payload) != {"format", "version", "entries"}
            or payload["format"] != _FORMAT
            or type(payload["version"]) is not int
            or payload["version"] != _VERSION
            or type(payload["entries"]) is not list
            or len(payload["entries"]) > MAX_FILES + MAX_DIRECTORIES
        ):
            raise HostPayloadError(_INVALID)
        entries = _Entries()
        for row in payload["entries"]:
            if type(row) is not list or len(row) != 3:
                raise HostPayloadError(_INVALID)
            scope, name, encoded = row
            _entry_parts(scope, name)
            value = None
            if encoded is not None:
                if type(encoded) is not str or len(encoded) > 4 * (
                    (entries.available(scope) + 2) // 3
                ):
                    raise HostPayloadError(_INVALID)
                value = base64.b64decode(encoded, validate=True)
                if base64.b64encode(value).decode("ascii") != encoded:
                    raise HostPayloadError(_INVALID)
            entries.add(scope, name, value)
        return entries.finish()
    except (ValueError, RecursionError, binascii.Error):
        raise HostPayloadError(_INVALID) from None


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _pin(parent, name, parent_mount, *, missing=False):
    # O_PATH does not activate devices, FIFOs or sockets before classification.
    try:
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        if not missing:
            raise
        yield None
        return
    try:
        if mount_id(fd) != parent_mount:
            raise HostPayloadError(_UNSAFE)
        yield fd
    finally:
        os.close(fd)


def _unchanged(parent, name, fd, info):
    expected = _stamp(info)
    return (
        expected
        == _stamp(os.fstat(fd))
        == _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
    )


def _read_file(parent, name, fd, info, limit, *, private=False):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & (0o7077 if private else 0o7022)
        or not 0 <= info.st_size <= limit
    ):
        raise HostPayloadError(_UNSAFE)
    # Reopen the classified inode, not a pathname that could now name a device.
    readable = os.open(
        f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    )
    try:
        if _stamp(os.fstat(readable)) != _stamp(info):
            raise HostPayloadError(_UNSAFE)
        value = bytearray()
        while len(value) <= info.st_size:
            chunk = os.read(readable, min(65536, info.st_size + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if len(value) != info.st_size or not _unchanged(parent, name, readable, info):
            raise HostPayloadError(_UNSAFE)
        return bytes(value)
    finally:
        os.close(readable)


class _Capture:
    def __init__(self, excluded, excluded_file=None):
        self.entries = _Entries()
        self.scanned = 0
        self.excluded = excluded
        self.excluded_file = excluded_file

    def file(self, parent, name, fd, info, scope, relative):
        if (info.st_dev, info.st_ino) == self.excluded_file:
            raise HostPayloadError(_UNSAFE)
        if self.entries.files >= MAX_FILES:
            raise HostPayloadError(_UNSAFE)
        self.entries.add(
            scope,
            relative,
            _read_file(
                parent,
                name,
                fd,
                info,
                self.entries.available(scope),
                private=scope == VAULT,
            ),
        )

    def profile(self, directory, root_mount, relative):
        original = _stamp(os.fstat(directory))
        if original[:2] in self.excluded:
            raise HostPayloadError(_BAD_ROOTS)
        self.entries.add(OPENCODE_CONFIG, relative, None)
        with os.scandir(directory) as children:
            for child in children:
                self.scanned += 1
                if self.scanned > MAX_SCAN_ENTRIES:
                    raise HostPayloadError(_UNSAFE)
                name = child.name
                path = f"{relative}/{name}" if relative else name
                with _pin(directory, name, root_mount) as fd:
                    info = os.fstat(fd)
                    if stat.S_ISDIR(info.st_mode):
                        _check_directory(info, leaf=True)
                        if name in _SKIP_DIRS:
                            continue
                        _relative_parts(path)
                        opened = os.open(".", _DIR_FLAGS, dir_fd=fd)
                        try:
                            if _stamp(os.fstat(opened)) != _stamp(info):
                                raise HostPayloadError(_UNSAFE)
                            self.profile(opened, root_mount, path)
                        finally:
                            os.close(opened)
                        if not _unchanged(directory, name, fd, info):
                            raise HostPayloadError(_UNSAFE)
                    else:
                        _relative_parts(path)
                        self.file(directory, name, fd, info, OPENCODE_CONFIG, path)
        if _stamp(os.fstat(directory)) != original:
            raise HostPayloadError(_UNSAFE)


def _scope_state(fd):
    return None if fd is None else (mount_id(fd), _stamp(os.fstat(fd)))


def capture(*, excluded_file=None):
    """Read only the four scoped sources; missing entries are not synthesized."""
    roots = destinations()
    if not hasattr(os, "O_PATH"):
        raise HostPayloadError(_UNSAFE)
    try:
        with ExitStack() as stack:
            directories = {
                scope: stack.enter_context(open_scope(scope, roots=roots))
                for scope in _SCOPES
            }
            before = {scope: _scope_state(fd) for scope, fd in directories.items()}
            identities = [
                state[1][:2] for state in before.values() if state is not None
            ]
            if len(set(identities)) != len(identities):
                raise HostPayloadError(_BAD_ROOTS)
            # An XDG base is allowed to be a bind mount; even then, never walk
            # into a file-only scope through an alternate path in the profile.
            copied = _Capture(
                {before[scope][1][:2] for scope in _FIXED if before[scope] is not None},
                excluded_file,
            )
            for scope, directory in directories.items():
                if directory is None:
                    continue
                root_mount, _ = before[scope]
                if scope == OPENCODE_CONFIG:
                    copied.profile(directory, root_mount, "")
                    continue
                for name in sorted(_FIXED[scope]):
                    with _pin(directory, name, root_mount, missing=True) as fd:
                        if fd is not None:
                            copied.file(directory, name, fd, os.fstat(fd), scope, name)
            for scope, directory in directories.items():
                with open_scope(scope, roots=roots) as current:
                    if _scope_state(current) != before[scope] or (
                        _scope_state(directory) != before[scope]
                    ):
                        raise HostPayloadError(_UNSAFE)
            return copied.entries.finish()
    except (OSError, UnicodeError):
        raise HostPayloadError(_UNSAFE) from None
