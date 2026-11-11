"""Fixed, privacy-safe dashboard action notices, with no UI/domain imports.

Callers supply an allowlisted ``reason`` (or an error's ``reason`` / OS errno),
never its text. ``dispatched`` means a mutation may have reached its target;
``completed`` means it returned successfully but the subsequent refresh failed.
Neither flag asks this module to inspect state, retry, or perform recovery.

Operations: delete, archive, unarchive, storage, workspace-purge,
backup-file-delete, or mutation (the fallback for unknown operation names).
"""

from dataclasses import dataclass
import errno


@dataclass(frozen=True)
class FailureNotice:
    title: str
    message: str
    severity: str = "warning"
    timeout: float = 8.0


_OPERATIONS = {
    "delete": ("Deletion", "selected session store"),
    "archive": ("Archive", "selected session store"),
    "unarchive": ("Restore", "selected session store"),
    "storage": ("Storage action", "selected storage"),
    "workspace-purge": ("Workspace purge", "selected workspace"),
    "backup-file-delete": ("Backup file deletion", "selected backup file"),
    "backup": ("Backup", "selected backup output"),
    "restore": ("Restore", "selected restore target"),
    "inspection": ("Backup inspection", "selected archive"),
    "mutation": ("Action", "selected source"),
}

_DETAILS = {
    "host-only": "Run this operation as the regular user in a host terminal, not inside a container or with sudo.",
    "invalid-options": "The internal operation handoff received invalid options. Open a new review through the dashboard instead of invoking it as a standalone command.",
    "invalid-request-descriptor": "The operation handoff is not using its private in-memory channel. Return to the dashboard and open a fresh review.",
    "host-terminal-required": "This action needs a foreground host terminal for native masked prompts. Run the dashboard in a host terminal; no password belongs in a selectable input field.",
    "invalid-scope": "The storage scope is invalid. Choose host, selected workspace or all registered workspaces, then select one explicit target.",
    "invalid-workspace": "Select a valid registered workspace ID before reviewing this action.",
    "inventory-unavailable": "Registered storage metadata could not be read safely. Refresh the list and inspect read-only diagnostics; no replacement workspace will be created.",
    "workspace-unavailable": "The reviewed workspace registration is no longer available. Refresh and select the intended registered target again.",
    "invalid-registration": "The canonical workspace registration or its resource identities are invalid. Inspect read-only diagnostics; do not repair or replace it implicitly.",
    "unsafe-registry": "Workspace metadata has an unsafe file type, link, ownership or write permission. Inspect it outside the dashboard; no permissions are changed automatically.",
    "invalid-plan": "The review is missing, expired or already consumed. Refresh and open a new confirmation for the current target.",
    "stale-plan": "Workspace identity or metadata changed after review. Refresh and open a new workspace-specific confirmation.",
    "plan-limit": "Too many reviews are pending. Cancel old reviews or wait, then open a fresh confirmation.",
    "invalid-backup-file": "This file does not have a supported encrypted backup envelope. Select a supported workspace or session-history archive; arbitrary files are not deletion targets.",
    "unsupported-backup-file": "The selected archive is not a supported workspace/session encrypted backup. Choose the correct file; the dashboard does not delete arbitrary host storage.",
    "protected-output-path": "Backup output must be outside the selected project and oc-box state. Choose a new file in a separate existing directory.",
    "invalid-destination": "Choose an explicit valid restore directory. Workspace restore requires a new host directory; session restore uses the reviewed native source directory.",
    "directory-unavailable": "The selected directory is unavailable. Check its presence and access before opening a new review.",
    "directory-required": "Choose an existing real directory for the requested source or output parent.",
    "invalid-parent": "A parent path is not a real directory. Choose a supported direct path and review again.",
    "invalid-backup-kind": "Choose a supported full/portable workspace archive or native session-history backup kind.",
    "invalid-recipients": "Use supported native age X25519 public recipient keys, or leave recipients empty for a masked passphrase prompt. Do not enter a password in the recipient field.",
    "too-many-keys": "At most 16 age recipients or identity files can be selected. Review the intended keys and try again.",
    "request-too-large": "The operation review exceeds its metadata size limit. Choose a smaller supported target; no oversized request is submitted.",
    "state-root-changed": "The configured oc-box registry changed after review. Refresh the intended registry and open a new confirmation.",
    "invalid-request": "The operation request could not be validated. Refresh and open a new supported action; no arbitrary command is accepted.",
    "unsupported-action": "This action is not supported for the chosen scope. Choose a reviewed session, workspace or encrypted-backup operation.",
    "native-service-unverified": "The native service identity or version could not be verified. Explicitly reconnect to the intended source using a compatible native client.",
    "native-version-mismatch": "The native CLI version differs from the connected host service. Use a matching client; the dashboard will not replace that service to force an import.",
    "native-version-unverified": "The native CLI did not report a supported version. Check the selected executable before reconnecting; no service replacement is attempted.",
    "invalid-import-body": "The native history import body could not be validated. Inspect the encrypted archive with the supported session tools; do not force a malformed import.",
    "invalid-import-directory": "The reviewed native import directory is invalid. Choose the intended absolute destination and review again.",
    "import-directory-mismatch": "The imported history and reviewed destination disagree. Open a new restore review instead of redirecting an existing one.",
    "import-acknowledgement-unverified": "Native import did not produce the expected acknowledgement. Inspect the destination history before any retry; success exit status alone is not proof of import.",
    "session-changed": "The selected native history changed after review. Refresh the intended source and prepare a new backup.",
    "read-only": "This dashboard is read-only. Use an explicitly writable dashboard to review and confirm changes.",
    "demo": "Synthetic demo mode cannot change native sessions, workspaces or backup files. Open a non-demo dashboard to manage real state.",
    "selection-required": "Select a session from an explicitly connected source before opening this action.",
    "busy": "Another action is still finishing. Wait for it to finish, then review the current state before trying again.",
    "session-active": "This store has active session or job activity. Wait for it to finish, then refresh and review again. Activity checks cover this service process only.",
    "session-terminal-running": "A selected session or descendant owns a running persistent shell. Close that shell deliberately in its native client, then refresh. Dashboard Disconnect does not stop it.",
    "managed-archive-has-children": "A managed host archive has native child sessions. Recursive deletion is refused to protect those children. Inspect the host tree in its native client and review a safe target separately.",
    "ownership-unknown": "Ownership could not be verified. Reconnect explicitly to the intended source and review again; do not bypass the ownership check.",
    "invalid-activity": "Native session activity could not be validated. Inspect the source in its native client, then refresh; a missing activity result is not proof of idleness.",
    "invalid-jobs": "Native background-job activity could not be validated. Inspect jobs in the native client, then refresh before reviewing this action again.",
    "invalid-terminals": "Native persistent-shell status could not be validated. Inspect the selected sessions' shells in the native client, then refresh before reviewing deletion again.",
    "origin-changed": "The selected source changed or was disconnected. Connect explicitly to the intended source, refresh and open a new confirmation.",
    "identity-changed": "The selected session's native identity changed. Refresh the intended source and select the target again; the old confirmation cannot be reused.",
    "snapshot-changed": "The session tree, ownership or metadata changed after review. Refresh and open a new confirmation; the old review is no longer valid.",
    "snapshot-required": "A current session snapshot is required. Refresh the selected source and review the action again.",
    "invalid-preview": "This deletion preview is missing, expired or no longer matches the reviewed tree. Refresh and open a new deletion confirmation.",
    "review-limit": "This cascade exceeds the dashboard's 50-session review limit. Review the complete tree in a native client; the dashboard will not enlarge that limit.",
    "confirmation-required": "Type DELETE exactly in the confirmation field, then confirm again. The confirmation remains open; no deletion was submitted by this click.",
    "invalid-confirmation": "The typed confirmation does not match the reviewed action. Read the required phrase in the open review, then confirm again. This click did not submit the action.",
    "invalid-archive-state": "The requested archive state is invalid. Refresh and choose Archive or Restore again.",
    "session-missing": "The selected session is no longer in the verified inventory. Refresh before choosing a target; do not reuse the old confirmation.",
    "invalid-identity": "The selected source or session identity could not be validated. Reconnect explicitly to the intended source and select the target again.",
    "invalid-directory": "The native session directory could not be validated. Select the intended source and refresh before reviewing the action again.",
    "invalid-session": "Native session metadata could not be validated. Refresh the selected source; if this persists, inspect that store with its native client.",
    "invalid-session-tree": "The native parent/child tree could not be verified. Inspect the complete tree in its native client before selecting a deletion target again.",
    "invalid-inventory": "The native inventory was incomplete or malformed. Refresh and review a complete list; a partial inventory cannot establish a safe deletion cascade.",
    "duplicate-session": "The native inventory contains duplicate session identities. Refresh the complete list; inspect the native store if duplicates persist.",
    "inconsistent-directory": "The native inventory does not match the selected directory. Reconnect to the intended source and refresh before reviewing changes.",
    "invalid-cursor": "Native pagination was incomplete or inconsistent. Refresh the complete inventory before reviewing changes; partial pages are not a safe deletion preview.",
    "inventory-limit": "The native inventory exceeded dashboard safety bounds. Inspect the complete store in its native client; the dashboard cannot safely review this inventory.",
    "invalid-provenance": "Managed archive provenance could not be verified. Refresh the intended source; do not infer ownership from a title or session ID.",
    "cancelled": "The action was cancelled. Wait for any bounded request to finish, then inspect the current state before starting another action.",
    "request-failed": "Native verification could not finish. Refresh the selected source and review again. No automatic retry or recovery was attempted.",
    "permission-denied": "Access was denied. Check the intended target's ownership and permissions outside this dashboard, then review again; do not weaken safety checks.",
    "file-missing": "The selected file is no longer available. Refresh the file list and review the current selection before trying again.",
    "state-changed": "The selected state changed after review. Refresh and open a new confirmation; do not reuse an old selection.",
    "storage-full": "Storage space or quota is exhausted. Inspect available storage outside this dashboard, then refresh before reviewing another action.",
    "read-only-storage": "The selected storage is mounted read-only. Inspect that storage outside this dashboard; no automatic remount or repair was attempted.",
    "unsafe-state": "The selected filesystem state could not be safely validated. Inspect ownership, file type and links outside this dashboard, then refresh.",
    "directory-not-empty": "The selected directory is not empty. Refresh and review its contents; this dashboard will not expand the deletion recursively.",
    "workspace-running": "The workspace is still running or has an active owner. Stop it explicitly through its normal lifecycle, then refresh and review the purge again.",
    "workspace-attached": "Workspace clients are still attached. Detach them explicitly, allow the owner to finish, then refresh before reviewing the purge again.",
    "workspace-unknown": "The workspace registration is unavailable. Refresh registered workspaces and verify the intended target before opening a new confirmation.",
    "unsafe-backup-file": "The selected backup file could not be safely validated. Refresh the backup list and inspect its file type, ownership and links outside this dashboard.",
    "outside-backup-root": "The selected file is outside the configured backup location. Select a file from the verified backup list; do not bypass the location check.",
    "invalid-backup": "The selected backup could not be validated. Inspect it using the supported backup tools before reviewing any deletion.",
    "connect-target-in-sessions-first": "Open Sessions, choose the intended source and Connect explicitly before using this action. Returning here does not connect or start a workspace automatically.",
    "session-workspace-mismatch": "The connected Sessions source belongs to a different workspace. In Sessions, explicitly connect to the intended workspace, then review this action again.",
    "host-session-backups-only": "Host scope supports individual native session history backups, not a whole host database or workspace archive. Choose Session tree/history or select a workspace.",
    "workspace-selection-required": "Select a registered workspace in this scope before opening the action. Refresh the workspace list if the intended target is missing.",
    "selection-changed": "The selected target changed after review. Refresh the list and open a new confirmation; do not reuse the previous review.",
    "output-exists": "The backup output already exists. Choose a new output file; this action will not overwrite an existing backup.",
    "destination-exists": "The restore destination already exists. Choose a new destination and review again; do not delete or overwrite existing data to force this operation.",
    "path-required": "A required file or destination path is missing. Complete the corresponding field and open a new review.",
    "private-owned-file-required": "The selected sensitive file must be private and owned by the current user. Inspect its ownership, access permissions and links outside this dashboard, then select it again.",
    "regular-file-required": "The selected input is not a safely verified regular, singly linked file. Choose an appropriate file; links and special files cannot be used for this action.",
    "file-unavailable": "The selected file could not be opened safely. Refresh the list and inspect its presence, ownership, permissions and links outside this dashboard.",
    "symlink-path": "The selected path or one of its parents is a symbolic link. Choose a supported direct path; do not bypass the link check.",
    "workspace-validation": "Workspace registration or physical identity could not be validated. Inspect it with read-only diagnostics and use explicit repair only if appropriate, then review again.",
    "trusted-path-exposure": "The workspace would expose oc-box's trusted application or state files. Choose a separate supported project location; do not bypass the exposure validation.",
    "unsafe-favorites-state": "Private favorite metadata could not be safely validated. Inspect its ownership and permissions outside this dashboard; native session history was not used as a fallback.",
    "favorites-busy": "Private favorite metadata is busy. Wait for the other UI operation to finish, then review again.",
    "favorites-state-failed": "Private favorite metadata could not be read or saved. Inspect its state outside this dashboard before trying again.",
    "invalid-favorites-state": "Private favorite metadata is invalid. Inspect that state outside this dashboard; no automatic replacement or repair was attempted.",
    "favorites-limit": "The private favorite metadata limit was reached. Review existing favorites before adding another entry.",
}

