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
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

if __package__:
    from . import status_report, tui_clipboard, tui_limits
    from .tui_backend import plain, TerminalAttachmentError
    from .tui_logs import (
        JOURNAL_LIMIT,
        JOURNAL_SOURCE,
        LOG_COLUMNS,
        PODMAN_UNAVAILABLE,
        LogBuffer,
        activity_path,
        fifo_tail_drop,
        raw_copy_text,
        sort_entries,
    )
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
        DEFAULT_SETTINGS,
        LOG_LEVELS,
        LOG_SOURCES,
        REFRESH_INTERVALS,
        RoundedSwitch,
        SettingsPanel,
    )
    from .tui_management import ManagementActions, StoragePanel, BackupPanel
else:
    import status_report
    import tui_clipboard
    import tui_limits
    from tui_backend import plain, TerminalAttachmentError
    from tui_logs import (
        JOURNAL_LIMIT,
        JOURNAL_SOURCE,
        LOG_COLUMNS,
        PODMAN_UNAVAILABLE,
        LogBuffer,
        activity_path,
        fifo_tail_drop,
        raw_copy_text,
        sort_entries,
    )
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
        DEFAULT_SETTINGS,
        LOG_LEVELS,
        LOG_SOURCES,
        REFRESH_INTERVALS,
        RoundedSwitch,
        SettingsPanel,
    )
    from tui_management import ManagementActions, StoragePanel, BackupPanel


def text(value):
    return Text(plain(value))


class _CurrentLogSelection:
    def get_selection(self, selection):
        # A Select change can be observed before its queued Changed message.
        # Never extract text from the previous source in that gap.
        if self.app._log_view_is_current(self.id):
            return super().get_selection(selection)
        return None


class LogTable(_CurrentLogSelection, SelectableDataTable):
    pass


class LogRaw(_CurrentLogSelection, SelectableRichLog):
    pass


LIMIT_STEPS = {
    f"limit-{resource}-{action}": (resource, direction)
    for resource in ("cpu", "memory")
    for action, direction in (("less", -1), ("more", 1))
}


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


class LogColumns(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]
    CSS = """
    LogColumns { align: center middle; }
    LogColumns > Vertical { width: 40; max-width: 95%; height: auto;
      padding: 1 2; border: round $primary; background: $surface; }
    LogColumns SelectionList { height: 5; }
    LogColumns Horizontal { height: 3; }
    """

    def __init__(self, columns):
        super().__init__()
        self.columns = columns

    def compose(self):
        with Vertical():
            yield Label("Visible metadata columns")
            yield SelectionList(
                *(
                    (label, key, key in self.columns)
                    for key, label, _ in LOG_COLUMNS
                    if key != "message"
                ),
                id="log-column-options",
            )
            with Horizontal():
                yield Button("Cancel", id="log-columns-cancel")
                yield Button("Apply", id="log-columns-apply", variant="primary")

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "log-columns-apply":
            self.dismiss(frozenset(self.query_one(SelectionList).selected))
        else:
            self.dismiss(None)


class LogDetails(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]
    CSS = """
    LogDetails { align: center middle; }
    LogDetails > Vertical { width: 96; max-width: 95%; height: 80%; max-height: 36;
      padding: 1 2; border: round $primary; background: $surface; }
    LogDetails VerticalScroll { height: 1fr; }
    LogDetails Button { height: 3; }
    """

    def __init__(self, entry):
        super().__init__()
        self.entry = entry

    def compose(self):
        with Vertical():
            yield Label("Log entry")
            with VerticalScroll():
                yield Static(
                    Text(
                        f"Time: {self.entry.timestamp or 'Unavailable'}\n"
                        f"Source: {self.entry.source or 'Unavailable'}\n"
                        f"Severity: {self.entry.level or 'Unavailable'}\n\n"
                        f"{self.entry.message}"
                    ),
                    id="log-detail-text",
                )
            yield Button("Close", id="log-detail-close")

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()


