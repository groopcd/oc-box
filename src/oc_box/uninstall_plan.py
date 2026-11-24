"""Compose the reviewed installation and optional workspace scope.

This is a read-only review layer. The executor must verify the original
snapshots again under its installation, lifecycle and admission locks. This
module never releases a lock, invokes a package manager, opens host OpenCode
state, or deletes anything.
"""

from dataclasses import dataclass

if __package__:
    from . import install_ownership, uninstall_review, workspace_uninstall
else:
    import install_ownership
    import uninstall_review
    import workspace_uninstall


@dataclass(frozen=True, slots=True, repr=False)
class UninstallSnapshot:
    request: uninstall_review.UninstallRequest
    installation: install_ownership.FlatInstallPlan | None
    workspace: workspace_uninstall.WorkspacePlan | None
    items: tuple[uninstall_review.ReviewItem, ...]
    blockers: tuple[str, ...]

    def render(self):
        return uninstall_review.render(self.request, self.items, blockers=self.blockers)


def _installation_items(installation):
    # Show the exact owned release as one group. The original per-artifact
    # identity inventory remains in the snapshot; review text is not authority.
    items = [uninstall_review.ReviewItem("release", str(installation.release))]
    for artifact in installation.artifacts:
        path = artifact.path
        if path == installation.release or path.is_relative_to(installation.release):
            continue
        if path.name in ("oc-box.1", "oc-box.info"):
            kind = "manual"
        elif path.parent.name == "completions":
            kind = "completion"
        else:
            kind = "launcher"
        items.append(uninstall_review.ReviewItem(kind, str(path)))
    return items


def _workspace_items(workspace):
    result = []
    for targets in workspace.workspaces:
        if targets.container is not None:
            result.append(
                uninstall_review.ReviewItem(
                    "container",
                    f"{targets.container.name} ({targets.container.ident})",
                )
            )
        if targets.home_volume is not None:
            result.append(
                uninstall_review.ReviewItem("home-volume", targets.home_volume.name)
            )
    return result


def prepare_review(request, runtime, *, home=None, workspace_review=None):
    """Prepare a bounded display of an installation and optional engine review.

    Default and dry-run modes never call the workspace inspector. A full
    interactive review borrows a trusted, already scoped engine review
    callable; it must not start/stop services or mutate the registry. Removable
    state is a separate plan and is not inferred from a workspace snapshot.
    """
    if (
        type(request) is not uninstall_review.UninstallRequest
        or type(request.full) is not bool
        or type(request.dry_run) is not bool
    ):
        raise uninstall_review.UninstallReviewError()
    items = []
    blockers = []
    installation = workspace = None
    try:
        captured = install_ownership.review_installation(runtime, home=home)
    except install_ownership.OwnershipError:
        blockers.append("Automatic removal could not verify the running installation.")
    else:
        if type(captured) is install_ownership.FlatInstallPlan:
            installation = captured
            items.extend(_installation_items(captured))
        else:
            # A package receipt can identify a manager for guidance, but
            # does not authorize removal of its whole environment.
            blockers.append(
                "No verified automatic removal for this package installation."
            )
    if request.full:
        if request.dry_run:
            blockers.append(
                "Workspace engine ownership was not queried in this dry run."
            )
        elif not blockers and callable(workspace_review):
            try:
                work = workspace_review()
            except workspace_uninstall.WorkspacePlanError:
                blockers.append(
                    "The full workspace removal scope could not be verified."
                )
            else:
                if type(work) is workspace_uninstall.WorkspacePlan:
                    workspace = work
                    items.extend(_workspace_items(work))
                else:
                    blockers.append("No verified full workspace removal scope.")
        elif not blockers:
            blockers.append(
                "Full workspace review requires a verified local engine scope."
            )
    return UninstallSnapshot(
        request, installation, workspace, tuple(items), tuple(blockers)
    )
