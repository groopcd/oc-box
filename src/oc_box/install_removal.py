"""Host-only executor for an already reviewed flat installation."""

from contextlib import contextmanager, ExitStack
import ctypes
import fcntl
import os
from pathlib import Path
import secrets
import stat
import sys

if __package__:
    from . import install_ownership as ownership
else:
    import install_ownership as ownership


class InstallRemovalError(ValueError):
    def __init__(self, message, *, changed=False, retained=None):
        super().__init__(message)
        self.changed = changed
        self.retained = retained


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _guard_host():
    if (
        sys.platform != "linux"
        or os.geteuid() == 0
        or os.geteuid() != os.getuid()
        or os.environ.get("OC_BOX_CONTAINER") == "1"
        or any(
            _exists(None, path)
            for path in ("/.dockerenv", "/run/.containerenv", "/run/systemd/container")
        )
    ):
        raise InstallRemovalError(
            "Remove oc-box as your regular user on the Linux host."
        )


def _rename_method():
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise InstallRemovalError(
            "Atomic no-replacement moves are unavailable."
        ) from None
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int

    def move(source, name, destination, new_name):
        if rename(source, os.fsencode(name), destination, os.fsencode(new_name), 1):
            raise OSError(ctypes.get_errno(), "atomic installation move failed")

    return move


def _check_anchor(fd, anchor):
    if ownership._anchor(fd) != anchor:
        raise InstallRemovalError(
            "An installation directory no longer matches its review."
        )


def _open_directory(stack, parent, parts, mount):
    for name in parts:
        parent = stack.enter_context(ownership._directory(parent, name, mount))
    return parent


