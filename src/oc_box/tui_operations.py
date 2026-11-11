"""Explicit dashboard operations with reviewed targets and native TTY handoff.

No passwords, transcripts or command output are retained by this module. The
foreground child uses the same pinned runtime and existing lifecycle CLI; it is
not a container shell or an alternate workspace controller.
"""

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

if __package__:
    from . import oc_box as box, tui_storage
    from .tui_backend import DashboardBackend
else:
    # -I deliberately removes the script directory; pin imports to this exact
    # runtime, not another installed oc-box or the current project directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import oc_box as box
    import tui_storage
    from tui_backend import DashboardBackend


MAX_REQUEST = 128 * 1024
ACTIONS = frozenset(
    {
        "workspace-backup",
        "workspace-inspect",
        "workspace-restore",
        "session-backup",
        "session-inspect",
        "session-restore",
        "backup-delete",
    }
)
READ_ACTIONS = frozenset({"workspace-inspect", "session-inspect"})


class OperationError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Dashboard operation refused ({reason}).")


def fail(reason):
    raise OperationError(reason) from None


def real_path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        fail("path-required")
    path = Path(value).expanduser().absolute()
    if ".." in path.parts or any(ord(char) < 32 for char in str(path)):
        fail("invalid-path")
    for ancestor in (*reversed(path.parents), path):
        try:
            mode = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode.st_mode):
            fail("symlink-path")
        if ancestor != path and not stat.S_ISDIR(mode.st_mode):
            fail("invalid-parent")
    return path


def file_identity(path, *, private=False):
    path = real_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        fail("file-unavailable")
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            fail("regular-file-required")
        if private and (mode.st_uid != os.getuid() or mode.st_mode & 0o077):
            fail("private-owned-file-required")
        return [
            mode.st_dev,
            mode.st_ino,
            mode.st_size,
            mode.st_mtime_ns,
            mode.st_ctime_ns,
        ]


def directory_identity(path):
    path = real_path(path)
    try:
        mode = path.stat()
    except OSError:
        fail("directory-unavailable")
    if not stat.S_ISDIR(mode.st_mode):
        fail("directory-required")
    return [mode.st_dev, mode.st_ino]


def _session(value):
    if not isinstance(value, dict):
        fail("session-selection-required")
    if __package__:
        from .tui_sessions import normalize_session
    else:
        from tui_sessions import normalize_session
    try:
        return normalize_session(value)
    except Exception:
        fail("invalid-session-selection")


def _workspace_action(runtime, workspace):
    try:
        return tui_storage.read_workspace_action(runtime, workspace)
    except tui_storage.StorageError as error:
        if error.reason == "trusted-path-exposure":
            fail("trusted-path-exposure")
        fail("workspace-validation")
    except Exception:
        fail("workspace-validation")


def _workspace_bindings(record):
    """Only immutable metadata leaves the bounded registry reader, never data."""
    return {
        "workspace_file": list(record.identity),
        "workspace_sha256": record.fingerprint,
        "workspace_registry": [list(value) for value in record.registry_identity],
        "workspace_condition": record.project_condition,
    }


class _OperationBackend(DashboardBackend):
    """Existing native clients with canonical selections and bounded inventories.

    Private registration data is reserved for unchanged native attachment; archive
    associations receive only ID/project metadata. Raw data never enters requests,
    UI reviews, diagnostics, environment or argv. Metadata discovery does not start
    services.
    """

    def __init__(self, runtime, *, request=None):
        super().__init__(runtime)
        self._operation_request = copy.deepcopy(request)

    def selected(self, workspace):
        record = _workspace_action(self.box, workspace)
        expected = self._operation_request
        if expected is not None and (
            workspace != expected.get("workspace")
            or str(record.row.state_root) != expected.get("state_root")
            or record.row.project != expected.get("project")
            or any(
                expected.get("bindings", {}).get(key) != value
                for key, value in _workspace_bindings(record).items()
            )
        ):
            fail("selection-changed")
        return record.row.record_path, record.data

    def inventory(self):
        # Host archive associations need multiple registrations, not native
        # workspace clients. Reuse the dashboard schema and safe storage adapter;
        # validate the directory ID/resources without probing unrelated projects.
        try:
            root = tui_storage._root(self.box)
            source = tui_storage._InventorySource(self.box, root)
            backend = DashboardBackend(source)
            result, skipped = [], 0
            for path, data in backend.inventory():
                try:
                    row = tui_storage._action_row(root, path, data)
                except tui_storage.StorageError:
                    skipped += 1
                    continue
                # Association provenance needs only these two native namespace
                # fields. Raw registration data is reserved for selected attach.
                result.append((path, {"id": row.workspace_id, "project": row.project}))
            self.inventory_skipped = (
                skipped + source.skipped + backend.inventory_skipped
            )
            return result
        except Exception:
            fail("workspace-validation")


