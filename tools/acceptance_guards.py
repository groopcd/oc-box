"""Small read-only predicates for synthetic host acceptance fixtures.

These are not fixture authorization: callers own purpose/schema checks, locks,
credential policy, engine execution, and reports. Candidate code must not be
imported to establish its own initial source trust. No probes run at import.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat


GUARD_CODES = frozenset(
    {
        "guard-error",
        "unsafe-path",
        "private-file",
        "private-json",
        "exact-files",
        "fresh-parent-required",
        "native-executable",
        "native-changed",
        "file-hash",
        "runtime-boot-current",
        "runtime-boot-marker",
        "runtime-boot-mismatch",
        "store-info-shape",
        "rootless",
        "remote-engine",
        "storage-driver",
        "private-store",
        "image-temporary-path",
    }
)


class GuardError(ValueError):
    """Only a fixed category, never a path, payload, or underlying error."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in GUARD_CODES else "guard-error"
        super().__init__(self.code)


@dataclass(frozen=True)
class NativeIdentity:
    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True)
class StorePaths:
    graphRoot: Path
    runRoot: Path
    volumePath: Path
    imageCopyTmpDir: Path
    configFile: Path


@dataclass(frozen=True)
class BootCheckpoint:
    cached_marker_present: bool
    cached_marker_empty: bool
    cached_id_valid: bool
    current_id_valid: bool
    boot_ids_match: bool | None


def _require(condition, code):
    if not condition:
        raise GuardError(code)


def _lexical(path):
    """Absolute without resolving or inspecting any filesystem component."""
    _require(isinstance(path, (str, Path)), "unsafe-path")
    _require(
        not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in str(path)),
        "unsafe-path",
    )
    result = Path(path)
    _require(".." not in result.parts, "unsafe-path")
    return result.absolute()


def _inspect_path(path, kind, optional):
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            _require(optional, "unsafe-path")
            continue
        _require(not stat.S_ISLNK(info.st_mode), "unsafe-path")
        if component != path or kind == "directory":
            _require(stat.S_ISDIR(info.st_mode), "unsafe-path")
        elif kind == "file":
            _require(stat.S_ISREG(info.st_mode), "unsafe-path")


def checked_path(path, *, root=None, kind=None, optional=False):
    """Reject traversal, controls, redirects and unexpected/missing path kinds."""
    try:
        _require(kind in (None, "file", "directory"), "unsafe-path")
        if root is not None:
            root = _lexical(root)
            _inspect_path(root, "directory", False)
            path = _lexical(root / path)
            _require(path != root and path.is_relative_to(root), "unsafe-path")
        else:
            path = _lexical(path)
        _inspect_path(path, kind, optional)
        return path
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, GuardError):
            raise
        raise GuardError("unsafe-path") from None


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
def _opened(path, code):
    """Walk with no-follow descriptors as well as the earlier lexical proof."""
    directory = descriptor = None
    failed = False
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open(path.anchor, flags)
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=directory)
            previous, directory = directory, child
            # Transfer ownership first. A failed close may have taken effect;
            # never retry its now-unowned number, which may have been reused.
            os.close(previous)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory,
        )
        initial = os.fstat(descriptor)
        _require(stat.S_ISREG(initial.st_mode), code)
        yield descriptor, initial
        final = os.fstat(descriptor)
        _require(_stamp(initial) == _stamp(final), code)
        _require(
            _stamp(final)
            == _stamp(os.stat(path.name, dir_fd=directory, follow_symlinks=False)),
            code,
        )
        checked_path(path, kind="file")
        _require(_stamp(final) == _stamp(path.lstat()), code)
    except BaseException as error:
        failed = True  # This invocation, not a caller's already-handled error.
        if isinstance(error, (OSError, ValueError, TypeError)):
            if isinstance(error, GuardError) and error.code != "unsafe-path":
                raise
            raise GuardError(code) from None
        raise
    finally:
        owned = (descriptor, directory)
        # Seal every owned number before any fallible cleanup, then attempt
        # each independently. An ambiguous failed close is never retried.
        descriptor = directory = None
        cleanup_error = None
        for closing in owned:
            if closing is not None:
                try:
                    os.close(closing)
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
        if not failed and cleanup_error is not None:
            # A body failure wins; otherwise keep the first cancellation/fixed
            # refusal, and classify ordinary cleanup errors without raw context.
            if isinstance(cleanup_error, GuardError) or not isinstance(
                cleanup_error, Exception
            ):
                raise cleanup_error
            raise GuardError(code) from None


