"""Optional Textual presentation; domain operations live outside the widgets."""

from __future__ import annotations

import time
import threading
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
    Label,
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
    from .tui_terminal import NativeTerminal
    from .tui_clipboard import (
        ClipboardActions,
        ClipboardBar,
        ClipboardButton,
        ClipboardInput,
        ClipboardFooter,
        SelectableDataTable,
        SelectableRichLog,
    )
    from .tui_status import MonitorPanel, percentage
    from .tui_settings import SettingsScreen, REFRESH_INTERVALS, DEFAULT_SETTINGS
else:
    import status_report
    from tui_backend import plain, TerminalAttachmentError
    from tui_logs import LogBuffer, LogEntry
    from tui_sessions import FavoriteStore, SessionCatalog
    from tui_terminal import NativeTerminal
    from tui_clipboard import (
        ClipboardActions,
        ClipboardBar,
        ClipboardButton,
        ClipboardInput,
        ClipboardFooter,
        SelectableDataTable,
        SelectableRichLog,
    )
    from tui_status import MonitorPanel, percentage
    from tui_settings import SettingsScreen, REFRESH_INTERVALS, DEFAULT_SETTINGS


def text(value):
    return Text(plain(value))


class Confirm(ModalScreen):
    CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 76; max-width: 95%; height: 80%; max-height: 40;
      padding: 1 2; border: thick $warning; background: $surface; }
    Confirm Horizontal { height: 3; }
    #confirmation-review { height: 1fr; }
    """

    def __init__(self, message, *, destructive=False):
        super().__init__()
        self.message, self.destructive = message, destructive

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="confirmation-review"):
                yield Static(text(self.message))
            if self.destructive:
                yield ClipboardInput(
                    placeholder="Type DELETE to confirm", id="confirmation"
                )
            yield ClipboardBar()
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
                return
            self.dismiss(True)


class DashboardApp(ClipboardActions, App):
    TITLE = "oc-box control center"
    AUTO_FOCUS = "#workspaces"
    CSS = """
    Screen { layout: vertical; }
    #host-summary { height: 1; padding: 0 1; text-style: bold;
      text-wrap: nowrap; text-overflow: ellipsis; }
    #log-tools { height: 3; }
    #log-search { width: 1fr; }
    #log-hits { width: 13; padding: 1 1; }
    #log-tools Button { width: auto; min-width: 6; padding: 0 1; }
    #body { height: 1fr; }
    #sidebar { width: 24; border: round $primary; }
    #workspaces { height: 1fr; }
    #content { width: 1fr; }
    #content, #content > ContentSwitcher, TabPane { height: 1fr; min-height: 0; }
    #status-summary, #settings-info, #terminal-info { height: auto; padding: 1; }
    MonitorPanel { height: auto; width: 1fr; padding: 0 1;
      text-wrap: nowrap; text-overflow: clip; }
    #full-diagnostics .monitor--border { color: $warning; }
    #system-report .monitor--border { color: $success; }
    #session-table { height: 1fr; min-height: 5; }
    #session-details { height: auto; max-height: 7; }
    #log-output { height: 1fr; }
    #notice { height: auto; max-height: 3; color: $warning; }
    .toolbar { height: auto; min-height: 3; overflow-x: auto; }
    .toolbar Button { min-width: 10; margin-right: 1; }
    .toolbar Button:last-child { margin-right: 0; }
    .toolbar Select { width: 24; }
    .toolbar Label { width: auto; padding: 1; }
    .toolbar .setting-label { width: 28; padding: 1; }
    .toolbar SelectCurrent > #label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    NativeTerminal { height: 1fr; min-height: 5; border: round $secondary; }
    TabPane { padding: 0 1; overflow-y: auto; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("ctrl+l", "logs", "Search logs"),
        ("s", "settings", "Settings"),
        Binding("f2", "settings", show=False),
        Binding("ctrl+shift+c", "copy_selection", show=False, priority=True),
        Binding("ctrl+shift+insert", "paste_copied_text", show=False, priority=True),
        ("1", "tab('status')", "Status"),
        ("2", "tab('settings')", "Settings"),
        ("3", "tab('sessions')", "Sessions"),
        ("4", "tab('terminals')", "Terminals"),
        ("5", "tab('logs')", "Logs"),
    ]

    def __init__(self, backend, *, read_only=False, selected=None, demo=False):
        super().__init__()
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
        self._poll_timer = None

    def compose(self) -> ComposeResult:
        yield Static(
            "Sampling host and workspace resources…", id="host-summary", markup=False
        )
        with Horizontal(id="log-tools"):
            yield ClipboardInput(
                placeholder="Search retained logs at any time · Ctrl+L opens Logs",
                id="log-search",
            )
            yield Static("0 matches", id="log-hits", markup=False)
            yield Button("Logs", id="show-logs")
            yield ClipboardButton(
                "Copy",
                id="copy-selection",
                tooltip="Drag-select text, then copy it. Ctrl+Shift+C also copies if your terminal forwards it.",
            )
            yield ClipboardButton(
                "Copy view",
                id="copy-view",
                tooltip="Copy the visible screen, including control labels. Offscreen text is not included.",
            )
            yield ClipboardButton(
                "Paste copied",
                id="paste-copied",
                tooltip="Paste text copied within this dashboard into the focused field or connected shell. For desktop clipboard text, use your terminal's paste command.",
            )
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("Registered workspaces")
                yield SelectableDataTable(
                    id="workspaces", cursor_type="row", zebra_stripes=True
                )
                yield Button("Refresh", id="refresh")
            with TabbedContent(id="content", initial="status"):
                with TabPane("Status", id="status"):
                    with VerticalScroll(id="status-dashboard"):
                        yield MonitorPanel("workspace", id="full-diagnostics")
                        yield MonitorPanel("system", id="system-report")
                        with Collapsible(
                            title="Workspace details and mounts",
                            collapsed=not DEFAULT_SETTINGS["details"],
                            id="workspace-details",
                        ):
                            yield Static(
                                "Select a workspace", id="status-summary", markup=False
                            )
                        yield Button("Scan storage sizes", id="scan-storage")
                with TabPane("Settings", id="settings"):
                    with VerticalScroll():
                        yield Button("All settings… (s / F2)", id="show-settings")
                        yield Static(id="settings-info", markup=False)
                        with Horizontal(classes="toolbar"):
                            yield Label(
                                "Automatic status refresh", classes="setting-label"
                            )
                            yield Switch(
                                value=DEFAULT_SETTINGS["auto_refresh"],
                                id="auto-refresh",
                            )
                        with Horizontal(classes="toolbar"):
                            yield Label("Refresh interval", classes="setting-label")
                            yield Select(
                                [
                                    (f"{n} second{'s' if n != 1 else ''}", n)
                                    for n in REFRESH_INTERVALS
                                ],
                                value=DEFAULT_SETTINGS["interval"],
                                allow_blank=False,
                                id="refresh-interval",
                            )
                        yield Static(
                            "Resource changes are intentionally deferred. Existing limits are shown above; "
                            "this dashboard never runs podman update or resets a container.\n"
                            "UI controls apply to this dashboard instance only.",
                            markup=False,
                        )
                with TabPane("Sessions", id="sessions"):
                    with Horizontal(classes="toolbar"):
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
                        yield Button("Disconnect", id="disconnect-sessions")
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
                        yield Switch(
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
        yield Static(
            "Synthetic demo · fixed sample readings · no host data"
            if self.demo
            else "Browsing never starts or stops a workspace.",
            id="notice",
            markup=False,
        )
        yield ClipboardFooter()

    def on_mount(self):
        self.query_one("#workspaces", DataTable).add_columns("Project", "State")
        self.query_one("#session-table", DataTable).add_columns(
            "★", "ID", "Title", "Kind", "Native directory"
        )
        self._poll_timer = self.set_interval(0.25, self._tick)
        self.action_refresh()

    def _message(self, value):
        self.query_one("#notice", Static).update(plain(value))

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
            record_event(
                event,
                command="tui",
                workspace=workspace,
                outcome="success",
                root=self.backend.box.state_root(),
            )
        except Exception:
            pass

    def action_tab(self, tab):
        self.query_one("#content", TabbedContent).active = tab

    def action_logs(self):
        self.action_tab("logs")
        self.query_one("#log-search", Input).focus()

    def action_settings(self):
        if isinstance(self.screen, ModalScreen):
            return
        values = {
            "auto_refresh": self.query_one("#auto-refresh", Switch).value,
            "interval": self.query_one("#refresh-interval", Select).value,
            "theme": self.theme,
            "details": not self.query_one("#workspace-details", Collapsible).collapsed,
            "session_scope": self.query_one("#session-scope", Select).value,
            "session_filter": self.query_one("#session-search", Input).value,
            "log_follow": self.query_one("#log-follow", Switch).value,
            "log_source": self.query_one("#log-source", Select).value,
            "log_query": self.query_one("#log-search", Input).value,
        }
        item = self._workspace()
        resources = "No workspace selected."
        if item:
            saved, observed = (
                item.get("configured_limits") or {},
                item.get("effective_limits") or {},
            )
            resources = (
                f"Project: {plain(item['project'])}\n"
                f"Saved CPU {saved.get('cpu_cores')} · RAM {status_report.byte_size(saved.get('memory_bytes'))} · PID {saved.get('pids')}\n"
                f"Observed CPU {observed.get('cpu_cores')} · RAM {status_report.byte_size(observed.get('memory_bytes'))} · PID {observed.get('pids')}"
            )
        self.push_screen(
            SettingsScreen(
                values,
                sorted(self.available_themes),
                read_only=self.read_only,
                resources=resources,
            ),
            self.apply_settings,
        )

    def apply_settings(self, values):
        if values is None:
            return
        valid = (
            isinstance(values, dict)
            and type(values.get("interval")) is int
            and values["interval"] in REFRESH_INTERVALS
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
        self.query_one("#auto-refresh", Switch).value = values["auto_refresh"]
        self.query_one("#refresh-interval", Select).value = values["interval"]
        self.query_one("#workspace-details", Collapsible).collapsed = not values[
            "details"
        ]
        self.query_one("#session-scope", Select).value = values["session_scope"]
        self.query_one("#session-search", Input).value = values["session_filter"]
        self.query_one("#log-follow", Switch).value = values["log_follow"]
        self.query_one("#log-source", Select).value = values["log_source"]
        self.query_one("#log-search", Input).value = values["log_query"]
        self._message(
            "Dashboard settings applied for this session. Workspace limits and OpenCode configuration are unchanged."
        )

    def _tick(self):
        if not self.is_running or not self.is_mounted:
            return
        try:
            interval = self.query_one("#refresh-interval", Select).value
            auto = self.query_one("#auto-refresh", Switch).value
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
        if self.selected not in {w["workspace_id"] for w in workspaces}:
            self.selected = workspaces[0]["workspace_id"] if workspaces else None
        table = self.query_one("#workspaces", DataTable)
        table.clear()
        for index, item in enumerate(workspaces):
            table.add_row(
                text(Path(item["project"]).name or item["project"]),
                text(item["state"]),
                key=item["workspace_id"],
            )
            if item["workspace_id"] == self.selected:
                table.move_cursor(row=index)
        host = report.get("host", {})
        cpu = host.get("cpu_percent")
        cpu_label = "n/a" if cpu is None else f"{cpu:.1f}%"
        size = status_report.byte_size
        self.query_one("#host-summary", Static).update(
            f"oc-box {'DEMO · ' if self.demo else ''}{'READ ONLY · ' if self.read_only else ''}"
            f"CPU {cpu_label}  RAM {size(host.get('memory_bytes'))}/{size(host.get('memory_total_bytes'))}  "
            f"Workspaces {len(workspaces)}"
            + (
                f" · {report['inventory_skipped']} invalid registrations skipped"
                if report.get("inventory_skipped")
                else ""
            )
        )
        self.cpu_history = (self.cpu_history + [cpu])[-60:]
        self.ram_history = (
            self.ram_history
            + [percentage(host.get("memory_bytes"), host.get("memory_total_bytes"))]
        )[-60:]
        self.query_one("#system-report", MonitorPanel).update_snapshot(
            host, self.cpu_history, self.ram_history
        )
        self._render_workspace()

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
        item = self._workspace()
        self.query_one("#full-diagnostics", MonitorPanel).update_snapshot(item)
        if not item:
            self.query_one("#status-summary", Static).update(
                "No registered workspaces. Host monitoring and logs remain available."
            )
            self.query_one("#settings-info", Static).update(
                "Select a registered workspace to inspect its resource settings."
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
        self.query_one("#status-summary", Static).update("\n".join(values))
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
        self.query_one("#settings-info", Static).update(
            f"Workspace {item['workspace_id']}\n"
            f"Saved: CPU {configured.get('cpu_cores')} · RAM {size(configured.get('memory_bytes'))} · PID {configured.get('pids')}\n"
            f"Effective: {effective_label}\n"
            "A CPU quota is not CPU affinity; a memory ceiling does not reserve RAM.\n"
            "No enforced workspace disk quota is configured by oc-box."
        )

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "workspaces":
            target = str(event.row_key.value)
            if target != self.selected:
                self._catalog_generation += 1
            self.selected = target
            self._render_workspace()
            self._render_sessions()
        elif event.data_table.id == "session-table":
            self._session_details()

    def on_data_table_row_highlighted(self, event):
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
                self.selected = target
                self._catalog_generation += 1
                self._render_workspace()
                self._render_sessions()
        elif event.data_table.id == "session-table":
            self._session_details()

    def on_input_changed(self, event):
        if event.input.id == "log-search":
            self.log_query = event.value
            self._render_logs()
        elif event.input.id == "session-search":
            self._render_sessions()

    def on_select_changed(self, event):
        if event.select.id == "log-source":
            self._render_logs()
        elif event.select.id == "session-scope":
            self._catalog_generation += 1
            self._render_sessions()

    def on_button_pressed(self, event):
        action = event.button.id
        if action == "copy-selection":
            self.action_copy_selection(event.button.consume_selection())
        elif action == "copy-view":
            self.action_copy_view()
        elif action == "paste-copied":
            self.action_paste_copied_text()
        elif action == "refresh":
            self.action_refresh()
        elif action == "show-logs":
            self.action_logs()
        elif action == "show-settings":
            self.action_settings()
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
        for source in {entry.source for entry in entries}:
            self.logs.replace(
                source, [entry for entry in entries if entry.source == source]
            )
        self._render_logs()

    def _render_logs(self):
        source = self.query_one("#log-source", Select).value
        if source == "podman" and self.podman_workspace:
            source = "podman:" + self.podman_workspace
        entries = self.logs.search(
            self.log_query, source=None if source == "all" else source
        )
        output = self.query_one("#log-output", RichLog)
        # Keep a highlighted log snapshot in place while it is being copied.
        # Collection still updates the bounded in-memory source buffers.
        if output.text_selection is not None:
            return
        self.query_one("#log-hits", Static).update(f"{len(entries)} matches")
        rendered = tuple(entries)
        if rendered == self._rendered_logs:
            return
        self._rendered_logs = rendered
        output.clear()
        for entry in entries:
            output.write(
                text(
                    f"{entry.timestamp} [{entry.source}] {entry.level}: {entry.message}"
                )
            )

    def connect_sessions(self, scope, workspace, generation=None):
        if self._session_busy or self.demo:
            return
        self._session_busy = True
        if generation is None:
            self._catalog_generation += 1
            generation = self._catalog_generation
        cancel = threading.Event()
        with self._pending_lock:
            self._pending_cancel = cancel
        return self._connect_sessions(scope, workspace, generation, cancel)

    @work(thread=True, group="sessions", exit_on_error=False)
    def _connect_sessions(self, scope, workspace, generation, cancel):
        connection = None

        def cancelled():
            return (
                cancel.is_set()
                or generation != self._catalog_generation
                or not self.is_running
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
            connection = self.backend.session_connection(
                workspace if scope == "workspace" else None
            )
            with self._pending_lock:
                if cancelled():
                    return
                self._pending_connection = connection
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
            if self.is_running:
                self.call_from_thread(
                    self._connected_sessions,
                    connection,
                    catalog,
                    sessions,
                    associations,
                    generation,
                    cancel,
                )
                connection = None
        except Exception:
            if self.is_running and not cancelled():
                self.call_from_thread(
                    self._message,
                    "Session inventory unavailable or incomplete. No workspace was started; previous rows are retained.",
                )
        finally:
            if connection:
                connection.close()
            with self._pending_lock:
                if self._pending_cancel is cancel:
                    self._pending_cancel = None
                    self._pending_connection = None
            self._session_busy = False

    def _cancel_pending_connection(self):
        with self._pending_lock:
            if self._pending_cancel:
                self._pending_cancel.set()
            connection = self._pending_connection
        if connection:
            connection.close()

    def _disconnect_sessions(self):
        self._catalog_generation += 1
        self._cancel_pending_connection()
        if self._session_connection:
            self._session_connection.close()
            self._session_connection = None
        self.catalog, self.sessions = None, []
        self._associations = []
        self._render_sessions()
        self.query_one("#session-details", Static).update(
            "Session source disconnected. Pending inventory is cancelled; its lease releases after the current bounded API request."
        )

    def _connected_sessions(
        self, connection, catalog, sessions, associations, generation, cancel=None
    ):
        if generation != self._catalog_generation:
            connection.close()
            self._message(
                "Source selection changed; discarded the stale session connection."
            )
            return
        with self._pending_lock:
            if self._pending_connection is connection:
                self._pending_connection = None
            if self._pending_cancel is cancel:
                self._pending_cancel = None
        self._disconnect_sessions()
        self._session_connection = connection
        self._associations = associations
        self._apply_sessions(catalog, sessions)

    def _apply_sessions(self, catalog, sessions):
        self.catalog, self.sessions = catalog, sessions
        self._catalog_generation += 1
        self._render_sessions()
        self._message(
            f"Complete {catalog.origin} inventory: {len(sessions)} sessions, including children and archived entries."
        )

    def _apply_mutation_result(self, catalog, sessions, generation):
        if catalog is not self.catalog or generation != self._catalog_generation:
            return
        self._apply_sessions(catalog, sessions)

    def _render_sessions(self):
        if not self.is_mounted:
            return
        previous = self._session()
        previous_id = previous["id"] if previous else None
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
        favorites = set()
        if self.favorites and values:
            try:
                favorites = {
                    (f["origin"], f["store"], f["id"]) for f in self.favorites.list()
                }
            except Exception:
                self._message(
                    "Favorite state unavailable; native session inventory is unchanged."
                )
        table = self.query_one("#session-table", DataTable)
        table.clear()
        for index, session in enumerate(values):
            favorite = (
                self.catalog
                and (self.catalog.origin, self.catalog.store, session["id"])
                in favorites
            )
            kind = (
                "archived"
                if session.get("time", {}).get("archived") is not None
                else "child"
                if session.get("parentID")
                else "root"
            )
            table.add_row(
                text("★" if favorite else ""),
                text(session["id"]),
                text(session.get("title") or "Untitled"),
                text(kind),
                text(session["location"]["directory"]),
                key=session["id"],
            )
            if session["id"] == previous_id:
                table.move_cursor(row=index)

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
        try:
            self.favorites.toggle(
                self.catalog.origin, self.catalog.store, session["id"]
            )
            self._journal("session.favorited", self.catalog)
            self._render_sessions()
        except Exception:
            self._message(
                "Favorite state could not be saved; native history was not changed."
            )

    def _archive(self):
        session = self._session()
        if self.read_only or not session or not self.catalog:
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
        if self.read_only or not session or not catalog:
            return
        if not self._operation_lock.acquire(blocking=False):
            return
        try:
            preview = catalog.prepare_delete(session["id"])
            if self.is_running:
                self.call_from_thread(
                    self._confirm_delete, catalog, session["id"], preview
                )
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Deletion refused: activity, ownership or cascade could not be safely established.",
                )
        finally:
            self._operation_lock.release()

    def _confirm_delete(self, catalog, ident, preview):
        if catalog is not self.catalog:
            return
        if len(preview["sessions"]) > 50:
            self._message(
                "Deletion refused: this cascade is larger than the dashboard's 50-session review limit."
            )
            return
        affected = "\n".join(
            f"  {s['id']} · {plain(s['location']['directory'], 256)}"
            for s in preview["sessions"]
        )
        self.push_screen(
            Confirm(
                f"Delete {ident} in {catalog.origin}, including its native descendants?\n"
                f"Descendants: {preview['descendant_count']}\n"
                f"Affected native entries:\n{affected}\n"
                f"{plain(preview.get('warning', ''))}\n"
                "Other stores/copies are not deleted. A managed host copy may return after later source updates.",
                destructive=True,
            ),
            lambda ok: (
                self.mutate_session(catalog, ident, preview=preview) if ok else None
            ),
        )

    @work(thread=True, group="session-action", exit_on_error=False)
    def mutate_session(self, catalog, ident, *, archived=None, preview=None):
        if self.read_only or catalog is not self.catalog:
            return
        if not self._operation_lock.acquire(blocking=False):
            return
        generation = self._catalog_generation
        try:
            if archived is None:
                catalog.delete(preview)
                self._journal("session.deleted", catalog)
            else:
                catalog.archive(ident, archived)
                self._journal(
                    "session.archived" if archived else "session.unarchived", catalog
                )
            sessions = catalog.list()
            if self.is_running:
                self.call_from_thread(
                    self._apply_mutation_result, catalog, sessions, generation
                )
        except Exception:
            if self.is_running:
                self.call_from_thread(
                    self._message,
                    "Session action refused or failed. Refresh to inspect the actual state; no automatic retry was made.",
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
        if self._poll_timer:
            self._poll_timer.stop()
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
        if (
            self._terminal_widget
            and self._terminal_widget.has_focus
            and self._terminal_widget.state == "connected"
            and action in ("quit", "help_quit", "command_palette")
        ):
            return False
        return True
