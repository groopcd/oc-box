"""Metadata-only storage scopes and single-workspace CLI removal reviews.

No engine, service, credential, native session, or lifecycle calls occur here.
Host OpenCode storage is deliberately limited to individual native session trees
managed by the existing SessionCatalog, never a whole-database operation. The
all-workspaces scope is a list, not a bulk deletion authority.

Confirmation returns arguments for the *current production CLI*, not an executor.
The caller must pin that CLI/Python and state root, preserve its normal locks and
validators, and never journal raw arguments. Process-local review checks cannot
eliminate a filesystem race before the CLI acquires its authoritative lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

if __package__:
    from . import status_report
    from .tui_backend import DashboardBackend
else:
    import status_report
    from tui_backend import DashboardBackend


HOST_SCOPE_SUMMARY = (
    "Host OpenCode: review individual native session trees in Sessions. "
    "No whole-host database wipe or restore, shared image/cache pruning, or "
    "vault deletion is available."
)
WORKSPACE_PURGE_REVIEW = (
    "Delete this one workspace's sandbox resources and oc-box registration only. "
    "Host project files, host OpenCode history, and verified archive recovery "
    "provenance are retained. The matching project marker may be removed. "
    "The owning launcher and attached clients must exit first; no forced stop "
    "or lock bypass is offered. The production purge command may briefly start "
    "the container to synchronize history before removal. This review does not "
    "unlock or refresh credentials. Owner locks and resource ownership are "
    "checked by the production CLI, not proved by this metadata review. "
    "Another process can change state after confirmation; final CLI validation "
    "remains authoritative. Shared base images and unrelated host data remain."
)
MAX_RECORD_BYTES = 1024 * 1024
MAX_PENDING_PLANS = 128
PLAN_LIFETIME_SECONDS = 600
_REASONS = frozenset(
    (
        "read-only",
        "demo",
        "invalid-scope",
        "invalid-workspace",
        "inventory-unavailable",
        "workspace-unavailable",
        "invalid-registration",
        "unsafe-registry",
        "trusted-path-exposure",
        "invalid-plan",
        "confirmation-mismatch",
        "stale-plan",
        "plan-limit",
    )
)


class StorageError(ValueError):
    """Fixed-category refusal, without private exception text or command output."""

    def __init__(self, reason):
        self.reason = reason if reason in _REASONS else "invalid-registration"
        super().__init__(f"Storage operation refused ({self.reason}).")


def _fail(reason):
    raise StorageError(reason) from None


def _workspace_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _text(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and all(char.isprintable() for char in value)
    )


def _absolute_path(value):
    if not _text(value):
        return False
    path = Path(value)
    return path.is_absolute() and str(path) == value and ".." not in path.parts


@dataclass(frozen=True)
class StorageRecord:
    """Allowlisted registration metadata, never a size estimate or owner proof."""

    workspace_id: str
    project: str
    distro: str
    container: str
    home_volume: str
    image: str
    opencode_version: str
    state_root: Path
    record_path: Path
    source: str = "workspace"

    @property
    def store(self):
        """Store identity includes the registry root, not just a copyable ID."""
        return str(self.record_path.parent)


@dataclass(frozen=True)
class StorageInventory:
    scope: str
    rows: tuple[StorageRecord, ...]
    skipped: int
    state_root: Path | None
    synthetic: bool = False


@dataclass(frozen=True)
class RemovalPlan:
    workspace_id: str
    project: str
    state_root: Path
    record_path: Path
    record_fingerprint: str
    record_identity: tuple[int, ...]
    registry_identity: tuple[tuple[int, int], ...]
    project_condition: str
    cli_args: tuple[str, ...]
    phrase: str
    review_text: str
    nonce: str = field(repr=False)


@dataclass(frozen=True)
class _RecordSnapshot:
    fingerprint: str
    identity: tuple[int, ...]
    registry_identity: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class WorkspaceActionRecord:
    """Canonical bounded action input, not a browsing/diagnostic payload.

    Raw registration data is needed by existing lifecycle validators and private
    service attachment. Never render, journal or serialize it into an action
    request; only the allowlisted row and immutable fingerprints leave this call.
    """

    row: StorageRecord
    fingerprint: str
    identity: tuple[int, ...]
    registry_identity: tuple[tuple[int, int], ...]
    project_condition: str
    data: dict = field(repr=False)


@dataclass(frozen=True)
class _IssuedPlan:
    box: object = field(repr=False)
    plan: RemovalPlan
    expected: RemovalPlan
    project_state: tuple[str, str | None]
    expires: float


_pending: dict[str, _IssuedPlan] = {}
_pending_lock = threading.Lock()


def _root(box):
    try:
        root = Path(box.state_root())
        if not _absolute_path(str(root)):
            _fail("unsafe-registry")
        return root
    except StorageError:
        raise
    except Exception:
        _fail("inventory-unavailable")


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _record_id(root, path):
    path = Path(path)
    ident = path.parent.name
    if (
        not _workspace_id(ident)
        or path != root / "workspaces" / ident / "workspace.json"
    ):
        _fail("unsafe-registry")
    return ident


def _read_record(root, ident):
    """Bounded, no-follow registry read with identity from the same open fds.

    Legacy owner-controlled 0755 directories and 0644 records are accepted. Both
    actions and inventories reject foreign/group-writable metadata; no chmod,
    migration, marker repair, or stronger privacy claim is made by browsing.
    """
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open(root, directory_flags)
        descriptors.append(directory)
        for component in ("workspaces", ident):
            directory = os.open(component, directory_flags, dir_fd=directory)
            descriptors.append(directory)
        registry = tuple(os.fstat(fd) for fd in descriptors)
        fd = os.open(
            "workspace.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail("unsafe-registry")
        if any(
            info.st_uid != os.getuid() or info.st_mode & 0o022
            for info in (*registry, before)
        ):
            _fail("unsafe-registry")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES or _file_identity(before) != _file_identity(
            os.fstat(fd)
        ):
            _fail("invalid-registration")
        data = json.loads(raw)
        if not isinstance(data, dict):
            _fail("invalid-registration")
        return data, _RecordSnapshot(
            hashlib.sha256(raw).hexdigest(),
            _file_identity(before),
            tuple((info.st_dev, info.st_ino) for info in registry),
        )
    except StorageError:
        raise
    except FileNotFoundError:
        _fail("workspace-unavailable")
    except Exception:
        _fail("invalid-registration")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _InventorySource:
    """Use the dashboard's metadata schema with safe, bounded registry reads."""

    BoxError = StorageError

    def __init__(self, box, root):
        self.box, self.root, self.skipped = box, root, 0

    def workspace_paths(self):
        for path in self.box.workspace_paths():
            try:
                _record_id(self.root, path)
            except (StorageError, TypeError, ValueError):
                self.skipped += 1
                continue
            yield Path(path)

    def read_json(self, path):
        return _read_record(self.root, _record_id(self.root, path))[0]


