"""Explicit dashboard operations with reviewed targets and native TTY handoff.

No passwords, transcripts or command output are retained by this module. The
foreground child uses the same pinned runtime and existing lifecycle CLI; it is
not a container shell or an alternate workspace controller.
"""

import copy
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys

if __package__:
    from . import (
        oc_box as box,
        opencode_compat,
        private_memory,
        tui_storage,
        workspace_service,
    )
    from .tui_backend import DashboardBackend
else:
    # -I deliberately removes the script directory; pin imports to this exact
    # runtime, not another installed oc-box or the current project directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import oc_box as box
    import opencode_compat
    import private_memory
    import tui_storage
    import workspace_service
    from tui_backend import DashboardBackend


MAX_REQUEST = 128 * 1024
MAX_RESULT = 8192
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


def operation_tag(action):
    return {
        "workspace-purge": "workspace-purge",
        "backup-delete": "backup-file-delete",
        "workspace-backup": "backup",
        "session-backup": "backup",
        "workspace-restore": "restore",
        "session-restore": "restore",
        "workspace-inspect": "inspection",
        "session-inspect": "inspection",
    }.get(action, "mutation")


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


def _session_import(value, executable, *, server=None):
    """Native 2.2.6's import CLI reads stdin; generic API --data does not.

    Do not put history in argv or pretend this is a generic body transport.
    Keep the engine's GET/export/inventory verification: native import exits zero
    for an existing-ID conflict as well as a successful import.
    """
    try:
        info, location = value["info"], value["location"]
        ident, directory = info["id"], location["directory"]
        if not isinstance(ident, str) or not re.fullmatch(r"ses[A-Za-z0-9_-]+", ident):
            fail("invalid-import-body")
        if (
            not isinstance(directory, str)
            or not directory.startswith("/")
            or ".." in Path(directory).parts
            or any(ord(char) < 32 for char in directory)
        ):
            fail("invalid-import-directory")
        if info.get("location") != location:
            fail("import-directory-mismatch")
        body = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except OperationError:
        raise
    except Exception:
        fail("invalid-import-body")
    command = [
        executable,
        "session",
        "import",
        "/proc/self/fd/0",
        "--directory",
        directory,
    ]
    if server is not None:
        command.extend(["--server", server])
    return command, body, ident


