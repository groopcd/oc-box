"""Own one private host archive scratch tree in a pinned directory."""

from contextlib import ExitStack, contextmanager
import os
import secrets
import stat

if __package__:
    from . import history_overlap_domains, history_volume_scratch
else:
    import history_overlap_domains
    import history_volume_scratch


class _HostAnchor:
    """Adapt an independently pinned local directory to owned-tree cleanup."""

    def __init__(self, path):
        self.path = path
        self.fd = path.fd
        self.home = path.path
        self.namespace = path.space

    def recheck(self):
        self.namespace.recheck()
        self.path.recheck()

    def below(self, fd, *, directory, private=False):
        obj = self.namespace.object(fd)
        if (obj.device, obj.mount) != (self.path.obj.device, self.path.obj.mount):
            raise history_volume_scratch.ScratchCleanupUncertain()
        return history_volume_scratch._stamp(
            self.namespace.observer, fd, directory=directory, private=private
        )


@contextmanager
def host_archive_scratch(parent, *, verify_retired):
    """Create one bound owner-only subtree, not a general temporary-dir sweep.

    The caller before native execution supplies a validated workspace-local
    parent with the required backing. It must keep that authority and return
    true from ``verify_retired`` only when all exact host children using this
    tree retired. An unexplained mount, link or replacement is retained.
    """
    if not callable(verify_retired):
        raise history_volume_scratch.ScratchCleanupUncertain()
    scratch = None
    name = b".oc-box-guarded-host-" + secrets.token_hex(16).encode()
    with ExitStack() as stack:
        host = stack.enter_context(history_overlap_domains.pin_current_namespace())
        pinned = history_overlap_domains._pin_path(
            stack, host, history_overlap_domains._path(os.fspath(parent))
        )
        if stat.S_IMODE(os.fstat(pinned.fd).st_mode) != 0o700:
            raise history_volume_scratch.ScratchRefusal("unsafe-scratch-parent")
        anchor = _HostAnchor(pinned)
        anchor.recheck()
        try:
            os.mkdir(name, 0o700, dir_fd=anchor.fd)
        except FileExistsError:
            raise history_volume_scratch.ScratchRefusal(
                "scratch-name-occupied"
            ) from None
        except (KeyboardInterrupt, SystemExit) as interruption:
            interruption.history_destination_cleanup_failed = True
            raise
        except OSError:
            raise history_volume_scratch.ScratchCleanupUncertain() from None
        try:
            fd = history_volume_scratch._keep(
                stack,
                history_volume_scratch._open(
                    anchor.fd, name, history_volume_scratch._DIR
                ),
            )
            scratch = history_volume_scratch._Scratch(anchor, name, fd)
            scratch.recheck()
        except (KeyboardInterrupt, SystemExit) as interruption:
            interruption.history_destination_cleanup_failed = True
            raise
        except Exception:
            # Creation happened, but exact ownership was not established.
            raise history_volume_scratch.ScratchCleanupUncertain() from None
        try:
            yield scratch._path
        except BaseException as original:
            try:
                history_volume_scratch._cleanup(scratch, verify_retired)
            except BaseException:
                original.history_destination_cleanup_failed = True
            raise
        else:
            try:
                history_volume_scratch._cleanup(scratch, verify_retired)
            except BaseException as failure:
                failure.history_destination_cleanup_failed = True
                raise
