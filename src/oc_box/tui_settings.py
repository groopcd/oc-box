"""Dashboard preferences and separately committed host logging settings."""

from types import MappingProxyType

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.validation import Integer
from textual.widgets import Button, Input, Label, Select, Static, Switch

if __package__:
    from . import log_config
    from .activity_log import _root as _activity_state_root
    from .tui_clipboard import ClipboardInput
    from .tui_logs import JOURNAL_SOURCE
else:
    import log_config
    from activity_log import _root as _activity_state_root
    from tui_clipboard import ClipboardInput
    from tui_logs import JOURNAL_SOURCE

REFRESH_INTERVALS = tuple(range(1, 61))
SESSION_SCOPES = (
    ("Host · all projects", "global"),
    ("Host · selected project", "project"),
    ("Live workspace sources", "workspace"),
)
LOG_SOURCES = (
    ("All sources", "all"),
    ("oc-box activity", "oc-box"),
    ("Selected Podman container", "podman"),
    ("System journal mirror", JOURNAL_SOURCE),
)
LOG_LEVELS = (
    ("All severities", "all"),
    ("Debug and higher", "debug"),
    ("Info and higher", "info"),
    ("Warn and higher", "warn"),
    ("Error only", "error"),
)
DEFAULT_SETTINGS = MappingProxyType(
    {
        "auto_refresh": True,
        "interval": 1,
        "theme": "oc-box-navy",
        "details": False,
        "session_scope": "global",
        "session_filter": "",
        "log_follow": True,
        "log_source": "all",
        "log_level": "all",
        "log_query": "",
    }
)


class RoundedSwitch(Switch):
    """Keep the native toggle and expose its state without relying on color."""

    def __init__(self, value=False, **kwargs):
        super().__init__(value, animate=False, **kwargs)

    def render(self):
        return Text("ON  ●" if self.value else "● OFF")


