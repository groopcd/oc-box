"""Session-only dashboard preferences, grouped without resource mutations."""

from types import MappingProxyType

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.validation import Integer
from textual.widgets import Button, Input, Label, Select, Static, Switch

if __package__:
    from .tui_clipboard import ClipboardInput
else:
    from tui_clipboard import ClipboardInput

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
    """One inline draft; only Apply changes the dashboard's active preferences."""

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
    SettingsPanel #preferences-actions { height: 3; }
    SettingsPanel #preferences-actions Button { width: auto; min-width: 8; margin-right: 1; }
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

    def __init__(self, values, themes, *, read_only=False, resources="", **kwargs):
        super().__init__(**kwargs)
        self.values, self.themes = dict(values), tuple(themes)
        self.read_only, self.resources = read_only, resources

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
                        yield Label("Log search")
                        yield ClipboardInput(
                            self.values["log_query"], id="pref-log-query"
                        )
                    yield Static(
                        "Search stays available while paused. Retention: 2,000 entries / eight sources in memory; "
                        "Podman logs require explicit Load. Search text is not saved to disk.",
                        markup=False,
                    )
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
        if event.button.id == "preferences-cancel":
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