def _private_bytes(
    path, *, uid, max_bytes, code="private-file", oversized=None, private=True
):
    _require(type(uid) is int and uid >= 0, code)
    with _opened(path, code) as (descriptor, info):
        _require(info.st_uid == uid and info.st_nlink == 1, code)
        _require(not private or not info.st_mode & 0o077, code)
        _require(info.st_size <= max_bytes, oversized or code)
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        _require(len(content) <= max_bytes, oversized or code)
        _require(len(content) == info.st_size, code)
    return bytes(content)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "private-json")
        result[key] = value
    return result


def _json_constant(value):
    raise GuardError("private-json")


def read_private_json(root, relative, *, uid, max_bytes):
    """Bounded private file proof followed by schema-neutral strict JSON."""
    _require(type(max_bytes) is int and max_bytes > 0, "private-file")
    path = checked_path(relative, root=root, kind="file")
    raw = _private_bytes(path, uid=uid, max_bytes=max_bytes)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_json_constant,
        )
    except (ValueError, RecursionError):
        raise GuardError("private-json") from None


def exact_private_files(root, expected, *, uid):
    """Check only supplied files, over complete bytes; not a directory schema."""
    _require(isinstance(expected, Mapping), "exact-files")
    for relative, content in expected.items():
        _require(
            isinstance(relative, str) and isinstance(content, bytes), "exact-files"
        )
        path = checked_path(relative, root=root, kind="file")
        actual = _private_bytes(
            path, uid=uid, max_bytes=len(content), oversized="exact-files"
        )
        _require(actual == content, "exact-files")


def fresh_parent(path, *, prefixes, markers):
    """Refuse retained fixture/evidence ancestors, including renamed evidence."""
    parent = checked_path(path, kind="directory")
    _require(isinstance(prefixes, tuple) and isinstance(markers, tuple), "unsafe-path")
    for name in (*prefixes, *markers):
        _require(
            isinstance(name, str)
            and name not in ("", ".", "..")
            and "/" not in name
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name),
            "unsafe-path",
        )
    try:
        for ancestor in (parent, *parent.parents):
            _require(not ancestor.name.startswith(prefixes), "fresh-parent-required")
            # Enumerating names, not following entries, also sees dangling links.
            if markers:
                with os.scandir(ancestor) as entries:
                    _require(
                        not any(
                            fnmatch.fnmatchcase(entry.name, marker)
                            for entry in entries
                            for marker in markers
                        ),
                        "fresh-parent-required",
                    )
        return parent
    except OSError:
        raise GuardError("fresh-parent-required") from None


def _hash_file(path, *, native=False):
    code = "native-executable" if native else "file-hash"
    with _opened(path, code) as (descriptor, info):
        if native:
            _require(info.st_mode & 0o111 and os.access(path, os.X_OK), code)
        digest = hashlib.sha256()
        length = 0
        while length <= info.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, info.st_size + 1 - length))
            if not chunk:
                break
            if native and not length:
                _require(chunk.startswith(b"\x7fELF"), code)
            digest.update(chunk)
            length += len(chunk)
        _require(length == info.st_size and (not native or length >= 4), code)
    return digest.hexdigest(), length


def sha256_file(path):
    """Hash the full regular file without following redirects."""
    try:
        path = checked_path(path, kind="file")
        return _hash_file(path)[0]
    except GuardError:
        raise GuardError("file-hash") from None


def native_identity(path, *, outside, resolve_entry=False):
    """Pin native bytes without executing; entry resolution is discovery-only."""
    try:
        _require(isinstance(outside, tuple), "native-executable")
        path = _lexical(path)
        if resolve_entry:
            checked_path(path.parent, kind="directory")
            path = path.resolve(strict=True)
        path = checked_path(path, kind="file")
        for excluded in outside:
            _require(not path.is_relative_to(_lexical(excluded)), "native-executable")
        digest, length = _hash_file(path, native=True)
        return NativeIdentity(path, digest, length)
    except (OSError, ValueError, TypeError):
        raise GuardError("native-executable") from None


