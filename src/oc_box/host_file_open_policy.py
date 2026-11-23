"""Fail-closed default for a fresh, verified GitHub launcher installation.

The receipt lives at the installation root, not in a release, archived host
configuration, or workspace state. It records the inspected locations by path:
in particular, a registry need not already exist. Selectors dropped by an
older bootstrap cannot authorize use under different locations at runtime.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import stat


RECEIPT = ".host-file-open-v1"
INSTALL_LOCK = ".install.lock"
INSTALL_PATH = Path(".local/share/oc-box")
LAUNCHER = Path(".local/bin/oc-box")
_AUXILIARY = (
    Path(".local/share/man/man1/oc-box.1"),
    Path(".local/share/info/oc-box.info"),
    Path(".local/share/bash-completion/completions/oc-box"),
)
_LEGACY = (
    Path(".oc-box"),
    Path(".oc-box-id"),
    Path(".local/pipx/venvs/oc-box"),
    Path(".local/share/pipx/venvs/oc-box"),
    Path(".local/share/uv/tools/oc-box"),
)
_XDG_PATHS = (
    ("XDG_CONFIG_HOME", Path(".config")),
    ("XDG_DATA_HOME", Path(".local/share")),
    ("XDG_STATE_HOME", Path(".local/state")),
    ("XDG_CACHE_HOME", Path(".cache")),
)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_OFF = (OSError, ValueError, RuntimeError, TypeError)


def launcher_bytes(release):
    """The exact shell wrapper written on activation."""
    return (
        "#!/bin/sh\nexec python3 " + shlex.quote(str(release / "oc_box.py")) + ' "$@"\n'
    ).encode()


def _absolute(path):
    path = Path(path)
    if path.anchor != "/" or ".." in path.parts:
        raise ValueError("Unbound installation path")
    return path


def _locations(home):
    home = _absolute(home)
    selected = tuple(
        _absolute(os.environ.get(key, home / default)) / "oc-box"
        for key, default in _XDG_PATHS
    )
    registry = _absolute(
        Path(os.environ.get("OC_BOX_STATE_HOME", selected[2])).expanduser()
    )
    return home, registry, selected


def _trusted_dir(fd, *, owned=False, private=False):
    info = os.fstat(fd)
    uid = os.geteuid()
    if owned and info.st_uid != uid:
        raise ValueError("Installation directory has another owner")
    if info.st_uid not in (0, uid):
        raise ValueError("Untrusted directory owner")
    if info.st_mode & (0o077 if private else 0o022):
        # A system-managed sticky ancestor such as /tmp is not the leaf.
        if owned or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            raise ValueError("Untrusted directory mode")
    return info


@contextmanager
def _directory(path):
    """Pin each existing ancestor without resolving redirects or broken links."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        _trusted_dir(fd)
        for name in path.parts[1:]:
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                yield None
                return
            os.close(fd)
            fd = child
            _trusted_dir(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _child_directory(fd, name, *, private=False):
    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
    try:
        _trusted_dir(child, owned=True, private=private)
        yield child
    finally:
        os.close(child)


def _entry(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _absent(path):
    with _directory(path.parent) as fd:
        return fd is None or _entry(fd, path.name) is None


def _no_prior_state(locations, *, launcher, auxiliary, install_root):
    home, registry, selected = locations
    target = home / INSTALL_PATH
    evidence = {home / item for item in _LEGACY}
    evidence.update(home / default / "oc-box" for _, default in _XDG_PATHS)
    evidence.update(selected)
    evidence.discard(target)  # Default XDG data is the installation itself.
    evidence.add(registry)
    if install_root:
        evidence.add(target)
    if launcher:
        evidence.add(home / LAUNCHER)
    if auxiliary:
        evidence.update(home / item for item in _AUXILIARY)
    return all(_absent(path) for path in evidence)


def fresh_locations(home):
    """Capture eligible locations before the installer creates any evidence."""
    try:
        locations = _locations(home)
        if _no_prior_state(locations, launcher=True, auxiliary=True, install_root=True):
            return locations
    except _OFF:
        pass
    return None


def _identity(info):
    return info.st_dev, info.st_ino


def _root(home, fd):
    if fd is None:
        raise ValueError("Missing installation root")
    info = _trusted_dir(fd, owned=True)
    # A home may not itself be another user's directory even when a child is
    # ours. Require the exact non-redirected home for this auto-default.
    with _directory(home) as home_fd:
        if home_fd is None:
            raise ValueError("Missing installation home")
        _trusted_dir(home_fd, owned=True)
    return _identity(info)


def new_root_identity(locations):
    """Identify the root immediately after this installer won its mkdir."""
    home = locations[0]
    try:
        with _directory(home / INSTALL_PATH) as fd:
            return _root(home, fd)
    except _OFF:
        return None


def _trusted_file(info, *, mode=None):
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
    ):
        raise ValueError("Untrusted installation file")
    return info


def _lock_identity(fd, held_lock=None):
    lock = _trusted_file(_entry(fd, INSTALL_LOCK))
    identity = (*_identity(lock), lock.st_ctime_ns)
    if held_lock is not None:
        live = os.fstat(held_lock)
        if identity != (*_identity(live), live.st_ctime_ns):
            raise ValueError("Installation lock changed")
    return identity


def _pending(locations, root_identity, fd, held_lock):
    if (
        _root(locations[0], fd) != root_identity
        or _locations(locations[0]) != locations
        or _entry(fd, ".upgrade.lock") is not None
        or _entry(fd, RECEIPT) is not None
    ):
        raise ValueError("Installation is not new")
    return _lock_identity(fd, held_lock)


def still_fresh(locations, root_identity, held_lock, *, initial=False, auxiliary=True):
    """Recheck under the install lock, including after any slow staging work."""
    try:
        with _directory(locations[0] / INSTALL_PATH) as fd:
            _pending(locations, root_identity, fd, held_lock)
            if initial:
                with os.scandir(fd) as entries:
                    if any(entry.name != INSTALL_LOCK for entry in entries):
                        return False
            return _no_prior_state(
                locations, launcher=True, auxiliary=auxiliary, install_root=False
            )
    except _OFF:
        return False


def _matches_file(fd, name, expected, *, mode):
    # Precheck the entry so a malformed receipt is not opened as a special
    # device. The opened file must then satisfy the same conditions.
    before = _trusted_file(_entry(fd, name), mode=mode)
    if before.st_size != len(expected):
        return False
    raw = os.open(name, _READ_FLAGS, dir_fd=fd)
    with os.fdopen(raw, "rb") as stream:
        opened = _trusted_file(os.fstat(stream.fileno()), mode=mode)
        return (
            _identity(opened) == _identity(before)
            and opened.st_size == len(expected)
            and stream.read(len(expected) + 1) == expected
        )


def _active(home, fd, package):
    if package.parent != home / INSTALL_PATH / "releases":
        return False
    if os.readlink("current", dir_fd=fd) != f"releases/{package.name}":
        return False
    with (
        _child_directory(fd, "releases") as releases,
        _child_directory(releases, package.name, private=True) as release,
    ):
        _trusted_file(_entry(release, "oc_box.py"), mode=0o644)
        _trusted_file(_entry(release, "build-info.json"), mode=0o644)
        if any(
            _entry(release, name) is not None
            for name in (
                "distribution.json",
                "pyproject.toml",
                ".git",
            )
        ):
            return False
    with _directory(home / LAUNCHER.parent) as bin_fd:
        if bin_fd is None:
            return False
        return _matches_file(bin_fd, LAUNCHER.name, launcher_bytes(package), mode=0o755)


def _receipt(locations, root_identity, lock_identity):
    home, registry, selected = locations
    context = json.dumps(
        [
            str(home / INSTALL_PATH),
            root_identity,
            lock_identity,
            str(registry),
            [str(path) for path in selected],
        ],
        separators=(",", ":"),
    ).encode("ascii")
    return (
        b"oc-box/host-file-open-v1 "
        + hashlib.sha256(context).hexdigest().encode()
        + b"\n"
    )


def record_fresh(locations, root_identity, held_lock, release):
    """Return whether a fully activated first launcher received its receipt."""
    try:
        home = locations[0]
        with _directory(home / INSTALL_PATH) as fd:
            lock_identity = _pending(locations, root_identity, fd, held_lock)
            if not _active(home, fd, release) or not _no_prior_state(
                locations, launcher=False, auxiliary=False, install_root=False
            ):
                return False
            expected = _receipt(locations, root_identity, lock_identity)
            temporary = RECEIPT + "." + secrets.token_hex(8)
            raw = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            try:
                with os.fdopen(raw, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
                # The receipt is visible only after a complete write, and the
                # temporary alias disappearing leaves its required single link.
                os.link(
                    temporary,
                    RECEIPT,
                    src_dir_fd=fd,
                    dst_dir_fd=fd,
                    follow_symlinks=False,
                )
            finally:
                os.unlink(temporary, dir_fd=fd)
            return _matches_file(fd, RECEIPT, expected, mode=0o600)
    except _OFF:
        # Receipt failure affects the optional default, not the installation.
        return False


def enabled(package, *, home, registry):
    """Whether the running package is the active, marked GitHub installation."""
    try:
        locations = _locations(home)
        home, bound_registry, selected = locations
        package = _absolute(package)
        target = home / INSTALL_PATH
        if (
            package.parent != target / "releases"
            or _absolute(registry) != bound_registry
        ):
            return False
        with _directory(target) as fd:
            expected = _receipt(locations, _root(home, fd), _lock_identity(fd))
            if not _matches_file(fd, RECEIPT, expected, mode=0o600):
                return False
            if not _active(home, fd, package):
                return False
        # Do not let an alias grant access to a registry we did not inspect.
        # Existing scope paths must still be safe directories without symlinks.
        for path in set(selected) | {bound_registry}:
            with _directory(path) as scope:
                if scope is not None:
                    _trusted_dir(scope, owned=True)
        return True
    except _OFF:
        return False
