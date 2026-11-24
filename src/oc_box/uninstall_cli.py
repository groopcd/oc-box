"""Host-only CLI for the exact verified installation removal scope."""

import json
import sys

if __package__:
    from . import install_removal, uninstall_plan, uninstall_review, uninstall_tty
else:
    import install_removal
    import uninstall_plan
    import uninstall_review
    import uninstall_tty


def _refusal(message):
    print(f"oc-box: {message}", file=sys.stderr)
    return 1


def cli(runtime, argv):
    # Parse before checking ownership, opening a terminal, or taking any lock.
    # The caller dispatches before workspace, updater and journal code.
    request = uninstall_review.parse(argv)
    try:
        install_removal._guard_host()
        # Full cleanup has no authorized engine/state executor yet. Never
        # downgrade --full to launcher removal on an incomplete plan.
        snapshot = uninstall_plan.prepare_review(request, runtime)
        if snapshot.blockers or request.dry_run:
            print(snapshot.render(), end="")
            return int(bool(snapshot.blockers))
        if request.full:
            return _refusal("Full uninstall could not verify the removal scope.")
        if not uninstall_tty.confirm(snapshot):
            return _refusal(
                "Uninstall not confirmed on the controlling terminal; nothing was removed."
            )
        removed = install_removal.remove_flat_installation(
            snapshot.installation, runtime=runtime
        )
    except install_removal.InstallRemovalError as error:
        if error.retained is not None:
            print(
                "oc-box: Retained uninstall staging: "
                + json.dumps(str(error.retained), ensure_ascii=True),
                file=sys.stderr,
            )
        return _refusal(str(error))
    except uninstall_review.UninstallReviewError as error:
        return _refusal(str(error))
    except OSError:
        return _refusal(
            "Uninstall could not be verified. Review the installation before retrying."
        )
    except KeyboardInterrupt:
        return 130
    if type(removed) is not int or removed != len(snapshot.installation.artifacts):
        return _refusal(
            "Removal completion could not be verified. Review the installation before retrying."
        )
    print(
        "Verified oc-box launcher removed. Workspace data, Podman resources, and host OpenCode history were kept."
    )
    return 0
