"""Read-only uninstall review for the installation that supplied this runtime.

The inventory is local installer evidence, not a signature or protection from
the owning user's deliberate modification. A review is not an execution permit.
"""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import stat
import sys

if __package__:
    from . import host_file_open_policy as layout
else:
    import host_file_open_policy as layout


INVENTORY = ".install-inventory-v1.json"
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


@dataclass(frozen=True)
class PackageInstallReview:
    """Local receipt evidence for manual guidance, not a deletion plan."""

    manager: str
    prefix: Path
    version: str


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


def _inventory_bytes(home, release, anchors, lock, receipt, artifacts):
    return (
        json.dumps(
            {
                "version": 1,
                "release": str(release),
                # Mount IDs are rechecked at review but are not stable across boots.
                "anchors": [anchor[:-1] for anchor in anchors],
                "lock": _stamp(lock),
                "receipt": _identity(receipt),
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


def _snapshot(home, release, *, published=None, publish=None):
    if __package__:
        from . import installation
    else:
        import installation

    recording = published is not None
    install = home / layout.INSTALL_PATH
    if (
        release.parent != install / "releases"
        or not release.name.isascii()
        or not (release.name.isprintable())
    ):
        raise OwnershipError("This is not the active, installed oc-box release.")
    aliases = (*installation.PROGRAM_FILES, "build-info.json", "docs", "completions")
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
        lock = _inspect(root, layout.INSTALL_LOCK, mount)
        if not stat.S_ISREG(lock.st_mode):
            raise OwnershipError("The installation lock is unsafe.")
        anchors = (_anchor(owner), _anchor(root), _anchor(releases), _anchor(pinned))
        current = f"releases/{release.name}"
        if recording:
            if _identity(os.fstat(pinned)) != published[release] or (
                _identity(lock) != published[install / layout.INSTALL_LOCK]
            ):
                raise OwnershipError("The staged release or installation lock changed.")
        else:
            try:
                receipt = _inspect(pinned, INVENTORY, mount)
            except FileNotFoundError:
                raise OwnershipError(
                    "This release has no verified uninstall inventory. "
                    "Reinstall with an inventory-aware oc-box installer or review it manually."
                ) from None
            raw = _read_file(pinned, INVENTORY, mount, receipt, _LIMIT, mode=0o600)
            expected = json.loads(raw)["artifacts"]
            if not isinstance(expected, dict) or len(expected) > _MAX_ENTRIES:
                raise OwnershipError("The release inventory is invalid.")

        def add(parent, path, info, link=None, *, output=False):
            item = _artifact(parent, path, info, link)
            if len(artifacts) >= _MAX_ENTRIES:
                raise OwnershipError("The release inventory exceeds its review limits.")
            if recording:
                if output and published.get(path) != published_identity(info):
                    raise OwnershipError(
                        "An installer output was replaced during activation."
                    )
            elif expected.get(str(path.relative_to(home))) != [
                list(item.parent[:-1]),
                list(item.stamp),
                item.link,
            ]:
                raise OwnershipError(
                    "The installation no longer matches its inventory."
                )
            artifacts.append(item)

        path = install / "current"
        add(root, path, _link(root, path.name, mount, current), current, output=True)
        for name in aliases:
            target = f"current/{name}"
            add(
                root,
                install / name,
                _link(root, name, mount, target),
                target,
                output=True,
            )
        for relative in (layout.LAUNCHER, *layout._AUXILIARY):
            parent = below(owner, relative.parent.parts)
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

        names = _names(pinned)
        top = set(aliases)
        for feature in _EXTRAS:
            support = f"{feature}-support.json"
            if support in names:
                top.update((support, f"{feature}-deps"))
        if names != top | (set() if recording else {INVENTORY}):
            raise OwnershipError(
                "The release contains missing or unrecognized artifacts."
            )

        fixed_dirs = {
            release / "docs": set(installation.MANUAL_FILES),
            release / "completions": {Path(installation.COMPLETION_FILE).name},
        }
        dep_dirs = {release / f"{feature}-deps" for feature in _EXTRAS}

        def visit(parent, path, depth, *, dependency=False):
            if depth > _MAX_DEPTH:
                raise OwnershipError("The release inventory exceeds its review limits.")
            info = _inspect(parent, path.name, mount)
            add(parent, path, info)
            is_dir = stat.S_ISDIR(info.st_mode)
            if is_dir != (path in fixed_dirs or path in dep_dirs) and not dependency:
                raise OwnershipError(
                    "The release contains an unexpected artifact type."
                )
            if is_dir:
                with _directory(parent, path.name, mount) as child:
                    if _stamp(os.fstat(child)) != _stamp(info):
                        raise OwnershipError(
                            "An installation directory changed during review."
                        )
                    children = _names(child)
                    if path in fixed_dirs and children != fixed_dirs[path]:
                        raise OwnershipError(
                            "The release contains unexpected manual or completion data."
                        )
                    for name in sorted(children):
                        visit(
                            child,
                            path / name,
                            depth + 1,
                            dependency=dependency or path in dep_dirs,
                        )
            elif not stat.S_ISREG(info.st_mode):
                raise OwnershipError(
                    "The release contains an unexpected artifact type."
                )

        for name in sorted(top):
            visit(pinned, release / name, 1)

        def encode(marker):
            data = _inventory_bytes(home, release, anchors, lock, marker, artifacts)
            if len(data) > _LIMIT:
                raise OwnershipError("The release inventory exceeds its review limits.")
            return data

        if recording:
            publish(pinned, encode)
            return None
        if raw != encode(receipt) or _stamp(
            _inspect(pinned, INVENTORY, mount)
        ) != _stamp(receipt):
            raise OwnershipError("The installation no longer matches its inventory.")
        artifacts.append(_artifact(pinned, release / INVENTORY, receipt))
        artifacts.append(_artifact(releases, release, os.fstat(pinned)))
        return FlatInstallPlan(release, tuple(artifacts), (*anchors, _stamp(lock)))


def record_inventory(home, release, outputs, *, lock_fd, staged):
    """Record newly staged installer artifacts; never adopt an existing release."""
    try:
        home, release = layout._absolute(home), layout._absolute(release)
        published = {
            **outputs,
            release: _identity(staged),
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

        _snapshot(home, release, published=published, publish=publish)
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