def _import_acknowledged(response, ident):
    if response.stdout.strip() != "Imported session: " + ident:
        fail("import-acknowledgement-unverified")


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
        self._expected_host_pid = None
        self._expected_host_version = None

    def host_request(self, method, target, value=None):
        if method.upper() == "POST" and target == "/api/experimental/session/import":
            command, body, ident = _session_import(value, "opencode")
            self.host_request("GET", "/api/info")
            env = self.box.maintenance_host_env()
            env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
            # Native import has a stricter service resolver than generic api.
            # Refuse a version mismatch before it can replace a healthy service.
            version = self.box.run(
                ["opencode", "--version"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture=True,
                private=True,
                timeout=15,
            )
            try:
                cli_version = opencode_compat.version(version.stdout)
            except (TypeError, ValueError):
                fail("native-version-unverified")
            if cli_version != self._expected_host_version:
                fail("native-version-mismatch")
            response = self.box.run(
                command, input=body, env=env, capture=True, private=True, timeout=60
            )
            _import_acknowledged(response, ident)
            self.host_request("GET", "/api/info")
            return None
        response = super().host_request(method, target, value)
        if method.upper() == "GET" and target == "/api/info":
            info = response.get("data", response) if isinstance(response, dict) else {}
            pid = info.get("pid")
            if (
                type(pid) is not int
                or pid <= 0
                or not str(info.get("version", "")).lstrip("v").startswith("2.")
            ):
                fail("native-service-unverified")
            try:
                version = opencode_compat.version(info.get("version", ""))
            except (TypeError, ValueError):
                fail("native-service-unverified")
            if self._expected_host_pid is not None and pid != self._expected_host_pid:
                fail("source-changed")
            if (
                self._expected_host_version is not None
                and version != self._expected_host_version
            ):
                fail("source-changed")
            self._expected_host_pid = pid
            self._expected_host_version = version
        return response

    def _workspace_connection_request(
        self, data, connection, method, target, value=None
    ):
        if method.upper() == "POST" and target == "/api/experimental/session/import":
            command, body, ident = _session_import(
                value, self.box.CONTAINER_OPENCODE, server=connection["url"]
            )
            response = self.box.run(
                workspace_service.client_command(self.box, data, connection, command),
                input=body,
                capture=True,
                private=True,
                timeout=60,
            )
            _import_acknowledged(response, ident)
            return None
        return super()._workspace_connection_request(
            data, connection, method, target, value
        )

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
        if origin == "host":
            if __package__:
                from .tui_sessions import PROCESS_LOCAL_WARNING
            else:
                from tui_sessions import PROCESS_LOCAL_WARNING
            review += PROCESS_LOCAL_WARNING + "\n"
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

    def cancel(self, plan):
        """Discard abandoned review metadata without inspecting any target."""
        if type(plan) is OperationPlan:
            self._plans.pop(plan.token, None)


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


def _result_notice(stream, code):
    """Accept only a bounded static-copy result, never captured command output."""
    if __package__:
        from .tui_notifications import FailureNotice
    else:
        from tui_notifications import FailureNotice
    try:
        stream.seek(0)
        content = stream.read(MAX_RESULT + 1)
        if len(content) > MAX_RESULT:
            return None
        result = json.loads(content)
        if (
            not isinstance(result, dict)
            or set(result) != {"code", "notice"}
            or type(result["code"]) is not int
            or result["code"] != code
        ):
            return None
        notice = result["notice"]
        if not isinstance(notice, dict) or set(notice) != {
            "title",
            "message",
            "severity",
            "timeout",
        }:
            return None
        if not all(
            type(notice[key]) is str
            and 0 < len(notice[key].encode("utf-8")) <= bound
            and all(char.isprintable() for char in notice[key])
            for key, bound in (("title", 256), ("message", 4096))
        ):
            return None
        if (
            notice["severity"] not in {"information", "warning", "error"}
            or type(notice["timeout"]) not in (int, float)
            or not 0 < notice["timeout"] <= 30
        ):
            return None
        return FailureNotice(**notice)
    except Exception:
        return None


@contextmanager
def _foreground_child_interrupts():
    # Ctrl-C reaches the whole foreground process group. Catch it in the parent
    # without raising: subprocess.call would otherwise SIGKILL the child before
    # its streaming/image/lock cleanup finishes. A caught handler resets across
    # exec, unlike SIG_IGN, so the child retains its normal cancellation behavior.
    try:
        previous = signal.signal(signal.SIGINT, lambda _number, _frame: None)
    except ValueError:
        fail("host-terminal-required")
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def handoff(runtime, request, *, on_result=None):
    """Caller suspends Textual; native foreground prompts own the actual TTY."""
    value = json.dumps(request, ensure_ascii=True).encode()
    if len(value) > MAX_REQUEST:
        fail("request-too-large")
    with (
        os.fdopen(private_memory.create("oc-box-dashboard-operation"), "w+b") as stream,
        os.fdopen(private_memory.create("oc-box-dashboard-result"), "w+b") as result,
    ):
        stream.write(value)
        stream.seek(0)
        with _foreground_child_interrupts():
            code = subprocess.call(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(runtime.PACKAGE / "tui_operations.py"),
                    "--request-fd",
                    str(stream.fileno()),
                    "--result-fd",
                    str(result.fileno()),
                ],
                env=environment(runtime),
                pass_fds=(stream.fileno(), result.fileno()),
                cwd=Path.home(),
            )
        notice = _result_notice(result, code)
        if notice is not None and on_result is not None:
            on_result(notice)
        return code


def execute(runtime, request, *, progress=None):
    def report(**fields):
        if progress is not None:
            progress(**fields)

    if (
        not isinstance(request, dict)
        or request.get("version") != 1
        or request.get("action") not in ACTIONS | {"workspace-purge"}
    ):
        fail("invalid-request")
    if request.get("state_root") != str(runtime.state_root().resolve()):
        fail("state-root-changed")
    report(operation=operation_tag(request["action"]))
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
        report(dispatched=True)
        code = runtime.main(list(argv))
        if code == 0:
            report(completed=True)
        return code
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
        report(dispatched=True)
        Path(archive).unlink()
        report(completed=True)
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
        report(dispatched=action != "workspace-inspect")
        code = runtime.main(argv)
        if code == 0:
            report(completed=True)
        return code
    if __package__:
        from . import session_backups
        from .tui_sessions import SessionCatalog
    else:
        import session_backups
        from tui_sessions import SessionCatalog
    if action == "session-inspect":
        metadata = session_backups.inspect(archive, identities=request["identities"])
        print(json.dumps(metadata, indent=2, ensure_ascii=True))
        report(completed=True)
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
            report(dispatched=True)
            session_backups.backup(
                catalog, selected["id"], archive, recipients=request["recipients"]
            )
        else:
            restore_plan = session_backups.prepare_restore(
                catalog,
                archive,
                request["destination"],
                identities=request["identities"],
            )
            try:
                print(json.dumps(restore_plan.review, indent=2, ensure_ascii=True))
                if (
                    input(
                        "Review the decrypted session metadata above. Type RESTORE to import fresh IDs: "
                    )
                    != "RESTORE"
                ):
                    print(
                        "Session restore cancelled; no session imports were submitted."
                    )
                    report(cancelled=True)
                    return 1
                report(dispatched=True)
                session_backups.restore(catalog, restore_plan)
            finally:
                restore_plan.close()
        print(
            "Encrypted session history operation completed; no existing session IDs were overwritten."
        )
        report(completed=True)
        return 0
    except session_backups.SessionBackupError as error:
        # Native imports can commit before verification/transport fails. Show
        # only engine-generated recovery IDs, never native errors or history.
        import re

        recovery = {}
        for field in ("created_ids", "attempted_ids"):
            values = getattr(error, field, ())
            if (
                isinstance(values, (tuple, list))
                and len(values) <= 50
                and all(
                    isinstance(value, str) and re.fullmatch(r"ses_[0-9a-f]{32}", value)
                    for value in values
                )
            ):
                recovery[field] = list(values)
        if any(recovery.values()):
            report(uncertain=True)
            print(
                json.dumps(
                    {
                        "outcome": "session-import-unverified",
                        "automatic_rollback": False,
                        **recovery,
                    },
                    indent=2,
                )
            )
        raise
    finally:
        connection.close()


