"""Optional Storage and Backup/restore presentation with explicit safe actions."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.text import Text

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, DataTable, Input, Label, Select, Static

if __package__:
    from . import status_report, tui_operations, tui_storage
    from .tui_backend import plain
    from .tui_clipboard import ClipboardInput, SelectableDataTable
    from .tui_settings import RoundedSwitch
else:
    import status_report
    import tui_operations
    import tui_storage
    from tui_backend import plain
    from tui_clipboard import ClipboardInput, SelectableDataTable
    from tui_settings import RoundedSwitch


SCOPES = [
    ("Host OpenCode · individual sessions", "host"),
    ("Selected workspace", "workspace"),
    ("All registered workspaces", "all"),
]

BACKUP_SWITCHES = (
    ("Include workspace data", "include-workspace", True),
    ("Portable backup (omit OS image)", "portable", False),
    ("Include host settings", "include-host", False),
    ("Include native credential export", "include-auth-export", False),
)

RESTORE_SWITCHES = (
    ("Restore workspace", "restore-workspace", True),
    ("Restore host settings", "restore-host", False),
    ("Restore native OpenCode", "restore-opencode", False),
)

ARCHIVE_LAYOUTS = {
    "host": "Host settings only; requires Restore host settings ON.",
    "workspace-host": "Workspace and host settings. Choose either component or both.",
    "workspace": "Workspace only. Host restore is unavailable.",
    "session": "Session tree only. Restore uses the connected Sessions source.",
}


class ActionReview(ModalScreen):
    AUTO_FOCUS = "#operation-cancel"
    CSS = """
    ActionReview { align: center middle; }
    ActionReview > Vertical { width: 82; max-width: 95%; height: 85%;
      padding: 1 2; border: round $warning; background: $surface; }
    #operation-review-text { height: 1fr; }
    ActionReview Horizontal { height: 3; }
    """

    def __init__(self, review, phrase=""):
        super().__init__()
        self.review, self.phrase = review, phrase

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="operation-review-text"):
                yield Static(plain(self.review, 32768), markup=False)
            if self.phrase:
                yield Static(f"Type exactly: {self.phrase}", markup=False)
                yield ClipboardInput(
                    placeholder="Typed confirmation", id="operation-confirmation"
                )
            yield Static(
                "Cancel makes no changes.",
                id="operation-confirmation-status",
                markup=False,
            )
            with Horizontal():
                yield Button("Cancel", id="operation-cancel")
                yield Button(
                    "Confirm",
                    id="operation-confirm",
                    variant="error" if self.phrase else "primary",
                )

    def on_button_pressed(self, event):
        if event.button.id not in {"operation-cancel", "operation-confirm"}:
            return
        event.stop()
        if event.button.id == "operation-cancel":
            self.dismiss(None)
        elif event.button.id == "operation-confirm":
            phrase = (
                self.query_one("#operation-confirmation", Input).value
                if self.phrase
                else ""
            )
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
    StoragePanel { height: auto; min-height: 12; padding: 0 0 1 0; }
    StoragePanel .storage-card { height: auto; padding: 0 1;
      border: solid $primary; background: $surface; margin-bottom: 1; }
    StoragePanel .card-heading { height: auto; color: $accent; text-style: bold; }
    StoragePanel .card-hint { height: auto; color: $text-muted; }
    StoragePanel .management-tools { height: 3; min-height: 3; overflow-x: auto; }
    StoragePanel .management-tools Select { width: 1fr; min-width: 24; }
    StoragePanel SelectCurrent > #label { height: 1; text-wrap: nowrap;
      text-overflow: ellipsis; }
    StoragePanel .management-tools Button { width: auto; min-width: 10;
      padding: 0; margin-right: 1; }
    StoragePanel #storage-table { height: 10; min-height: 5; margin-top: 1; }
    StoragePanel #storage-details { height: auto; padding: 0; margin-bottom: 1; }
    StoragePanel #storage-session-delete { margin-left: 3; }
    """

    def __init__(self, *, read_only=False, demo=False):
        super().__init__()
        self.read_only, self.demo = read_only, demo

    def compose(self) -> ComposeResult:
        with Vertical(classes="storage-card"):
            yield Static("STORAGE / INVENTORY", classes="card-heading", markup=False)
            yield Static(
                "Source scope · independent of the workspace context",
                classes="card-hint",
                markup=False,
            )
            with Horizontal(classes="management-tools"):
                yield Select(
                    SCOPES, value="workspace", allow_blank=False, id="storage-scope"
                )
                yield Button("Refresh list", id="storage-refresh")
            yield SelectableDataTable(
                id="storage-table", cursor_type="row", zebra_stripes=True
            )
            yield Static(
                "Choose one row for workspace actions. Session deletion uses the connected Sessions source.",
                classes="card-hint",
                markup=False,
            )
        with Vertical(classes="storage-card"):
            yield Static(
                "SELECTED TARGET / ACTIONS", classes="card-heading", markup=False
            )
            yield Static(
                "Loading registered workspace metadata…",
                id="storage-details",
                markup=False,
            )
            with Horizontal(classes="management-tools"):
                yield Button("Scan sizes", id="storage-scan", disabled=True)
                yield Button(
                    "Delete selected session…",
                    id="storage-session-delete",
                    variant="error",
                    disabled=self.read_only or self.demo,
                )
                yield Button(
                    "Remove workspace storage…",
                    id="storage-purge",
                    variant="error",
                    disabled=self.read_only or self.demo,
                )
            yield Static(
                "Listing never starts a workspace or deletes data.",
                classes="card-hint",
                markup=False,
            )