@dataclass(frozen=True)
class OperationPlan:
    token: str
    action: str
    confirmation: str
    review: str
    request: dict


class OperationController:
    """Single-use, source-bound reviews; browsing never executes an operation."""

    def __init__(self, runtime, *, read_only=False, demo=False):
        self.box, self.read_only, self.demo = runtime, read_only, demo
        self._plans = {}

    def _allowed(self, action):
        if action not in ACTIONS:
            fail("unsupported-action")
        if self.demo:
            fail("demo")
        if self.read_only and action not in READ_ACTIONS:
            fail("read-only")

    def prepare(
        self,
        action,
        *,
        workspace=None,
        backup=None,
        destination=None,
        portable=False,
        recipients=(),
        identities=(),
        origin=None,
        store=None,
        session=None,
    ):
        self._allowed(action)
        if type(portable) is not bool:
            fail("invalid-backup-kind")
        if len(recipients) > 16 or len(identities) > 16:
            fail("too-many-keys")
        request = {
            "version": 1,
            "action": action,
            "state_root": str(self.box.state_root().resolve()),
            "workspace": workspace,
            "backup": str(real_path(backup)),
            "destination": None,
            "portable": portable,
            "recipients": list(recipients),
            "identities": [str(real_path(p)) for p in identities],
            "origin": origin,
            "store": store,
            "session": None,
            "bindings": {},
        }
        bindings = request["bindings"]
        archive = Path(request["backup"])
        bindings["backup_parent"] = directory_identity(archive.parent)
        creating = action in {"workspace-backup", "session-backup"}
        if creating:
            if archive.exists() or archive.is_symlink():
                fail("output-exists")
            bindings["backup_file"] = None
        else:
            bindings["backup_file"] = file_identity(
                archive, private=action == "backup-delete"
            )
        if recipients:
            if __package__:
                from .backup_crypto import validate_recipients
            else:
                from backup_crypto import validate_recipients
            try:
                validate_recipients(list(recipients))
            except Exception:
                fail("invalid-recipients")
        for path in request["identities"]:
            bindings["identity:" + path] = file_identity(path, private=True)
        if action.startswith("session-") and action != "session-inspect":
            if (
                origin not in {"host", "workspace"}
                or not isinstance(store, str)
                or not store
            ):
                fail("source-selection-required")
            if origin == "workspace" and workspace != store:
                fail("source-changed")
            if action == "session-backup":
                request["session"] = _session(session)
        if workspace is not None:
            record = _workspace_action(self.box, workspace)
            bindings.update(_workspace_bindings(record))
            request["project"] = record.row.project
        elif action == "workspace-backup":
            fail("workspace-selection-required")
        if creating:
            protected = [Path(request["state_root"])]
            if request.get("project"):
                protected.append(Path(request["project"]))
            if any(archive.is_relative_to(path) for path in protected):
                fail("protected-output-path")
        if destination is not None:
            if action == "session-restore" and origin == "workspace":
                if (
                    not isinstance(destination, str)
                    or not destination.startswith("/")
                    or any(ord(c) < 32 for c in destination)
                ):
                    fail("invalid-destination")
                request["destination"] = destination
            else:
                target = real_path(destination)
                request["destination"] = str(target)
                if action == "workspace-restore":
                    if target.exists() or target.is_symlink():
                        fail("destination-exists")
                    bindings["destination_parent"] = directory_identity(target.parent)
                else:
                    bindings["destination_directory"] = directory_identity(target)
        elif action in {"workspace-restore", "session-restore"}:
            fail("destination-required")
        if action == "backup-delete":
            _recognized_archive(archive)
        label = {
            "workspace-backup": "Create encrypted workspace backup",
            "workspace-inspect": "Inspect encrypted workspace backup",
            "workspace-restore": "Restore workspace to a NEW directory",
            "session-backup": "Create encrypted session-tree backup",
            "session-inspect": "Inspect encrypted session-tree backup",
            "session-restore": "Restore history with fresh native session IDs",
            "backup-delete": "Delete this encrypted backup FILE only",
        }[action]
        review = f"{label}\nBackup: {archive}\n"
        if workspace:
            review += f"Workspace: {workspace}\nProject: {request.get('project', '')}\n"
        if origin:
            review += f"Native source: {origin}\nStore: {store}\n"
        if request["session"]:
            review += f"Session tree: {request['session']['id']} (including native children)\n"
        if request["destination"]:
            review += f"Destination: {request['destination']}\n"
        review += (
            "No whole host database, global vault or shared cache is replaced or deleted. "
            "Restores never overwrite existing workspaces or session IDs.\n"
            "Close owning clients before workspace lifecycle operations. Existing locks and identity checks remain authoritative. "
            "A consistent workspace backup can stop an unowned running container and leaves it stopped.\n"
            "Passwords are entered masked in the native host terminal; no password is copied into the dashboard."
            " Whole-workspace archives can contain copied configuration/authentication and complete native history; protect the encrypted file."
        )
        confirmation = (
            "DELETE"
            if action == "backup-delete"
            else "RESTORE"
            if action.endswith("restore")
            else "CONFIRM"
        )
        plan = OperationPlan(
            secrets.token_hex(24), action, confirmation, review, copy.deepcopy(request)
        )
        self._plans = {plan.token: copy.deepcopy(plan)}
        return plan

    def confirm(self, plan, phrase):
        self._allowed(getattr(plan, "action", ""))
        expected = self._plans.pop(getattr(plan, "token", ""), None)
        if expected is None or expected != plan or phrase != plan.confirmation:
            fail("invalid-confirmation")
        request = plan.request
        current = self.prepare(
            plan.action,
            workspace=request["workspace"],
            backup=request["backup"],
            destination=request["destination"],
            portable=request["portable"],
            recipients=request["recipients"],
            identities=request["identities"],
            origin=request["origin"],
            store=request["store"],
            session=request["session"],
        )
        self._plans.clear()
        if current.request != request:
            fail("selection-changed")
        return copy.deepcopy(request)