# These are definite domain/UI preflight refusals, even when the caller entered
# a mutation method that performs its own validation before dispatching.
_PREFLIGHT_REASONS = frozenset(
    {
        "read-only",
        "demo",
        "selection-required",
        "busy",
        "session-active",
        "session-terminal-running",
        "managed-archive-has-children",
        "ownership-unknown",
        "invalid-activity",
        "invalid-jobs",
        "invalid-terminals",
        "origin-changed",
        "identity-changed",
        "snapshot-changed",
        "snapshot-required",
        "invalid-preview",
        "review-limit",
        "confirmation-required",
        "invalid-confirmation",
        "invalid-archive-state",
        "session-missing",
        "invalid-identity",
        "invalid-directory",
        "invalid-session",
        "invalid-session-tree",
        "invalid-inventory",
        "duplicate-session",
        "inconsistent-directory",
        "invalid-cursor",
        "inventory-limit",
        "invalid-provenance",
        "workspace-running",
        "workspace-attached",
        "workspace-unknown",
        "unsafe-backup-file",
        "outside-backup-root",
        "invalid-backup",
        "connect-target-in-sessions-first",
        "session-workspace-mismatch",
        "host-session-backups-only",
        "workspace-selection-required",
        "selection-changed",
        "output-exists",
        "destination-exists",
        "path-required",
        "private-owned-file-required",
        "regular-file-required",
        "file-unavailable",
        "symlink-path",
        "workspace-validation",
        "trusted-path-exposure",
    }
)