@contextmanager
def _exclusive(parent, name, mount, stamp, *, release=None):
    private = release is not None
    info = ownership._lock_info(parent, name, mount, private=private)
    if ownership._stamp(info) != stamp:
        raise InstallRemovalError("An installation lock no longer matches its review.")
    lease = ownership._runtime_lease if private else None
    with ExitStack() as stack:
        own = (
            lease is not None and lease.pid == os.getpid() and lease.release == release
        )
        if own:
            fd = lease.fd
            if (
                lease.stamp != stamp
                or ownership._stamp(os.fstat(fd)) != stamp
                or ownership._mount(fd) != mount
            ):
                raise InstallRemovalError("The launcher's own release lease changed.")
        else:
            fd = stack.enter_context(ownership._open_lock(parent, name, mount, info))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallRemovalError(
                "The installation is in use. Exit other launchers and wait for any install or upgrade."
            ) from None
        finally:
            if own:
                # flock upgrades can drop the shared lock even when they fail.
                # Other removal attempts cannot enter while we hold install/upgrade.
                stack.callback(fcntl.flock, fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (
            ownership._stamp(ownership._lock_info(parent, name, mount, private=private))
            != stamp
        ):
            raise InstallRemovalError(
                "An installation lock changed during acquisition."
            )
        yield fd


def _match(parent, name, mount, artifact, *, moved=False, stamp=None):
    info = ownership._inspect(parent, name, mount)
    seen = ownership._stamp(info)
    expected = artifact.stamp if stamp is None else stamp
    if moved:
        seen, expected = seen[:-1], expected[:-1]  # rename updates ctime
    if seen != expected or (
        artifact.link is not None and os.readlink(name, dir_fd=parent) != artifact.link
    ):
        raise InstallRemovalError("An installation artifact changed during removal.")
    return info


@contextmanager
def _pin_candidate(parent, mount, artifact, *, stamp=None):
    fd = os.open(artifact.path.name, ownership._ENTRY, dir_fd=parent)
    try:
        if (
            ownership._mount(fd) != mount
            or ownership._stamp(os.fstat(fd))
            != (artifact.stamp if stamp is None else stamp)
            or (
                artifact.link is not None
                and os.readlink("", dir_fd=fd) != artifact.link
            )
        ):
            raise InstallRemovalError(
                "An installation artifact changed during removal."
            )
        yield fd
    finally:
        os.close(fd)


def _walk_release(fd, directory, mount, children, *, discard=None):
    expected = children.get(directory, ())
    if ownership._names(fd) != {item.path.name for item in expected}:
        raise InstallRemovalError("The selected release contains an unexpected entry.")
    # Keep the use-lock file named until all other release contents are gone.
    for item in sorted(expected, key=lambda a: a.path.name == ownership.USE_LOCK):
        _check_anchor(fd, item.parent)
        info = _match(fd, item.path.name, mount, item)
        expected = item.stamp
        if stat.S_ISDIR(info.st_mode):
            with ownership._directory(fd, item.path.name, mount) as child:
                if ownership._stamp(os.fstat(child)) != item.stamp:
                    raise InstallRemovalError(
                        "A release directory changed during removal."
                    )
                _walk_release(child, item.path, mount, children, discard=discard)
                expected = ownership._stamp(os.fstat(child))
        if discard is not None:
            discard(fd, item, expected)


class _LockedRemoval:
    def __init__(self, plan, home, owner, root, release, install_lock, move):
        self.plan = plan
        self.home = home
        self.owner = owner
        self.root = root
        self.release = release
        self.install_lock = install_lock
        self.move = move
        self.pid = os.getpid()
        self.ready = True

    def remove(self):
        """Recheck the original review and remove only its artifacts once."""
        if not self.ready or self.pid != os.getpid():
            raise InstallRemovalError(
                "The installation removal authorization is no longer active."
            )
        self.ready = False
        try:
            if (
                ownership.review_installation(self.plan.release, home=self.home)
                != self.plan
            ):
                raise InstallRemovalError(
                    "The installation changed after review; nothing was removed."
                )
            _check_anchor(self.root, self.plan.anchors[1])
            if ownership._stamp(os.fstat(self.install_lock)) != self.plan.anchors[-1]:
                raise InstallRemovalError("The installation lock changed after review.")
            return self._remove()
        except (OSError, ownership.OwnershipError):
            raise InstallRemovalError(
                "The reviewed installation could not be verified; nothing was removed."
            ) from None

    def _remove(self):
        plan = self.plan
        move = self.move
        mount = ownership._mount(self.root)
        launcher = self.home / ownership.layout.LAUNCHER
        children = {}
        heads = []
        for item in plan.artifacts:
            if item.path != plan.release and item.path.is_relative_to(plan.release):
                children.setdefault(item.path.parent, []).append(item)
            else:
                heads.append(item)
        heads.sort(key=lambda item: (item.path != launcher, item.path == plan.release))
        # The random name is not a new uninstall target on subsequent calls.
        install = self.home / ownership.layout.INSTALL_PATH
        stage_name = ".oc-box-uninstall-" + secrets.token_hex(16)
        attempted = []
        staged = []
        committed = False
        changed = False
        with ExitStack() as stack:
            parents = {}
            for item in heads:
                path = item.path.parent
                if path not in parents:
                    parents[path] = _open_directory(
                        stack, self.owner, path.relative_to(self.home).parts, mount
                    )
                _check_anchor(parents[path], item.parent)
                _match(parents[path], item.path.name, mount, item)

            try:
                os.mkdir(stage_name, 0o700, dir_fd=self.root)
            except KeyboardInterrupt:
                raise InstallRemovalError(
                    "Creation of private installation staging could not be confirmed.",
                    changed=True,
                    retained=install / stage_name,
                ) from None
            try:
                stage = stack.enter_context(
                    ownership._directory(self.root, stage_name, mount, private=True)
                )
                stage_anchor = ownership._anchor(stage)
            except (OSError, ownership.OwnershipError, KeyboardInterrupt):
                raise InstallRemovalError(
                    "The private installation staging directory could not be pinned.",
                    changed=True,
                    retained=install / stage_name,
                ) from None

            try:
                for index, item in enumerate(heads):
                    parent = parents[item.path.parent]
                    _check_anchor(parent, item.parent)
                    slot = f"{index}-{item.path.name}"
                    with _pin_candidate(parent, mount, item) as fd:
                        # Record the attempted destination before the syscall so an
                        # interrupt after a successful rename still permits recovery.
                        attempted.append((item, parent, slot))
                        move(parent, item.path.name, stage, slot)
                        staged.append((item, slot))
                        changed = True
                        actual = _match(stage, slot, mount, item, moved=True)
                        if ownership._stamp(actual) != ownership._stamp(os.fstat(fd)):
                            raise InstallRemovalError(
                                "An atomic installation move changed its target."
                            )

                _walk_release(self.release, plan.release, mount, children)
                self._verify_detached(heads, parents, mount)
                for item, slot in staged:
                    _match(stage, slot, mount, item, moved=True)

                # The historical file-open policy binds its receipt to this
                # inode and ctime. Keep the inode and marker (even if it is
                # foreign); invalidate prior enrollment for old runtimes too.
                before = os.fstat(self.install_lock)
                os.fchmod(self.install_lock, stat.S_IMODE(before.st_mode))
                after = os.fstat(self.install_lock)
                if (
                    ownership._identity(before) != ownership._identity(after)
                    or before.st_ctime_ns == after.st_ctime_ns
                ):
                    raise InstallRemovalError(
                        "Previous automatic file-open enrollment could not be invalidated."
                    )
                os.fsync(self.install_lock)

                committed = True

                def discard(parent, item, stamp):
                    # A program with a pre-existing fd to a release directory
                    # may still replace a name even after its root was detached.
                    # Claim the leaf atomically before unlinking it as well.
                    leaf = ".leaf-" + secrets.token_hex(16)
                    with _pin_candidate(parent, mount, item, stamp=stamp) as candidate:
                        move(parent, item.path.name, stage, leaf)
                        actual = _match(
                            stage, leaf, mount, item, moved=True, stamp=stamp
                        )
                        if ownership._stamp(actual) != ownership._stamp(
                            os.fstat(candidate)
                        ):
                            raise InstallRemovalError(
                                "A detached release artifact changed."
                            )
                        if stat.S_ISDIR(actual.st_mode):
                            os.rmdir(leaf, dir_fd=stage)
                        else:
                            os.unlink(leaf, dir_fd=stage)

                for item, slot in staged:
                    if item.path == plan.release:
                        _match(stage, slot, mount, item, moved=True)
                        _walk_release(
                            self.release, plan.release, mount, children, discard=discard
                        )
                        current = ownership._stamp(os.fstat(self.release))
                        _match(stage, slot, mount, item, stamp=current)
                        os.rmdir(slot, dir_fd=stage)
                    else:
                        _match(stage, slot, mount, item, moved=True)
                        os.unlink(slot, dir_fd=stage)
                self._verify_detached(heads, parents, mount)
                self._drop_stage(stage_name, stage_anchor, mount)
                for parent in {*parents.values(), self.root}:
                    os.fsync(parent)
            except (OSError, ValueError, KeyboardInterrupt):
                retained = install / stage_name
                if not committed:
                    try:
                        for item, parent, slot in reversed(attempted):
                            if _exists(stage, slot):
                                changed = True
                                _check_anchor(parent, item.parent)
                                move(stage, slot, parent, item.path.name)
                        self._drop_stage(stage_name, stage_anchor, mount)
                        retained = None
                    except (OSError, ValueError, KeyboardInterrupt):
                        pass  # preserve any unresolved artifact; never overwrite a replacement
                else:
                    try:
                        if not _exists(self.root, stage_name):
                            retained = None
                    except (OSError, KeyboardInterrupt):
                        pass
                raise InstallRemovalError(
                    "Installation removal was not completed; inspect the retained installation before retrying."
                    if changed or retained
                    else "Installation removal was refused; nothing was removed.",
                    changed=changed or retained is not None,
                    retained=retained,
                ) from None
        return len(plan.artifacts)

    def _drop_stage(self, name, anchor, mount):
        with ownership._directory(self.root, name, mount, private=True) as directory:
            _check_anchor(directory, anchor)
            os.rmdir(name, dir_fd=self.root)

    def _verify_detached(self, heads, parents, mount):
        with ExitStack() as stack:
            owner = stack.enter_context(ownership._home(self.home))
            _check_anchor(owner, self.plan.anchors[0])
            actual = {
                path: _open_directory(
                    stack, owner, path.relative_to(self.home).parts, mount
                )
                for path in parents
            }
            for item in heads:
                directory = actual[item.path.parent]
                _check_anchor(directory, item.parent)
                if _exists(directory, item.path.name):
                    raise InstallRemovalError(
                        "An installation artifact reappeared during removal."
                    )


@contextmanager
def locked_flat_removal(plan, *, runtime: Path, home: Path | None = None):
    """Revalidate and hold the install and release-use fences without deleting.

    The caller owns consent. Use the same context through any other reviewed
    pre-removal work and call its ``remove`` method only after that work passes.
    Package-manager receipts never satisfy this interface.
    """
    try:
        _guard_host()
    except OSError:
        raise InstallRemovalError(
            "Cannot verify that uninstall is running on the host."
        ) from None
    if (
        type(plan) is not ownership.FlatInstallPlan
        or not isinstance(runtime, Path)
        or runtime != plan.release
    ):
        raise InstallRemovalError("No matching verified flat launcher removal plan.")
    layout = ownership.layout
    try:
        home = layout._absolute(Path.home() if home is None else home)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise InstallRemovalError(
            "Cannot verify the installation's home directory."
        ) from None
    if ownership._runtime_home(runtime) != home:
        raise InstallRemovalError("The reviewed release is outside this installation.")
    move = _rename_method()
    with ExitStack() as stack:
        try:
            owner = stack.enter_context(ownership._home(home))
            mount = ownership._mount(owner)
            root = _open_directory(stack, owner, layout.INSTALL_PATH.parts, mount)
            _check_anchor(owner, plan.anchors[0])
            _check_anchor(root, plan.anchors[1])
            # Existing updaters take these in this order; never replace the inodes.
            stack.enter_context(
                _exclusive(root, ownership.UPGRADE_LOCK, mount, plan.upgrade_lock)
            )
            install_lock = stack.enter_context(
                _exclusive(root, layout.INSTALL_LOCK, mount, plan.anchors[-1])
            )
            releases = stack.enter_context(
                ownership._directory(root, "releases", mount)
            )
            pinned = stack.enter_context(
                ownership._directory(releases, runtime.name, mount, private=True)
            )
            _check_anchor(releases, plan.anchors[2])
            _check_anchor(pinned, plan.anchors[3])
            stack.enter_context(
                _exclusive(
                    pinned, ownership.USE_LOCK, mount, plan.use_lock, release=runtime
                )
            )
            if ownership.review_installation(runtime, home=home) != plan:
                raise InstallRemovalError(
                    "The installation changed after review; nothing was removed."
                )
        except (OSError, ownership.OwnershipError, IndexError):
            raise InstallRemovalError(
                "Cannot lock and recheck the reviewed installation; nothing was removed."
            ) from None
        action = _LockedRemoval(plan, home, owner, root, pinned, install_lock, move)
        try:
            yield action
        finally:
            action.ready = False


def remove_flat_installation(plan, *, runtime: Path, home: Path | None = None):
    with locked_flat_removal(plan, runtime=runtime, home=home) as action:
        return action.remove()
