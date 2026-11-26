"""Read-only uninstall review for the installation that supplied this runtime.

The inventory is local installer evidence, not a signature or protection from
the owning user's deliberate modification. A review is not an execution permit.
"""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from email.parser import BytesParser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

if __package__:
    from . import host_file_open_policy as layout
    from . import workspace_identity
else:
    import host_file_open_policy as layout
    import workspace_identity


INVENTORY = ".install-inventory-v2.json"
_VFS_IDENTITY = "vfs_identity"
UPGRADE_LOCK = ".upgrade.lock"
USE_LOCK = ".install-use.lock"
HANDOFF_FD = "OC_BOX_INSTALL_USE_FD"
_LIMIT = 2 * 1024 * 1024
_MAX_ENTRIES = 8192
_MAX_DEPTH = 16
_ENTRY = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR = _READ | os.O_DIRECTORY
_EXTRAS = ("vault", "tui")


class OwnershipError(ValueError):
    """Fixed refusal; underlying paths, metadata and errors stay private."""


@dataclass(frozen=True)
class InstallArtifact:
    path: Path
    parent: tuple[int, ...]
    stamp: tuple[int, ...]
    link: str | None = None


@dataclass(frozen=True)
class FlatInstallPlan:
    release: Path
    artifacts: tuple[InstallArtifact, ...]
    anchors: tuple[tuple[int, ...], ...]
    upgrade_lock: tuple[int, ...]
    use_lock: tuple[int, ...]


@dataclass(frozen=True)
class PackageInstallReview:
    """Local receipt evidence for manual guidance, not a deletion plan."""

    manager: str
    prefix: Path
    version: str


@dataclass(frozen=True)
class _StagedInventory:
    directory: tuple[int, ...]
    artifacts: tuple[InstallArtifact, ...]


@dataclass(frozen=True)
class ActiveInventory:
    token: str
    install_lock: tuple[int, ...]
    upgrade_lock: tuple[int, ...]
    use_lock: tuple[int, ...]
    build: dict


@dataclass(frozen=True)
class _RuntimeLease:
    pid: int
    release: Path
    fd: int
    stamp: tuple[int, ...]