_ALIASES = {
    "readonly": "read-only",
    "read-only-mode": "read-only",
    "no-selection": "selection-required",
    "operation-busy": "busy",
    "source-changed": "origin-changed",
    "source-disconnected": "origin-changed",
    "confirmation-mismatch": "invalid-confirmation",
    "wrong-confirmation": "invalid-confirmation",
    "review-limit-50": "review-limit",
    "permission": "permission-denied",
    "not-found": "file-missing",
    "backup-not-found": "file-missing",
    "backup-changed": "state-changed",
    "identity-mismatch": "state-changed",
    "stale-workspace": "state-changed",
    "unsafe-path": "unsafe-state",
    "owner-active": "workspace-running",
}

_ERRNOS = {
    errno.EACCES: "permission-denied",
    errno.EPERM: "permission-denied",
    errno.ENOENT: "file-missing",
    errno.ESTALE: "state-changed",
    errno.EBUSY: "busy",
    errno.EAGAIN: "busy",
    errno.EEXIST: "state-changed",
    errno.ENOSPC: "storage-full",
    errno.EDQUOT: "storage-full",
    errno.EROFS: "read-only-storage",
    errno.ENOTEMPTY: "directory-not-empty",
    errno.ENOTDIR: "unsafe-state",
    errno.EISDIR: "unsafe-state",
    errno.ELOOP: "unsafe-state",
}