class DashboardApp(ManagementActions, ClipboardActions, App):
    TITLE = "oc-box control center"
    # Four Braille cells give eight dot columns. Mirror around their
    # shared midpoint so the cube is centered in the terminal-cell slot.
    _CUBE = "⡠⠔⠢⢄\n⡏⢢⡔⢹\n⠑⠼⠧⠊"
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
    Input.-invalid { border: round $error 60%; }
    Input.-invalid:focus { border: round $error; }
    Button.-style-default.-error:focus { border: round $error; }
    Button.-style-default:disabled { border: round $primary 18%; }
    #dashboard-header { height: 3; background: $surface; }
    #dashboard-mark { width: 4; height: 3; margin-left: 1; color: $primary;
      content-align: center middle; text-wrap: nowrap; }
    #host-summary { width: 18; height: 1; margin: 1 1; text-style: bold;
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
    #workspace-limit-card { height: auto; layers: readings adjusters; }
    #workspace-limit-card > #full-diagnostics { layer: readings; }
    #workspace-limit-card > .limit-step-row { layer: adjusters; dock: right;
      width: 7; height: 1; margin-right: 3; background: $surface; }
    #limit-cpu-controls { offset: 0 3; }
    #limit-memory-controls { offset: 0 6; }
    #workspace-limit-card .limit-step-row Button { width: 3; min-width: 0;
      height: 1; padding: 0; border: none; }
    #workspace-limit-card .limit-step-row Button:focus,
    #workspace-limit-card .limit-step-row Button:hover { background: $primary 30%; }
    #workspace-limit-actions { height: 3; padding: 0 1; }
    #workspace-limit-actions Button { width: auto; min-width: 12; margin-right: 1; }
    #apply-live-limits.-style-default:disabled {
      border: round $primary 35% !important;
      background: $surface !important; color: $text-muted; }
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
    #log-table, #log-output { height: 1fr; min-height: 4; }
    #log-source { width: 26; }
    #log-level { width: 21; margin-left: 1; }
    #log-view-tools Button { width: auto; min-width: 8; padding: 0 1; }
    #log-help, #log-target { height: auto; color: $text-muted; }
    #log-mirror-status { height: auto; color: $text-muted; }
    #notice { height: auto; max-height: 3; color: $warning; }
    .toolbar { height: auto; min-height: 3; overflow-x: auto; }
    .toolbar Button { margin-right: 1; }
    #log-view-tools Button { padding: 0; }
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
    Screen.compact #dashboard-mark { display: none; }
    Screen.compact #host-summary { width: 17; min-width: 17; margin: 0 1; }
    Screen.compact #workspace-picker { width: 1fr; height: 1; }
    Screen.compact #section-picker { height: 1; }
    Screen.compact #dashboard-header Select > SelectCurrent {
      height: 1; border: none; padding: 0 1; }
    Screen.compact #log-tools { height: 1; }
    Screen.compact #log-search { height: 1; border: none; padding: 0 2 0 1; }
    Screen.compact #log-hits { height: 1; padding: 0 1; }
    Screen.compact #log-tools Button { height: 1; border: none; padding: 0 1; }
    Screen.compact #dashboard-header Select:focus > SelectCurrent,
    Screen.compact #log-search:focus {
      border: none; background-tint: $accent 22%; }
    Screen.compact .management-tools { height: 4; min-height: 4; }
    Screen.compact #log-filter-tools Select { width: 20; }
    Screen.compact #log-view-tools { min-height: 4; }
    Screen.compact.settings-view ToastRack { margin-bottom: 5; }
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
        self._mirror_logs = LogBuffer(max_entries=JOURNAL_LIMIT)
        self.log_query = ""
        self.podman_workspace = None
        self.favorites = (
            FavoriteStore(backend.box.state_root() / "ui") if backend.box else None
        )
        self._refreshing = self._logging = self._session_busy = self._terminal_busy = (
            False
        )
        self._storage_scanning = False
        self._limit_draft = None
        self._limit_attempt = None
        self._limit_recheck = False
        self._limit_revision = 0
        self._last_report = self._last_logs = 0
        self._rendered_logs = None
        self._log_scope_versions = dict.fromkeys(
            ("oc-box", "podman", JOURNAL_SOURCE), 0
        )
        self._private_log_request = 0
        self._private_log_path = None
        self._private_logs_empty = True
        self._podman_log_state = None
        self._podman_container_id = None
        self._log_revision = 0
        self._raw_render_key = None
        self._log_search_snapshot = None
        self._log_table_render_key = None
        self._log_deferred_viewport = None
        self._log_mode = "table"
        self._log_sort = ("timestamp", False)
        self._log_columns = frozenset(key for key, _, _ in LOG_COLUMNS)
        self._mounted_log_columns = None
        self._log_table_entries = {}
        self._log_row_keys = {}
        self._log_row_sequence = 0
        self._podman_request = 0
        self._mirror_loading = False
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
        self._podman_log_lock = threading.Lock()
        self._operation_lock = threading.Lock()
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
            yield Static(self._CUBE, id="dashboard-mark", markup=False)
            yield Static(self._host_heading(), id="host-summary", markup=False)
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
                                with Vertical(id="workspace-limit-card"):
                                    yield WorkspaceInspector(
                                        "workspace", id="full-diagnostics"
                                    )
                                    for resource, step in (
                                        ("cpu", "0.5 core"),
                                        ("memory", "1 GiB"),
                                    ):
                                        with Horizontal(
                                            id=f"limit-{resource}-controls",
                                            classes="limit-step-row",
                                        ) as controls:
                                            controls.display = False
                                            for label, action in (
                                                ("−", "less"),
                                                ("+", "more"),
                                            ):
                                                button = Button(
                                                    label,
                                                    id=f"limit-{resource}-{action}",
                                                    tooltip=f"Stage {step} {action} {resource}; "
                                                    "changes only on Apply.",
                                                    disabled=True,
                                                    compact=True,
                                                    flat=True,
                                                )
                                                button.active_effect_duration = 0
                                                yield button
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
                                with Horizontal(id="workspace-limit-actions"):
                                    yield Button(
                                        "Scan sizes", id="scan-storage", disabled=True
                                    )
                                    yield Button(
                                        "Apply",
                                        id="apply-live-limits",
                                        variant="primary",
                                        disabled=True,
                                        tooltip="Update this running container only. "
                                        "Saved settings stay unchanged. "
                                        "Lowering memory can interrupt workloads.",
                                    )
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
                        demo=self.demo,
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
                    with Horizontal(classes="toolbar", id="log-filter-tools"):
                        yield Select(
                            LOG_SOURCES,
                            value=DEFAULT_SETTINGS["log_source"],
                            allow_blank=False,
                            id="log-source",
                        )
                        yield Select(
                            LOG_LEVELS,
                            value=DEFAULT_SETTINGS["log_level"],
                            allow_blank=False,
                            id="log-level",
                        )
                        yield Label("Follow")
                        yield RoundedSwitch(
                            value=DEFAULT_SETTINGS["log_follow"], id="log-follow"
                        )
                    with Horizontal(classes="toolbar", id="log-view-tools"):
                        yield Button("RAW", id="log-mode")
                        yield Button("Columns…", id="log-columns")
                        yield Button("Details…", id="log-details", disabled=True)
                        copy_all = Button("Copy all", id="log-copy-all")
                        copy_all.display = False
                        yield copy_all
                        yield Button("Load Podman…", id="load-podman")
                        yield Button("Load mirror", id="load-journald")
                    yield Static(
                        "Click a header to sort. Choose a row, then Details.",
                        id="log-help",
                        markup=False,
                    )
                    yield Static(
                        "Podman output may contain private data; redaction is best-effort. "
                        "External logs are kept in memory only.",
                        id="log-target",
                        markup=False,
                    )
                    mirror_status = Static(
                        "Mirror not loaded. Load mirror reads one same-user snapshot.",
                        id="log-mirror-status",
                        markup=False,
                    )
                    mirror_status.display = False
                    yield mirror_status
                    yield LogTable(
                        id="log-table",
                        cursor_type="row",
                        zebra_stripes=True,
                    )
                    output = LogRaw(
                        id="log-output",
                        max_lines=2000,
                        wrap=False,
                        markup=False,
                        highlight=False,
                    )
                    output.display = False
                    yield output
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
        self.watch(self.screen, "selections", self._log_selection_changed, init=False)
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
        self.query_one("#body").screen.set_class(tab == "settings", "settings-view")
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
        elif tab == "logs":
            self._render_logs()

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
        self._update_text("#host-summary", self._host_heading())
        self._render_monitoring_state()

    def _host_heading(self):
        mode = " READ ONLY" if self.read_only else " DEMO" if self.demo else ""
        return "oc-box" + mode

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
        self._limit_draft = None
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
            "log_level": self.query_one("#log-level", Select).value,
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

    def on_settings_panel_save_host_logging_requested(self, event):
        event.stop()
        if (
            self.demo
            or self.read_only
            or not self._admit_settings_request(event)
            or self._clipboard_private()
            or isinstance(self.screen, ModalScreen)
            or self.query_one("#content", TabbedContent).active != "settings"
        ):
            return
        previous = activity_path()
        event.panel.save_host_logging()
        current = activity_path()
        if current != previous or current != self._private_log_path:
            self._private_log_request += 1
            self._reset_private_logs(current)
            if not self._log_lock.locked():
                self.refresh_logs()

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
            and values.get("log_source") in tuple(value for _, value in LOG_SOURCES)
            and values.get("log_level") in tuple(value for _, value in LOG_LEVELS)
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
        self.query_one("#log-level", Select).value = values["log_level"]
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
            revision = self._limit_revision
            report = self.backend.collect(scan_storage=scan_storage)
            if self.is_running:
                self.call_from_thread(
                    self._apply_report, report, scan_storage, revision
                )
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Status refresh unavailable; previous readings are stale. No recovery was attempted.",
                )
        finally:
            self._refreshing = False
            self._status_lock.release()

    def _apply_report(self, report, scanned, limit_revision=None):
        if limit_revision is not None and limit_revision != self._limit_revision:
            # A pre-Apply read cannot put the old verified limits back on screen.
            return
        self._limit_recheck = False
        if self._limit_attempt is not None:
            target = self._limit_attempt.target
            previous_limits = next(
                (item for item in self.report["workspaces"] if target.same_run(item)),
                None,
            )
            if previous_limits is not None:
                for item in report["workspaces"]:
                    if target.same_run(item):
                        # Do not let polling display half of a multi-limit
                        # update (or a new result before post-checks finish).
                        for key in ("limits", "effective_limits"):
                            item[key] = previous_limits.get(key)
                        break
        target = self.podman_workspace
        prior_target = (
            self._podman_status_binding(self.report, target) if target else None
        )
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
        if target is not None:
            current_target = self._podman_status_binding(report, target)
            if current_target != prior_target and current_target != (
                self._podman_container_id,
            ):
                # Status independently observed a different resource. Revoke
                # the retained rows and selection without starting a log query.
                self._podman_container_id = None
                self._log_scope_versions["podman"] += 1
                self._apply_logs((), target, private=False)
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

    def _update_table(
        self, selector, rows, *, selected=None, source=None, tail_drop=None
    ):
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
        if tail_drop is not None and same_source:
            kept = len(previous) - tail_drop
            scroll, cursor = table.scroll_offset, table.cursor_row
            for key, _ in previous[:tail_drop]:
                table.remove_row(key)
            for key, cells in rows[kept:]:
                table.add_row(
                    *(
                        self._table_text(selector, i, cell)
                        for i, cell in enumerate(cells)
                    ),
                    key=key,
                )
            if tail_drop:
                # Anchor the viewport and cursor to the same retained records.
                table.move_cursor(row=max(0, cursor - tail_drop), scroll=False)
                scroll = scroll._replace(y=max(0, scroll.y - tail_drop))
                table.scroll_to(x=scroll.x, y=scroll.y, animate=False, force=True)
        elif same_source and old_ids == new_ids:
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
        if same_source and old_ids != new_ids and tail_drop != 0:
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
                "log-table",
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
        self._render_scan_button("#scan-storage", available=item is not None)
        self._render_context()
        self._update_monitor("#full-diagnostics", item)
        self._render_limits(item)
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
        elif event.select.id in {"log-source", "log-level"}:
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
        elif action in LIMIT_STEPS:
            self._stage_limit(*LIMIT_STEPS[action])
        elif action == "apply-live-limits":
            self._apply_limits()
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
            if self._clipboard_private():
                return
            target = self.selected
            self.push_screen(
                Confirm(
                    "Load this workspace's container logs into memory? They may contain "
                    "private application output. Redaction is best-effort; no copy is saved."
                ),
                lambda ok: self._load_podman(target) if ok else None,
            )
        elif action == "load-journald" and not self._clipboard_private():
            self._load_mirror()
        elif action == "log-mode" and not self._clipboard_private():
            self._switch_log_mode()
        elif action == "log-columns" and not self._clipboard_private():
            self.push_screen(LogColumns(self._log_columns), self._choose_log_columns)
        elif action == "log-details" and not self._clipboard_private():
            self._show_log_details()
        elif (
            action == "log-copy-all"
            and self._log_mode == "raw"
            and not self._clipboard_private()
        ):
            self._copy_all_logs()
        elif action in ("terminal-owner", "terminal-selected"):
            self._confirm_terminal(action == "terminal-selected")
        elif action == "terminal-disconnect":
            self.query_one(NativeTerminal).disconnect()

    def _render_scan_button(self, selector, *, available):
        button = self.query_one(selector, Button)
        button.disabled = self._storage_scanning or not available
        label = "Running…" if self._storage_scanning else "Scan sizes"
        if button.label.plain != label:
            button.label = label

    def _limit_target(self, item):
        if (
            self.read_only
            or self.demo
            or self._limit_recheck
            or not callable(getattr(self.backend, "apply_live_limits", None))
        ):
            return None
        return tui_limits.LimitTarget.from_report(item)

    def _render_limits(self, item):
        target = self._limit_target(item)
        draft = self._limit_draft
        if draft and draft.target != target:
            draft = self._limit_draft = None
            self._message(
                "Staged limits cleared because the workspace or its limits changed."
            )
        editable = target is not None and self._limit_attempt is None
        candidate = (draft or tui_limits.LimitDraft(target)) if editable else None
        for resource in ("cpu", "memory"):
            self.query_one(f"#limit-{resource}-controls", Horizontal).display = (
                item is not None
            )
        for ident, (resource, direction) in LIMIT_STEPS.items():
            self.query_one(f"#{ident}", Button).disabled = (
                not candidate or candidate.step(resource, direction) == candidate
            )
        self.query_one(WorkspaceInspector).show_limit_draft(
            draft.captions() if draft else ("", ""), controls=item is not None
        )
        apply = self.query_one("#apply-live-limits", Button)
        apply.disabled = not (editable and draft and draft.pending)
        label = "Applying…" if self._limit_attempt is not None else "Apply"
        if apply.label.plain != label:
            apply.label = label

    def _allow_limit_press(self):
        return (
            self._limit_attempt is None
            and not self._clipboard_private()
            and not isinstance(self.screen, ModalScreen)
            and self.query_one("#content", TabbedContent).active == "status"
        )

    def _stage_limit(self, resource, direction):
        if not self._allow_limit_press():
            return
        item = self._workspace()
        target = self._limit_target(item)
        if target is None:
            return
        draft = self._limit_draft or tui_limits.LimitDraft(target)
        if draft.target != target:
            self._render_limits(item)
            return
        staged = draft.step(resource, direction)
        self._limit_draft = staged if staged.pending else None
        self._render_limits(item)

    def _apply_limits(self):
        if not self._allow_limit_press() or not self._limit_draft:
            return
        item = self._workspace()
        draft = self._limit_draft
        if draft.target != self._limit_target(item) or not draft.pending:
            self._render_limits(item)
            return
        self._limit_draft = None
        self._limit_attempt = draft
        self._render_limits(item)
        self._apply_limits_worker(draft)

    @work(thread=True, group="live-limits", exit_on_error=False)
    def _apply_limits_worker(self, draft):
        outcome = "verified"
        try:
            self.backend.apply_live_limits(
                draft, read_only=self.read_only, demo=self.demo
            )
        except tui_limits.LimitError as error:
            outcome = error.reason
        except Exception:
            outcome = "unverified"
        if self.is_running:
            self.call_from_thread(self._finish_limits, draft, outcome)

    def _finish_limits(self, draft, outcome):
        self._limit_attempt = None
        self._limit_revision += 1
        self._limit_recheck = outcome != "verified"
        if outcome == "verified":
            for item in self.report["workspaces"]:
                if tui_limits.LimitTarget.from_report(item) != draft.target:
                    continue
                changes = {}
                if draft.cpu is not None:
                    changes["cpu_cores"] = draft.cpu / tui_limits.CPU_UNIT
                if draft.memory is not None:
                    changes["memory_bytes"] = draft.memory
                for key in ("limits", "effective_limits"):
                    item[key] = {**(item.get(key) or {}), **changes}
                break
        self._render_workspace()
        self._message(tui_limits.LIMIT_MESSAGES[outcome])

    def _set_storage_scanning(self, scanning):
        self._storage_scanning = scanning
        self._render_workspace()

    def scan_storage(self, workspace):
        if not workspace or self._storage_scanning:
            return
        self._set_storage_scanning(True)
        return self._scan_storage(workspace)

    @work(thread=True, group="storage", exit_on_error=False)
    def _scan_storage(self, workspace):
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
            if self.is_running:
                self.call_from_thread(self._set_storage_scanning, False)

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
        self._podman_request += 1
        self._podman_log_state = None
        self._podman_container_id = None
        if self.logs.refresh("podman:" + workspace, ()):
            self._log_revision += 1
        self._log_scope_versions["podman"] += 1
        self._update_text(
            "#log-target",
            f"Loading Podman workspace: {workspace} · redaction is best-effort.",
        )
        self._render_logs()
        self.refresh_logs()

    @work(thread=True, group="logs", exit_on_error=False)
    def refresh_logs(self):
        if not self._log_lock.acquire(blocking=False):
            return
        self._logging = True
        self._last_logs = time.monotonic()
        private_request = self._private_log_request
        podman_request = self._podman_request
        workspace = self.podman_workspace
        split = hasattr(self.backend, "private_logs")
        try:
            if split:
                path, entries = self.backend.private_logs()
                current = activity_path()
                if self.is_running:
                    self.call_from_thread(
                        self._apply_private_logs,
                        entries if path is not None and path == current else (),
                        current,
                        private_request,
                    )
            else:
                entries = self.backend.logs(workspace)
                if self.is_running:
                    self.call_from_thread(
                        self._apply_combined_logs,
                        entries,
                        workspace,
                        private_request,
                        podman_request,
                    )
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Log source unavailable; retained entries remain searchable.",
                )
        finally:
            self._logging = False
            self._log_lock.release()
            if self.is_running:
                if private_request != self._private_log_request or (
                    not split and podman_request != self._podman_request
                ):
                    # Explicit changes racing a slow read are not lost when
                    # Follow is paused. Coalesce to the latest reviewed source.
                    self.call_from_thread(self.refresh_logs)
                if split:
                    self.call_from_thread(self._queue_podman_logs)

    def _reset_private_logs(self, path):
        self._private_log_path = path
        self._apply_logs((), reset_private=True)

    def _apply_private_logs(self, entries, path, request):
        if request == self._private_log_request:
            reset = path != self._private_log_path
            self._private_log_path = path
            self._apply_logs(entries, reset_private=reset)

    def _apply_combined_logs(self, entries, workspace, private_request, podman_request):
        if private_request != self._private_log_request:
            return
        if podman_request != self._podman_request:
            entries = (entry for entry in entries if entry.source != "podman")
            workspace = None
        self._apply_logs(entries, workspace)

    @staticmethod
    def _podman_status_binding(report, workspace):
        # Distinguish an unlisted registration from a listed one whose
        # container was unavailable. Status is a fence, not read authority.
        return next(
            (
                (item.get("container_id"),)
                for item in report["workspaces"]
                if item["workspace_id"] == workspace
            ),
            None,
        )

    def _queue_podman_logs(self):
        workspace = self.podman_workspace
        if workspace and not self._podman_log_lock.locked():
            self._refresh_podman_logs(
                workspace,
                self._podman_request,
                self._podman_status_binding(self.report, workspace),
            )

    @work(thread=True, group="podman-logs", exit_on_error=False)
    def _refresh_podman_logs(self, workspace, request, status_binding):
        if not self._podman_log_lock.acquire(blocking=False):
            return
        try:
            if request == self._podman_request:
                try:
                    entries = self.backend.podman_logs(workspace)
                except Exception:
                    entries = [PODMAN_UNAVAILABLE]
                if self.is_running:
                    self.call_from_thread(
                        self._apply_podman_logs,
                        entries,
                        workspace,
                        request,
                        status_binding,
                    )
        finally:
            self._podman_log_lock.release()
            if self.is_running and request != self._podman_request:
                self.call_from_thread(self._queue_podman_logs)

    def _apply_podman_logs(self, entries, workspace, request, status_binding):
        if request == self._podman_request and workspace == self.podman_workspace:
            container_id = getattr(entries, "container_id", None)
            current = self._podman_status_binding(self.report, workspace)
            if current != status_binding and (
                container_id is None or current != (container_id,)
            ):
                return
            if container_id != self._podman_container_id:
                # The same registration can now refer to a replacement
                # container. A selected viewport must not freeze its old logs.
                self._podman_container_id = container_id
                self._log_scope_versions["podman"] += 1
            self._apply_logs(entries, workspace, private=False)

    def _apply_logs(
        self, entries, workspace=None, *, private=True, reset_private=False
    ):
        scoped = (
            "podman:" + workspace
            if workspace and workspace == self.podman_workspace
            else None
        )
        sources = {"oc-box": []} if private else {}
        if scoped:
            sources[scoped] = []
        for entry in entries:
            if entry.source == JOURNAL_SOURCE:
                continue
            source = entry.source
            if entry.source == "podman":
                if workspace and scoped is None:
                    continue
                source = scoped or source
            sources.setdefault(source, []).append(entry)
        if private:
            empty = not sources["oc-box"]
            reset_private |= empty and not self._private_logs_empty
            self._private_logs_empty = empty
            if reset_private:
                self._log_scope_versions["oc-box"] += 1
        if scoped:
            snapshot = sources[scoped]
            state = (
                "Podman logs unavailable; no current logs."
                if snapshot == [PODMAN_UNAVAILABLE]
                else "No current verified container logs."
                if not snapshot
                else "Memory only; redaction is best-effort."
            )
            if state != self._podman_log_state:
                self._podman_log_state = state
                self._log_scope_versions["podman"] += 1
                self._update_text(
                    "#log-target", f"Podman workspace: {workspace} · {state}"
                )
        changed = False
        preserve = [] if private else ["oc-box"]
        for source, snapshot in sources.items():
            changed |= self.logs.refresh(source, snapshot, preserve=preserve)
            preserve.append(source)
        if changed:
            self._log_revision += 1
        self._render_logs()
        if reset_private or self._activity_revision != self._log_revision:
            if reset_private:
                self._clear_log_selection(self.query_one("#overview-activity", Static))
            self._render_overview_activity()

    def _load_mirror(self):
        source = self.query_one("#log-source", Select)
        source.value = JOURNAL_SOURCE
        if self.demo:
            self._update_text(
                "#log-mirror-status", "Synthetic demo does not read the system journal."
            )
        elif not self._mirror_loading:
            self._mirror_loading = True
            self.query_one("#load-journald", Button).disabled = True
            self._update_text(
                "#log-mirror-status", "Loading a same-user mirror snapshot…"
            )
            self._read_mirror()

    @work(thread=True, group="log-mirror", exit_on_error=False)
    def _read_mirror(self):
        try:
            entries = self.backend.journal_mirror_logs()
        except Exception:
            entries = None
        if self.is_running:
            self.call_from_thread(self._apply_mirror, entries)

    def _apply_mirror(self, entries):
        self._mirror_loading = False
        self.query_one("#load-journald", Button).disabled = False
        if entries is None:
            self._update_text(
                "#log-mirror-status",
                "Journal mirror unavailable. Any earlier snapshot remains; "
                "select oc-box activity for the private log.",
            )
        else:
            if self._mirror_logs.refresh(JOURNAL_SOURCE, entries):
                self._log_revision += 1
                if not entries:
                    self._log_scope_versions[JOURNAL_SOURCE] += 1
            self._update_text(
                "#log-mirror-status",
                "Same-user mirror snapshot · Load mirror to refresh.",
            )
        self._render_logs()

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

    def _log_scope(self):
        source = self.query_one("#log-source", Select).value
        if source == "all":
            version = tuple(self._log_scope_versions[s] for s in ("oc-box", "podman"))
        else:
            version = self._log_scope_versions.get(source, 0)
            if source == "podman" and self.podman_workspace:
                source = "podman:" + self.podman_workspace
        return source, version

    def _log_view_is_current(self, widget_id, scope=None):
        if widget_id == "log-table":
            table = self._log_table_render_key
            painted = table[0] if table else None
        else:
            painted = self._raw_render_key
        return painted is not None and painted[1:3] == (scope or self._log_scope())

    def _matching_logs(self, query):
        source, version = self._log_scope()
        level = self.query_one("#log-level", Select).value
        key = (self._log_revision, source, version, level, query.casefold())
        if self._log_search_snapshot is None or self._log_search_snapshot[0] != key:
            buffer = self._mirror_logs if source == JOURNAL_SOURCE else self.logs
            entries = tuple(
                buffer.search(
                    query,
                    source=None if source == "all" else source,
                    level=None if level == "all" else level,
                    source_order=True,
                )
            )
            self._log_search_snapshot = (key, entries)
        return self._log_search_snapshot

    def _copy_all_logs(self):
        # Inspect the current matching retained records even before a debounced
        # paint. The display snapshot may be frozen during selection.
        key, entries = self._matching_logs(self.query_one("#log-search", Input).value)
        if key[1] == "all":
            scopes = {
                entry.source
                for entry in entries
                if entry.source == "podman" or entry.source.startswith("podman:")
            }
            if len(scopes) > 1:
                self._clipboard_notice(
                    "Matches include multiple Podman workspaces. "
                    "Choose Selected Podman container or another scoped source before copying."
                )
                return
        payload = raw_copy_text(entries, tui_clipboard.MAX_CLIPBOARD_BYTES)
        if payload is None:
            self._clipboard_notice("Clipboard text exceeds the 1 MiB UTF-8 limit.")
            return
        self.action_copy_selection(text=payload)

    def _render_logs(self):
        key, entries = self._matching_logs(self.log_query)
        count = len(entries)
        noun = (
            ("match" if count == 1 else "matches")
            if self.log_query
            else ("entry" if count == 1 else "entries")
        )
        # This fixed-size label must not request layout and invalidate the
        # separate log viewport's selection while its contents remain frozen.
        self._update_text("#log-hits", f"{count} log {noun}", layout=False)
        self.query_one("#log-mirror-status", Static).display = key[1] == JOURNAL_SOURCE
        for widget_id in ("log-table", "log-output"):
            if not self._log_view_is_current(widget_id, key[1:3]):
                self._clear_log_selection(self.query_one("#" + widget_id))
        if self.query_one("#content", TabbedContent).active != "logs":
            return
        if self._log_mode == "table":
            self._render_log_table(key, entries)
        else:
            self._render_log_raw(key, entries)

    def _clear_log_selection(self, viewport):
        gesture = getattr(self, "_selection_gesture", None)
        if gesture is not None and gesture[2] is viewport:
            self._cancel_selection_gesture()
        elif viewport.text_selection is not None:
            viewport.screen.clear_selection()

    def _log_paint_blocked(self, viewport, unchanged, same_view):
        if not unchanged and viewport.text_selection is not None:
            if same_view:
                self._log_deferred_viewport = viewport
                return True
            self._clear_log_selection(viewport)
        self._log_deferred_viewport = None
        return unchanged

    def _log_selection_changed(self, previous, current):
        viewport = self._log_deferred_viewport
        if (
            viewport is not None
            and previous.get(viewport) is not None
            and current.get(viewport) is None
        ):
            self.call_later(self._resume_log_paint, viewport)

    def _resume_log_paint(self, viewport):
        if (
            self.is_running
            and self._log_deferred_viewport is viewport
            and viewport.is_attached
            and viewport.text_selection is None
        ):
            self._render_logs()

    def _switch_log_mode(self):
        raw = self._log_mode != "raw"
        self._log_mode = "raw" if raw else "table"
        self.screen.clear_selection()
        self.query_one("#log-mode", Button).label = "Table" if raw else "RAW"
        self.query_one("#log-table", DataTable).display = not raw
        self.query_one("#log-output", RichLog).display = raw
        self.query_one("#log-columns", Button).display = not raw
        self.query_one("#log-details", Button).display = not raw
        self.query_one("#log-copy-all", Button).display = raw
        self._render_logs()

    def _choose_log_columns(self, columns):
        if columns is not None:
            self._log_columns = columns | {"message"}
            self._render_logs()

    def on_data_table_header_selected(self, event):
        if (
            event.data_table.id != "log-table"
            or self._log_mode != "table"
            or self._clipboard_private()
        ):
            return
        self.screen.clear_selection()
        column = event.column_key.value
        current, descending = self._log_sort
        if column in self._log_columns:
            self._log_sort = (column, not descending if column == current else False)
            self._render_logs()

    def _show_log_details(self):
        table = self.query_one("#log-table", DataTable)
        rows = self._table_snapshots.get("#log-table", (None, ()))[1]
        if (
            self._log_mode == "table"
            and self._log_view_is_current("log-table")
            and 0 <= table.cursor_row < len(rows)
        ):
            entry = self._log_table_entries[rows[table.cursor_row][0]]
            self.push_screen(LogDetails(entry))

    def copy_to_clipboard(self, text):
        # Native Screen copy joins rejected log selections into an empty
        # string. Do not clear the app cache or send an empty OSC 52 write.
        if (
            text == ""
            and self.screen_stack
            and any(
                isinstance(widget, _CurrentLogSelection)
                for widget in self.screen.selections
            )
        ):
            return False
        return super().copy_to_clipboard(text)

    def action_copy_view(self):
        if not isinstance(self.screen, ModalScreen):
            widget_id = None
            if self.query_one("#content", TabbedContent).active == "logs":
                widget_id = "log-table" if self._log_mode == "table" else "log-output"
                if not self._log_view_is_current(widget_id):
                    return
            try:
                # The widget render key can advance before Textual lays out
                # the new mode or tab. Check the same compositor map that
                # Copy View uses, not the widgets' requested visibility.
                if any(
                    isinstance(widget, _CurrentLogSelection) and widget.id != widget_id
                    for widget in self.screen._compositor.visible_widgets
                ):
                    return
            except (AttributeError, TypeError):
                self._clipboard_notice("Visible screen copy is unavailable.")
                return
        super().action_copy_view()

    def _render_log_table(self, key, entries):
        table = self.query_one("#log-table", DataTable)
        column, descending = self._log_sort
        label = next(label for name, label, _ in LOG_COLUMNS if name == column)
        self._update_text(
            "#log-help",
            f"{label} {'↓' if descending else '↑'} · Click a header to sort. "
            "Choose a row, then Details.",
        )
        render_key = (key, self._log_sort, self._log_columns)
        previous = self._log_table_render_key
        same_view = (
            previous is not None
            and previous[0][1:] == key[1:]
            and previous[1:] == render_key[1:]
        )
        same_selection_scope = (
            previous is not None
            and previous[0][1:3] == key[1:3]
            and previous[1:] == render_key[1:]
        )
        if self._log_paint_blocked(table, render_key == previous, same_selection_scope):
            return
        follow_tail = (
            same_view
            and self._log_sort == ("timestamp", False)
            and table.is_vertical_scroll_end
            and self.query_one("#log-follow", Switch).value
        )
        columns = tuple(item for item in LOG_COLUMNS if item[0] in self._log_columns)
        old_rows = self._table_snapshots.get("#log-table", (None, ()))[1]
        selected = None
        if columns != self._mounted_log_columns:
            if previous is not None and previous[0][1] == key[1]:
                if 0 <= table.cursor_row < len(old_rows):
                    selected = old_rows[table.cursor_row][0]
            table.clear(columns=True)
            for name, label, width in columns:
                table.add_column(label, key=name, width=width)
            self._mounted_log_columns = columns

        ordered = tuple(sort_entries(entries, column, descending))
        drop = (
            fifo_tail_drop(
                tuple(self._log_table_entries[row_key] for row_key, _ in old_rows),
                ordered,
            )
            if same_view
            else None
        )
        kept = len(old_rows) - drop if drop is not None else 0
        # A rolling tail retains the exact keys for its overlap, even when
        # equal rows near the cutoff have different prior occurrence ordinals.
        # Other changes use occurrence identities to keep a stable cursor.
        keys, items, rows, occurrences = {}, {}, [], {}
        for index, entry in enumerate(ordered):
            ordinal = occurrences.get(entry, 0)
            occurrences[entry] = ordinal + 1
            identity = (entry, ordinal)
            row_key = (
                old_rows[drop + index][0]
                if drop is not None and index < kept
                else None
                if drop is not None
                else self._log_row_keys.get(identity)
            )
            if row_key is None:
                self._log_row_sequence += 1
                row_key = str(self._log_row_sequence)
            keys[identity] = row_key
            items[row_key] = entry
            cells = []
            for name, _, _ in columns:
                value = getattr(entry, name)
                if name == "source":
                    if value == JOURNAL_SOURCE:
                        value = "Journal mirror"
                    elif value.startswith("podman:"):
                        value = value[:13]
                cells.append(value or ("—" if name != "message" else ""))
            rows.append((row_key, tuple(cells)))
        self._log_row_keys, self._log_table_entries = keys, items
        changed = self._update_table(
            "#log-table",
            tuple(rows),
            source=(key[1:3], self._log_columns),
            tail_drop=drop,
            selected=selected,
        )
        self.query_one("#log-details", Button).disabled = not rows
        self._log_table_render_key = render_key
        if changed and follow_tail:
            table.call_after_refresh(
                self._follow_log_tail,
                render_key,
                self._table_scroll_generations.get("#log-table", 0),
            )

    def _follow_log_tail(self, key, generation):
        if (
            not self.is_running
            or key != self._log_table_render_key
            or generation != self._table_scroll_generations.get("#log-table", 0)
            or self._log_mode != "table"
            or self.query_one("#content", TabbedContent).active != "logs"
            or not self.query_one("#log-follow", Switch).value
        ):
            return
        table = self.query_one("#log-table", DataTable)
        if table.text_selection is None:
            table.scroll_to(y=table.max_scroll_y, animate=False, force=True)

    def _render_log_raw(self, key, entries):
        self._update_text(
            "#log-help",
            "RAW · Source order. Scroll sideways for long lines. Copy all includes off-screen matches.",
        )
        output = self.query_one("#log-output", RichLog)
        # A same-source highlight stays in place while search feedback updates.
        # Source changes and invalidated snapshots have a different scope.
        previous_key = self._raw_render_key
        same_scope = previous_key is not None and previous_key[1:3] == key[1:3]
        if self._log_paint_blocked(output, key == previous_key, same_scope):
            return
        self._raw_render_key = key
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
                Text(entry.raw_line()),
                shrink=False,
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
