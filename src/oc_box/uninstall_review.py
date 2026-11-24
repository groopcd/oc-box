"""Pure parsing and bounded review text for the proposed host uninstaller.

The review is not an ownership proof or deletion plan. Trusted installation
and engine planners must pin and revalidate the exact resources before any
executor uses them. No host paths or services are opened by this module.
"""

import argparse
import json
from dataclasses import dataclass, field


_INSTALLATION = frozenset(
    {
        "launcher",
        "release",
        "manual",
        "completion",
        "package",
    }
)
_WORKSPACES = frozenset(
    {
        "container",
        "home-volume",
        "registry",
        "settings",
        "vault",
        "private-logs",
    }
)
_MAX_ITEMS = 512
_MAX_TEXT = 16384


class UninstallReviewError(ValueError):
    def __init__(self):
        super().__init__("The uninstall review is incomplete or invalid.")


@dataclass(frozen=True, slots=True)
class UninstallRequest:
    full: bool
    dry_run: bool

    @property
    def phrase(self):
        if self.full:
            return "UNINSTALL OC-BOX AND WORKSPACES"
        return "UNINSTALL OC-BOX"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    kind: str
    identity: str = field(repr=False)


def parse(argv):
    """Parse only the dedicated first-token mode, without inspecting the host."""
    parser = argparse.ArgumentParser(
        prog="oc-box --uninstall",
        description=(
            "Remove the verified oc-box installation. By default all oc-box data "
            "and Podman workspaces are kept. Host OpenCode history is never removed."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="also review verified oc-box workspace containers, home volumes, and removable oc-box state",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the reviewed scope without deleting anything",
    )
    if not argv or argv[0] != "--uninstall":
        parser.error("--uninstall must be the first argument")
    args = parser.parse_args(argv[1:])
    return UninstallRequest(args.full, args.dry_run)


def _quoted(text):
    # This is explicit operator-visible review, not a journal entry. Quoting
    # also keeps filenames from injecting control characters into a terminal.
    if type(text) is not str or not text or len(text) > 1024:
        raise UninstallReviewError()
    return json.dumps(text, ensure_ascii=True)


def render(request, items, *, blockers=()):
    """Render a trusted review projection; never confer execution authority."""
    if (
        type(request) is not UninstallRequest
        or type(request.full) is not bool
        or type(request.dry_run) is not bool
        or type(items) is not tuple
        or type(blockers) is not tuple
        or not 0 < len(items) + len(blockers) <= _MAX_ITEMS
    ):
        raise UninstallReviewError()
    kinds = _INSTALLATION | _WORKSPACES if request.full else _INSTALLATION
    lines = ["oc-box uninstall review", "", "Proposed removal:"]
    seen = set()
    for item in items:
        if (
            type(item) is not ReviewItem
            or type(item.kind) is not str
            or item.kind not in kinds
        ):
            raise UninstallReviewError()
        identity = _quoted(item.identity)
        key = (item.kind, identity)
        if key in seen:
            raise UninstallReviewError()
        seen.add(key)
        lines.append(f"  {item.kind}: {identity}")
    if not items:
        lines.append("  None; ownership or removal scope was not verified.")
    if blockers:
        lines.extend(("", "Blockers (nothing may be removed):"))
        lines.extend(f"  {_quoted(item)}" for item in blockers)
    if request.full:
        lines.extend(
            (
                "",
                "Only the reviewed oc-box resources are in scope. Workspace volume data",
                "may be lost. Make any desired workspace backups beforehand; the optional",
                "`oc-box backup --host --output FILE` preserves supported host settings",
                "and the encrypted vault, not workspace container storage.",
            )
        )
    else:
        lines.extend(("", "All oc-box data and Podman workspaces are kept."))
    lines.extend(
        (
            "Project files, host OpenCode history, unrelated Podman resources, and",
            "external backups are outside uninstall scope.",
        )
    )
    if request.dry_run:
        lines.extend(("", "Dry run; nothing was removed."))
    elif not blockers:
        lines.extend(("", f"Required confirmation: {request.phrase}"))
    answer = "\n".join(lines) + "\n"
    if len(answer) > _MAX_TEXT:
        raise UninstallReviewError()
    return answer