def _row(root, path, data):
    ident = _record_id(root, path)
    if (
        data.get("id") != ident
        or not _absolute_path(data.get("project"))
        or not _absolute_path(data.get("home"))
        or not all(
            _text(data.get(key))
            for key in ("distro", "container", "home_volume", "image")
        )
        or ("opencode_version" in data and not _text(data["opencode_version"]))
    ):
        _fail("invalid-registration")
    return StorageRecord(
        ident,
        data["project"],
        data["distro"],
        data["container"],
        data["home_volume"],
        data["image"],
        data.get("opencode_version", "unknown"),
        root,
        Path(path),
    )


def workspace_rows(box, selected=None, all_workspaces=False, *, demo=False):
    """Return selected/all registration metadata and a malformed-record count.

    No project paths, archives, session databases, home volumes, or engines are
    traversed. A missing selection is an empty selected-workspace scope. Demo
    returns an empty synthetic scope without touching box/filesystem/environment;
    the UI can supply its own synthetic rows. All-workspaces never grants bulk
    removal: prepare still accepts exactly one explicit workspace ID.
    """
    if selected is not None and not _workspace_id(selected):
        _fail("invalid-workspace")
    if type(all_workspaces) is not bool or (all_workspaces and selected is not None):
        _fail("invalid-scope")
    scope = "all-workspaces" if all_workspaces else "workspace"
    if demo:
        return StorageInventory(scope, (), 0, None, synthetic=True)
    root = _root(box)
    source = _InventorySource(box, root)
    backend = DashboardBackend(source)
    rows, skipped = [], 0
    try:
        for path, data in backend.inventory():
            try:
                row = _row(root, path, data)
            except StorageError:
                skipped += 1
                continue
            if all_workspaces or row.workspace_id == selected:
                rows.append(row)
    except Exception:
        _fail("inventory-unavailable")
    return StorageInventory(
        scope, tuple(rows), skipped + source.skipped + backend.inventory_skipped, root
    )


def _action_mode(*, read_only, demo):
    if read_only:
        _fail("read-only")
    if demo:
        _fail("demo")


def _action_record(box, ident):
    root = _root(box)
    path = root / "workspaces" / ident / "workspace.json"
    data, snapshot = _read_record(root, ident)
    row = _action_row(root, path, data)
    try:
        project = Path(row.project)
        if project.exists() or project.is_symlink():
            box.validate_project(project)
    except Exception as error:
        # Match only the common validator's fixed exposure prefix; never retain
        # or render its private path/error detail. Do not substitute APP or relax
        # the validator to authorize management of its own editable checkout.
        if str(error).startswith(
            "Project would expose trusted application/state files:"
        ):
            _fail("trusted-path-exposure")
        _fail("invalid-registration")
    try:
        # This shared helper uses the common VFS identity check. Missing/stale
        # markers are review information, not grounds to forbid plain purge.
        project_state = status_report.project_condition(data)
    except Exception:
        _fail("invalid-registration")
    return row, snapshot, project_state, data