class BackupPanel(Vertical):
    DEFAULT_CSS = """
    BackupPanel { height: auto; min-height: 18; padding: 0 0 1 0; }
    BackupPanel .backup-card { height: auto; padding: 0 1;
      border: solid $primary; background: $surface; margin-bottom: 1; }
    BackupPanel .card-heading { height: auto; color: $accent; text-style: bold; }
    BackupPanel .card-hint { height: auto; color: $text-muted; }
    BackupPanel .backup-field { height: 3; }
    BackupPanel .backup-field > Static { width: 13; padding: 1 0; }
    BackupPanel .backup-field > Input,
    BackupPanel .backup-field > Select { width: 1fr; min-width: 14; }
    BackupPanel .backup-option { height: auto; min-height: 3; }
    BackupPanel .backup-option > Label { width: 1fr; height: auto;
      min-height: 3; content-align: left middle; }
    BackupPanel #backup-auth-export-field { display: none; }
    BackupPanel Input { margin-bottom: 0; }
    BackupPanel Collapsible { padding: 0; margin: 0; border: none; }
    BackupPanel Collapsible > Contents { padding: 0 0 0 1; }
    BackupPanel .advanced-label { height: auto; color: $text-muted; }
    BackupPanel .management-tools { height: 3; min-height: 3; overflow-x: auto; }
    BackupPanel .management-tools Select { width: 1fr; min-width: 24; }
    BackupPanel SelectCurrent > #label { height: 1; text-wrap: nowrap;
      text-overflow: ellipsis; }
    BackupPanel .management-tools Button { width: auto; min-width: 10;
      padding: 0; margin-right: 1; }
    BackupPanel #backup-table { height: 6; min-height: 4; }
    BackupPanel #backup-details { height: auto; padding: 0; margin-bottom: 1; }
    """

    def __init__(self, *, read_only=False, demo=False):
        super().__init__()
        self.read_only, self.demo = read_only, demo
        self._archive_hint = None
        self._restore_archive_path = ""

    def _compose_switches(self, options):
        for label, name, default in options:
            with Horizontal(classes="backup-option"):
                yield Label(label)
                yield RoundedSwitch(
                    default,
                    id="backup-" + name,
                    disabled=self.demo or self.read_only,
                )

    def compose(self) -> ComposeResult:
        with Vertical(classes="backup-card"):
            yield Static("BACKUP / RESTORE", classes="card-heading", markup=False)
            with Horizontal(classes="backup-field"):
                yield Static("Archive file", markup=False)
                yield ClipboardInput(
                    placeholder="New output or existing encrypted input",
                    id="backup-file",
                )
            with Horizontal(classes="backup-field"):
                yield Static("Destination", markup=False)
                yield ClipboardInput(
                    placeholder="NEW project or native session directory",
                    id="backup-destination",
                )
            with Collapsible(
                title="Advanced · age recipients / identity files", collapsed=True
            ):
                yield Static(
                    "Public recipients · creation only; blank uses a password",
                    classes="advanced-label",
                    markup=False,
                )
                yield ClipboardInput(
                    placeholder="Existing age public recipients",
                    id="backup-recipients",
                )
                yield Static(
                    "Private identity · existing file path or JSON array of paths",
                    classes="advanced-label",
                    markup=False,
                )
                yield ClipboardInput(
                    placeholder="Existing age identity file(s)",
                    id="backup-identities",
                )
            yield Static(
                "Password requested privately by default · no key file needs copying",
                classes="card-hint",
                markup=False,
            )
        with Vertical(classes="backup-card"):
            yield Static("SOURCE / TARGET", classes="card-heading", markup=False)
            yield Static(
                "Source scope · independent of the workspace context",
                classes="card-hint",
                markup=False,
            )
            with Horizontal(classes="management-tools"):
                yield Select(
                    SCOPES, value="workspace", allow_blank=False, id="backup-scope"
                )
                yield Button("Refresh targets", id="backup-refresh")
            yield SelectableDataTable(
                id="backup-table", cursor_type="row", zebra_stripes=True
            )
            yield Static(
                "Choose one row. Session trees use the connected Sessions source.",
                classes="card-hint",
                markup=False,
            )
            with Horizontal(classes="backup-field"):
                yield Static("Create type", markup=False)
                yield Select(
                    [
                        ("Workspace / host settings (.ocbox)", "full"),
                        ("Session tree · history (.ocsession)", "session"),
                    ],
                    value="full",
                    allow_blank=False,
                    id="backup-kind",
                )
            yield Static(
                "Host OpenCode scope is for individual session trees. For host migration, choose a workspace scope and the workspace / host settings type.",
                classes="card-hint",
                markup=False,
            )
        with Vertical(classes="backup-card"):
            yield Static("CREATE OPTIONS", classes="card-heading", markup=False)
            yield from self._compose_switches(BACKUP_SWITCHES)
            with Horizontal(classes="backup-field", id="backup-auth-export-field"):
                yield Static("Export file", markup=False)
                yield ClipboardInput(
                    placeholder="Absolute path to a private OpenCode auth export",
                    id="backup-auth-export-path",
                    disabled=True,
                )
            yield Static(
                "For a host-only backup, turn OFF Include workspace data and turn ON Include host settings. "
                "Native credential export requires a separate private file; the dashboard never exports it for you.",
                classes="card-hint",
                markup=False,
            )
        with Vertical(classes="backup-card"):
            yield Static("RESTORE OPTIONS", classes="card-heading", markup=False)
            yield from self._compose_switches(RESTORE_SWITCHES)
            yield Static(
                "Inspect / restore checks the archive layout only when requested. A host-only restore does not use a destination path.",
                classes="card-hint",
                markup=False,
            )
            yield Static(
                "", id="backup-archive-layout", classes="card-hint", markup=False
            )
        yield Static(
            "No global host database replacement. Restores use fresh workspace/session IDs and never auto-start a workspace.",
            id="backup-details",
            markup=False,
        )
        with Horizontal(classes="management-tools"):
            yield Button(
                "Create backup…",
                id="backup-create",
                disabled=self.read_only or self.demo,
            )
            yield Button("Inspect backup…", id="backup-inspect", disabled=self.demo)
            yield Button(
                "Restore…", id="backup-restore", disabled=self.read_only or self.demo
            )

    def switch(self, name):
        return self.query_one("#backup-" + name, RoundedSwitch)

    def archive_hint(self, path):
        if self._archive_hint is not None and self._archive_hint[0] == path:
            return self._archive_hint[1]
        return None

    def set_archive_hint(self, path, kind, identity=None):
        self._archive_hint = (path, kind, identity) if kind is not None else None
        self._sync_options()

    def archive_selection_changed(self, path, kind, identity):
        return (
            self._archive_hint is not None
            and self._archive_hint[0] == path
            and self._archive_hint[1:] != (kind, identity)
        )

    def reset_restore_options(self):
        self.switch("restore-workspace").value = True
        self.switch("restore-host").value = False
        self.switch("restore-opencode").value = False

    def _sync_options(self):
        disabled = self.demo or self.read_only
        kind_select = self.query_one("#backup-kind", Select)
        host_scope = self.query_one("#backup-scope", Select).value == "host"
        kind_select.disabled = host_scope
        workspace_mode = kind_select.value == "full" and not host_scope
        workspace = self.switch("include-workspace")
        portable = self.switch("portable")
        host = self.switch("include-host")
        native_export = self.switch("include-auth-export")
        workspace.disabled = host.disabled = disabled or not workspace_mode
        portable.disabled = disabled or not (workspace_mode and workspace.value)
        if not workspace_mode:
            host.value = False
        if not (workspace_mode and workspace.value):
            portable.value = False
        native_export.disabled = disabled or not (workspace_mode and host.value)
        if not (workspace_mode and host.value):
            native_export.value = False
        show_export = not native_export.disabled and native_export.value
        self.query_one("#backup-auth-export-field", Horizontal).display = show_export
        self.query_one("#backup-auth-export-path", Input).disabled = not show_export

        kind = self.archive_hint(self.query_one("#backup-file", Input).value.strip())
        restore_workspace = self.switch("restore-workspace")
        restore_host = self.switch("restore-host")
        restore_native = self.switch("restore-opencode")
        restore_workspace.disabled = disabled or kind in {"host", "session"}
        can_restore_host = kind in {"host", "workspace-host"}
        restore_host.disabled = disabled or not can_restore_host
        if kind in {"host", "session"}:
            restore_workspace.value = False
        if not can_restore_host:
            restore_host.value = False
        restore_native.disabled = disabled or not restore_host.value
        if not restore_host.value:
            restore_native.value = False
        self.query_one("#backup-archive-layout", Static).update(
            "Unverified archive layout: " + ARCHIVE_LAYOUTS[kind]
            if kind in ARCHIVE_LAYOUTS
            else ""
        )

    def on_mount(self):
        self._sync_options()

    def on_select_changed(self, event):
        if event.select.id in {"backup-scope", "backup-kind"}:
            self._sync_options()

    def on_switch_changed(self, event):
        self._sync_options()

    def on_input_changed(self, event):
        if event.input.id != "backup-file":
            return
        path = event.value.strip()
        if path != self._restore_archive_path:
            self._restore_archive_path = path
            self.reset_restore_options()
            self.set_archive_hint(None, None)


