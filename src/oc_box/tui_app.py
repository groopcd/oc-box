"""Optional Textual presentation; domain operations live outside the widgets."""

from __future__ import annotations

import time
import threading
from copy import deepcopy
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.errors import NoWidget
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBar
from textual.theme import Theme
from textual.worker import get_current_worker
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
    Label,
    LoadingIndicator,
    RichLog,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

if __package__:
    from . import status_report
    from .tui_backend import plain, TerminalAttachmentError
    from .tui_logs import LogBuffer, LogEntry
    from .tui_sessions import FavoriteStore, SessionCatalog
    from .tui_notifications import failure_notice
    from .tui_terminal import NativeTerminal
    from .tui_clipboard import (
        ClipboardActions,
        ClipboardHelp,
        ClipboardInput,
        ClipboardFooter,
        SelectableDataTable,
        SelectableRichLog,
    )
    from .tui_status import (
        HostResourceCards,
        MonitorPanel,
        WorkspaceInspector,
        numeric,
        percentage,
    )
    from .tui_settings import (
        SettingsPanel,
        RoundedSwitch,
        REFRESH_INTERVALS,
        DEFAULT_SETTINGS,
    )
    from .tui_management import ManagementActions, StoragePanel, BackupPanel
else:
    import status_report
    from tui_backend import plain, TerminalAttachmentError
    from tui_logs import LogBuffer, LogEntry
    from tui_sessions import FavoriteStore, SessionCatalog
    from tui_notifications import failure_notice
    from tui_terminal import NativeTerminal
    from tui_clipboard import (
        ClipboardActions,
        ClipboardHelp,
        ClipboardInput,
        ClipboardFooter,
        SelectableDataTable,
        SelectableRichLog,
    )
    from tui_status import (
        HostResourceCards,
        MonitorPanel,
        WorkspaceInspector,
        numeric,
        percentage,
    )
    from tui_settings import (
        SettingsPanel,
        RoundedSwitch,
        REFRESH_INTERVALS,
        DEFAULT_SETTINGS,
    )
    from tui_management import ManagementActions, StoragePanel, BackupPanel


def text(value):
    return Text(plain(value))


