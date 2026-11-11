"""Optional Storage and Backup/restore presentation with explicit safe actions."""

from __future__ import annotations

import sys

from rich.text import Text

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Select, Static

if __package__:
    from . import status_report, tui_operations, tui_storage
    from .tui_backend import plain
    from .tui_clipboard import ClipboardBar, ClipboardInput, SelectableDataTable
else:
    import status_report
    import tui_operations
    import tui_storage
    from tui_backend import plain
    from tui_clipboard import ClipboardBar, ClipboardInput, SelectableDataTable


SCOPES = [
    ("Host OpenCode · individual sessions", "host"),
    ("Selected workspace", "workspace"),
    ("All registered workspaces", "all"),
]


class ActionReview(ModalScreen):
    CSS = """
    ActionReview { align: center middle; }
    ActionReview > Vertical { width: 82; max-width: 95%; height: 85%;
      padding: 1 2; border: thick $warning; background: $surface; }
    #operation-review-text { height: 1fr; }
    ActionReview Horizontal { height: 3; }
    """

    def __init__(self, review, phrase):
        super().__init__()
        self.review, self.phrase = review, phrase

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="operation-review-text"):
                yield Static(plain(self.review, 32768), markup=False)
            yield Static(f"Type exactly: {self.phrase}", markup=False)
            yield ClipboardInput(
                placeholder="Typed confirmation", id="operation-confirmation"
            )
            yield Static(
                "Cancel makes no changes.",
                id="operation-confirmation-status",
                markup=False,
            )
            yield ClipboardBar()
            with Horizontal():
                yield Button("Cancel", id="operation-cancel")
                yield Button("Confirm", id="operation-confirm", variant="error")

    def on_button_pressed(self, event):
        if event.button.id not in {"operation-cancel", "operation-confirm"}:
            return  # Clipboard controls must bubble to the normal app handlers.
        event.stop()
        if event.button.id == "operation-cancel":
            self.dismiss(None)
        elif event.button.id == "operation-confirm":
            phrase = self.query_one("#operation-confirmation", Input).value
            if phrase != self.phrase:
                self.query_one("#operation-confirmation-status", Static).update(
                    "Confirmation does not match; nothing was changed."
                )
                self.app._operation_failure(
                    operation="mutation", reason="confirmation-mismatch"
                )
            else:
                self.dismiss(phrase)


class StoragePanel(Vertical):
    DEFAULT_CSS = """
    StoragePanel { height: auto; min-height: 12; }
    #storage-table { height: 12; min-height: 5; }
    #storage-details { height: auto; padding: 1 0; }
    """

    def __init__(self, *, read_only=False, demo=False):
        super().__init__()
        self.read_only, self.demo = read_only, demo

    def compose(self) -> ComposeResult:
        yield Static(
            "Storage scopes are independent. Listing never starts a workspace or deletes data.",
            markup=False,
        )
        with Horizontal(classes="toolbar"):
            yield Select(
                SCOPES, value="workspace", allow_blank=False, id="storage-scope"
            )
            yield Button("Refresh storage list", id="storage-refresh")
        yield SelectableDataTable(
            id="storage-table", cursor_type="row", zebra_stripes=True
        )
        yield Static(
            "Loading registered workspace metadata…", id="storage-details", markup=False
        )
        with Horizontal(classes="toolbar"):
            yield Button("Scan sizes", id="storage-scan")
            yield Button(
                "Delete selected session…",
                id="storage-session-delete",
                disabled=self.read_only or self.demo,
            )
            yield Button(
                "Remove workspace storage…",
                id="storage-purge",
                variant="error",
                disabled=self.read_only or self.demo,
            )