def check_native(expected):
    """Recheck the recorded path and bytes; never re-resolve or repin a target."""
    try:
        _require(
            isinstance(expected, NativeIdentity)
            and isinstance(expected.path, Path)
            and expected.path.is_absolute()
            and type(expected.bytes) is int
            and expected.bytes >= 4
            and isinstance(expected.sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected.sha256) is not None,
            "native-changed",
        )
        _require(
            native_identity(expected.path, outside=()) == expected, "native-changed"
        )
    except GuardError:
        raise GuardError("native-changed") from None


def boot_checkpoint(marker, current, *, uid):
    """Only boolean evidence escapes; caller supplies current boot bytes."""
    try:
        marker = checked_path(marker, kind="file", optional=True)
        try:
            marker.lstat()
        except FileNotFoundError:
            cached = None
        else:
            cached = _private_bytes(
                marker,
                uid=uid,
                max_bytes=128,
                code="runtime-boot-marker",
                private=False,
            )
    except GuardError:
        raise GuardError("runtime-boot-marker") from None
    except OSError:
        raise GuardError("runtime-boot-marker") from None
    pattern = rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n?"
    valid_cached = (
        isinstance(cached, bytes) and re.fullmatch(pattern, cached) is not None
    )
    valid_current = (
        isinstance(current, bytes) and re.fullmatch(pattern, current) is not None
    )
    matches = cached == current if valid_cached and valid_current else None
    return BootCheckpoint(
        cached is not None, cached == b"", valid_cached, valid_current, matches
    )


def require_boot(checkpoint, *, fresh_empty=False):
    """The fresh-empty exception requires a separate caller-owned store proof."""
    _require(isinstance(checkpoint, BootCheckpoint), "runtime-boot-marker")
    _require(checkpoint.current_id_valid is True, "runtime-boot-current")
    if fresh_empty is True and (
        checkpoint.cached_marker_present is False
        or checkpoint.cached_marker_empty is True
    ):
        return
    _require(checkpoint.cached_id_valid is True, "runtime-boot-marker")
    _require(checkpoint.boot_ids_match is True, "runtime-boot-mismatch")


def private_selectors(root):
    """Path transformation only: callers prove the private source/config bytes."""
    root = _lexical(root)
    return {
        "CONTAINERS_STORAGE_CONF": str(root / "config/containers/storage.conf"),
        "CONTAINERS_CONF": str(root / "config/containers/containers.conf"),
    }


def _store_path(actual, expected, code):
    try:
        _require(type(actual) is str and Path(actual).is_absolute(), code)
        _require(
            isinstance(expected, (str, Path)) and Path(expected).is_absolute(), code
        )
        _require(actual == str(_lexical(actual)) == str(_lexical(expected)), code)
    except (OSError, ValueError, TypeError):
        raise GuardError(code) from None


def validate_store_info(info, expected, *, allow_audited_614_omission=False):
    """Data-only native-info validation: no engine, fixture or filesystem probes."""
    _require(
        type(info) is dict and isinstance(expected, StorePaths), "store-info-shape"
    )
    host, store = info.get("host"), info.get("store")
    _require(type(host) is dict and type(store) is dict, "store-info-shape")
    security = host.get("security")
    _require(type(security) is dict, "store-info-shape")
    _require(security.get("rootless") is True, "rootless")
    _require(host.get("serviceIsRemote") is False, "remote-engine")
    _require(store.get("graphDriverName") == "overlay", "storage-driver")
    for field in ("graphRoot", "runRoot", "volumePath"):
        _store_path(store.get(field), getattr(expected, field), "private-store")
    if "configFile" in store:
        _store_path(store["configFile"], expected.configFile, "store-info-shape")
    else:
        _store_path(str(expected.configFile), expected.configFile, "store-info-shape")
        version = info.get("version")
        _require(
            allow_audited_614_omission is True
            and type(version) is dict
            and type(version.get("Version")) is str
            and version["Version"] == "6.1.4",
            "store-info-shape",
        )
    _store_path(
        store.get("imageCopyTmpDir"), expected.imageCopyTmpDir, "image-temporary-path"
    )