def _recognized_archive(path):
    import tarfile

    if __package__:
        from .backup_archive import BoundedTarInfo
    else:
        from backup_archive import BoundedTarInfo
    try:
        names = []
        with tarfile.open(path, "r:", tarinfo=BoundedTarInfo) as archive:
            for entry in archive:
                if (
                    not entry.isfile()
                    or entry.sparse is not None
                    or entry.name in names
                    or entry.size < 0
                ):
                    fail("invalid-backup-file")
                names.append(entry.name)
                if len(names) > 7:
                    fail("invalid-backup-file")
        allowed = (
            {"key.age", "manifest.age", "project.age", "home.age", "registry.age"},
            {
                "key.age",
                "manifest.age",
                "project.age",
                "home.age",
                "registry.age",
                "image.age",
            },
            {"key.age", "manifest.age", "sessions.age"},
        )
        if set(names) not in allowed:
            fail("unsupported-backup-file")
    except OperationError:
        raise
    except Exception:
        fail("invalid-backup-file")


def environment(runtime):
    env = runtime.maintenance_host_env()
    env.update(
        OC_BOX_STATE_HOME=str(runtime.state_root().resolve()),
        OC_BOX_UPDATE_CHECKED="1",
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def purge_request(runtime, plan, phrase, *, read_only=False, demo=False):
    if __package__:
        from . import tui_storage
    else:
        import tui_storage
    tui_storage.confirm_workspace_purge(
        runtime, plan, phrase, read_only=read_only, demo=demo
    )
    return {
        "version": 1,
        "action": "workspace-purge",
        "workspace": plan.workspace_id,
        "state_root": str(plan.state_root),
        "record_fingerprint": plan.record_fingerprint,
        "record_identity": list(plan.record_identity),
        "registry_identity": [list(value) for value in plan.registry_identity],
        "project_condition": plan.project_condition,
    }


def handoff(runtime, request):
    """Caller suspends Textual; native foreground prompts own the actual TTY."""
    value = json.dumps(request, ensure_ascii=True).encode()
    if len(value) > MAX_REQUEST:
        fail("request-too-large")
    descriptor = os.memfd_create("oc-box-dashboard-operation", os.MFD_CLOEXEC)
    with os.fdopen(descriptor, "w+b") as stream:
        stream.write(value)
        stream.seek(0)
        return subprocess.call(
            [
                sys.executable,
                "-I",
                "-B",
                str(runtime.PACKAGE / "tui_operations.py"),
                "--request-fd",
                str(stream.fileno()),
            ],
            env=environment(runtime),
            pass_fds=(stream.fileno(),),
            cwd=Path.home(),
        )


def execute(runtime, request):
    if (
        not isinstance(request, dict)
        or request.get("version") != 1
        or request.get("action") not in ACTIONS | {"workspace-purge"}
    ):
        fail("invalid-request")
    if request.get("state_root") != str(runtime.state_root().resolve()):
        fail("state-root-changed")
    if request["action"] == "workspace-purge":
        if __package__:
            from . import tui_storage
        else:
            import tui_storage
        plan = tui_storage.prepare_workspace_purge(runtime, request.get("workspace"))
        expected = {
            "version": 1,
            "action": "workspace-purge",
            "workspace": plan.workspace_id,
            "state_root": str(plan.state_root),
            "record_fingerprint": plan.record_fingerprint,
            "record_identity": list(plan.record_identity),
            "registry_identity": [list(value) for value in plan.registry_identity],
            "project_condition": plan.project_condition,
        }
        if request != expected:
            tui_storage.cancel_workspace_purge(plan)
            fail("selection-changed")
        argv = tui_storage.confirm_workspace_purge(runtime, plan, plan.phrase)
        return runtime.main(list(argv))
    controller = OperationController(runtime)
    plan = controller.prepare(
        request["action"],
        workspace=request.get("workspace"),
        backup=request.get("backup"),
        destination=request.get("destination"),
        portable=request.get("portable", False),
        recipients=request.get("recipients", ()),
        identities=request.get("identities", ()),
        origin=request.get("origin"),
        store=request.get("store"),
        session=request.get("session"),
    )
    if plan.request != request:
        fail("selection-changed")
    action, archive = request["action"], request["backup"]
    if action == "backup-delete":
        Path(archive).unlink()
        print(
            "Selected encrypted backup file deleted; sessions and workspace storage were not changed."
        )
        return 0
    if action.startswith("workspace-"):
        if action == "workspace-backup":
            argv = ["backup", "--workspace", request["workspace"], "--output", archive]
            if request["portable"]:
                argv.append("--portable")
            for recipient in request["recipients"]:
                argv.extend(["--recipient", recipient])
        else:
            argv = (
                ["restore", archive, "--project", request["destination"]]
                if action == "workspace-restore"
                else ["backup-info", archive]
            )
            for identity in request["identities"]:
                argv.extend(["--identity", identity])
        return runtime.main(argv)
    if __package__:
        from . import session_backups
        from .tui_sessions import SessionCatalog
    else:
        import session_backups
        from tui_sessions import SessionCatalog
    if action == "session-inspect":
        metadata = session_backups.inspect(archive, identities=request["identities"])
        print(json.dumps(metadata, indent=2, ensure_ascii=True))
        return 0
    backend = _OperationBackend(runtime, request=request)
    connection = backend.session_connection(
        request["workspace"] if request["origin"] == "workspace" else None
    )
    try:
        if (connection.origin, connection.store) != (
            request["origin"],
            request["store"],
        ):
            fail("source-changed")
        associations = (
            backend.associations(connection.store)
            if connection.origin == "host"
            else []
        )
        catalog = SessionCatalog(
            connection.request,
            connection.origin,
            connection.store,
            ownership=connection.ownership,
            managed_ids=[item["archive"]["id"] for item in associations],
        )
        sessions = catalog.list()
        if action == "session-backup":
            selected = next(
                (item for item in sessions if item["id"] == request["session"]["id"]),
                None,
            )
            if selected != request["session"]:
                fail("session-changed")
            session_backups.backup(
                catalog, selected["id"], archive, recipients=request["recipients"]
            )
        else:
            session_backups.restore(
                catalog,
                archive,
                request["destination"],
                identities=request["identities"],
            )
        print(
            "Encrypted session history operation completed; no existing session IDs were overwritten."
        )
        return 0
    finally:
        connection.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 2 or argv[0] != "--request-fd" or not argv[1].isdigit():
            fail("invalid-options")
        if (
            os.geteuid() == 0
            or os.environ.get("OC_BOX_CONTAINER") == "1"
            or Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists()
        ):
            fail("host-only")
        with os.fdopen(os.dup(int(argv[1])), "rb") as stream:
            content = stream.read(MAX_REQUEST + 1)
        if len(content) > MAX_REQUEST:
            fail("request-too-large")
        code = execute(box, json.loads(content))
    except KeyboardInterrupt:
        print(
            "Dashboard operation interrupted; inspect the explicit target before retrying."
        )
        return 130
    except OperationError as error:
        print(str(error))
        return 1
    except box.BoxError as error:
        if (
            str(error)
            == "This workspace already has an active oc-box process. Exit it before changing its environment."
        ):
            print(
                "The workspace has an active owner. Exit its owning launcher and attached clients before retrying this operation."
            )
        else:
            print(
                "Workspace lifecycle operation refused or failed; no automatic retry was made. Existing identity, ownership and lock checks remain enforced."
            )
        return 1
    except Exception:
        print(
            "Dashboard operation refused or failed. Private tool output was not retained by the dashboard; no automatic retry was made."
        )
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