class BackupPanel(Vertical):
    DEFAULT_CSS = """
    BackupPanel { height: auto; min-height: 18; }
    #backup-table { height: 9; min-height: 4; }
    #backup-details { height: auto; padding: 1 0; }
    BackupPanel Input { margin-bottom: 1; }
    """

    def __init__(self, *, read_only=False, demo=False):
        super().__init__()
        self.read_only, self.demo = read_only, demo

    def compose(self) -> ComposeResult:
        yield Static(
            "Encrypted backups only. Workspace archives and individual native session history are different formats.",
            markup=False,
        )
        with Horizontal(classes="toolbar"):
            yield Select(
                SCOPES, value="workspace", allow_blank=False, id="backup-scope"
            )
            yield Button("Refresh backup targets", id="backup-refresh")
        yield SelectableDataTable(
            id="backup-table", cursor_type="row", zebra_stripes=True
        )
        with Horizontal(classes="toolbar"):
            yield Select(
                [
                    ("Workspace · full (.ocbox)", "full"),
                    ("Workspace · portable (.ocbox)", "portable"),
                    ("Session tree · history (.ocsession)", "session"),
                ],
                value="full",
                allow_blank=False,
                id="backup-kind",
            )
        yield ClipboardInput(
            placeholder="Encrypted backup file: new output, or existing input",
            id="backup-file",
        )
        yield ClipboardInput(
            placeholder="Restore destination: NEW project directory, or native session directory",
            id="backup-destination",
        )
        yield ClipboardInput(
            placeholder="Optional age recipients (space separated); blank uses a masked password prompt",
            id="backup-recipients",
        )
        yield ClipboardInput(
            placeholder="Optional private age identity file paths (one path, or JSON array of paths)",
            id="backup-identities",
        )
        yield Static(
            "No global host database replacement. Restores use fresh workspace/session IDs and never auto-start a workspace.",
            id="backup-details",
            markup=False,
        )
        with Horizontal(classes="toolbar"):
            yield Button(
                "Create backup…",
                id="backup-create",
                disabled=self.read_only or self.demo,
            )
            yield Button("Inspect backup…", id="backup-inspect", disabled=self.demo)
            yield Button(
                "Restore…", id="backup-restore", disabled=self.read_only or self.demo
            )
            yield Button(
                "Delete backup file…",
                id="backup-delete",
                variant="error",
                disabled=self.read_only or self.demo,
            )