_runtime_lease = None


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _stamp(info):
    return (
        *_identity(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def published_identity(info):
    """Bind the installer's publication to the inode it actually wrote."""
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _mount(fd):
    # st_dev alone misses bind mounts, including file-to-file bind mounts.
    with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
        for line in stream.read(4096).splitlines():
            if line.startswith(b"mnt_id:"):
                return int(line.partition(b":")[2])
    raise OwnershipError("Cannot verify installation mount boundaries.")


def _anchor(fd):
    return (*_identity(os.fstat(fd)), _mount(fd))


def _inventory_vfs(mount, descriptors):
    """Prove one filesystem for the four inventory anchors and wrapper parent."""
    device = descriptors[0][1][0]
    filesystem = None
    handles = []
    for fd, anchor in descriptors:
        if anchor[-1] != mount:
            raise OwnershipError("An installation anchor crosses a mount boundary.")
        if anchor[0] != device:
            return None
        identity, stable = workspace_identity.snapshot_fd(fd)
        if tuple(identity) != anchor[:2] or _anchor(fd) != anchor:
            raise OwnershipError("An installation anchor changed during verification.")
        if stable is None:
            return None
        observed = stable["method"], stable["fsid"]
        if filesystem is None:
            filesystem = observed
        if observed != filesystem:
            return None
        handles.append(stable["handle"])
    method, fsid = filesystem
    return {"method": method, "fsid": fsid, "handles": handles}


def _matches_inventory_stamp(recorded, observed, device_remap):
    if (
        not isinstance(recorded, list)
        or len(recorded) != len(observed)
        or any(type(value) is not int for value in recorded)
    ):
        return False
    if device_remap is not None:
        previous, current = device_remap
        if observed[0] != current:
            return False
        observed = (previous, *observed[1:])
    return recorded == list(observed)


def _owner(info, *, private=False):
    if info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022):
        raise OwnershipError("Installation ownership or permissions are unsafe.")


@contextmanager
def _home(path):
    # The home can itself be on a different filesystem. Below it, never cross
    # an unreviewed mount boundary to inspect or claim artifacts.
    fd = os.open("/", _DIR)
    try:
        for name in path.parts[1:]:
            layout._trusted_dir(fd)
            child = os.open(name, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        layout._trusted_dir(fd, owned=True)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _directory(parent, name, mount, *, private=False):
    fd = os.open(name, _DIR, dir_fd=parent)
    try:
        _owner(os.fstat(fd), private=private)
        if _mount(fd) != mount:
            raise OwnershipError("An installation directory crosses a mount boundary.")
        yield fd
    finally:
        os.close(fd)


def _inspect(parent, name, mount):
    fd = os.open(name, _ENTRY, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if _mount(fd) != mount:
            raise OwnershipError("An installation artifact crosses a mount boundary.")
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise OwnershipError("An installation link is unsafe.")
        else:
            _owner(info)
            if not (
                stat.S_ISDIR(info.st_mode)
                or (
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and not info.st_mode & 0o7000
                )
            ):
                raise OwnershipError("An installation artifact is unsafe.")
        return info
    finally:
        os.close(fd)


def _read_file(parent, name, mount, info, limit, *, mode=None):
    if not stat.S_ISREG(info.st_mode) or (
        mode is not None and stat.S_IMODE(info.st_mode) != mode
    ):
        raise OwnershipError("An installation file is unsafe.")
    if not 0 <= info.st_size <= limit:
        raise OwnershipError("An installation file exceeds its review limit.")
    pinned = os.open(name, _ENTRY, dir_fd=parent)
    try:
        if _mount(pinned) != mount or _stamp(os.fstat(pinned)) != _stamp(info):
            raise OwnershipError("An installation file changed during review.")
        # Reopen the checked descriptor via the kernel's own fd directory,
        # not the pathname where a late special file might now live.
        fd = os.open(
            f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC | os.O_NOATIME
        )
        with os.fdopen(fd, "rb") as stream:
            if _mount(fd) != mount or _stamp(os.fstat(fd)) != _stamp(info):
                raise OwnershipError("An installation file changed during review.")
            raw = stream.read(info.st_size + 1)
            if len(raw) != info.st_size or _stamp(os.fstat(fd)) != _stamp(info):
                raise OwnershipError("An installation file changed during review.")
        if _stamp(_inspect(parent, name, mount)) != _stamp(info):
            raise OwnershipError("An installation file changed during review.")
        return raw
    finally:
        os.close(pinned)


def _lock_info(parent, name, mount, *, private=False):
    info = _inspect(parent, name, mount)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size
        or (private and stat.S_IMODE(info.st_mode) != 0o600)
    ):
        raise OwnershipError("An installation lock is unsafe.")
    return info


@contextmanager
def _open_lock(parent, name, mount, info):
    entry = os.open(name, _ENTRY, dir_fd=parent)
    try:
        if _mount(entry) != mount or _stamp(os.fstat(entry)) != _stamp(info):
            raise OwnershipError("An installation lock changed.")
        fd = os.open(f"/proc/self/fd/{entry}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            if _mount(fd) != mount or _stamp(os.fstat(fd)) != _stamp(info):
                raise OwnershipError("An installation lock changed.")
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(entry)


def _runtime_home(release):
    suffix = (*layout.INSTALL_PATH.parts, "releases", release.name)
    if release.is_absolute() and release.parts[-len(suffix) :] == suffix:
        return release.parents[len(suffix) - 1]
    return None


@contextmanager
def installed_runtime_use(release):
    """Keep installed launchers pinned until they exit; no lock for source."""
    global _runtime_lease
    home = _runtime_home(release)
    if home is None:
        yield
        return
    with ExitStack() as stack:
        try:
            # Normal launch does not require the stricter uninstall ancestry
            # or single-mount layout. Pin the runtime's actual lock regardless.
            directory = os.open(release, _DIR)
            stack.callback(os.close, directory)
            mount = _mount(directory)
            info = _lock_info(directory, USE_LOCK, mount, private=True)
            inherited = os.environ.pop(HANDOFF_FD, None)
            if inherited is None:
                fd = stack.enter_context(_open_lock(directory, USE_LOCK, mount, info))
            else:
                match = re.fullmatch(r"([0-9]{1,12}):([0-9]{1,10})", inherited)
                if match is None or int(match[1]) != os.getpid():
                    raise OwnershipError(
                        "The launch handoff is not bound to this process."
                    )
                fd = int(match[2])
                if (
                    fd < 3
                    or not os.get_inheritable(fd)
                    or _stamp(os.fstat(fd)) != _stamp(info)
                    or _mount(fd) != mount
                ):
                    raise OwnershipError(
                        "The launch handoff is not bound to this release."
                    )
                os.set_inheritable(fd, False)
                stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if _stamp(_lock_info(directory, USE_LOCK, mount, private=True)) != _stamp(
                info
            ):
                raise OwnershipError("The installed release changed during launch.")
        except (OSError, ValueError):
            raise OwnershipError(
                "The installed release is unavailable or being removed. Use the active launcher."
            ) from None
        previous = _runtime_lease
        _runtime_lease = _RuntimeLease(os.getpid(), release, fd, _stamp(info))
        try:
            yield
        finally:
            _runtime_lease = previous


def inspect_active_installation(home, release):
    """Verify activation and lock evidence without importing another release."""
    try:
        return _snapshot(
            layout._absolute(home), layout._absolute(release), activation=True
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        raise OwnershipError("The active installation could not be verified.") from None


def active_release_path(home):
    """Read only the literal pin; callers must separately verify its evidence."""
    with layout._directory(home / layout.INSTALL_PATH) as root:
        if root is None:
            raise OwnershipError("The active installation is unavailable.")
        name = os.readlink("current", dir_fd=root)
    if not re.fullmatch(r"releases/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
        raise OwnershipError("The active installation pin is unsafe.")
    return home / layout.INSTALL_PATH / name


def require_activation(home, token, *, lock_fd):
    """Called under the install lock immediately before an automatic install."""
    match = re.fullmatch(r"([a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}):([0-9a-f]{64})", token)
    if match is not None:
        try:
            active = inspect_active_installation(
                home, home / layout.INSTALL_PATH / "releases" / match[1]
            )
            if (
                active.install_lock == _stamp(os.fstat(lock_fd))
                and active.token == token
            ):
                return
        except (OSError, OwnershipError):
            pass
    raise OwnershipError(
        "The active installation changed before the automatic update; no release was activated."
    )


def verify_active_launcher(home, release):
    """Allow explicit recovery/update with an exact launcher but no inventory."""
    try:
        if _runtime_home(release) != home:
            raise ValueError()
        with layout._directory(home / layout.INSTALL_PATH) as root:
            if root is None:
                raise ValueError()
            layout._trusted_dir(root, owned=True)
            if os.readlink("current", dir_fd=root) != f"releases/{release.name}":
                raise ValueError()
            with (
                layout._child_directory(root, "releases") as releases,
                layout._child_directory(releases, release.name, private=True) as pinned,
            ):
                layout._trusted_file(layout._entry(pinned, "oc_box.py"), mode=0o644)
                layout._trusted_file(
                    layout._entry(pinned, "build-info.json"), mode=0o644
                )
        with layout._directory(home / layout.LAUNCHER.parent) as bin_fd:
            wrapper = layout.launcher_bytes(release)
            old = wrapper.replace(b"exec python3 -B ", b"exec python3 ", 1)
            if bin_fd is None or not any(
                layout._matches_file(bin_fd, layout.LAUNCHER.name, content, mode=0o755)
                for content in (wrapper, old)
            ):
                raise ValueError()
    except (OSError, ValueError):
        raise OwnershipError(
            "This is not the active launcher. Use the installed launcher or the verified installer."
        ) from None


def _names(fd):
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            name = entry.name
            if (
                len(names) >= _MAX_ENTRIES
                or not name.isascii()
                or not name.isprintable()
            ):
                raise OwnershipError("The release inventory exceeds its review limits.")
            names.add(name)
    return names


def _link(fd, name, mount, target):
    info = _inspect(fd, name, mount)
    if (
        not stat.S_ISLNK(info.st_mode)
        or os.readlink(name, dir_fd=fd) != target
        or _stamp(_inspect(fd, name, mount)) != _stamp(info)
    ):
        raise OwnershipError("An installation link does not match the pinned release.")
    return info


def _artifact(parent, path, info, link=None):
    return InstallArtifact(path, _anchor(parent), _stamp(info), link)


def _inventory_bytes(
    home, release, anchors, lock, upgrade, receipt, artifacts, *, vfs_identity
):
    return (
        json.dumps(
            {
                "version": 2,
                "capture": "before-activation",
                "release": str(release),
                # Mount IDs are rechecked at review but are not stable across boots.
                "anchors": [anchor[:-1] for anchor in anchors],
                "lock": _stamp(lock),
                "upgrade": _stamp(upgrade),
                "receipt": _identity(receipt),
                **({_VFS_IDENTITY: vfs_identity} if vfs_identity is not None else {}),
                "artifacts": {
                    str(item.path.relative_to(home)): [
                        item.parent[:-1],
                        item.stamp,
                        item.link,
                    ]
                    for item in artifacts
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _release_layout():
    if __package__:
        from . import installation
    else:
        import installation

    aliases = (*installation.PROGRAM_FILES, "build-info.json", "docs", "completions")
    fixed_dirs = {
        "docs": set(installation.MANUAL_FILES),
        "completions": {Path(installation.COMPLETION_FILE).name},
    }
    return aliases, fixed_dirs


def _walk_release(release, pinned, mount, add, *, has_inventory):
    aliases, fixed_dirs = _release_layout()
    names = _names(pinned)
    top = {*aliases, USE_LOCK}
    for feature in _EXTRAS:
        support = f"{feature}-support.json"
        if support in names:
            top.update((support, f"{feature}-deps"))
    if names != top | ({INVENTORY} if has_inventory else set()):
        raise OwnershipError("The release contains missing or unrecognized artifacts.")
    deps = {f"{feature}-deps" for feature in _EXTRAS}

    def visit(parent, path, depth, *, dependency=False):
        if depth > _MAX_DEPTH:
            raise OwnershipError("The release inventory exceeds its review limits.")
        info = _inspect(parent, path.name, mount)
        # Compare with trusted evidence before opening an expected directory.
        add(parent, path, info)
        expected_dir = depth == 1 and (path.name in fixed_dirs or path.name in deps)
        is_dir = stat.S_ISDIR(info.st_mode)
        if is_dir != expected_dir and not dependency:
            raise OwnershipError("The release contains an unexpected artifact type.")
        if is_dir:
            with _directory(parent, path.name, mount) as child:
                if _stamp(os.fstat(child)) != _stamp(info):
                    raise OwnershipError(
                        "An installation directory changed during review."
                    )
                children = _names(child)
                if (
                    depth == 1
                    and path.name in fixed_dirs
                    and children != fixed_dirs[path.name]
                ):
                    raise OwnershipError(
                        "The release contains unexpected manual or completion data."
                    )
                for name in sorted(children):
                    visit(
                        child,
                        path / name,
                        depth + 1,
                        dependency=dependency or (depth == 1 and path.name in deps),
                    )
        elif not stat.S_ISREG(info.st_mode):
            raise OwnershipError("The release contains an unexpected artifact type.")

    for name in sorted(top):
        visit(pinned, release / name, 1)


def capture_staged_inventory(home, release, *, staged):
    """Take a bounded, exact snapshot before publishing any activation output."""
    try:
        home, release = layout._absolute(home), layout._absolute(release)
        if release.parent != home / layout.INSTALL_PATH / "releases":
            return None
        artifacts = []
        with ExitStack() as stack:
            owner = stack.enter_context(_home(home))
            mount = _mount(owner)
            parent = owner
            for name in (*layout.INSTALL_PATH.parts, "releases"):
                parent = stack.enter_context(_directory(parent, name, mount))
            pinned = stack.enter_context(
                _directory(parent, release.name, mount, private=True)
            )
            before = os.fstat(pinned)
            if _identity(before) != _identity(staged):
                return None

            def capture(directory, path, info):
                if len(artifacts) >= _MAX_ENTRIES:
                    raise OwnershipError(
                        "The release inventory exceeds its review limits."
                    )
                artifacts.append(_artifact(directory, path, info))

            _walk_release(release, pinned, mount, capture, has_inventory=False)
            if _stamp(before) != _stamp(os.fstat(pinned)):
                return None
        return _StagedInventory(_stamp(before), tuple(artifacts))
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _snapshot(
    home, release, *, published=None, publish=None, staging=None, activation=False
):
    recording = published is not None
    install = home / layout.INSTALL_PATH
    if (
        release.parent != install / "releases"
        or not release.name.isascii()
        or not (release.name.isprintable())
    ):
        raise OwnershipError("This is not the active, installed oc-box release.")
    aliases, _ = _release_layout()
    artifacts = []
    with ExitStack() as stack:
        owner = stack.enter_context(_home(home))
        mount = _mount(owner)

        def below(parent, parts):
            for name in parts:
                parent = stack.enter_context(_directory(parent, name, mount))
            return parent

        root = below(owner, layout.INSTALL_PATH.parts)
        releases = below(root, ("releases",))
        pinned = stack.enter_context(
            _directory(releases, release.name, mount, private=True)
        )
        lock = _lock_info(root, layout.INSTALL_LOCK, mount)
        anchor_fds = (owner, root, releases, pinned)
        anchors = tuple(map(_anchor, anchor_fds))
        current = f"releases/{release.name}"
        if recording:
            if _stamp(os.fstat(pinned)) != staging.directory or (
                _identity(lock) != published[install / layout.INSTALL_LOCK]
            ):
                raise OwnershipError("The staged release or installation lock changed.")
            staged_artifacts = {item.path: item for item in staging.artifacts}
            # Provision the existing updater protocol's lock only after any
            # fresh-install enrollment, which tests for the prior absence of it.
            try:
                fd = os.open(
                    UPGRADE_LOCK, _READ | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root
                )
            except FileExistsError:
                pass  # Existing locks are admitted only through the checks below.
            else:
                os.close(fd)
        else:
            try:
                receipt = _inspect(pinned, INVENTORY, mount)
            except FileNotFoundError:
                raise OwnershipError(
                    "This release has no verified uninstall inventory. "
                    "Reinstall with an inventory-aware oc-box installer or review it manually."
                ) from None
            raw = _read_file(pinned, INVENTORY, mount, receipt, _LIMIT, mode=0o600)
            ledger = json.loads(raw)
            expected = ledger["artifacts"]
            if not isinstance(expected, dict) or len(expected) > _MAX_ENTRIES:
                raise OwnershipError("The release inventory is invalid.")
            if activation:
                recorded_anchors = ledger.get("anchors")
                if not isinstance(recorded_anchors, list) or len(
                    recorded_anchors
                ) != len(anchors):
                    raise OwnershipError("The activation inventory is invalid.")
        upgrade = _lock_info(root, UPGRADE_LOCK, mount)
        use_lock = _lock_info(pinned, USE_LOCK, mount, private=True)
        wrapper_parent = below(owner, layout.LAUNCHER.parent.parts)
        vfs_identity = None
        device_remap = None
        # A legacy receipt can only be used as recorded; a live probe cannot
        # establish where older st_dev values came from.
        if recording or _VFS_IDENTITY in ledger:
            vfs_identity = _inventory_vfs(
                mount,
                (*zip(anchor_fds, anchors), (wrapper_parent, _anchor(wrapper_parent))),
            )
            if not recording:
                recorded_vfs = ledger[_VFS_IDENTITY]
                if (
                    vfs_identity is None
                    or recorded_vfs != vfs_identity
                    or any(
                        type(handle["type"]) is not int
                        for handle in recorded_vfs["handles"]
                    )
                ):
                    raise OwnershipError(
                        "The filesystem identity no longer matches the installation inventory."
                    )
                if activation:
                    old_home = recorded_anchors[0]
                    if (
                        not isinstance(old_home, list)
                        or not old_home
                        or type(old_home[0]) is not int
                    ):
                        raise OwnershipError("The activation inventory is invalid.")
                    if old_home[0] != anchors[0][0]:
                        # Rebase comparisons to the historical number only. Live
                        # stamps, mount checks and the handoff token stay exact.
                        device_remap = (old_home[0], anchors[0][0])

        def matches(recorded, observed):
            return _matches_inventory_stamp(recorded, observed, device_remap)

        def add(parent, path, info, link=None, *, output=False):
            item = _artifact(parent, path, info, link)
            if len(artifacts) >= _MAX_ENTRIES:
                raise OwnershipError("The release inventory exceeds its review limits.")
            if recording:
                if output and published.get(path) != published_identity(info):
                    raise OwnershipError(
                        "An installer output was replaced during activation."
                    )
                if not output and staged_artifacts.get(path) != item:
                    raise OwnershipError(
                        "The installed release changed after it was staged."
                    )
            else:
                entry = expected.get(str(path.relative_to(home)))
                if (
                    not isinstance(entry, list)
                    or len(entry) != 3
                    or not matches(entry[0], item.parent[:-1])
                    or not matches(entry[1], item.stamp)
                    or entry[2] != item.link
                ):
                    raise OwnershipError(
                        "The installation no longer matches its inventory."
                    )
            artifacts.append(item)

        path = install / "current"
        add(root, path, _link(root, path.name, mount, current), current, output=True)
        if not activation:
            for name in aliases:
                target = f"current/{name}"
                add(
                    root,
                    install / name,
                    _link(root, name, mount, target),
                    target,
                    output=True,
                )
        for relative in (
            (layout.LAUNCHER,) if activation else (layout.LAUNCHER, *layout._AUXILIARY)
        ):
            parent = (
                wrapper_parent
                if relative == layout.LAUNCHER
                else below(owner, relative.parent.parts)
            )
            path = home / relative
            info = _inspect(parent, path.name, mount)
            add(parent, path, info, output=True)
            if relative == layout.LAUNCHER:
                wrapper = layout.launcher_bytes(release)
                if (
                    _read_file(parent, path.name, mount, info, len(wrapper), mode=0o755)
                    != wrapper
                ):
                    raise OwnershipError(
                        "The launcher does not match the pinned release."
                    )
            elif not stat.S_ISREG(info.st_mode):
                raise OwnershipError("An installed manual or completion is unsafe.")

        if activation:
            # The donor may ship a different runtime/manual list. Bind the
            # fixed launch entries, not the executing code's list of extras.
            if (
                ledger.get("version") != 2
                or ledger.get("capture") != "before-activation"
                or ledger.get("release") != str(release)
                or not all(
                    matches(recorded, anchor[:-1])
                    for recorded, anchor in zip(recorded_anchors, anchors)
                )
                or not matches(ledger.get("lock"), _stamp(lock))
                or not matches(ledger.get("upgrade"), _stamp(upgrade))
                or not matches(ledger.get("receipt"), _identity(receipt))
            ):
                raise OwnershipError("The activation inventory no longer matches.")
            for name in ("oc_box.py", "build-info.json", USE_LOCK):
                entry = _inspect(pinned, name, mount)
                mode = 0o600 if name == USE_LOCK else 0o644
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or stat.S_IMODE(entry.st_mode) != mode
                ):
                    raise OwnershipError("The active runtime file is unsafe.")
                add(pinned, release / name, entry)
                if name == "build-info.json":
                    build = json.loads(
                        _read_file(pinned, name, mount, entry, 64 * 1024, mode=mode)
                    )
                    if not isinstance(build, dict) or not isinstance(
                        build.get("version"), str
                    ):
                        raise OwnershipError(
                            "The active runtime build identity is invalid."
                        )
            if _stamp(_inspect(pinned, INVENTORY, mount)) != _stamp(receipt):
                raise OwnershipError("The activation inventory changed.")
            digest = hashlib.sha256(
                raw + repr(_stamp(receipt)).encode("ascii")
            ).hexdigest()
            return ActiveInventory(
                f"{release.name}:{digest}",
                _stamp(lock),
                _stamp(upgrade),
                _stamp(use_lock),
                build,
            )

        outputs = len(artifacts)
        _walk_release(release, pinned, mount, add, has_inventory=not recording)

        def encode(marker):
            data = _inventory_bytes(
                home,
                release,
                anchors,
                lock,
                upgrade,
                marker,
                artifacts,
                vfs_identity=vfs_identity,
            )
            if len(data) > _LIMIT:
                raise OwnershipError("The release inventory exceeds its review limits.")
            return data

        if recording:
            if (
                _stamp(os.fstat(pinned)) != staging.directory
                or tuple(artifacts[outputs:]) != staging.artifacts
            ):
                raise OwnershipError(
                    "The installed release changed after it was staged."
                )
            publish(pinned, encode)
            return None
        if raw != encode(receipt) or _stamp(
            _inspect(pinned, INVENTORY, mount)
        ) != _stamp(receipt):
            raise OwnershipError("The installation no longer matches its inventory.")
        artifacts.append(_artifact(pinned, release / INVENTORY, receipt))
        artifacts.append(_artifact(releases, release, os.fstat(pinned)))
        return FlatInstallPlan(
            release,
            tuple(artifacts),
            (*anchors, _stamp(lock)),
            _stamp(upgrade),
            _stamp(use_lock),
        )


def record_inventory(home, release, outputs, *, lock_fd, staging):
    """Record newly staged installer artifacts; never adopt an existing release."""
    if type(staging) is not _StagedInventory:
        return False
    try:
        home, release = layout._absolute(home), layout._absolute(release)
        published = {
            **outputs,
            home / layout.INSTALL_PATH / layout.INSTALL_LOCK: _identity(
                os.fstat(lock_fd)
            ),
        }

        def publish(parent, encode):
            # The release is freshly staged and private; a marker is never
            # overwritten. A broken or partial write cannot pass review.
            fd = os.open(
                INVENTORY,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encode(os.fstat(stream.fileno())))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)

        _snapshot(home, release, published=published, publish=publish, staging=staging)
        return True
    except (OSError, ValueError, RuntimeError, TypeError):
        # A usable installation need not qualify for automated uninstall.
        return False


def _package_review(home, runtime):
    if __package__:
        from . import package_updates
    else:
        import package_updates

    prefix = layout._absolute(sys.prefix)
    python = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if (
        prefix == layout._absolute(sys.base_prefix)
        or not prefix.is_relative_to(home)
        or prefix == home
        or runtime
        not in {
            prefix / lib / python / "site-packages/oc_box" for lib in ("lib", "lib64")
        }
    ):
        raise OwnershipError("This is not a verified private oc-box tool environment.")

    with ExitStack() as stack:
        owner = stack.enter_context(_home(home))
        mount = _mount(owner)

        def below(parent, parts):
            for part in parts:
                parent = stack.enter_context(_directory(parent, part, mount))
            return parent

        def read(parent, name):
            return _read_file(
                parent, name, mount, _inspect(parent, name, mount), 64 * 1024
            )

        def exists(parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False

        env = below(owner, prefix.relative_to(home).parts)
        cfg = read(env, "pyvenv.cfg")
        if re.findall(
            rb"(?mi)^include-system-site-packages[ \t]*=[ \t]*(\S+)[ \t]*\r?$", cfg
        ) != [b"false"] or exists(env, "EXTERNALLY-MANAGED"):
            raise OwnershipError(
                "System, shared and externally managed Python is not supported."
            )
        pipx = exists(env, "pipx_metadata.json")
        uv = exists(env, "uv-receipt.toml")
        if not (pipx or uv):
            raise OwnershipError(
                "No verified persistent tool owner. Use the original private environment "
                "for a pip installation; editable and transient environments are not supported."
            )
        if pipx and uv:
            raise OwnershipError("Multiple package managers claim this environment.")

        lib = below(env, runtime.parent.relative_to(prefix).parts)
        package = below(lib, (runtime.name,))
        marker = json.loads(read(package, "distribution.json"))
        version = marker.get("version")
        if (
            not isinstance(version, str)
            or not package_updates.channels.PUBLIC_VERSION.fullmatch(version)
            or marker
            != {"installer": "python-package", "name": "oc-box", "version": version}
        ):
            raise OwnershipError(
                "The installed Python package identity is not verified."
            )
        dist = below(lib, (f"oc_box-{version}.dist-info",))
        if exists(dist, "direct_url.json"):
            raise OwnershipError(
                "Editable and direct-source installs are not supported."
            )
        metadata = BytesParser().parsebytes(read(dist, "METADATA"), headersonly=True)
        names = metadata.get_all("Name") or []
        if (
            len(names) != 1
            or names[0].lower().replace("_", "-") != "oc-box"
            or metadata.get_all("Version") != [version]
        ):
            raise OwnershipError(
                "The installed Python package identity is not verified."
            )

        bin_fd = below(env, ("bin",))
        entry = _inspect(bin_fd, "oc-box", mount)
        if not stat.S_ISREG(entry.st_mode) or not entry.st_mode & stat.S_IXUSR:
            raise OwnershipError("The tool environment launcher is unsafe.")
        launcher = home / layout.LAUNCHER
        if pipx:
            receipt = json.loads(read(env, "pipx_metadata.json"))
            package_updates._pipx_receipt(receipt, prefix, version)
            if receipt["main_package"]["apps"] != ["oc-box"]:
                raise OwnershipError("The pipx receipt has additional launchers.")
            manager = "pipx"
        else:
            try:
                import tomllib
            except ImportError:
                raise OwnershipError(
                    "Reading the uv tool receipt requires Python 3.11."
                ) from None
            _, launcher_path = package_updates._uv_receipt(
                tomllib.loads(read(env, "uv-receipt.toml").decode()), prefix
            )
            if launcher_path != str(launcher):
                raise OwnershipError(
                    "A custom uv tool launcher requires its original owner."
                )
            manager = "uv"
        launcher_parent = below(owner, layout.LAUNCHER.parent.parts)
        target = prefix / "bin/oc-box"
        targets = (str(target), os.path.relpath(target, launcher.parent))
        if not any(
            _link_matches(launcher_parent, launcher.name, mount, target)
            for target in targets
        ):
            raise OwnershipError(
                "The package-manager launcher does not select this environment."
            )
        return PackageInstallReview(manager, prefix, version)


def _link_matches(fd, name, mount, target):
    try:
        _link(fd, name, mount, target)
    except OwnershipError:
        return False
    return True


def review_installation(
    runtime: Path, *, home: Path | None = None
) -> FlatInstallPlan | PackageInstallReview:
    """Review the running installation without locks, writes or manager calls.

    Only a FlatInstallPlan names artifacts; a PackageInstallReview provides
    manual guidance without certifying a manager executable or deletion scope.
    """
    try:
        home = layout._absolute(Path.home() if home is None else home)
        runtime = layout._absolute(runtime)
        if runtime.parent == home / layout.INSTALL_PATH / "releases":
            return _snapshot(home, runtime)
        if runtime.name == "oc_box" and runtime.parent.name == "site-packages":
            if __package__:
                from .update_channels import ChannelError
            else:
                from update_channels import ChannelError
            try:
                return _package_review(home, runtime)
            except ChannelError:
                raise OwnershipError(
                    "The package-manager receipt could not be verified."
                ) from None
        raise OwnershipError(
            "This is not the active, installed oc-box release. "
            "Source, editable and unverifiable legacy invocations are not supported."
        )
    except OwnershipError:
        raise
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
        raise OwnershipError(
            "Cannot verify this oc-box installation or its inventory."
        ) from None
