"""Inline, session-only settings on the real dashboard with synthetic data."""

import copy
import importlib.util
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from oc_box.tui import DemoBackend

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        Input,
        Label,
        Select,
        Static,
        Switch,
        TabbedContent,
    )

    from oc_box.tui_app import DashboardApp
    from oc_box.tui_settings import DEFAULT_SETTINGS, SettingsPanel


class SettingsDemo(DemoBackend):
    def session_connection(self, workspace=None):
        raise AssertionError("Settings must never connect to a native service")

    def collect(self, *, scan_storage=False):
        report = super().collect(scan_storage=scan_storage)
        other = copy.deepcopy(report["workspaces"][0])
        other.update(workspace_id="b" * 32, project="/home/demo/projects/second")
        other["configured_limits"]["cpu_cores"] = 2
        report["workspaces"].append(other)
        return report


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class InlineSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()

    def assert_visible(self, app, widget):
        geometry = app.screen.find_widget(widget)
        self.assertGreater(geometry.region.width, 0)
        self.assertGreater(geometry.region.height, 0)
        self.assertEqual(geometry.region.intersection(geometry.clip), geometry.region)

    async def test_every_topic_and_field_is_inline_and_reachable_at_narrow_and_wide_sizes(
        self,
    ):
        for size in ((80, 24), (140, 44)):
            with self.subTest(size=size):
                app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    screen = app.screen
                    await pilot.press("2")
                    self.assertIs(app.screen, screen)
                    self.assertNotIsInstance(app.screen, ModalScreen)
                    self.assertEqual(
                        app.query_one("#content", TabbedContent).active, "settings"
                    )
                    panel = app.query_one(SettingsPanel)
                    self.assertEqual(
                        [
                            str(label.render())
                            for label in panel.query(".settings-topic-title")
                        ],
                        [
                            "Monitoring",
                            "Display",
                            "Sessions",
                            "Logs",
                            "Resources · information only",
                            "Safety and terminals",
                        ],
                    )
                    self.assertFalse(
                        app.query("#show-settings, #auto-refresh, #refresh-interval")
                    )
                    for ident in (
                        "pref-auto-refresh",
                        "pref-interval",
                        "pref-theme",
                        "pref-details",
                        "pref-session-scope",
                        "pref-session-filter",
                        "pref-log-follow",
                        "pref-log-source",
                        "pref-log-query",
                        "settings-info",
                    ):
                        with self.subTest(field=ident):
                            field = panel.query_one("#" + ident)
                            field.scroll_visible(animate=False, force=True)
                            await pilot.pause()
                            self.assert_visible(app, field)
                            for action in (
                                "preferences-apply",
                                "preferences-reset",
                                "preferences-cancel",
                            ):
                                self.assert_visible(app, panel.query_one("#" + action))
                    safety = panel.query(".settings-topic")[-1]
                    safety.scroll_visible(animate=False, force=True)
                    await pilot.pause()
                    self.assert_visible(app, safety)
                    self.assertIn(
                        "cannot be weakened", str(safety.query(Static)[-1].render())
                    )
                    # The normal application header never scrolls off screen.
                    self.assertEqual(app.query_one("#body").scroll_offset, (0, 0))
                    self.assertEqual(app.query_one("#content").scroll_offset, (0, 0))

    async def test_entry_status_refresh_and_resource_selection_preserve_unsaved_drafts(
        self,
    ):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            app.action_settings()
            await pilot.pause()
            panel = app.query_one(SettingsPanel)
            panel.query_one("#pref-interval", Input).value = "17"
            panel.query_one("#pref-auto-refresh", Switch).value = False
            panel.query_one("#pref-session-scope", Select).value = "workspace"
            panel.query_one("#pref-session-filter", Input).value = "unsaved sessions"
            panel.query_one("#pref-log-query", Input).value = "unsaved logs"
            draft = panel._draft_values()
            applied = app._settings_snapshot()
            app.action_tab("status")
            await pilot.pause()
            # A local report re-render is not a native inventory or connection.
            report = copy.deepcopy(app.report)
            report["sampled_at"] = "2026-01-01T12:00:01Z"
            app._apply_report(report, False)
            app._choose_workspace("b" * 32)
            app.action_settings()
            await pilot.pause()
            await pilot.press("f2")
            await pilot.pause()
            self.assertEqual(panel._draft_values(), draft)
            self.assertEqual(app._settings_snapshot(), applied)
            self.assertTrue(panel.dirty)
            self.assertIn(
                "/home/demo/projects/second",
                str(panel.query_one("#settings-info", Static).render()),
            )
            self.assertIn(
                "Saved: CPU 2", str(panel.query_one("#settings-info", Static).render())
            )
            self.assertIsNone(app.catalog)
            self.assertIsNone(app._session_connection)
            # Draft monitoring changes do not silently pause or alter cadence.
            app._last_report = app._last_logs = 100
            with (
                patch("oc_box.tui_app.time.monotonic", return_value=101),
                patch.object(app, "refresh_report") as report_refresh,
                patch.object(app, "refresh_logs") as log_refresh,
            ):
                app._tick()
                report_refresh.assert_called_once_with()
                log_refresh.assert_called_once_with()
            await pilot.click("#preferences-cancel")
            await pilot.pause()
            self.assertFalse(panel.dirty)
            self.assertEqual(panel.query_one("#pref-interval", Input).value, "1")
            # A clean entry, unlike a draft entry, reflects other live controls.
            app.action_tab("logs")
            app.query_one("#log-search", Input).value = "live search"
            app.query_one("#log-follow", Switch).value = False
            await pilot.pause()
            app.action_settings()
            await pilot.pause()
            self.assertEqual(
                panel.query_one("#pref-log-query", Input).value, "live search"
            )
            self.assertFalse(panel.query_one("#pref-log-follow", Switch).value)
            self.assertFalse(panel.dirty)

    async def test_reset_and_revert_do_no_backend_io_and_never_weaken_read_only(self):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            modified = {
                **DEFAULT_SETTINGS,
                "auto_refresh": False,
                "interval": 59,
                "session_scope": "project",
                "log_follow": False,
                "session_filter": "retained session filter",
                "log_query": "retained log filter",
            }
            app.apply_settings(modified)
            await pilot.pause()
            retained = list(app.logs.search())
            generation = app._catalog_generation
            with (
                patch.object(app.backend, "collect") as collect,
                patch.object(app.backend, "logs") as logs,
                patch.object(app.backend, "session_connection") as connect,
                patch.object(app, "connect_sessions") as connect_ui,
            ):
                app.action_settings()
                await pilot.pause()
                panel = app.query_one(SettingsPanel)
                for action in ("button", "escape"):
                    with self.subTest(revert=action):
                        await pilot.click("#preferences-reset")
                        await pilot.pause()
                        self.assertEqual(
                            panel.query_one("#pref-interval", Input).value, "1"
                        )
                        self.assertEqual(app._settings_snapshot(), modified)
                        if action == "button":
                            await pilot.click("#preferences-cancel")
                        else:
                            panel.query_one("#pref-interval", Input).focus()
                            await pilot.press("escape")
                        await pilot.pause()
                        self.assertEqual(panel.values, modified)
                        self.assertFalse(panel.dirty)
                        self.assertEqual(app._settings_snapshot(), modified)
                collect.assert_not_called()
                logs.assert_not_called()
                connect.assert_not_called()
                connect_ui.assert_not_called()
            self.assertEqual(app._catalog_generation, generation)
            self.assertEqual(app.logs.search(), retained)
            self.assertTrue(app.read_only)
            for ident in (
                "delete",
                "archive",
                "favorite",
                "terminal-owner",
                "terminal-selected",
            ):
                self.assertTrue(app.query_one("#" + ident, Button).disabled)
            self.assertIsNone(app.catalog)

    async def test_source_drafts_cancel_pending_inventory_only_after_valid_apply(self):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            app.action_settings()
            await pilot.pause()
            panel = app.query_one(SettingsPanel)
            panel.query_one("#preferences-apply", Button).active_effect_duration = 0
            cancel, connection = threading.Event(), Mock()
            app._pending_cancel, app._pending_connection = cancel, connection
            generation = app._catalog_generation
            try:
                with patch.object(app, "connect_sessions") as connect:
                    panel.query_one("#pref-session-scope", Select).value = "workspace"
                    panel.query_one("#pref-interval", Input).value = "0"
                    await pilot.pause()
                    self.assertEqual(app._catalog_generation, generation)
                    self.assertFalse(cancel.is_set())
                    await pilot.click("#preferences-apply")
                    await pilot.pause()
                    self.assertEqual(app._catalog_generation, generation)
                    self.assertFalse(cancel.is_set())
                    connection.close.assert_not_called()
                    panel.query_one("#pref-interval", Input).value = "17"
                    await pilot.click("#preferences-apply")
                    await pilot.pause()
                    self.assertGreater(app._catalog_generation, generation)
                    self.assertTrue(cancel.is_set())
                    connection.close.assert_called_once_with()
                    self.assertEqual(
                        app.query_one("#session-scope", Select).value, "workspace"
                    )
                    self.assertEqual(app._settings_snapshot()["interval"], 17)
                    self.assertFalse(panel.dirty)
                    connect.assert_not_called()
            finally:
                app._pending_cancel = app._pending_connection = None

    async def test_rounded_switches_align_labels_and_keep_native_click_and_space(
        self,
    ):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            for tab, ident in (
                ("settings", "pref-auto-refresh"),
                ("settings", "pref-details"),
                ("settings", "pref-log-follow"),
                ("logs", "log-follow"),
            ):
                with self.subTest(toggle=ident):
                    app.action_tab(tab)
                    toggle = app.query_one("#" + ident, Switch)
                    toggle.scroll_visible(animate=False, force=True)
                    await pilot.pause()
                    self.assert_visible(app, toggle)
                    self.assertEqual(toggle.region.height, 3)
                    self.assertEqual(toggle.region.width, 8)
                    label = toggle.parent.query_one(Label)
                    strips = app.screen._compositor.render_strips()
                    middle = toggle.region.y + 1
                    self.assertIn(str(label.render()), strips[middle].text)
                    self.assertIn(
                        "ON  ●" if toggle.value else "● OFF", strips[middle].text
                    )
                    # No overlap with the following form row.
                    self.assertLessEqual(
                        toggle.region.bottom, toggle.parent.region.bottom
                    )
                    before = toggle.value
                    await pilot.click(toggle, offset=(3, 1))
                    await pilot.pause()
                    self.assertEqual(toggle.value, not before)
                    self.assertTrue(toggle.has_focus)
                    for side in (
                        toggle.styles.border.top,
                        toggle.styles.border.bottom,
                        toggle.styles.border.left,
                        toggle.styles.border.right,
                    ):
                        self.assertEqual(side[0], "round")
                    focus_border = toggle.styles.border.top
                    self.assertEqual(toggle.region.height, 3)
                    position = toggle._slider_position
                    await pilot.press("space")
                    await pilot.pause()
                    self.assertEqual(toggle.value, before)
                    self.assertNotEqual(toggle._slider_position, position)
                    self.assertEqual(toggle.styles.border.top, focus_border)
                    self.assertTrue(toggle.has_focus)
                    self.assertTrue(app.read_only)
                    self.assertTrue(app.query_one("#terminal-owner", Button).disabled)
                    if ident.startswith("pref-"):
                        self.assertTrue(app._settings_snapshot()["auto_refresh"])
                        self.assertFalse(app._settings_snapshot()["details"])
                        self.assertTrue(app._settings_snapshot()["log_follow"])

    async def test_invalid_non_interval_preferences_leave_all_applied_values_unchanged(
        self,
    ):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            baseline = app._settings_snapshot()
            generation = app._catalog_generation
            for name, changes in (
                ("unknown theme", {"theme": "not-an-installed-theme"}),
                ("malformed theme", {"theme": ["oc-box-navy"]}),
                ("unknown session scope", {"session_scope": "connect-now"}),
                ("partial source selector", {"session_scope": None}),
                ("unknown log source", {"log_source": "unloaded-file"}),
                ("non-boolean auto", {"auto_refresh": 1}),
                ("non-boolean details", {"details": "true"}),
                ("non-boolean follow", {"log_follow": None}),
                ("non-text session filter", {"session_filter": ["private"]}),
                ("non-text log query", {"log_query": {"query": "private"}}),
            ):
                with self.subTest(invalid_preference=name):
                    app.apply_settings(
                        {
                            **baseline,
                            "interval": 17,
                            "auto_refresh": False,
                            "details": True,
                            "log_follow": False,
                            "session_scope": "workspace",
                            "session_filter": "must not apply",
                            "log_query": "must not apply",
                            **changes,
                        }
                    )
                    self.assertEqual(app._settings_snapshot(), baseline)
                    self.assertEqual(app.query_one(SettingsPanel).values, baseline)
                    self.assertEqual(app._catalog_generation, generation)
                    self.assertIsNone(app.catalog)
                    self.assertTrue(app.read_only)

    async def test_inline_settings_navigation_and_queued_messages_respect_private_shield(
        self,
    ):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            panel = app.query_one(SettingsPanel)
            panel.query_one("#pref-interval", Input).value = "17"
            baseline = app._settings_snapshot()
            with patch.object(app, "_clipboard_private", return_value=True):
                app.action_settings()
                await pilot.press("f2")
                app.on_settings_panel_apply_requested(
                    SettingsPanel.ApplyRequested(panel, {**baseline, "interval": 17})
                )
                app.on_settings_panel_revert_requested(
                    SettingsPanel.RevertRequested(panel)
                )
                await pilot.pause()
                self.assertEqual(
                    app.query_one("#content", TabbedContent).active, "status"
                )
                self.assertEqual(app._settings_snapshot(), baseline)
                self.assertEqual(panel.query_one("#pref-interval", Input).value, "17")
                self.assertTrue(panel.dirty)
            self.assertTrue(app.read_only)

    def settings_requests(self, panel, baseline):
        return (
            (
                "on_settings_panel_apply_requested",
                SettingsPanel.ApplyRequested(
                    panel, {**baseline, "auto_refresh": False, "interval": 17}
                ),
            ),
            (
                "on_settings_panel_revert_requested",
                SettingsPanel.RevertRequested(panel),
            ),
        )

    def assert_settings_requests_ignored(self, app, requests, *, no_queries=False):
        monitoring = dict(app._monitoring_settings)
        generation, theme = app._catalog_generation, app.theme
        with ExitStack() as stack:
            guarded = [
                stack.enter_context(patch.object(app, name))
                for name in (
                    "_clipboard_private",
                    "apply_settings",
                    "_settings_snapshot",
                    "connect_sessions",
                )
            ]
            stack.enter_context(
                patch.object(app, "query_one", side_effect=AssertionError("late query"))
            )
            if no_queries:
                stack.enter_context(
                    patch.object(app, "query", side_effect=AssertionError("late query"))
                )
            for name, event in requests:
                with self.subTest(request=name):
                    getattr(app, name)(event)
            for call in guarded:
                call.assert_not_called()
        self.assertEqual(app._monitoring_settings, monitoring)
        self.assertEqual(app._catalog_generation, generation)
        self.assertEqual(app.theme, theme)
        self.assertTrue(app.read_only)

    async def test_retained_settings_requests_are_ignored_after_detach_and_shutdown(
        self,
    ):
        app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            panel = app.query_one(SettingsPanel)
            requests = self.settings_requests(panel, app._settings_snapshot())
            await panel.remove()
            self.assertFalse(panel.is_attached)
            for private in (False, True):
                with self.subTest(private_shield=private):
                    app._private_interaction = private
                    self.assert_settings_requests_ignored(
                        app, requests, no_queries=True
                    )
            app._private_interaction = False
        self.assertFalse(app.is_running)
        self.assert_settings_requests_ignored(app, requests, no_queries=True)

    async def test_settings_requests_require_the_current_mounted_dashboard_panel(self):
        for state in ("foreign same id", "unmounted source", "missing root"):
            with self.subTest(state=state):
                app = DashboardApp(SettingsDemo(), read_only=True, demo=True)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(app, pilot)
                    baseline = app._settings_snapshot()
                    panel = app.query_one(SettingsPanel)
                    if state in ("foreign same id", "missing root"):
                        if state == "missing root":
                            await app.query_one("#content").remove()
                        foreign_screen = Screen()
                        await app.push_screen(foreign_screen)
                        panel = SettingsPanel(
                            baseline,
                            sorted(app.available_themes),
                            read_only=True,
                            id="settings-panel",
                        )
                        await foreign_screen.mount(panel)
                        await pilot.pause()
                        self.assertTrue(panel.is_attached)
                        self.assertTrue(app.is_mounted(panel))
                    requests = self.settings_requests(panel, baseline)
                    with ExitStack() as stack:
                        if state == "unmounted source":
                            stack.enter_context(
                                patch.object(app, "is_mounted", return_value=False)
                            )
                        self.assert_settings_requests_ignored(
                            app, requests, no_queries=state == "unmounted source"
                        )


if __name__ == "__main__":
    unittest.main()