class ManagementActions:
    """Mixin: all I/O remains explicit, outside widget composition and settings."""

    def _management_init(self):
        self._management_records = ()
        self._management_loading = False
        self._maintenance_busy = False
        self._management_last_completed = None
        self._management_controller = tui_operations.OperationController(
            self.backend.box, read_only=self.read_only, demo=self.demo
        )

    def _management_mount(self):
        for prefix in ("storage", "backup"):
            self.query_one(f"#{prefix}-table", DataTable).add_columns(
                "Workspace", "Project", "OS", "Home", "Project files"
            )
        self._management_refresh()

    def _management_refresh(self):
        if self._management_loading or self._maintenance_busy:
            self._message(
                "Storage metadata is already loading or a reviewed operation is running."
            )
            return
        if self.demo or self.backend.box is None:
            self._management_records = ()
            self._render_management()
            return
        self._management_loading = True
        self.query_one("#storage-details", Static).update(
            "Loading registered workspace metadata… No native session connection is made."
        )
        self._load_management()

    @work(thread=True, group="management-inventory", exit_on_error=False)
    def _load_management(self):
        try:
            inventory = tui_storage.workspace_rows(
                self.backend.box, all_workspaces=True
            )
            if self.is_running:
                self.call_from_thread(self._apply_management, inventory)
        except Exception:
            if self.is_running:
                self.call_from_thread(self._management_failed)

    def _apply_management(self, inventory):
        self._management_loading = False
        self._management_records = inventory.rows
        self._render_management()
        self._management_last_completed = None
        if inventory.skipped:
            self._message(
                f"Storage inventory skipped {inventory.skipped} unavailable or invalid registrations; no resources were changed."
            )

    def _management_failed(self):
        self._management_loading = False
        if self._management_last_completed is not None:
            self._operation_failure(
                operation=self._management_last_completed, completed=True
            )
            self._management_last_completed = None
        else:
            self._operation_failure(operation="storage", reason="inventory-unavailable")

    def _render_management(self):
        if not self.is_mounted:
            return
        reports = {
            item["workspace_id"]: item for item in self.report.get("workspaces", ())
        }
        for prefix in ("storage", "backup"):
            scope = self.query_one(f"#{prefix}-scope", Select).value
            rows = (
                ()
                if scope == "host"
                else tuple(
                    row
                    for row in self._management_records
                    if scope == "all" or row.workspace_id == self.selected
                )
            )
            table = self.query_one(f"#{prefix}-table", DataTable)
            previous = (
                str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
                if table.row_count
                else None
            )
            new_values = []
            for row in rows:
                metrics = reports.get(row.workspace_id, {}).get("storage", {})
                new_values.append(
                    (
                        row.workspace_id,
                        row.project,
                        row.distro,
                        status_report.byte_size(metrics.get("private_home_bytes")),
                        status_report.byte_size(metrics.get("project_bytes")),
                    )
                )
            rendered = tuple(new_values)
            cache_name = "_management_rendered_" + prefix
            if getattr(self, cache_name, None) != rendered:
                table.clear()
                for values in new_values:
                    table.add_row(
                        *(Text(plain(value, 4096)) for value in values), key=values[0]
                    )
                if previous in {row.workspace_id for row in rows}:
                    table.move_cursor(
                        row=next(
                            i
                            for i, row in enumerate(rows)
                            if row.workspace_id == previous
                        )
                    )
                setattr(self, cache_name, rendered)
            if prefix == "storage":
                details = (
                    tui_storage.HOST_SCOPE_SUMMARY
                    if scope == "host"
                    else (
                        f"{len(rows)} registered workspace(s). Unknown sizes are not zero; Scan sizes is explicit. "
                        "Removing workspace storage retains host project files and host history. "
                        "All-workspaces is an inventory, not an unreviewed bulk-delete action."
                    )
                )
                self.query_one("#storage-details", Static).update(details)
                self.query_one("#storage-purge", Button).disabled = (
                    self.read_only or self.demo or scope == "host" or not rows
                )
                self.query_one("#storage-scan", Button).disabled = (
                    self.demo or scope == "host" or not rows
                )

    def _management_target(self, prefix):
        table = self.query_one(f"#{prefix}-table", DataTable)
        if not table.row_count:
            raise tui_operations.OperationError("workspace-selection-required")
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _management_select(self, event):
        if event.select.id in {"storage-scope", "backup-scope", "backup-kind"}:
            self._render_management()
            return True
        return False

    def _management_session(self, prefix):
        scope = self.query_one(f"#{prefix}-scope", Select).value
        expected = "host" if scope == "host" else "workspace"
        if not self.catalog or self.catalog.origin != expected:
            raise tui_operations.OperationError("connect-target-in-sessions-first")
        workspace = None if expected == "host" else self._management_target(prefix)
        if workspace is not None and self.catalog.store != workspace:
            raise tui_operations.OperationError("session-workspace-mismatch")
        return expected, workspace, self.catalog.store

    def _management_button(self, event):
        action = event.button.id or ""
        if action not in {
            "storage-refresh",
            "backup-refresh",
            "storage-scan",
            "storage-session-delete",
            "storage-purge",
            "backup-create",
            "backup-inspect",
            "backup-restore",
            "backup-delete",
        }:
            return False
        event.stop()
        try:
            if action.endswith("refresh"):
                self._management_refresh()
            elif action == "storage-scan":
                self.scan_storage(self._management_target("storage"))
            elif action == "storage-session-delete":
                self._management_session("storage")
                self.prepare_delete(self._session(), self.catalog)
            elif action == "storage-purge":
                self._review_storage_removal()
            else:
                self._review_backup(action)
        except (tui_operations.OperationError, tui_storage.StorageError) as error:
            operation = (
                "workspace-purge"
                if action == "storage-purge"
                else "backup-file-delete"
                if action == "backup-delete"
                else "storage"
            )
            self._operation_failure(error, operation=operation)
        except Exception:
            self._operation_failure(operation="storage")
        return True

    def _review_storage_removal(self):
        plan = tui_storage.prepare_workspace_purge(
            self.backend.box,
            self._management_target("storage"),
            read_only=self.read_only,
            demo=self.demo,
        )

        def finish(phrase):
            if phrase is None:
                tui_storage.cancel_workspace_purge(plan)
                return
            try:
                request = tui_operations.purge_request(
                    self.backend.box,
                    plan,
                    phrase,
                    read_only=self.read_only,
                    demo=self.demo,
                )
                self._perform_management(request)
            except Exception as error:
                self._operation_failure(error, operation="workspace-purge")

        review = f"Workspace: {plan.workspace_id}\nProject: {plan.project}\n{plan.review_text}"
        self.push_screen(ActionReview(review, plan.phrase), finish)

    def _review_backup(self, button):
        import json

        kind = self.query_one("#backup-kind", Select).value
        scope = self.query_one("#backup-scope", Select).value
        verb = {
            "backup-create": "backup",
            "backup-inspect": "inspect",
            "backup-restore": "restore",
            "backup-delete": "delete",
        }[button]
        action = (
            "backup-delete"
            if verb == "delete"
            else ("session-" if kind == "session" else "workspace-") + verb
        )
        workspace = None
        origin = store = session = None
        if action in {"session-backup", "session-restore"}:
            origin, workspace, store = self._management_session("backup")
            if action == "session-backup":
                session = self._session()
        elif action == "workspace-backup":
            if scope == "host":
                raise tui_operations.OperationError("host-session-backups-only")
            workspace = self._management_target("backup")
        identities = self.query_one("#backup-identities", Input).value.strip()
        identities = (
            json.loads(identities)
            if identities.startswith("[")
            else [identities]
            if identities
            else []
        )
        if not isinstance(identities, list) or not all(
            isinstance(value, str) for value in identities
        ):
            raise tui_operations.OperationError("invalid-identities")
        destination = self.query_one("#backup-destination", Input).value.strip() or None
        if (
            action == "session-restore"
            and origin == "workspace"
            and destination is None
        ):
            data = tui_storage.read_workspace_action(
                self.backend.box, workspace, read_only=self.read_only, demo=self.demo
            ).data
            destination = self.backend.box.container_project(data)
        self._management_controller.read_only = self.read_only
        plan = self._management_controller.prepare(
            action,
            workspace=workspace,
            backup=self.query_one("#backup-file", Input).value.strip(),
            destination=destination if verb == "restore" else None,
            portable=kind == "portable",
            recipients=self.query_one("#backup-recipients", Input).value.split()
            if verb == "backup"
            else (),
            identities=identities if verb in {"restore", "inspect"} else (),
            origin=origin,
            store=store,
            session=session,
        )

        def finish(phrase):
            if phrase is None:
                self._management_controller.cancel(plan)
                return
            try:
                self._management_controller.read_only = self.read_only
                request = self._management_controller.confirm(plan, phrase)
                self._perform_management(request)
            except Exception as error:
                operation = (
                    "backup-file-delete" if action == "backup-delete" else "storage"
                )
                self._operation_failure(error, operation=operation)

        self.push_screen(ActionReview(plan.review, plan.confirmation), finish)

    def _perform_management(self, request):
        operation = tui_operations.operation_tag(request["action"])
        if self.demo or (
            self.read_only and request["action"] not in tui_operations.READ_ACTIONS
        ):
            self._operation_failure(
                operation=operation, reason="demo" if self.demo else "read-only"
            )
            return
        if (
            self._maintenance_busy
            or self._session_busy
            or self._terminal_busy
            or self._management_loading
        ):
            self._operation_failure(operation=operation, reason="busy")
            return
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            self._operation_failure(
                operation=operation, reason="host-terminal-required"
            )
            return
        self._maintenance_busy = True
        self._management_last_completed = None
        notices, submitted = [], False
        try:
            with self.suspend():
                submitted = True
                code = tui_operations.handoff(
                    self.backend.box, request, on_result=notices.append
                )
                print(
                    f"Dashboard operation exited with status {code}. No automatic retry will run."
                )
                try:
                    input("Press Enter to return to the dashboard… ")
                except EOFError:
                    pass
            if code == 0:
                self._management_last_completed = operation
                self._message(
                    "Management operation completed. Native session inventories remain manual; Connect / refresh to inspect restored history."
                )
            elif notices:
                self._show_failure_notice(notices[-1])
            else:
                self._operation_failure(
                    operation=operation, dispatched=True, uncertain=True
                )
        except KeyboardInterrupt:
            self._operation_failure(
                operation=operation,
                reason="cancelled",
                dispatched=submitted,
                uncertain=submitted,
            )
        except Exception:
            self._operation_failure(
                operation=operation, dispatched=submitted, uncertain=submitted
            )
        finally:
            self._maintenance_busy = False
        self._management_refresh()
        self.action_refresh()