class SettingsPanel(Vertical):
    """Independent session and host drafts; only the host Save writes to disk."""

    BINDINGS = [("escape", "revert", "Revert edits")]
    DEFAULT_CSS = """
    SettingsPanel { height: 1fr; min-height: 0; }
    SettingsPanel #preferences-title { height: auto; text-style: bold; }
    SettingsPanel #preferences-topics { height: 1fr; min-height: 0; }
    SettingsPanel .settings-topic { height: auto; border: round $primary;
      padding: 0 1; margin-bottom: 1; }
    SettingsPanel .settings-topic-title { height: auto; text-style: bold; }
    SettingsPanel .settings-row { height: auto; min-height: 3; }
    SettingsPanel .settings-row Label { width: 24; height: 3;
      content-align: left middle; }
    SettingsPanel .settings-row Select, SettingsPanel .settings-row Input { width: 1fr; }
    SettingsPanel .settings-row SelectCurrent > #label {
      height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    SettingsPanel #pref-interval-error { height: auto; color: $error; }
    SettingsPanel #preferences-actions, SettingsPanel #host-logging-actions { height: 3; }
    SettingsPanel #preferences-actions Button, SettingsPanel #host-logging-actions Button {
      width: auto; min-width: 8; margin-right: 1;
    }
    """

    class ApplyRequested(Message):
        def __init__(self, panel, values):
            super().__init__()
            self.panel = panel
            self.values = values

    class RevertRequested(Message):
        def __init__(self, panel):
            super().__init__()
            self.panel = panel

    class SaveHostLoggingRequested(Message):
        def __init__(self, panel):
            super().__init__()
            self.panel = panel

    def __init__(
        self, values, themes, *, read_only=False, demo=False, resources="", **kwargs
    ):
        super().__init__(**kwargs)
        self.values, self.themes = dict(values), tuple(themes)
        self.read_only, self.demo, self.resources = read_only, demo, resources
        self._host_logging = None

    def _host_logging_view(self):
        if self.demo:
            return "/home/demo/.local/state/oc-box/log", "Off (default)"
        settings = self._host_logging
        if settings is None:
            return "Unavailable", "Unavailable"
        directory = settings.effective_directory
        if directory is None:
            try:
                directory = _activity_state_root(None) / "log"
            except (OSError, RuntimeError, ValueError):
                pass
        shown = str(directory) if directory is not None else ""
        if not shown.isprintable():
            shown = "Unavailable"
        mirror = (
            ("On" if settings.journald else "Off") + " (saved)"
            if settings.journald_configured
            else "Off (default)"
        )
        return shown, mirror

    @property
    def _can_edit_host_logging(self):
        return not (self.demo or self.read_only) and self._host_logging is not None

    def _compose_host_logging(self):
        if not self.demo:
            try:
                self._host_logging = log_config.load_host_settings()
            except log_config.LogConfigError:
                pass
        directory, mirror = self._host_logging_view()
        welcome = (
            "Demo: synthetic settings only."
            if self.demo
            else "Read-only mode: host logging settings cannot be saved."
            if self.read_only
            else ""
        )
        if not self.demo and self._host_logging is None:
            welcome = "Host logging configuration is invalid or unsafe."
        disabled = not self._can_edit_host_logging
        saved = self._host_logging
        with Vertical(classes="settings-topic", id="host-logging-settings"):
            yield Label("Host logging", classes="settings-topic-title")
            yield Static(
                f"Effective private log directory: {directory}",
                id="host-logging-directory-status",
                markup=False,
            )
            yield Static(
                f"System journal mirror: {mirror}",
                id="host-logging-mirror-status",
                markup=False,
            )
            with Horizontal(classes="settings-row"):
                yield Label("Private log directory")
                yield ClipboardInput(
                    (saved.directory or "") if saved else "",
                    placeholder="Default (accepts absolute paths or ~/…)",
                    id="host-logging-directory",
                    disabled=disabled,
                )
            yield Label("Mirror oc-box activity to system journal")
            with Horizontal(classes="settings-row settings-toggle-row"):
                yield Switch(
                    saved.journald if saved else False,
                    id="host-logging-mirror",
                    disabled=disabled,
                )
            yield Static(
                "The private file remains primary. Authorized host journal viewers can read mirrored activity; "
                "visibility and retention follow host policy.",
                markup=False,
            )
            yield Static(
                "Use default clears the directory override after Save. Saving affects future activity and "
                "does not create a log directory or move existing logs. Rotation and retention are config-only.",
                markup=False,
            )
            with Horizontal(id="host-logging-actions"):
                yield Button(
                    "Use default", id="host-logging-default", disabled=disabled
                )
                yield Button(
                    "Save Host Logging",
                    id="host-logging-save",
                    variant="primary",
                    disabled=disabled,
                )
            yield Static(welcome, id="host-logging-result", markup=False)

    def compose(self):
        with Vertical():
            yield Static(
                "Dashboard settings · session only · Reset fills defaults; Apply activates them",
                id="preferences-title",
                markup=False,
            )
            with VerticalScroll(id="preferences-topics"):
                with Vertical(classes="settings-topic"):
                    yield Label("Monitoring", classes="settings-topic-title")
                    with Horizontal(classes="settings-row settings-toggle-row"):
                        yield Label("Automatic refresh")
                        yield RoundedSwitch(
                            self.values["auto_refresh"], id="pref-auto-refresh"
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Refresh seconds (1–60)")
                        yield ClipboardInput(
                            str(self.values["interval"]),
                            type="integer",
                            validators=[Integer(minimum=1, maximum=60)],
                            id="pref-interval",
                        )
                    yield Static("", id="pref-interval-error", markup=False)
                    yield Static(
                        "Use any whole number from 1 to 60 seconds. Status and followed logs use this interval. "
                        "Slow queries never overlap or queue; storage scans remain manual.",
                        markup=False,
                    )
                with Vertical(classes="settings-topic"):
                    yield Label("Display", classes="settings-topic-title")
                    with Horizontal(classes="settings-row"):
                        yield Label("Theme")
                        yield Select(
                            [(name, name) for name in self.themes],
                            value=self.values["theme"],
                            allow_blank=False,
                            id="pref-theme",
                        )
                    with Horizontal(classes="settings-row settings-toggle-row"):
                        yield Label("Workspace details")
                        yield RoundedSwitch(self.values["details"], id="pref-details")
                    yield Static(
                        "Meters and trends use fixed scales; missing readings are not zero.",
                        markup=False,
                    )
                with Vertical(classes="settings-topic"):
                    yield Label("Sessions", classes="settings-topic-title")
                    with Horizontal(classes="settings-row"):
                        yield Label("Browse scope")
                        yield Select(
                            list(SESSION_SCOPES),
                            value=self.values["session_scope"],
                            allow_blank=False,
                            id="pref-session-scope",
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Session filter")
                        yield ClipboardInput(
                            self.values["session_filter"], id="pref-session-filter"
                        )
                    yield Static(
                        "Changing scope never connects automatically. Existing sources close only on Disconnect or exit.",
                        markup=False,
                    )
                with Vertical(classes="settings-topic"):
                    yield Label("Logs", classes="settings-topic-title")
                    with Horizontal(classes="settings-row settings-toggle-row"):
                        yield Label("Follow new entries")
                        yield RoundedSwitch(
                            self.values["log_follow"], id="pref-log-follow"
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Visible source")
                        yield Select(
                            list(LOG_SOURCES),
                            value=self.values["log_source"],
                            allow_blank=False,
                            id="pref-log-source",
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Minimum severity")
                        yield Select(
                            list(LOG_LEVELS),
                            value=self.values["log_level"],
                            allow_blank=False,
                            id="pref-log-level",
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Log search")
                        yield ClipboardInput(
                            self.values["log_query"], id="pref-log-query"
                        )
                    yield Static(
                        "Search stays available while paused. Retention: 2,000 entries / eight sources "
                        "plus a separate 1,000-entry mirror snapshot in memory. "
                        "Podman and journal mirror logs require explicit Load. "
                        "Search text is not saved to disk.",
                        markup=False,
                    )
                yield from self._compose_host_logging()
                with Vertical(classes="settings-topic"):
                    yield Label(
                        "Resources · information only", classes="settings-topic-title"
                    )
                    yield Static(
                        self.resources or "No workspace selected.",
                        id="settings-info",
                        markup=False,
                    )
                    yield Static(
                        "CPU/RAM/storage editing is deferred. These preferences never reset a container or apply resource limits.",
                        markup=False,
                    )
                with Vertical(classes="settings-topic"):
                    yield Label("Safety and terminals", classes="settings-topic-title")
                    yield Static(
                        (
                            "Read-only mode: session mutations and terminal attachment are disabled."
                            if self.read_only
                            else "Session mutations and terminal attachment require explicit actions."
                        )
                        + "\nThis mode cannot be weakened here. No browsing action unlocks the vault or starts a workspace. "
                        "Native host Connect may start the host service. Ctrl-] detaches a terminal; "
                        "shell input keeps its normal keys while focused.",
                        markup=False,
                    )
            with Horizontal(id="preferences-actions"):
                yield Button("Apply", id="preferences-apply", variant="primary")
                yield Button("Reset to defaults", id="preferences-reset")
                yield Button("Revert", id="preferences-cancel")

    def _draft_values(self):
        return {
            "auto_refresh": self.query_one("#pref-auto-refresh", Switch).value,
            "interval": self.query_one("#pref-interval", Input).value,
            "theme": self.query_one("#pref-theme", Select).value,
            "details": self.query_one("#pref-details", Switch).value,
            "session_scope": self.query_one("#pref-session-scope", Select).value,
            "session_filter": self.query_one("#pref-session-filter", Input).value,
            "log_follow": self.query_one("#pref-log-follow", Switch).value,
            "log_source": self.query_one("#pref-log-source", Select).value,
            "log_level": self.query_one("#pref-log-level", Select).value,
            "log_query": self.query_one("#pref-log-query", Input).value,
        }

    @property
    def dirty(self):
        return self._draft_values() != {
            **self.values,
            "interval": str(self.values["interval"]),
        }

    def load_values(self, values):
        """Replace a clean or explicitly reverted/applied draft, never on a poll."""
        self.values = dict(values)
        for key, selector in (
            ("auto_refresh", "#pref-auto-refresh"),
            ("details", "#pref-details"),
            ("log_follow", "#pref-log-follow"),
        ):
            self.query_one(selector, Switch).value = values[key]
        for key, selector in (
            ("theme", "#pref-theme"),
            ("session_scope", "#pref-session-scope"),
            ("log_source", "#pref-log-source"),
            ("log_level", "#pref-log-level"),
        ):
            self.query_one(selector, Select).value = values[key]
        for key, selector in (
            ("interval", "#pref-interval"),
            ("session_filter", "#pref-session-filter"),
            ("log_query", "#pref-log-query"),
        ):
            self.query_one(selector, Input).value = str(values[key])
        self.query_one("#pref-interval-error", Static).update("")

    def action_revert(self):
        self.post_message(self.RevertRequested(self))

    def save_host_logging(self):
        if not self._can_edit_host_logging:
            return
        directory = self.query_one("#host-logging-directory", Input)
        mirror = self.query_one("#host-logging-mirror", Switch)
        result = self.query_one("#host-logging-result", Static)
        try:
            settings, changed = log_config.save_host_settings(
                self._host_logging,
                directory=directory.value or None,
                journald=mirror.value,
            )
        except log_config.LogConfigError as exc:
            result.update(str(exc))
        else:
            self._host_logging = settings
            effective, configured = self._host_logging_view()
            self.query_one("#host-logging-directory-status", Static).update(
                f"Effective private log directory: {effective}"
            )
            self.query_one("#host-logging-mirror-status", Static).update(
                f"System journal mirror: {configured}"
            )
            result.update(
                "Host logging settings saved."
                if changed
                else "No host logging changes."
            )
        result.scroll_visible(animate=False)

    def reset_defaults(self):
        # Stage changes just like ordinary edits: Revert still discards them.
        # Read-only mode, resources, connections, favorites and log buffers are
        # deliberately outside this preference schema.
        for key, selector in (
            ("auto_refresh", "#pref-auto-refresh"),
            ("details", "#pref-details"),
            ("log_follow", "#pref-log-follow"),
        ):
            self.query_one(selector, Switch).value = DEFAULT_SETTINGS[key]
        for key, selector in (
            ("session_scope", "#pref-session-scope"),
            ("log_source", "#pref-log-source"),
            ("log_level", "#pref-log-level"),
        ):
            self.query_one(selector, Select).value = DEFAULT_SETTINGS[key]
        self.query_one("#pref-interval", Input).value = str(
            DEFAULT_SETTINGS["interval"]
        )
        self.query_one("#pref-interval-error", Static).update("")
        theme = DEFAULT_SETTINGS["theme"]
        self.query_one("#pref-theme", Select).value = (
            theme if theme in self.themes else self.values["theme"]
        )
        self.query_one("#pref-session-filter", Input).value = DEFAULT_SETTINGS[
            "session_filter"
        ]
        self.query_one("#pref-log-query", Input).value = DEFAULT_SETTINGS["log_query"]

    def on_button_pressed(self, event):
        if event.button.id in ("host-logging-save", "host-logging-default"):
            event.stop()
            if (
                not self._can_edit_host_logging
                or event.button.disabled
                or event.button is not self.query_one("#" + event.button.id, Button)
            ):
                return
            if event.button.id == "host-logging-default":
                self.query_one("#host-logging-directory", Input).value = ""
            else:
                self.post_message(self.SaveHostLoggingRequested(self))
        elif event.button.id == "preferences-cancel":
            event.stop()
            self.action_revert()
        elif event.button.id == "preferences-reset":
            event.stop()
            self.reset_defaults()
        elif event.button.id == "preferences-apply":
            event.stop()
            interval_input = self.query_one("#pref-interval", Input)
            raw_interval = interval_input.value
            try:
                # Input's integer filter and validators are presentation aids,
                # not the Apply boundary: programmatic values bypass filtering.
                interval = int(raw_interval)
            except (TypeError, ValueError):
                interval = None
            if (
                not raw_interval.isascii()
                or not raw_interval.isdecimal()
                or interval not in REFRESH_INTERVALS
            ):
                self.query_one("#pref-interval-error", Static).update(
                    "Enter a whole number from 1 to 60 seconds."
                )
                interval_input.focus()
                interval_input.scroll_visible(animate=False)
                return
            self.post_message(
                self.ApplyRequested(
                    self, {**self._draft_values(), "interval": interval}
                )
            )