class ManagementActions:
    """Mixin: all I/O remains explicit, outside widget composition and settings."""

    def _management_init(self):
        self._management_records = ()
        self._management_loading = False
        self._maintenance_busy = False
        self._management_last_completed = None
        self._backup_preparing = False
        self._private_interaction = False
        self._embedded_operation = None
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
                self._render_scan_button(
                    "#storage-scan",
                    available=not self.demo and scope != "host" and bool(rows),
                )

    def _management_target(self, prefix):
        table = self.query_one(f"#{prefix}-table", DataTable)
        if not table.row_count:
            raise tui_operations.OperationError("workspace-selection-required")
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _management_select(self, event):
        if event.select.id in {"storage-scope", "backup-scope", "backup-kind"}:
            if event.select.id == "backup-scope" and event.select.value == "host":
                self.query_one("#backup-kind", Select).value = "session"
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
            operation = "workspace-purge" if action == "storage-purge" else "storage"
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

    def _backup_default_project(self):
        return next(
            (
                row.project
                for row in self._management_records
                if row.workspace_id == self.selected
            ),
            None,
        )

    def _backup_detection_selection(self, button):
        """Snapshot form and source metadata, without touching any archive or service."""
        panel = self.query_one(BackupPanel)
        paths = (
            panel.query_one("#backup-file", Input).value,
            panel.query_one("#backup-identities", Input).value,
        )
        if button == "backup-inspect":
            return paths
        scope = panel.query_one("#backup-scope", Select).value
        destination = panel.query_one("#backup-destination", Input).value
        table = panel.query_one("#backup-table", DataTable)
        session = (
            self._session() if scope == "host" and not destination.strip() else None
        )
        return (
            *paths,
            destination,
            *(panel.switch(name).value for _, name, _ in RESTORE_SWITCHES),
            scope,
            self._management_target("backup") if table.row_count else None,
            self.selected,
            self._backup_default_project(),
            (self.catalog.origin, self.catalog.store) if self.catalog else None,
            (session["id"], session["location"]["directory"]) if session else None,
        )

    def _review_backup(
        self, button, *, detected=None, expected_path=None, expected_file=None
    ):
        import json

        # Admission precedes even format probing; browsing/demo never opens an archive.
        if self.demo:
            raise tui_operations.OperationError("demo")
        if self.read_only and button != "backup-inspect":
            raise tui_operations.OperationError("read-only")
        if self._maintenance_busy or self._backup_preparing:
            raise tui_operations.OperationError("busy")
        panel = self.query_one(BackupPanel)
        archive_path = panel.query_one("#backup-file", Input).value.strip()
        if button in {"backup-inspect", "backup-restore"} and detected is None:
            selection = self._backup_detection_selection(button)
            self._backup_preparing = True
            self._detect_backup(button, archive_path, selection)
            return
        if expected_path is not None and (
            archive_path != expected_path
            or tui_operations.file_identity(tui_operations.real_path(archive_path))
            != expected_file
        ):
            raise tui_operations.OperationError("selection-changed")
        kind = panel.query_one("#backup-kind", Select).value
        scope = panel.query_one("#backup-scope", Select).value
        restore_workspace = panel.switch("restore-workspace").value
        restore_host = panel.switch("restore-host").value
        restore_native = panel.switch("restore-opencode").value
        if detected is not None:
            kind = "session" if detected == "session" else "full"
            archive_changed = panel.archive_selection_changed(
                archive_path, detected, expected_file
            ) or (restore_host and panel.archive_hint(archive_path) is None)
            if archive_changed:
                panel.reset_restore_options()
                restore_workspace = panel.switch("restore-workspace").value
                restore_host = restore_native = False
            panel.set_archive_hint(archive_path, detected, expected_file)
            if archive_changed and button == "backup-restore":
                self._message(
                    "The selected archive changed. Review it and choose the restore switches again."
                )
                return
        verb = {
            "backup-create": "backup",
            "backup-inspect": "inspect",
            "backup-restore": "restore",
        }[button]
        action = ("session-" if kind == "session" else "workspace-") + verb
        workspace = None
        origin = store = session = None
        host = portable = include_opencode = False
        auth_export = None
        if (
            verb == "restore"
            and restore_host
            and detected not in {"host", "workspace-host"}
        ):
            self._message(
                "This archive layout has no host settings. Review the restore switches and try again."
            )
            return
        if action in {"session-backup", "session-restore"}:
            origin, workspace, store = self._management_session("backup")
            if action == "session-backup":
                session = self._session()
        elif action == "workspace-backup":
            if scope == "host":
                raise tui_operations.OperationError("host-session-backups-only")
            include_workspace = panel.switch("include-workspace").value
            host = panel.switch("include-host").value
            if include_workspace:
                workspace = self._management_target("backup")
                portable = panel.switch("portable").value
            elif not host:
                self._message(
                    "Turn ON Include workspace data or Include host settings."
                )
                return
            if host and panel.switch("include-auth-export").value:
                auth_export = panel.query_one(
                    "#backup-auth-export-path", Input
                ).value.strip()
                if not auth_export:
                    self._message(
                        "Choose an existing private OpenCode auth export file or turn OFF Include native credential export."
                    )
                    return
        elif action == "workspace-restore":
            restore_workspace = restore_workspace and detected != "host"
            host = restore_host
            include_opencode = host and restore_native
            if not (restore_workspace or host):
                self._message(
                    "Turn ON Restore host settings for this host-only archive."
                    if detected == "host"
                    else "Turn ON Restore workspace or Restore host settings where available."
                )
                return
        identities = panel.query_one("#backup-identities", Input).value.strip()
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
        destination = (
            panel.query_one("#backup-destination", Input).value.strip() or None
        )
        if (
            action == "session-restore"
            and origin == "workspace"
            and destination is None
        ):
            data = tui_storage.read_workspace_action(
                self.backend.box, workspace, read_only=self.read_only, demo=self.demo
            ).data
            destination = self.backend.box.container_project(data)
        if action == "session-restore" and origin == "host" and destination is None:
            session_target = self._session()
            if session_target is None:
                raise tui_operations.OperationError("session-selection-required")
            destination = session_target["location"]["directory"]
        if action == "workspace-restore":
            if not restore_workspace:
                destination = None
            elif destination is None:
                selected_project = self._backup_default_project()
                base = (
                    Path(selected_project)
                    if selected_project
                    else tui_operations.real_path(archive_path).with_suffix("")
                )
                for count in range(100):
                    candidate = base.with_name(
                        base.name + "-restored" + (f"-{count}" if count else "")
                    )
                    if not candidate.exists() and not candidate.is_symlink():
                        destination = str(candidate)
                        break
                if destination is None:
                    raise tui_operations.OperationError("destination-required")
        if verb == "restore" and destination is not None:
            panel.query_one("#backup-destination", Input).value = destination
        self._management_controller.read_only = self.read_only
        plan = self._management_controller.prepare(
            action,
            workspace=workspace,
            backup=archive_path,
            destination=destination if verb == "restore" else None,
            portable=portable,
            host=host,
            include_opencode=include_opencode,
            auth_export=auth_export,
            recipients=panel.query_one("#backup-recipients", Input).value.split()
            if verb == "backup"
            else (),
            identities=identities if verb in {"restore", "inspect"} else (),
            origin=origin,
            store=store,
            session=session,
        )
        if (
            expected_file is not None
            and isinstance(plan, tui_operations.OperationPlan)
            and plan.request["bindings"]["backup_file"] != expected_file
        ):
            self._management_controller.cancel(plan)
            raise tui_operations.OperationError("selection-changed")

        def finish(phrase):
            if phrase is None:
                self._management_controller.cancel(plan)
                return
            try:
                self._management_controller.read_only = self.read_only
                request = self._management_controller.confirm(plan, phrase)
                self._perform_management(request)
            except Exception as error:
                self._operation_failure(error, operation="storage")

        self.push_screen(ActionReview(plan.review, plan.confirmation), finish)

    @work(thread=True, group="backup-format", exit_on_error=False)
    def _detect_backup(self, button, path, selection):
        try:
            location = tui_operations.real_path(path)
            identity = tui_operations.file_identity(location)
            detected = tui_operations.archive_kind(location, expected=identity)
        except Exception:
            identity = detected = None
        if self.is_running:
            self.call_from_thread(
                self._backup_detected, button, path, selection, identity, detected
            )

    def _backup_detected(self, button, path, selection, identity, detected):
        self._backup_preparing = False
        try:
            if selection != self._backup_detection_selection(button):
                raise tui_operations.OperationError("selection-changed")
            if detected not in ARCHIVE_LAYOUTS:
                panel = self.query_one(BackupPanel)
                if panel.archive_hint(path) is not None:
                    panel.reset_restore_options()
                    panel.set_archive_hint(None, None)
                raise tui_operations.OperationError("invalid-backup-file")
            self._review_backup(
                button, detected=detected, expected_path=path, expected_file=identity
            )
        except Exception as error:
            self._operation_failure(
                error,
                operation="inspection" if button == "backup-inspect" else "restore",
            )

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
        if request["action"] in tui_operations.ACTIONS:
            if __package__:
                from .tui_backup_workflow import BackupWorkflow, private_ui_allowed
                from .tui_operation_io import CoreDumpPolicyError
            else:
                from tui_backup_workflow import BackupWorkflow, private_ui_allowed
                from tui_operation_io import CoreDumpPolicyError

            self._maintenance_busy = True
            self._private_interaction = True
            self._management_last_completed = None
            try:
                private_ui_allowed(self)
                self._embedded_operation = BackupWorkflow(self.backend.box, request)
                self.push_screen(self._embedded_operation, self._embedded_finished)
            except Exception as error:
                self._maintenance_busy = self._private_interaction = False
                self._embedded_operation = None
                if isinstance(error, CoreDumpPolicyError):
                    self._operation_failure(error, operation=operation)
                else:
                    self._message(
                        "Private backup input is unavailable. Disable dashboard debug, developer tools, "
                        "and event logging; check private terminal support before retrying."
                    )
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

    def _embedded_finished(self, result):
        if self._embedded_operation is None:
            return  # a queued callback during final app teardown is not a new operation
        operation = tui_operations.operation_tag(
            self._embedded_operation.transport.request["action"]
        )
        self._embedded_operation = None
        self._private_interaction = self._maintenance_busy = False
        if result is not None and result["code"] == 0:
            self._management_last_completed = operation
            self._message(
                "Backup operation completed. Restored workspaces remain stopped; native session refresh remains explicit."
            )
        elif result is not None and result.get("notice"):
            if __package__:
                from .tui_notifications import FailureNotice
            else:
                from tui_notifications import FailureNotice

            self._show_failure_notice(FailureNotice(**result["notice"]))
        else:
            self._operation_failure(
                operation=operation, dispatched=True, uncertain=True
            )
        self._management_refresh()
        self.action_refresh()