def _checked_memory_fd(descriptor, name):
    info = os.fstat(descriptor)
    if (
        descriptor < 3
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 0
        or info.st_uid != os.getuid()
        or not os.readlink(f"/proc/self/fd/{descriptor}").startswith(
            "/memfd:" + name + " "
        )
    ):
        fail("invalid-request-descriptor")


def _publish_result(descriptor, code, notice):
    _checked_memory_fd(descriptor, "oc-box-dashboard-result")
    content = json.dumps(
        {"code": code, "notice": asdict(notice) if notice else None}, ensure_ascii=True
    ).encode("ascii")
    if len(content) > MAX_RESULT:
        fail("result-too-large")
    with os.fdopen(os.dup(descriptor), "wb") as stream:
        stream.write(content)


def _host_guard():
    if (
        os.geteuid() == 0
        or os.environ.get("OC_BOX_CONTAINER") == "1"
        or Path("/.dockerenv").exists()
        or Path("/run/.containerenv").exists()
    ):
        fail("host-only")


def main(argv=None):
    if __package__:
        from .tui_notifications import failure_notice
    else:
        from tui_notifications import failure_notice
    argv = sys.argv[1:] if argv is None else argv
    state = {
        "operation": "mutation",
        "dispatched": False,
        "completed": False,
        "uncertain": False,
        "cancelled": False,
    }
    result_fd, notice, error, code = None, None, None, 1
    try:
        if (
            len(argv) not in (2, 4)
            or argv[0] != "--request-fd"
            or not argv[1].isdigit()
        ):
            fail("invalid-options")
        _host_guard()
        request_fd = int(argv[1])
        if len(argv) == 4:
            if (
                argv[2] != "--result-fd"
                or not argv[3].isdigit()
                or int(argv[3]) == request_fd
            ):
                fail("invalid-options")
            result_fd = int(argv[3])
            _checked_memory_fd(result_fd, "oc-box-dashboard-result")
        _checked_memory_fd(request_fd, "oc-box-dashboard-operation")
        with os.fdopen(os.dup(request_fd), "rb") as stream:
            content = stream.read(MAX_REQUEST + 1)
        if len(content) > MAX_REQUEST:
            fail("request-too-large")
        code = execute(box, json.loads(content), progress=state.update)
        if type(code) is not int:
            code = 0 if code is None else 1
    except KeyboardInterrupt:
        code = 130
        state["uncertain"] = state["uncertain"] or state["dispatched"]
        state["cancelled"] = True
    except Exception as caught:
        error = caught
        code = 1
        if (
            isinstance(caught, box.BoxError)
            and str(caught)
            == "This workspace already has an active oc-box process. Exit it before changing its environment."
        ):
            notice = failure_notice(state["operation"], reason="owner-active")
    if code != 0 and notice is None:
        notice = failure_notice(
            state["operation"],
            error,
            reason="cancelled" if state["cancelled"] else None,
            dispatched=state["dispatched"],
            completed=state["completed"],
            uncertain=state["uncertain"],
        )
    if notice is not None:
        print(f"{notice.title}: {notice.message}")
    if result_fd is not None:
        try:
            _publish_result(result_fd, code, notice)
        except Exception:
            print(
                "Dashboard result notification unavailable; inspect the explicit target before any retry."
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
