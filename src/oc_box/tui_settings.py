"""Session-only dashboard preferences, grouped without resource mutations."""

from types import MappingProxyType

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.validation import Integer
from textual.widgets import Button, Input, Label, Select, Static, Switch

if __package__:
    from .tui_clipboard import ClipboardBar, ClipboardInput
else:
    from tui_clipboard import ClipboardBar, ClipboardInput

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
        "theme": "textual-dark",
        "details": False,
        "session_scope": "global",
        "session_filter": "",
        "log_follow": True,
        "log_source": "all",
        "log_query": "",
    }
)


class SettingsScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    SettingsScreen { align: center middle; }
    #preferences-window { width: 100; max-width: 95%; height: 85%;
      border: round $accent; background: $surface; padding: 0 1; }
    #preferences-title { height: 2; text-style: bold; }
    #preferences-topics { height: 1fr; min-height: 0; }
    .settings-topic { height: auto; border: round $primary; padding: 0 1; margin-bottom: 1; }
    .settings-topic-title { height: 1; text-style: bold; }
    .settings-row { height: auto; min-height: 3; }
    .settings-row Label { width: 24; padding: 1 0; }
    .settings-row Select, .settings-row Input { width: 1fr; }
    .settings-row SelectCurrent > #label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #pref-interval-error { height: auto; color: $error; }
    #preferences-actions { height: 3; }
    #preferences-actions Button { margin-right: 1; }
    """

    def __init__(self, values, themes, *, read_only=False, resources=""):
        super().__init__()
        self.values, self.themes = dict(values), tuple(themes)
        self.read_only, self.resources = read_only, resources

    def compose(self):
        with Vertical(id="preferences-window"):
            yield Static(
                "Dashboard settings · session only · Reset fills defaults; Apply activates them",
                id="preferences-title",
                markup=False,
            )
            with VerticalScroll(id="preferences-topics"):
                with Vertical(classes="settings-topic"):
                    yield Label("Monitoring", classes="settings-topic-title")
                    with Horizontal(classes="settings-row"):
                        yield Label("Automatic refresh")
                        yield Switch(
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
                    with Horizontal(classes="settings-row"):
                        yield Label("Workspace details")
                        yield Switch(self.values["details"], id="pref-details")
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
                    with Horizontal(classes="settings-row"):
                        yield Label("Follow new entries")
                        yield Switch(self.values["log_follow"], id="pref-log-follow")
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
                        self.resources or "No workspace selected.", markup=False
                    )
                    yield Static(
                        "CPU/RAM/storage editing is deferred. This window never resets a container or applies resource limits.",
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
            yield ClipboardBar()
            with Horizontal(id="preferences-actions"):
                yield Button("Apply", id="preferences-apply", variant="primary")
                yield Button("Reset to defaults", id="preferences-reset")
                yield Button("Cancel", id="preferences-cancel")

    def action_cancel(self):
        self.dismiss(None)

    def reset_defaults(self):
        # Stage changes just like ordinary edits: Cancel still discards them.
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
            self.action_cancel()
        elif event.button.id == "preferences-reset":
            self.reset_defaults()
        elif event.button.id == "preferences-apply":
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
            self.dismiss(
                {
                    "auto_refresh": self.query_one("#pref-auto-refresh", Switch).value,
                    "interval": interval,
                    "theme": self.query_one("#pref-theme", Select).value,
                    "details": self.query_one("#pref-details", Switch).value,
                    "session_scope": self.query_one(
                        "#pref-session-scope", Select
                    ).value,
                    "session_filter": self.query_one(
                        "#pref-session-filter", Input
                    ).value,
                    "log_follow": self.query_one("#pref-log-follow", Switch).value,
                    "log_source": self.query_one("#pref-log-source", Select).value,
                    "log_query": self.query_one("#pref-log-query", Input).value,
                }
            )
