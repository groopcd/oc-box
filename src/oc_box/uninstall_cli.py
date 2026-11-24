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


def _incomplete_full(snapshot):
    print(
        "Full uninstall incomplete: Podman cleanup and oc-box state removal were not verified.\n"
        "Nothing was removed. The launcher, any workspace containers and home volumes, and\n"
        "oc-box state were left in place. Project files, .oc-box-id markers, external backups,\n"
        "and host OpenCode history were not touched."
    )
    # This is not a removal review: no proposed targets or confirmation, and
    # the operator guidance must not depend on rendering the launcher inventory.
    if snapshot.blockers:
        print("\nBlockers:")
        for blocker in snapshot.blockers:
            print("  " + json.dumps(blocker, ensure_ascii=True))
    print(
        "\nKeep the launcher installed while you review any workspaces you intend to clean up.\n"
        "Use `oc-box list` for registrations. If you can identify the original rootless Podman\n"
        "store, manually compare the intended containers and home volumes with those\n"
        "registrations. Names and labels alone do not prove ownership. Leave ambiguous,\n"
        "shared, or unrelated resources untouched."
    )
    if snapshot.package_guidance:
        print(f"\nFor package-only removal after review: {snapshot.package_guidance}")
    else:
        if snapshot.installation is None:
            print(
                "\nIdentify your installation first. For a Python-package installation, use\n"
                "its owning package manager instead; do not remove the whole environment."
            )
        print(
            "\nFor launcher-only removal from a verified active GitHub installation, separately run\n"
            "`oc-box --uninstall --dry-run` to preview, then `oc-box --uninstall` without `--full`.\n"
            "The usual ownership checks and typed confirmation still apply."
        )
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
        if request.full:
            return _incomplete_full(snapshot)
        if snapshot.blockers or request.dry_run:
            print(snapshot.render(), end="")
            return int(bool(snapshot.blockers))
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