class Confirm(ModalScreen):
    AUTO_FOCUS = "#cancel"
    CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 76; max-width: 95%; height: 80%; max-height: 40;
      padding: 1 2; border: round $warning; background: $surface; }
    Confirm Horizontal { height: 3; }
    #confirmation-review { height: 1fr; }
    #confirmation-error { height: auto; color: $warning; }
    """

    def __init__(self, message, *, destructive=False, operation="delete"):
        super().__init__()
        self.message, self.destructive = message, destructive
        self.operation = operation

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="confirmation-review"):
                yield Static(text(self.message))
            if self.destructive:
                yield ClipboardInput(
                    placeholder="Type DELETE to confirm", id="confirmation"
                )
                yield Static("", id="confirmation-error", markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Confirm",
                    id="confirm",
                    variant="error" if self.destructive else "primary",
                )

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "confirm":
            if (
                self.destructive
                and self.query_one("#confirmation", Input).value != "DELETE"
            ):
                notice = failure_notice(self.operation, reason="confirmation-required")
                self.query_one("#confirmation-error", Static).update(notice.message)
                self.app._show_failure_notice(notice)
                return
            self.dismiss(True)


class DashboardApp(ManagementActions, ClipboardActions, App):
    TITLE = "oc-box control center"
    AUTO_FOCUS = "#workspaces"
    SECTIONS = (
        ("Status", "status"),
        ("Settings", "settings"),
        ("Sessions", "sessions"),
        ("Terminals", "terminals"),
        ("Logs", "logs"),
        ("Storage", "storage"),
        ("Backups", "backups"),
    )
    CSS = """
    Screen { layout: vertical; }
    Button.-style-default, Input, SelectCurrent, SelectOverlay, Checkbox, TextArea {
      border: round $primary 35%; background: $surface; color: $text; }
    Button.-style-default { height: 3; min-width: 6; padding: 0 1; }
    Button.-style-default.-primary { color: $primary; background: $primary 12%; }
    Button.-style-default.-error { border: round $error 55%; color: $error; }
    Button.-style-default:hover { border: round $primary 65%; background: $primary 12%; }
    Button.-style-default.-active { border: round $primary 75%; background: $primary 18%; }
    Button.-style-default:focus, Input:focus, Select:focus > SelectCurrent,
    SelectOverlay:focus, Checkbox:focus, TextArea:focus {
      border: round $accent; background-tint: transparent; }
    Button.-style-default.-error:focus { border: round $error; }
    Button.-style-default:disabled { border: round $primary 18%; }
    #dashboard-header { height: 3; background: $surface; }
    #host-summary { width: 24; height: 1; margin: 1 1; text-style: bold;
      text-wrap: nowrap; text-overflow: ellipsis; }
    #view-title { width: 1fr; height: 1; margin: 1 1; color: $primary; }
    #workspace-picker { width: 38; margin-right: 1; }
    #section-picker { display: none; width: 19; margin-right: 1; }
    #monitoring-state { width: 15; height: 1; margin: 1 1; color: $success; }
    #log-tools { height: 3; }
    #log-search { width: 1fr; }
    #log-hits { width: 18; height: 3; padding: 1 1; }
    #log-tools Button { width: auto; }
    #body { height: 1fr; }
    #sidebar { width: 18; padding: 0 1; background: $surface; }
    #section-heading { height: 2; padding-top: 1; color: $text-muted; }
    .nav-choice.-style-default { width: 1fr; min-width: 0; height: 1; margin: 1 0 0 0;
       border: none; padding: 0 1; text-align: left; background: transparent;
       content-align: left middle; color: $text-muted; }
    .nav-choice.-style-default.-selected { color: $primary; background: $primary 20%; text-style: bold; }
    .nav-choice.-style-default:focus, .nav-choice.-style-default:hover,
    .nav-choice.-style-default.-active {
      border: none; color: $text; background: $primary 25%; }
    #nav-context { dock: bottom; height: auto; max-height: 7; margin-top: 1;
      border: round $primary 25%; padding: 0 1; color: $text-muted; }
    #workspaces { height: 1fr; }
    #content { width: 1fr; }
    #content, #content > ContentSwitcher, TabPane { height: 1fr; min-height: 0; }
    #content > Tabs { display: none; }
    #status-dashboard { padding: 0; }
    #system-report { margin-bottom: 1; }
    #overview-body { height: auto; }
    .card { border: round $primary 25%; background: $surface; padding: 0 1; }
    .card-title { height: 1; color: $primary; text-style: bold; }
    .card-caption { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #workspace-card { width: 1fr; height: 17; margin-right: 1; }
    #workspace-inspector { width: 40%; height: auto; padding: 0;
      border: none; background: $surface; }
    #workspace-inspector > CollapsibleTitle { display: none; }
    #workspace-inspector > Contents { padding: 0; }
    #workspace-source-context { height: auto; color: $text-muted; padding: 0 1; }
    #workspace-details { padding: 0; border: none; margin: 0; }
    #scan-storage { width: auto; min-width: 12; margin: 0 1; }
    #activity-card { height: 8; margin-top: 1; }
    #overview-activity { height: 4; text-wrap: nowrap; text-overflow: ellipsis; }
    #status-summary, #settings-info, #terminal-info { height: auto; padding: 1; }
    MonitorPanel { height: auto; width: 1fr; padding: 0 1;
      text-wrap: nowrap; text-overflow: clip; }
    #full-diagnostics .monitor--border { color: $warning; }
    #system-report .monitor--border { color: $success; }
    #session-table { height: 1fr; min-height: 5; }
    #session-progress { height: auto; min-height: 1; }
    #session-spinner { display: none; width: 5; height: 1; }
    #session-progress-text { width: 1fr; height: auto; }
    #session-details { height: auto; max-height: 7; }
    #log-output { height: 1fr; }
    #notice { height: auto; max-height: 3; color: $warning; }
    .toolbar { height: auto; min-height: 3; overflow-x: auto; }
    .toolbar Button { margin-right: 1; }
    .toolbar Button:last-child { margin-right: 0; }
    .toolbar Select { width: 24; }
    #session-source-tools Select { width: 1fr; min-width: 12; }
    #session-source-tools Button { width: auto; padding: 0 1; }
    .toolbar Label { width: auto; padding: 1; }
    .toolbar .setting-label { width: 28; padding: 1; }
    .toolbar SelectCurrent > #label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    RoundedSwitch { width: 8; height: 3; padding: 0; border: round $primary 35%;
      background: $surface; color: $text-muted; content-align: center middle; }
    RoundedSwitch.-on { color: $success; background: $success 12%; }
    RoundedSwitch:hover { border: round $primary 65%; }
    RoundedSwitch:focus { border: round $accent; background-tint: transparent; }
    .toolbar RoundedSwitch { margin-right: 1; }
    NativeTerminal { height: 1fr; min-height: 5; border: round $secondary; }
    TabPane { padding: 0 1; overflow-y: auto; }
    Screen.compact #sidebar, Screen.compact #view-title,
    Screen.compact #monitoring-state { display: none; }
    Screen.compact #section-picker { display: block; }
    Screen.compact #dashboard-header { height: 1; }
    Screen.compact #host-summary { width: 17; min-width: 17; margin: 0 1; }
    Screen.compact #workspace-picker { width: 1fr; height: 1; }
    Screen.compact #section-picker { height: 1; }
    Screen.compact #dashboard-header Select > SelectCurrent {
      height: 1; border: none; padding: 0 1; }
    Screen.compact #log-tools { height: 1; }
    Screen.compact #log-search { height: 1; border: none; padding: 0 2 0 1; }
    Screen.compact #log-hits { height: 1; padding: 0 1; }
    Screen.compact #log-tools Button { height: 1; border: none; padding: 0 1; }
    Screen.compact #overview-body { layout: vertical; }
    Screen.compact #workspace-card { width: 1fr; height: 11; margin-right: 0; }
    Screen.compact #workspace-inspector { width: 1fr; }
    Screen.compact #workspace-inspector > CollapsibleTitle { display: block; }
    Screen.compact #activity-card { display: none; }
    """
    BINDINGS = [
        Binding("f1", "clipboard_help", "Clipboard help", priority=True),
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("ctrl+l", "logs", "Search logs"),
        ("s", "settings", "Settings"),
        Binding("f2", "settings", show=False),
        Binding("ctrl+shift+c", "copy_selection", show=False, priority=True),
        Binding("ctrl+shift+y", "copy_view", show=False, priority=True),
        Binding("ctrl+shift+insert", "paste_copied_text", show=False, priority=True),
        ("1", "tab('status')", "Status"),
        ("2", "tab('settings')", "Settings"),
        ("3", "tab('sessions')", "Sessions"),
        ("4", "tab('terminals')", "Terminals"),
        ("5", "tab('logs')", "Logs"),
        ("6", "tab('storage')", "Storage"),
        ("7", "tab('backups')", "Backup/restore"),
    ]

    def __init__(self, backend, *, read_only=False, selected=None, demo=False):
        super().__init__()
        self.register_theme(
            Theme(
                name="oc-box-navy",
                primary="#57d6e5",
                secondary="#65d996",
                accent="#57d6e5",
                foreground="#d9e7ee",
                background="#060d14",
                surface="#0b1720",
                panel="#0b1720",
                warning="#edb94e",
                error="#e57480",
                success="#65d996",
                dark=True,
            )
        )
        self.theme = DEFAULT_SETTINGS["theme"]
        self.backend, self.read_only, self.selected, self.demo = (
            backend,
            read_only,
            selected,
            demo,
        )
        self.report = {"workspaces": [], "host": {}}
        self.catalog = None
        self._session_connection = None
        self._associations = []
        self.sessions, self.visible_sessions = [], []
        self.logs = LogBuffer(max_entries=2000)
        self.log_query = ""
        self.podman_workspace = None
        self.favorites = (
            FavoriteStore(backend.box.state_root() / "ui") if backend.box else None
        )
        self._refreshing = self._logging = self._session_busy = self._terminal_busy = (
            False
        )
        self._last_report = self._last_logs = 0
        self._rendered_logs = None
        self._log_snapshot = None
        self._log_revision = 0
        self._log_render_key = None
        self._log_search_snapshot = None
        self._filter_timers = {}
        self._filter_generations = {}
        self._table_snapshots = {}
        self._table_view_generations = {}
        self._table_scroll_generations = {}
        self._text_snapshots = {}
        self._monitor_snapshots = {}
        self._session_favorites = set()
        self._favorites_busy = False
        self._picker_options = None
        self._activity_revision = -1
        self._compact_layout = None
        self._catalog_generation = 0
        self.cpu_history = []
        self.ram_history = []
        self._terminal_widget = None
        self._terminal_target = None
        self._status_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._storage_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_cancel = None
        self._pending_connection = None
        self._pending_generation = None
        self._session_source = ""
        self._poll_timer = None
        self._monitoring_settings = {
            key: DEFAULT_SETTINGS[key] for key in ("auto_refresh", "interval")
        }
        self._management_init()

    def compose(self) -> ComposeResult:
        with Horizontal(id="dashboard-header"):
            yield Static("oc-box", id="host-summary", markup=False)
            yield Static("Status / Overview", id="view-title", markup=False)
            yield Select(
                [
                    (f"{i} {label}", key)
                    for i, (label, key) in enumerate(self.SECTIONS, 1)
                ],
                value="status",
                allow_blank=False,
                id="section-picker",
            )
            yield Select(
                [("Loading workspaces…", "__none__")],
                value="__none__",
                allow_blank=False,
                id="workspace-picker",
                disabled=True,
            )
            yield Static("Auto · 1s", id="monitoring-state", markup=False)
        with Horizontal(id="log-tools"):
            yield ClipboardInput(
                placeholder="Search retained logs at any time · Ctrl+L opens Logs",
                id="log-search",
            )
            log_hits = Static("0 log entries", id="log-hits", markup=False)
            log_hits.tooltip = (
                "Retained log entries in the selected Logs source, not sessions "
                "or workspaces. Searching shows matching entries; clearing the "
                "search shows all retained entries, without deleting logs. "
                "The count follows the current filter even while selected "
                "output is held still for copying."
            )
            yield log_hits
            yield Button("Logs", id="show-logs")
            yield Button("Refresh", id="refresh")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("SECTIONS", id="section-heading", markup=False)
                for i, (label, key) in enumerate(self.SECTIONS, 1):
                    yield Button(f"{i} {label}", id=f"nav-{key}", classes="nav-choice")
                yield Static("No workspace selected.", id="nav-context", markup=False)
            with TabbedContent(id="content", initial="status"):
                with TabPane("Status", id="status"):
                    with VerticalScroll(id="status-dashboard"):
                        yield HostResourceCards("system", id="system-report")
                        with Horizontal(id="overview-body"):
                            with Vertical(id="workspace-card", classes="card"):
                                yield Static(
                                    "WORKSPACES · context only", classes="card-title"
                                )
                                yield SelectableDataTable(
                                    id="workspaces",
                                    cursor_type="row",
                                    zebra_stripes=True,
                                )
                                yield Static(
                                    "CPU: 100%=1 core · storage sizes require a scan",
                                    id="inventory-notice",
                                    classes="card-caption",
                                    markup=False,
                                )
                            with Collapsible(
                                title="Selected workspace",
                                collapsed=False,
                                id="workspace-inspector",
                            ):
                                yield WorkspaceInspector(
                                    "workspace", id="full-diagnostics"
                                )
                                yield Static(
                                    "Native sessions: not connected.",
                                    id="workspace-source-context",
                                    markup=False,
                                )
                                with Collapsible(
                                    title="Workspace details and mounts",
                                    collapsed=not DEFAULT_SETTINGS["details"],
                                    id="workspace-details",
                                ):
                                    yield Static(
                                        "Select a workspace",
                                        id="status-summary",
                                        markup=False,
                                    )
                                yield Button("Scan sizes", id="scan-storage")
                        with Vertical(id="activity-card", classes="card"):
                            yield Static("RETAINED ACTIVITY", classes="card-title")
                            yield Static(
                                "No retained oc-box activity.",
                                id="overview-activity",
                                markup=False,
                            )
                            yield Static(
                                "Fixed categories only · external logs require explicit Load",
                                classes="card-caption",
                                markup=False,
                            )
                with TabPane("Settings", id="settings"):
                    yield SettingsPanel(
                        DEFAULT_SETTINGS,
                        sorted(self.available_themes),
                        read_only=self.read_only,
                        id="settings-panel",
                    )
                with TabPane("Sessions", id="sessions"):
                    with Horizontal(id="session-source-tools", classes="toolbar"):
                        yield Select(
                            [
                                ("Host · all projects", "global"),
                                ("Host · selected project", "project"),
                                ("Live workspace sources", "workspace"),
                            ],
                            value=DEFAULT_SETTINGS["session_scope"],
                            allow_blank=False,
                            id="session-scope",
                        )
                        yield Button("Connect / refresh", id="connect-sessions")
                        yield Button("Disconnect", id="disconnect-sessions")
                    with Horizontal(id="session-progress"):
                        yield LoadingIndicator(id="session-spinner")
                        yield Static(
                            "Not connected. Choose a source and Connect to load its inventory.",
                            id="session-progress-text",
                            markup=False,
                        )
                    yield ClipboardInput(
                        placeholder="Filter title, ID or native directory",
                        id="session-search",
                    )
                    yield SelectableDataTable(
                        id="session-table", cursor_type="row", zebra_stripes=True
                    )
                    yield Static(
                        "Not connected. Host connect may start the native host OpenCode service. "
                        "Workspace connect only attaches to an existing owner.",
                        id="session-details",
                        markup=False,
                    )
                    with Horizontal(classes="toolbar"):
                        yield Button("Favorite", id="favorite", disabled=self.read_only)
                        yield Button(
                            "Archive / restore", id="archive", disabled=self.read_only
                        )
                        yield Button(
                            "Delete…",
                            id="delete",
                            variant="error",
                            disabled=self.read_only,
                        )
                with TabPane("Terminals", id="terminals"):
                    yield Static(
                        "Explicit connection only. A connection may create a native session shell "
                        "and take interactive control. Ctrl-] detaches; the shell remains running.",
                        id="terminal-info",
                        markup=False,
                    )
                    with Horizontal(classes="toolbar"):
                        yield Button(
                            "Connect owner session…",
                            id="terminal-owner",
                            disabled=self.read_only,
                        )
                        yield Button(
                            "Connect selected source…",
                            id="terminal-selected",
                            disabled=self.read_only,
                        )
                        yield Button("Detach", id="terminal-disconnect")
                    self._terminal_widget = NativeTerminal(id="native-terminal")
                    yield self._terminal_widget
                with TabPane("Logs", id="logs"):
                    with Horizontal(classes="toolbar"):
                        yield Select(
                            [
                                ("All sources", "all"),
                                ("oc-box activity", "oc-box"),
                                ("Selected Podman container", "podman"),
                            ],
                            value=DEFAULT_SETTINGS["log_source"],
                            allow_blank=False,
                            id="log-source",
                        )
                        yield Label("Follow")
                        yield RoundedSwitch(
                            value=DEFAULT_SETTINGS["log_follow"], id="log-follow"
                        )
                        yield Button("Load Podman logs…", id="load-podman")
                    yield Static(
                        "Generated oc-box events contain fixed categories only. External container logs "
                        "may contain private data: redaction is best-effort, and they are never copied to disk.",
                        markup=False,
                    )
                    yield Static(
                        "No external container log source selected.",
                        id="log-target",
                        markup=False,
                    )
                    yield SelectableRichLog(
                        id="log-output",
                        max_lines=2000,
                        wrap=True,
                        markup=False,
                        highlight=False,
                    )
                with TabPane("Storage", id="storage"):
                    yield StoragePanel(read_only=self.read_only, demo=self.demo)
                with TabPane("Backup/restore", id="backups"):
                    yield BackupPanel(read_only=self.read_only, demo=self.demo)
        yield Static(
            "Synthetic demo · fixed sample readings · no host data"
            if self.demo
            else "Browsing never starts or stops a workspace.",
            id="notice",
            markup=False,
        )
        yield ClipboardFooter()

    def on_mount(self):
        table = self.query_one("#workspaces", DataTable)
        for label, width in (
            ("Project", 18),
            ("State", 8),
            ("CPU", 7),
            ("RAM", 10),
            ("Home size", 10),
        ):
            table.add_column(label, width=width)
        self.query_one("#session-table", DataTable).add_columns(
            "★", "ID", "Title", "Kind", "Native directory"
        )
        self._poll_timer = self.set_interval(0.25, self._tick)
        self._management_mount()
        self._refresh_favorites()
        self._sync_navigation("status")
        self._responsive_layout(self.size.width, self.size.height)
        self.action_refresh()

    def _message(self, value):
        self.query_one("#notice", Static).update(plain(value))

    def _show_failure_notice(self, notice):
        if not self.is_running or not self.is_mounted:
            return
        self._message(f"{notice.title}: {notice.message}")
        self.notify(
            notice.message,
            title=notice.title,
            severity=notice.severity,
            timeout=notice.timeout,
            markup=False,
        )

    def _notify_failure(self, operation, error=None, **context):
        return self._operation_failure(error, operation=operation, **context)

    def _operation_failure(
        self,
        error=None,
        *,
        operation="delete",
        reason=None,
        dispatched=False,
        completed=False,
        count=None,
        uncertain=False,
    ):
        """UI-thread presentation of fixed copy; never render error text."""
        notice = failure_notice(
            operation,
            error,
            reason=reason,
            dispatched=dispatched,
            completed=completed,
            count=count,
            uncertain=uncertain,
        )
        self._show_failure_notice(notice)
        return notice

    def _session_action_reason(self, catalog, selected):
        if self.read_only:
            return "read-only"
        if self.demo:
            return "demo"
        if not catalog or not selected:
            return "selection-required"
        if catalog is not self.catalog:
            return "source-changed"
        if self._session_busy:
            return "busy"
        return None

    def _journal(self, event, catalog=None):
        if not self.backend.box or self.demo:
            return
        try:
            if __package__:
                from .activity_log import record_event
            else:
                from activity_log import record_event
            workspace = (
                catalog.store if catalog and catalog.origin == "workspace" else None
            )
            # The journal must open the raw state path without following links.
            record_event(
                event,
                command="tui",
                workspace=workspace,
                outcome="success",
            )
        except Exception:
            pass

    def action_tab(self, tab):
        self.query_one("#content", TabbedContent).active = tab
        self._sync_navigation(tab)

    def _sync_navigation(self, tab):
        labels = dict((key, label) for label, key in self.SECTIONS)
        if tab not in labels:
            return
        for _, key in self.SECTIONS:
            # "-active" belongs to Button's transient press-feedback timer.
            self.query_one(f"#nav-{key}", Button).set_class(key == tab, "-selected")
        picker = self.query_one("#section-picker", Select)
        if picker.value != tab:
            with picker.prevent(Select.Changed):
                picker.value = tab
        self._update_text(
            "#view-title", "Status / Overview" if tab == "status" else labels[tab]
        )
        if tab == "settings":
            panel = self.query_one(SettingsPanel)
            if not panel.dirty:
                panel.load_values(self._settings_snapshot())

    def on_tabbed_content_tab_activated(self, event):
        if event.tabbed_content.id == "content" and event.tabbed_content.is_attached:
            self._sync_navigation(event.tabbed_content.active)

    def on_resize(self, event):
        if self.is_mounted and self.query("#body"):
            self._responsive_layout(event.size.width, event.size.height)

    def _responsive_layout(self, width, height):
        compact = width < 118 or height < 32
        if compact == self._compact_layout:
            return
        self._compact_layout = compact
        self.query_one("#body").screen.set_class(compact, "compact")
        self.query_one("#workspace-inspector", Collapsible).collapsed = compact

    def _sync_workspace_picker(self):
        choices = tuple(
            (
                item["workspace_id"],
                Path(item["project"]).name or item["project"],
            )
            for item in self.report["workspaces"]
        )
        picker = self.query_one("#workspace-picker", Select)
        with picker.prevent(Select.Changed):
            if choices != self._picker_options:
                picker.set_options(
                    [(text(label), ident) for ident, label in choices]
                    or [("No workspaces", "__none__")]
                )
                self._picker_options = choices
            picker.disabled = not choices
            selected = self.selected if choices else "__none__"
            if picker.value != selected:
                picker.value = selected

    def _render_context(self):
        item = self._workspace()
        context = "No workspace selected."
        if item:
            context = (
                f"{plain(Path(item['project']).name)}\n"
                f"{plain(item.get('state', 'n/a'))}\n"
                f"{plain(item.get('distro', 'n/a'))}"
            )
        self._update_text("#nav-context", context)
        source = "Native sessions: not connected."
        if self.catalog:
            source = f"Sessions source: {plain(self.catalog.origin)}" + (
                f" · {plain(self.catalog.store, 64)} · client lease held"
                if self.catalog.origin == "workspace"
                else " · process-local checks"
            )
        self._update_text("#workspace-source-context", source)
        self._update_text(
            "#host-summary",
            "oc-box READ ONLY"
            if self.read_only
            else "oc-box DEMO"
            if self.demo
            else "oc-box",
        )
        self._render_monitoring_state()

    def _render_monitoring_state(self):
        interval = self._monitoring_settings["interval"]
        auto = self._monitoring_settings["auto_refresh"]
        self._update_text(
            "#monitoring-state", f"{'Auto' if auto else 'Paused'} · {interval}s"
        )

    def _choose_workspace(self, target):
        if target == self.selected:
            return
        rows = self.report["workspaces"]
        if target not in {row["workspace_id"] for row in rows}:
            return
        self.selected = target
        self._session_source_changed()
        table = self.query_one("#workspaces", DataTable)
        index = next(i for i, row in enumerate(rows) if row["workspace_id"] == target)
        table.move_cursor(row=index, scroll=True)
        self._sync_workspace_picker()
        self._render_workspace()
        self._render_sessions()

    def action_logs(self):
        self.action_tab("logs")
        self.query_one("#log-search", Input).focus()

    def action_clipboard_help(self):
        if self._clipboard_private():
            return
        if isinstance(self.screen, ClipboardHelp):
            self.pop_screen()
        else:
            self.push_screen(ClipboardHelp())

    def action_settings(self):
        if self._clipboard_private() or isinstance(self.screen, ModalScreen):
            return
        self.action_tab("settings")
        panel = self.query_one(SettingsPanel)
        if self.focused not in panel.walk_children():
            panel.query_one("#pref-auto-refresh", Switch).focus()

    def _settings_snapshot(self):
        return {
            **self._monitoring_settings,
            "theme": self.theme,
            "details": not self.query_one("#workspace-details", Collapsible).collapsed,
            "session_scope": self.query_one("#session-scope", Select).value,
            "session_filter": self.query_one("#session-search", Input).value,
            "log_follow": self.query_one("#log-follow", Switch).value,
            "log_source": self.query_one("#log-source", Select).value,
            "log_query": self.query_one("#log-search", Input).value,
        }

    def on_settings_panel_apply_requested(self, event):
        event.stop()
        if not self._admit_settings_request(event) or self._clipboard_private():
            return
        self.apply_settings(event.values)

    def on_settings_panel_revert_requested(self, event):
        event.stop()
        if not self._admit_settings_request(event) or self._clipboard_private():
            return
        event.panel.load_values(self._settings_snapshot())

    def _admit_settings_request(self, event):
        panel = event.panel
        if (
            not self.is_running
            or not isinstance(panel, SettingsPanel)
            or not panel.is_attached
            or not self.is_mounted(panel)
        ):
            return False
        panels = self.query("#settings-panel")
        content = self.query("#content")
        return (
            len(panels) == 1
            and panels[0] is panel
            and len(content) == 1
            and self.is_mounted(content[0])
        )

    def apply_settings(self, values):
        if values is None:
            return
        valid = (
            isinstance(values, dict)
            and type(values.get("interval")) is int
            and values["interval"] in REFRESH_INTERVALS
            and isinstance(values.get("theme"), str)
            and values.get("theme") in self.available_themes
            and values.get("session_scope") in ("global", "project", "workspace")
            and values.get("log_source") in ("all", "oc-box", "podman")
            and all(
                type(values.get(key)) is bool
                for key in ("auto_refresh", "details", "log_follow")
            )
            and all(
                isinstance(values.get(key), str)
                for key in ("session_filter", "log_query")
            )
        )
        if not valid:
            self._message("Unsupported dashboard setting; no changes applied.")
            return
        self.theme = values["theme"]
        self._monitoring_settings.update(
            auto_refresh=values["auto_refresh"], interval=values["interval"]
        )
        self.query_one("#workspace-details", Collapsible).collapsed = not values[
            "details"
        ]
        if values["details"]:
            self.query_one("#workspace-inspector", Collapsible).collapsed = False
        self.query_one("#session-scope", Select).value = values["session_scope"]
        self.query_one("#session-search", Input).value = values["session_filter"]
        self.query_one("#log-follow", Switch).value = values["log_follow"]
        self.query_one("#log-source", Select).value = values["log_source"]
        self.query_one("#log-search", Input).value = values["log_query"]
        self.query_one(SettingsPanel).load_values(self._settings_snapshot())
        self._render_monitoring_state()
        self._message(
            "Dashboard settings applied for this session. Workspace limits and OpenCode configuration are unchanged."
        )

    def _tick(self):
        if not self.is_running or not self.is_mounted or self._maintenance_busy:
            return
        try:
            interval = self._monitoring_settings["interval"]
            auto = self._monitoring_settings["auto_refresh"]
            follow = self.query_one("#log-follow", Switch).value
        except NoMatches:
            # A timer callback can already be queued while child widgets are
            # unmounting. Shutdown must not turn it into a UI failure.
            return
        if (
            auto
            and not self._status_lock.locked()
            and time.monotonic() - self._last_report >= interval
        ):
            self.refresh_report()
        if (
            follow
            and not self._log_lock.locked()
            and time.monotonic() - self._last_logs >= interval
        ):
            self.refresh_logs()

    def action_refresh(self):
        self.refresh_report()
        self.refresh_logs()
        if (
            self.catalog
            and self.query_one("#content", TabbedContent).active == "sessions"
        ):
            self.connect_sessions(
                self.query_one("#session-scope", Select).value, self.selected
            )

    @work(thread=True, group="status", exit_on_error=False)
    def refresh_report(self, scan_storage=False):
        if not self._status_lock.acquire(blocking=False):
            return
        self._refreshing = True
        self._last_report = time.monotonic()
        try:
            report = self.backend.collect(scan_storage=scan_storage)
            if self.is_running:
                self.call_from_thread(self._apply_report, report, scan_storage)
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Status refresh unavailable; previous readings are stale. No recovery was attempted.",
                )
        finally:
            self._refreshing = False
            self._status_lock.release()

    def _apply_report(self, report, scanned):
        if not scanned:
            previous = {w["workspace_id"]: w for w in self.report["workspaces"]}
            for item in report["workspaces"]:
                old = previous.get(item["workspace_id"], {})
                if old.get("container_id") == item.get("container_id"):
                    for key in (
                        "project_bytes",
                        "private_home_bytes",
                        "writable_layer_bytes",
                        "rootfs_bytes",
                    ):
                        if item["storage"].get(key) is None:
                            item["storage"][key] = old.get("storage", {}).get(key)
                    item["storage_sampled_at"] = old.get("storage_sampled_at")
        self.report = report
        workspaces = report["workspaces"]
        skipped = report.get("inventory_skipped")
        self._update_text(
            "#inventory-notice",
            f"{skipped} invalid registrations skipped · sizes are manual"
            if skipped
            else "CPU: 100%=1 core · storage sizes require a scan",
        )
        if self.selected not in {w["workspace_id"] for w in workspaces}:
            selected = workspaces[0]["workspace_id"] if workspaces else None
            if selected != self.selected:
                self._session_source_changed()
            self.selected = selected
        self._sync_workspace_picker()
        self._update_table(
            "#workspaces",
            tuple(
                (
                    item["workspace_id"],
                    (
                        Path(item["project"]).name or item["project"],
                        item["state"],
                        self._workspace_cpu(item),
                        self._display_bytes(
                            (item.get("metrics") or {}).get("memory_bytes")
                            if item.get("state") == "running"
                            else None
                        ),
                        self._display_bytes(
                            (item.get("storage") or {}).get("private_home_bytes")
                        ),
                    ),
                )
                for item in workspaces
            ),
            selected=self.selected,
        )
        host = report.get("host", {})
        cpu = host.get("cpu_percent")
        self.cpu_history = (self.cpu_history + [cpu])[-60:]
        self.ram_history = (
            self.ram_history
            + [percentage(host.get("memory_bytes"), host.get("memory_total_bytes"))]
        )[-60:]
        overview = {
            **host,
            "workspace_counts": {
                "running": sum(item.get("state") == "running" for item in workspaces),
                "registered": len(workspaces),
            },
        }
        self._update_monitor(
            "#system-report", overview, self.cpu_history, self.ram_history
        )
        self._render_workspace()

    @staticmethod
    def _workspace_cpu(item):
        value = numeric((item.get("metrics") or {}).get("cpu_percent"))
        if value is None or item.get("state") != "running":
            return "n/a"
        return f"{value:.1f}%"

    @staticmethod
    def _display_bytes(value):
        return status_report.byte_size(value) if numeric(value) is not None else "n/a"

    def _table_text(self, selector, index, value):
        cell = text(value)
        if selector == "#workspaces" and index == 1:
            cell.stylize(
                (self.current_theme.success or "#65d996")
                if value == "running"
                else "dim"
            )
        return cell

    def _update_text(self, selector, value, *, layout=True):
        if self._text_snapshots.get(selector) != value:
            self.query_one(selector, Static).update(value, layout=layout)
            self._text_snapshots[selector] = value

    def _update_monitor(self, selector, snapshot, cpu_history=(), ram_history=()):
        state = (snapshot, tuple(cpu_history), tuple(ram_history))
        if self._monitor_snapshots.get(selector) != state:
            self.query_one(selector, MonitorPanel).update_snapshot(*state)
            # Storage scans update an existing report in place.
            self._monitor_snapshots[selector] = deepcopy(state)

    def _update_table(self, selector, rows, *, selected=None, source=None):
        """Only changed cells touch Textual; structural changes retain the view.

        A source switch is a new selection namespace even when native IDs match.
        No table selection is copied or read here: redraw is not a user gesture.
        """
        had_view = selector in self._table_snapshots
        previous_source, previous = self._table_snapshots.get(selector, (None, ()))
        if previous_source == source and previous == rows:
            return False
        generation = self._table_view_generations.get(selector, 0) + 1
        self._table_view_generations[selector] = generation
        table = self.query_one(selector, DataTable)
        # A first/empty view has no paused viewport to restore over the initial
        # selected row. None is also the workspace table's real source identity.
        same_source = had_view and bool(previous) and previous_source == source
        old_ids = tuple(key for key, _ in previous)
        new_ids = tuple(key for key, _ in rows)
        if same_source and old_ids == new_ids:
            columns = tuple(table.columns)
            for (key, old), (_, new) in zip(previous, rows):
                for index, (column, before, after) in enumerate(zip(columns, old, new)):
                    if before != after:
                        table.update_cell(
                            key,
                            column,
                            self._table_text(selector, index, after),
                            update_width=selector != "#workspaces",
                        )
        else:
            scroll = table.scroll_offset
            cursor = table.cursor_row
            if selected is None and same_source and 0 <= cursor < len(previous):
                selected = previous[cursor][0]
            table.clear()
            for key, cells in rows:
                table.add_row(
                    *(
                        self._table_text(selector, i, cell)
                        for i, cell in enumerate(cells)
                    ),
                    key=key,
                )
            if new_ids:
                row = (
                    new_ids.index(selected)
                    if selected in new_ids
                    else min(cursor, len(new_ids) - 1)
                    if same_source
                    else 0
                )
                table.move_cursor(row=row, scroll=not same_source)
            if same_source:
                table.scroll_to(x=scroll.x, y=scroll.y, animate=False, force=True)
        self._table_snapshots[selector] = (source, rows)
        if same_source and old_ids != new_ids:
            # DataTable's coordinate watcher queues its own cursor-into-view
            # after rebuilding dimensions, even with move_cursor(scroll=False).
            # Restore after that callback, but never over a newer view or user
            # scroll. Offsets alone cannot distinguish that automatic scrolling
            # from a wheel/scrollbar gesture with an unchanged cursor row.
            table.call_after_refresh(
                self._restore_table_scroll,
                selector,
                generation,
                self._table_scroll_generations.get(selector, 0),
                table.cursor_row,
                scroll,
            )
        return True

    def _restore_table_scroll(
        self, selector, generation, scroll_generation, cursor, scroll
    ):
        if (
            not self.is_running
            or not self.is_mounted
            or self._table_view_generations.get(selector) != generation
            or self._table_scroll_generations.get(selector, 0) != scroll_generation
        ):
            return
        table = self.query_one(selector, DataTable)
        if table.cursor_row == cursor:
            table.scroll_to(x=scroll.x, y=scroll.y, animate=False, force=True)

    async def on_event(self, event):
        # Observe original input before routing: scrollbars stop mouse events,
        # and a table's coordinate watcher also scrolls without user input.
        # Keep the normal clipboard/selection event chain entirely intact.
        if not event.is_forwarded and self.is_running and self.is_mounted:
            self._track_table_scroll_intent(event)
        await super().on_event(event)

    def _track_table_scroll_intent(self, event):
        widget = None
        if isinstance(event, events.Key):
            if event.key.rsplit("+", 1)[-1] not in (
                "up",
                "down",
                "left",
                "right",
                "home",
                "end",
                "pageup",
                "pagedown",
            ):
                return
            widget = self.focused
        elif isinstance(event, (events.MouseMove, events.MouseUp)):
            # Capture remains with the scrollbar when a drag leaves its cells.
            if isinstance(self.mouse_captured, ScrollBar):
                widget = self.mouse_captured
        elif isinstance(
            event,
            (
                events.MouseScrollUp,
                events.MouseScrollDown,
                events.MouseScrollLeft,
                events.MouseScrollRight,
                events.MouseDown,
            ),
        ):
            try:
                widget, _ = self.screen.get_widget_at(*event.screen_offset)
            except NoWidget:
                return
            if isinstance(event, events.MouseDown) and (
                event.button != 1 or not isinstance(widget, ScrollBar)
            ):
                return
        if widget is None or not widget.is_attached or widget.screen is not self.screen:
            return
        while widget is not None:
            if isinstance(widget, DataTable) and widget.id in (
                "workspaces",
                "session-table",
            ):
                selector = "#" + widget.id
                self._table_scroll_generations[selector] = (
                    self._table_scroll_generations.get(selector, 0) + 1
                )
                return
            widget = widget.parent

    def _workspace(self):
        return next(
            (
                w
                for w in self.report["workspaces"]
                if w["workspace_id"] == self.selected
            ),
            None,
        )

    def _render_workspace(self):
        self._render_management()
        item = self._workspace()
        self._render_context()
        self._update_monitor("#full-diagnostics", item)
        if not item:
            self._update_text(
                "#status-summary",
                "No registered workspaces. Host monitoring and logs remain available.",
            )
            self._update_text(
                "#settings-info",
                "Select a registered workspace to inspect its resource settings.",
            )
            return
        size = status_report.byte_size
        metrics, storage = item.get("metrics") or {}, item.get("storage") or {}
        cpu = metrics.get("cpu_percent")
        mounts = (
            "\n".join(
                f"  {plain(m.get('source'))} → {plain(m.get('destination'))} "
                f"({'rw' if m.get('writable') else 'ro'}, {plain(m.get('type'))})"
                for m in item.get("mounts", [])
            )
            or "  unavailable"
        )
        values = [
            f"Project: {plain(item['project'])}",
            f"Workspace: {item['workspace_id']}",
            f"State: {plain(item['state'])} · identity: {plain(item.get('project_status'))}",
            f"Distro: {plain(item.get('distro'))} · image: {plain(item.get('image'))}",
            f"Container: {plain(item.get('container_name'))}",
            f"Internal project: {plain(item.get('container_project'))}",
            f"CPU: {'n/a' if cpu is None else f'{cpu:.1f}%'} (100% = one CPU)",
            f"RAM: {size(metrics.get('memory_bytes'))} · processes: {metrics.get('pids', 'n/a')}",
            f"Project allocated size: {size(storage.get('project_bytes'))}",
            f"Private home: {size(storage.get('private_home_bytes'))}",
            f"Writable OS layer: {size(storage.get('writable_layer_bytes'))}",
            "Size scans are manual allocated-block estimates, not exclusive physical accounting.",
            f"Storage scan completed: {plain(item.get('storage_sampled_at') or 'not scanned')}",
            f"Recorded sessions: {item.get('recorded_sessions', 0)} · selected: {plain(item.get('session_id'))}",
            f"Mounts:\n{mounts}",
            *[plain(note) for note in item.get("notes", [])],
            f"Sample: {plain(self.report.get('sampled_at'))}",
        ]
        self._update_text("#status-summary", "\n".join(values))
        configured, effective = (
            item.get("configured_limits", {}),
            item.get("effective_limits"),
        )
        effective_label = "not observed"
        if effective is not None:
            cpu, ram, pids = (
                effective.get(key) for key in ("cpu_cores", "memory_bytes", "pids")
            )
            effective_label = (
                f"CPU {'unlimited' if cpu == 0 else cpu if cpu is not None else 'n/a'} · "
                f"RAM {'unlimited' if ram == 0 else size(ram)} · "
                f"PID {'unlimited' if pids == 0 else pids if pids is not None else 'n/a'}"
            )
        self._update_text(
            "#settings-info",
            f"Project: {plain(item['project'])}\nWorkspace {item['workspace_id']}\n"
            f"Saved: CPU {configured.get('cpu_cores')} · RAM {size(configured.get('memory_bytes'))} · PID {configured.get('pids')}\n"
            f"Effective: {effective_label}\n"
            "A CPU quota is not CPU affinity; a memory ceiling does not reserve RAM.\n"
            "No enforced workspace disk quota is configured by oc-box.",
        )

    def on_data_table_row_selected(self, event):
        if not self.is_running or not event.data_table.is_attached:
            return
        if event.data_table.id == "workspaces":
            target = str(event.row_key.value)
            self._choose_workspace(target)
        elif event.data_table.id == "session-table":
            self._session_details()

    def on_data_table_row_highlighted(self, event):
        if not self.is_running or not event.data_table.is_attached:
            return
        if event.data_table.id == "workspaces":
            # Ignore queued highlights from a table rebuild, not user movement.
            if event.cursor_row != event.data_table.cursor_row:
                return
            rows = self.report["workspaces"]
            if not 0 <= event.cursor_row < len(rows):
                return
            target = str(event.row_key.value)
            if rows[event.cursor_row]["workspace_id"] != target:
                return
            if target != self.selected:
                self._choose_workspace(target)
        elif event.data_table.id == "session-table":
            self._session_details()

    def on_input_changed(self, event):
        if event.input.id == "log-search":
            self.log_query = event.value
            self._schedule_filter("logs")
        elif event.input.id == "session-search":
            self._schedule_filter("sessions")

    def _schedule_filter(self, kind):
        # Input keeps its native immediate editing/caret behavior. Only the
        # potentially large metadata/log view waits for a short typing pause.
        generation = self._filter_generations.get(kind, 0) + 1
        self._filter_generations[kind] = generation
        previous = self._filter_timers.pop(kind, None)
        if previous:
            previous.stop()
        self._filter_timers[kind] = self.set_timer(
            0.12, lambda: self._finish_filter(kind, generation)
        )

    def _finish_filter(self, kind, generation):
        # Timer.stop cannot retract an already queued callback.
        if (
            generation != self._filter_generations.get(kind)
            or not self.is_running
            or not self.is_mounted
        ):
            return
        self._filter_timers.pop(kind, None)
        if kind == "logs":
            self._render_logs()
        else:
            self._render_sessions()

    def on_select_changed(self, event):
        if event.select.id in {"workspace-picker", "section-picker"}:
            # Queued picker events must still belong to the mounted dashboard,
            # even when a private modal covers its root screen.
            if (
                not self.is_running
                or not event.select.is_attached
                or not self.is_mounted(event.select)
                or event.value != event.select.value
            ):
                return
            pickers = self.query(f"#{event.select.id}")
            content = self.query("#content")
            if (
                len(pickers) != 1
                or pickers[0] is not event.select
                or not content
                or not self.is_mounted(content[0])
            ):
                return
        if self._management_select(event):
            return
        if event.select.id == "workspace-picker":
            if self._clipboard_private():
                self._sync_workspace_picker()
                return
            if isinstance(event.value, str):
                self._choose_workspace(event.value)
        elif event.select.id == "section-picker":
            if self._clipboard_private():
                self._sync_navigation(self.query_one("#content", TabbedContent).active)
                return
            if event.value != self.query_one("#content", TabbedContent).active:
                self.action_tab(event.value)
        elif event.select.id == "log-source":
            self._render_logs()
        elif event.select.id == "session-scope":
            self._session_source_changed()
            self._render_sessions()

    def on_button_pressed(self, event):
        if self._management_button(event):
            return
        action = event.button.id
        if action and action.startswith("nav-"):
            if self.check_action("tab", (action[4:],)):
                self.action_tab(action[4:])
        elif action == "refresh":
            self.action_refresh()
        elif action == "show-logs":
            self.action_logs()
        elif action == "scan-storage":
            self.scan_storage(self.selected)
        elif action == "connect-sessions":
            self.connect_sessions(
                self.query_one("#session-scope", Select).value,
                self.selected,
            )
        elif action == "disconnect-sessions":
            self._disconnect_sessions()
        elif action == "favorite":
            self._favorite()
        elif action == "archive":
            self._archive()
        elif action == "delete":
            self.prepare_delete(self._session(), self.catalog)
        elif action == "load-podman":
            target = self.selected
            self.push_screen(
                Confirm(
                    "Load this workspace's container logs into memory? They may contain "
                    "private application output. Redaction is best-effort; no copy is saved."
                ),
                lambda ok: self._load_podman(target) if ok else None,
            )
        elif action in ("terminal-owner", "terminal-selected"):
            self._confirm_terminal(action == "terminal-selected")
        elif action == "terminal-disconnect":
            self.query_one(NativeTerminal).disconnect()

    @work(thread=True, group="storage", exit_on_error=False)
    def scan_storage(self, workspace):
        if not workspace or not self._storage_lock.acquire(blocking=False):
            return
        try:
            item = self.backend.scan_storage(workspace)
            if self.is_running:
                self.call_from_thread(self._apply_storage, item)
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Storage scan unavailable; cached sizes are retained. Live metrics and log search remain independent.",
                )
        finally:
            self._storage_lock.release()

    def _apply_storage(self, scanned):
        item = next(
            (
                w
                for w in self.report["workspaces"]
                if w["workspace_id"] == scanned["workspace_id"]
            ),
            None,
        )
        if item is None or item.get("container_id") != scanned.get("container_id"):
            self._message(
                "Workspace changed during storage scan; stale sizes were discarded."
            )
            return
        item["storage"].update(scanned["storage"])
        item["storage_sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._render_workspace()
        self._message(
            f"Storage scan completed for {scanned['workspace_id']}; allocated-block estimates, not quotas."
        )

    def _load_podman(self, workspace):
        if not workspace:
            self._message(
                "Select a registered workspace before loading container logs."
            )
            return
        self.podman_workspace = workspace
        self.query_one("#log-target", Static).update(
            f"External container logs: workspace {workspace}. Source changes only on explicit Load."
        )
        self.refresh_logs()

    @work(thread=True, group="logs", exit_on_error=False)
    def refresh_logs(self):
        if not self._log_lock.acquire(blocking=False):
            return
        self._logging = True
        self._last_logs = time.monotonic()
        workspace = self.podman_workspace
        try:
            entries = self.backend.logs(workspace)
            if self.is_running:
                self.call_from_thread(self._apply_logs, entries, workspace)
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Log source unavailable; retained entries remain searchable.",
                )
        finally:
            self._logging = False
            self._log_lock.release()

    def _apply_logs(self, entries, workspace=None):
        if workspace != self.podman_workspace:
            entries = [entry for entry in entries if entry.source != "podman"]
        if workspace:
            entries = [
                LogEntry(
                    entry.timestamp, "podman:" + workspace, entry.level, entry.message
                )
                if entry.source == "podman"
                else entry
                for entry in entries
            ]
        snapshot = (workspace, tuple(entries))
        if snapshot != self._log_snapshot:
            for source in {entry.source for entry in entries}:
                self.logs.replace(
                    source, [entry for entry in entries if entry.source == source]
                )
            self._log_snapshot = snapshot
            self._log_revision += 1
        self._render_logs()
        if self._activity_revision != self._log_revision:
            self._render_overview_activity()

    def _render_overview_activity(self):
        output = self.query_one("#overview-activity", Static)
        if output.text_selection is not None:
            return
        entries = self.logs.search("", source="oc-box")[-4:]
        rows = [
            f"{entry.timestamp[11:19] if len(entry.timestamp) >= 19 else entry.timestamp}  {entry.message}"
            for entry in entries
        ]
        self._update_text(
            "#overview-activity", "\n".join(rows) or "No retained oc-box activity."
        )
        self._activity_revision = self._log_revision

    def _render_logs(self):
        source = self.query_one("#log-source", Select).value
        if source == "podman" and self.podman_workspace:
            source = "podman:" + self.podman_workspace
        output = self.query_one("#log-output", RichLog)
        key = (self._log_revision, source, self.log_query.casefold())
        if self._log_search_snapshot is None or self._log_search_snapshot[0] != key:
            entries = tuple(
                self.logs.search(
                    self.log_query, source=None if source == "all" else source
                )
            )
            self._log_search_snapshot = (key, entries)
        else:
            entries = self._log_search_snapshot[1]
        count = len(entries)
        noun = (
            ("match" if count == 1 else "matches")
            if self.log_query
            else ("entry" if count == 1 else "entries")
        )
        # This fixed-size label must not request layout and invalidate the
        # separate log viewport's selection while its contents remain frozen.
        self._update_text("#log-hits", f"{count} log {noun}", layout=False)
        # Keep a highlighted log snapshot in place while it is being copied.
        # Search feedback still follows the current query/source while copying.
        if output.text_selection is not None:
            return
        if key == self._log_render_key:
            return
        self._log_render_key = key
        rendered = entries
        if rendered == self._rendered_logs:
            return
        # Follow snapshots commonly append a few entries. Do not clear the
        # whole log (or move a paused viewport) just to append those lines.
        previous = self._rendered_logs
        appended = previous is not None and rendered[: len(previous)] == previous
        self._rendered_logs = rendered
        scroll = output.scroll_offset
        at_end = output.is_vertical_scroll_end
        if not appended:
            output.clear()
        for entry in entries[len(previous) if appended else 0 :]:
            output.write(
                text(
                    f"{entry.timestamp} [{entry.source}] {entry.level}: {entry.message}"
                ),
                scroll_end=at_end,
            )
        if not at_end:
            output.scroll_to(x=scroll.x, y=scroll.y, animate=False, force=True)

    def connect_sessions(self, scope, workspace, generation=None):
        if self.demo:
            self._message("Synthetic demo does not connect to native session sources.")
            return
        if self._session_busy:
            with self._pending_lock:
                cancelling = self._pending_cancel and self._pending_cancel.is_set()
            self._message(
                "Cancellation is still finishing the current bounded request; wait before connecting again."
                if cancelling
                else "Session inventory is already loading. Cancel or wait before connecting again."
            )
            return
        self._session_busy = True
        if generation is None:
            self._catalog_generation += 1
            generation = self._catalog_generation
        cancel = threading.Event()
        with self._pending_lock:
            self._pending_cancel = cancel
            self._pending_generation = generation
        self._session_source = {
            "global": "Host · all projects",
            "project": "Host · selected project",
            "workspace": f"Live workspace {plain(workspace or 'not selected', 64)}",
        }.get(scope, "Selected source")
        self._session_loading_controls()
        self._session_progress(generation, cancel, "Connecting to the native service")
        return self._connect_sessions(scope, workspace, generation, cancel)

    def _session_loading_controls(self):
        with self._pending_lock:
            cancelling = self._pending_cancel and self._pending_cancel.is_set()
        connect = self.query_one("#connect-sessions", Button)
        connect.disabled = self._session_busy
        connect.label = (
            ("Cancelling..." if cancelling else "Loading...")
            if self._session_busy
            else "Connect / refresh"
        )
        self.query_one("#disconnect-sessions", Button).label = (
            "Cancel" if self._session_busy else "Disconnect"
        )
        self.query_one(
            "#session-spinner", LoadingIndicator
        ).display = self._session_busy

    def _session_progress(self, generation, cancel, stage):
        if (
            not self.is_running
            or not self.is_mounted
            or generation != self._catalog_generation
            or generation != self._pending_generation
            or self._pending_cancel is not cancel
            or cancel.is_set()
        ):
            return
        self.query_one("#session-progress-text", Static).update(
            f"Loading {self._session_source}... {stage}. Previous rows remain available."
        )

    def _finish_session_request(self, generation, cancel, outcome):
        # A cancelled request keeps its controls until its bounded call returns.
        # Identity, not the current selection, owns that final reset: selection
        # changes invalidate results but must not leave the spinner stuck.
        if not self.is_running or not self.is_mounted:
            return
        with self._pending_lock:
            if (
                self._pending_cancel is not cancel
                or self._pending_generation != generation
            ):
                return
            cancelled = cancel.is_set() or outcome == "cancelled"
            self._pending_cancel = None
            self._pending_connection = None
            self._pending_generation = None
        self._session_busy = False
        self._session_loading_controls()
        if cancelled:
            message = (
                f"Cancelled {self._session_source}. The bounded request has finished. "
                "Choose a source and Connect when ready; no replacement was connected."
            )
        elif outcome == "complete":
            message = f"Loaded {self._session_source}: {len(self.sessions)} sessions."
        else:
            message = (
                f"Session inventory unavailable or incomplete for {self._session_source}. "
                "No workspace was started; previous rows are retained."
            )
        self.query_one("#session-progress-text", Static).update(message)
        if outcome != "complete" or cancelled:
            self._message(message)

    @work(thread=True, group="sessions", exit_on_error=False)
    def _connect_sessions(self, scope, workspace, generation, cancel):
        connection = None
        outcome = "cancelled"
        worker = get_current_worker()

        def cancelled():
            return (
                cancel.is_set()
                or generation != self._catalog_generation
                or not self.is_running
                or worker.is_cancelled
            )

        try:
            if cancelled():
                return
            if scope == "workspace":
                if not workspace:
                    raise ValueError("No selected workspace")
                _, data = self.backend.selected(workspace)
                directory = self.backend.box.container_project(data)
            else:
                directory = None
            if cancelled():
                return
            connection = self.backend.session_connection(
                workspace if scope == "workspace" else None
            )
            with self._pending_lock:
                if cancelled():
                    return
                self._pending_connection = connection
            self.call_from_thread(
                self._session_progress, generation, cancel, "Fetching session inventory"
            )
            if cancelled():
                return
            associations = (
                self.backend.associations(connection.store)
                if connection.origin == "host" and self.backend.box
                else []
            )
            catalog = SessionCatalog(
                connection.request,
                connection.origin,
                connection.store,
                ownership=connection.ownership,
                managed_ids=[a["archive"]["id"] for a in associations],
            )
            sessions = catalog.list(directory=directory, cancelled=cancelled)
            if cancelled():
                return
            if self.is_running:
                accepted = self.call_from_thread(
                    self._connected_sessions,
                    connection,
                    catalog,
                    sessions,
                    associations,
                    generation,
                    cancel,
                )
                connection = None
                outcome = "complete" if accepted else "cancelled"
        except Exception:
            outcome = "cancelled" if cancelled() else "failed"
        finally:
            try:
                if connection:
                    connection.close()
            finally:
                if self.is_running:
                    self.call_from_thread(
                        self._finish_session_request, generation, cancel, outcome
                    )

    def _session_source_changed(self):
        self._catalog_generation += 1
        self._cancel_pending_connection(reason="Source selection changed")

    def _cancel_pending_connection(self, *, reason=None):
        with self._pending_lock:
            cancel = self._pending_cancel
            if cancel:
                cancel.set()
            connection = self._pending_connection
        if cancel and reason and self.is_running and self.is_mounted:
            self._session_loading_controls()
            self.query_one("#session-progress-text", Static).update(
                f"Cancelling {self._session_source}... {reason}; the current bounded request "
                "is still finishing. No replacement source will connect automatically."
            )
        if connection:
            connection.close()

    def _disconnect_sessions(self):
        self._catalog_generation += 1
        self._cancel_pending_connection(reason="Disconnect requested")
        if self._session_connection:
            self._session_connection.close()
            self._session_connection = None
        self.catalog, self.sessions = None, []
        self._associations = []
        self._render_sessions()
        if not self._session_busy:
            self.query_one("#session-progress-text", Static).update(
                "Session source disconnected. Choose a source and Connect to load its inventory."
            )
        self.query_one("#session-details", Static).update(
            "Session source disconnected. Pending inventory is cancelled; its lease releases after the current bounded API request."
        )

    def _connected_sessions(
        self, connection, catalog, sessions, associations, generation, cancel=None
    ):
        if (
            not self.is_running
            or not self.is_mounted
            or generation != self._catalog_generation
            or (cancel is not None and cancel.is_set())
            or (cancel is not None and self._pending_cancel is not cancel)
        ):
            connection.close()
            return False
        with self._pending_lock:
            if self._pending_connection is connection:
                self._pending_connection = None
        if self._session_connection:
            self._session_connection.close()
        self._session_connection = connection
        self._associations = associations
        self._apply_sessions(catalog, sessions)
        return True

    def _apply_sessions(self, catalog, sessions):
        self.catalog, self.sessions = catalog, sessions
        self._catalog_generation += 1
        self._refresh_favorites()
        self._render_sessions()
        self._message(
            f"Complete {catalog.origin} inventory: {len(sessions)} sessions, including children and archived entries."
        )

    def _apply_mutation_result(self, catalog, sessions, generation, operation=None):
        if catalog is not self.catalog or generation != self._catalog_generation:
            if operation:
                self._notify_failure(operation, reason="source-changed", completed=True)
            return
        self._apply_sessions(catalog, sessions)

    def _render_sessions(self):
        if not self.is_mounted:
            return
        self._render_context()
        query = self.query_one("#session-search", Input).value.casefold()
        scope = self.query_one("#session-scope", Select).value
        workspace = self._workspace()
        values = self.sessions
        if self.catalog and scope == "workspace" and self.catalog.origin != "workspace":
            values = []
        elif (
            self.catalog
            and scope == "workspace"
            and self.catalog.store != self.selected
        ):
            values = []
        elif self.catalog and scope != "workspace" and self.catalog.origin != "host":
            values = []
        if scope == "project":
            project = workspace["project"] if workspace else None
            mirrors = {
                a["archive"]["id"]: a["native_directory"]
                for a in self._associations
                if a["project"] == project
            }
            values = [
                s
                for s in values
                if s["location"]["directory"] == project
                or mirrors.get(s["id"]) == s["location"]["directory"]
            ]
        values = [
            s
            for s in values
            if query
            in (
                s.get("title", "") + " " + s["id"] + " " + s["location"]["directory"]
            ).casefold()
        ]
        values.sort(key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)
        self.visible_sessions = values
        rows = []
        for session in values:
            favorite = (
                self.catalog
                and (self.catalog.origin, self.catalog.store, session["id"])
                in self._session_favorites
            )
            kind = (
                "archived"
                if session.get("time", {}).get("archived") is not None
                else "child"
                if session.get("parentID")
                else "root"
            )
            rows.append(
                (
                    session["id"],
                    (
                        "★" if favorite else "",
                        session["id"],
                        session.get("title") or "Untitled",
                        kind,
                        session["location"]["directory"],
                    ),
                )
            )
        source = (self.catalog.origin, self.catalog.store) if self.catalog else None
        if self._update_table("#session-table", tuple(rows), source=source):
            self._session_details()

    def _session(self):
        index = self.query_one("#session-table", DataTable).cursor_row
        return (
            self.visible_sessions[index]
            if 0 <= index < len(self.visible_sessions)
            else None
        )

    def _session_details(self):
        session = self._session()
        if session and self.catalog:
            self.query_one("#session-details", Static).update(
                f"Source: {self.catalog.origin} · store: {plain(self.catalog.store)}\n"
                f"ID: {session['id']} · native directory: {plain(session['location']['directory'])}\n"
                "Host copies and workspace sources are independent. Later synchronization can replace a managed host copy.\n"
                + (
                    "Workspace browser lease held until Disconnect or dashboard exit; the owner may wait."
                    if self.catalog.origin == "workspace"
                    else "Host activity checks cover this service process only."
                )
            )

    def _favorite(self):
        session = self._session()
        if self.read_only or not session or not self.catalog or not self.favorites:
            return
        if self._favorites_busy:
            self._message("Favorite metadata is busy; wait before changing it again.")
            return
        self._favorites_busy = True
        self._load_favorites(self.favorites, self.catalog, session["id"])

    def _refresh_favorites(self):
        if self.favorites and not self._favorites_busy:
            self._favorites_busy = True
            self._load_favorites(self.favorites)

    @work(thread=True, group="favorites", exit_on_error=False)
    def _load_favorites(self, store, catalog=None, ident=None):
        try:
            if ident is not None:
                store.toggle(catalog.origin, catalog.store, ident)
                self._journal("session.favorited", catalog)
            values = {(f["origin"], f["store"], f["id"]) for f in store.list()}
            if self.is_running:
                self.call_from_thread(self._apply_favorites, store, values)
        except Exception:
            if self.is_running:
                self.call_from_thread(self._apply_favorites, store, None)

    def _apply_favorites(self, store, values):
        self._favorites_busy = False
        if not self.is_running or not self.is_mounted or store is not self.favorites:
            return
        if values is None:
            self._message(
                "Favorite state unavailable; native session inventory is unchanged."
            )
        elif values != self._session_favorites:
            self._session_favorites = values
            self._render_sessions()

    def _archive(self):
        session = self._session()
        operation = (
            "unarchive"
            if session and session.get("time", {}).get("archived") is not None
            else "archive"
        )
        reason = self._session_action_reason(self.catalog, session)
        if reason or self._operation_lock.locked():
            self._notify_failure(operation, reason=reason or "busy")
            return
        catalog, ident = self.catalog, session["id"]
        archived = session.get("time", {}).get("archived") is None
        self.push_screen(
            Confirm(
                f"{'Archive' if archived else 'Restore'} {ident} in {catalog.origin}? "
                "Only this native object changes. A managed host copy can be replaced by later sync. "
                "Activity checks cover this service process only; other native clients can race."
            ),
            lambda ok: (
                self.mutate_session(catalog, ident, archived=archived) if ok else None
            ),
        )

    @work(thread=True, group="session-action", exit_on_error=False)
    def prepare_delete(self, session, catalog):
        reason = self._session_action_reason(catalog, session)
        if reason:
            if self.is_running:
                self.call_from_thread(self._notify_failure, "delete", reason=reason)
            return
        if not self._operation_lock.acquire(blocking=False):
            if self.is_running:
                self.call_from_thread(self._notify_failure, "delete", reason="busy")
            return
        generation = self._catalog_generation
        try:
            preview = catalog.prepare_delete(session["id"])
            if self.is_running:
                self.call_from_thread(
                    self._confirm_delete, catalog, session["id"], preview, generation
                )
        except Exception as error:
            if self.is_running:
                self.call_from_thread(self._notify_failure, "delete", error)
        finally:
            self._operation_lock.release()

    def _confirm_delete(self, catalog, ident, preview, generation=None):
        reason = self._session_action_reason(catalog, ident)
        if (
            not reason
            and generation is not None
            and generation != self._catalog_generation
        ):
            reason = "source-changed"
        if reason:
            self._notify_failure("delete", reason=reason)
            return
        try:
            selected = preview["sessions"]
            if not isinstance(selected, list) or not selected:
                raise ValueError("Invalid preview")
            if len(selected) > 50:
                self._notify_failure(
                    "delete", reason="review-limit", count=len(selected)
                )
                return
            affected = "\n".join(
                f"  {s['id']} · {plain(s['location']['directory'], 256)}"
                for s in selected
            )
            message = (
                f"Delete {ident} in {catalog.origin}, including its native descendants?\n"
                f"Descendants: {preview['descendant_count']}\n"
                f"Affected native entries:\n{affected}\n"
                f"{plain(preview.get('warning', ''))}\n"
                "Other stores/copies are not deleted. A managed host copy may return after later source updates."
            )
        except Exception:
            self._notify_failure("delete", reason="invalid-preview")
            return
        self.push_screen(
            Confirm(message, destructive=True),
            lambda ok: (
                self.mutate_session(catalog, ident, preview=preview) if ok else None
            ),
        )

    @work(thread=True, group="session-action", exit_on_error=False)
    def mutate_session(self, catalog, ident, *, archived=None, preview=None):
        operation = (
            "delete" if archived is None else "archive" if archived else "unarchive"
        )
        reason = self._session_action_reason(catalog, ident)
        if reason:
            if self.is_running:
                self.call_from_thread(self._notify_failure, operation, reason=reason)
            return
        if not self._operation_lock.acquire(blocking=False):
            if self.is_running:
                self.call_from_thread(self._notify_failure, operation, reason="busy")
            return
        generation = self._catalog_generation
        dispatched = completed = False
        try:
            # Catalog methods revalidate before dispatch. Only their known
            # preflight reasons prove refusal; transport errors are ambiguous.
            dispatched = True
            if archived is None:
                catalog.delete(preview)
                completed = True
                self._journal("session.deleted", catalog)
            else:
                catalog.archive(ident, archived)
                completed = True
                self._journal(
                    "session.archived" if archived else "session.unarchived", catalog
                )
            sessions = catalog.list()
            if self.is_running:
                self.call_from_thread(
                    self._apply_mutation_result,
                    catalog,
                    sessions,
                    generation,
                    operation,
                )
        except Exception as error:
            if self.is_running:
                self.call_from_thread(
                    self._notify_failure,
                    operation,
                    error,
                    dispatched=dispatched,
                    completed=completed,
                )
        finally:
            self._operation_lock.release()

    def _confirm_terminal(self, selected):
        if self.read_only or not self.selected or self.demo:
            return
        workspace, target = self.selected, None
        if selected:
            session = self._session()
            if (
                not session
                or not self.catalog
                or self.catalog.origin != "workspace"
                or self.catalog.store != workspace
            ):
                self._message(
                    "Select a source session from this live workspace first; host copies are not terminal targets."
                )
                return
            target = session["id"]
        self.push_screen(
            Confirm(
                f"Attach to {'session ' + target if target else 'the owner-published session'} "
                f"of workspace {workspace}? This can create a persistent shell and take control. "
                "No credentials are refreshed. Ctrl-] detaches without closing the shell."
            ),
            lambda ok: self.open_terminal(workspace, target) if ok else None,
        )

    @work(thread=True, group="terminal-connect", exit_on_error=False)
    def open_terminal(self, workspace, target):
        if self.read_only or self._terminal_busy:
            return
        self._terminal_busy = True
        handle = None
        try:
            handle = self.backend.terminal(workspace, target)
            if self.is_running:
                self.call_from_thread(self._attach_terminal, handle, workspace)
                handle = None
        except TerminalAttachmentError as error:
            if self.is_running:
                self.call_from_thread(self._message, str(error))
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    str(TerminalAttachmentError("client-launch")),
                )
        finally:
            if handle:
                handle.close()
            self._terminal_busy = False

    def _attach_terminal(self, handle, workspace):
        terminal = self.query_one(NativeTerminal)
        terminal.disconnect()
        self._terminal_target = (workspace, handle.session_id)
        terminal.connect(handle.command, handle.environment, on_close=handle.close)
        self.query_one("#terminal-info", Static).update(
            f"Workspace {workspace} · actual session {handle.session_id} · Ctrl-] detaches"
        )
        self.action_tab("terminals")
        terminal.focus()

    def on_unmount(self):
        workflow = getattr(self, "_embedded_operation", None)
        if workflow is not None and not workflow.finished:
            workflow.transport.cancel()
        if self._poll_timer:
            self._poll_timer.stop()
        for kind, timer in self._filter_timers.items():
            timer.stop()
            self._filter_generations[kind] += 1
        self._filter_timers.clear()
        self._catalog_generation += 1
        self._cancel_pending_connection()
        if self._terminal_widget:
            self._terminal_widget.transport.disconnect()
        if self._session_connection:
            self._session_connection.close()

    def on_native_terminal_detached(self, event):
        self.query_one("#terminal-owner", Button).focus()
        self._message(
            "Terminal client detached; the native shell is retained. Ctrl+L searches logs."
        )

    def on_native_terminal_state_changed(self, event):
        if self._terminal_target:
            workspace, session = self._terminal_target
            self.query_one("#terminal-info", Static).update(
                f"Workspace {workspace} · actual session {session} · {plain(event.state)}\n"
                f"{plain(event.reason)} · Ctrl-] detaches the client, not the native shell."
            )

    def check_action(self, action, parameters):
        # Keep the private workflow's app-wide shield and the clipboard
        # screen/ancestor fences. Traversal stays within the active modal so
        # keyboard users can deliberately focus its editor or Cancel button.
        if self._clipboard_private() and action not in {
            "focus_next",
            "focus_previous",
        }:
            return False
        if (
            self._terminal_widget
            and self._terminal_widget.has_focus
            and self._terminal_widget.state == "connected"
            and action in ("quit", "help_quit", "command_palette")
        ):
            return False
        return True