def _fixed_reason(error, reason):
    if reason is None:
        try:
            reason = getattr(error, "reason", None)
        except Exception:
            reason = None
    if type(reason) is str:
        reason = _ALIASES.get(reason, reason)
        if reason in _DETAILS or reason == "invalid-mutation-response":
            return reason
    if isinstance(error, OSError):
        try:
            number = error.errno
        except Exception:
            number = None
        if type(number) is int and number in _ERRNOS:
            return _ERRNOS[number]
    return "unknown"


def failure_notice(
    operation,
    error=None,
    *,
    reason=None,
    dispatched=False,
    completed=False,
    count=None,
    uncertain=False,
):
    """Return fixed copy only; never format an exception, target, or raw reason.

    Known catalog preflight reasons remain refusals. An unknown/transport error
    after entering a mutation is conservative: the outcome is unverified.
    A malformed mutation acknowledgement is always unverified. A successful
    mutation followed by a failed refresh must pass ``completed=True`` instead.
    ``count`` is used only for the fixed 50-session review-limit explanation.
    """
    operation = operation if type(operation) is str else "mutation"
    title, target = _OPERATIONS.get(operation, _OPERATIONS["mutation"])
    if completed is True:
        return FailureNotice(
            f"{title} completed; refresh unavailable",
            f"The action completed, but refreshing the {target} failed. Displayed rows may be stale. "
            "Refresh and inspect the actual state before further actions; do not repeat the completed action.",
        )
    key = _fixed_reason(error, reason)
    # An OS error from a dispatched, possibly multi-step operation cannot prove
    # that earlier steps left the target unchanged, even for an EBUSY refusal.
    preflight = (
        key in _PREFLIGHT_REASONS
        and not isinstance(error, OSError)
        and uncertain is not True
    )
    if (
        uncertain is True
        or key in {"invalid-mutation-response", "import-acknowledgement-unverified"}
        or (dispatched is True and not preflight)
    ):
        detail = _DETAILS.get(key)
        return FailureNotice(
            f"{title} outcome unverified",
            f"The action may already have changed the {target}, but its outcome could not be verified. "
            "Inspect and refresh the actual state before any retry. No automatic retry was made."
            + (" " + detail if detail else ""),
            severity="error",
        )
    message = _DETAILS.get(
        key,
        "The action could not be safely verified. Refresh the intended source and review it again. "
        "No automatic retry or recovery was attempted.",
    )
    if key == "selection-required" and operation not in (
        "delete",
        "archive",
        "unarchive",
    ):
        message = (
            "Choose the target from its verified list before opening this action. "
            "Refresh the list if the intended target is missing."
        )
    if key == "review-limit" and type(count) is int and 0 <= count <= 1000000000:
        message = (
            f"The selected cascade contains {count} sessions; the dashboard review limit is 50. "
            "Review the complete tree in a native client; the dashboard will not enlarge that limit."
        )
    return FailureNotice(
        f"{title} cancelled" if key == "cancelled" else f"{title} refused",
        message,
        severity="information" if key == "cancelled" else "warning",
    )