def _action_row(root, path, data):
    row = _row(root, path, data)
    if (
        data.get("version") != 2
        or row.container != "oc-box-" + row.workspace_id
        or row.home_volume != "oc-box-home-" + row.workspace_id
    ):
        _fail("invalid-registration")
    return row


def read_workspace_action(box, workspace_id, *, read_only=False, demo=False):
    """Read exactly one canonical, bounded, owner-controlled registration.

    Never scan unrelated registrations or call a native/engine/service API. The
    directory ID must match the JSON ID and generated workspace resource names.
    Fingerprints bind the same open record and root/workspaces/workspace directory
    chain for review, confirmation and child revalidation. Existing projects pass
    the unchanged common exposure validator; missing/stale markers are reported,
    not repaired. Final lifecycle/attachment validators remain authoritative.

    ``data`` is a private in-memory input solely for existing service attachment;
    never render/journal/serialize it. Reviews and action requests may use only
    ``row`` and the immutable fingerprint/identity/condition fields. Read-only or
    demo explicit-action requests refuse before all I/O.
    """
    _action_mode(read_only=read_only, demo=demo)
    if not _workspace_id(workspace_id):
        _fail("invalid-workspace")
    row, snapshot, project_state, data = _action_record(box, workspace_id)
    return WorkspaceActionRecord(
        row,
        snapshot.fingerprint,
        snapshot.identity,
        snapshot.registry_identity,
        project_state[0],
        # Attachment already requires real native config/identity metadata. Do
        # not put it into the allowlisted row or any serialized request instead.
        data,
    )


def prepare_workspace_purge(box, workspace_id, *, read_only=False, demo=False):
    """Issue a bounded, process-local, single-use review; never run purge."""
    _action_mode(read_only=read_only, demo=demo)
    if not _workspace_id(workspace_id):
        _fail("invalid-workspace")
    row, snapshot, project_state, _ = _action_record(box, workspace_id)
    plan = RemovalPlan(
        workspace_id,
        row.project,
        row.state_root,
        row.record_path,
        snapshot.fingerprint,
        snapshot.identity,
        snapshot.registry_identity,
        project_state[0],
        ("purge", "--workspace", workspace_id, "--yes"),
        "DELETE WORKSPACE " + workspace_id,
        WORKSPACE_PURGE_REVIEW,
        secrets.token_urlsafe(32),
    )
    now = time.monotonic()
    with _pending_lock:
        for nonce, issued in tuple(_pending.items()):
            if issued.expires <= now:
                del _pending[nonce]
        if len(_pending) >= MAX_PENDING_PLANS:
            _fail("plan-limit")
        _pending[plan.nonce] = _IssuedPlan(
            box, plan, replace(plan), project_state, now + PLAN_LIFETIME_SECONDS
        )
    return plan


def cancel_workspace_purge(plan):
    """Discard an abandoned review without accessing state or invoking a CLI."""
    if type(plan) is RemovalPlan and isinstance(plan.nonce, str):
        with _pending_lock:
            _pending.pop(plan.nonce, None)


def confirm_workspace_purge(box, plan, phrase, *, read_only=False, demo=False):
    """Consume on *every attempt*, then verify exact review identity and state.

    Wrong phrases, read-only/demo mode, stale inputs, cross-root/controller use,
    copied/forged dataclasses, and expired tokens all require a fresh review.
    Returned arguments never contain --all or --delete-sessions. This function
    does not execute them or establish that a workspace owner is idle.
    """
    issued = None
    if type(plan) is RemovalPlan and isinstance(plan.nonce, str):
        with _pending_lock:
            issued = _pending.pop(plan.nonce, None)
    _action_mode(read_only=read_only, demo=demo)
    if (
        issued is None
        or issued.plan is not plan
        or issued.box is not box
        or plan != issued.expected
        or issued.expires <= time.monotonic()
    ):
        _fail("invalid-plan")
    expected = issued.expected
    if not isinstance(phrase, str) or phrase != expected.phrase:
        _fail("confirmation-mismatch")
    try:
        row, snapshot, project_state, _ = _action_record(box, expected.workspace_id)
    except StorageError as error:
        if error.reason == "trusted-path-exposure":
            raise
        _fail("stale-plan")
    if (
        row.state_root != expected.state_root
        or row.record_path != expected.record_path
        or snapshot.fingerprint != expected.record_fingerprint
        or snapshot.identity != expected.record_identity
        or snapshot.registry_identity != expected.registry_identity
        or project_state != issued.project_state
    ):
        _fail("stale-plan")
    return expected.cli_args
