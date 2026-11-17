"""Synthetic layout and explicit-action contracts for the card dashboard."""

import copy
from contextlib import ExitStack
import importlib.util
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from textual.screen import Screen
    from textual.widgets import (
        Button,
        Collapsible,
        DataTable,
        Input,
        Select,
        TabbedContent,
    )


if AVAILABLE:

    class TwoWorkspaces(DemoBackend):
        def __init__(self):
            self.samples = self.log_samples = 0

        def collect(self, *, scan_storage=False):
            self.samples += 1
            report = super().collect(scan_storage=scan_storage)
            second = copy.deepcopy(report["workspaces"][0])
            second.update(
                workspace_id="b" * 32, project="/projects/second", state="stopped"
            )
            second["metrics"] = None
            report["workspaces"].append(second)
            return report

        def logs(self, workspace=None):
            self.log_samples += 1
            return super().logs(workspace)


@unittest.skipUnless(AVAILABLE, "optional dashboard dependencies unavailable")
class CardLayoutTests(unittest.IsolatedAsyncioTestCase):
    def test_workspace_cells_keep_invalid_and_stopped_metrics_unknown(self):
        for value in (None, True, False, float("nan"), float("inf"), -1, "16"):
            with self.subTest(value=repr(value)):
                self.assertEqual(DashboardApp._display_bytes(value), "n/a")
                self.assertEqual(
                    DashboardApp._workspace_cpu(
                        {"state": "running", "metrics": {"cpu_percent": value}}
                    ),
                    "n/a",
                )
        self.assertEqual(DashboardApp._display_bytes(0), "0 B")
        self.assertEqual(
            DashboardApp._workspace_cpu(
                {"state": "stopped", "metrics": {"cpu_percent": 42}}
            ),
            "n/a",
        )

    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    def fully_visible(self, app, widget):
        region = app.screen.find_widget(widget)
        self.assertEqual(region.region.intersection(region.clip), region.region)

    async def test_wide_navigation_and_compact_picker_keep_all_seven_sections(self):
        backend = TwoWorkspaces()
        app = DashboardApp(backend, demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                self.assertEqual(app.theme, "oc-box-navy")
                self.assertEqual(len(app.query(".nav-choice")), 7)
                self.assertFalse(app.query_one("#section-picker").display)
                self.assertTrue(app.query_one("#sidebar").display)
                for label, key in app.SECTIONS:
                    await pilot.click(f"#nav-{key}")
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one("#content", TabbedContent).active, key
                    )
                    self.assertTrue(app.query_one(f"#nav-{key}").has_class("-selected"))
                for size in ((80, 24), (100, 30)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    self.assertFalse(app.query_one("#sidebar").display)
                    picker = app.query_one("#section-picker", Select)
                    self.assertTrue(picker.display)
                    self.fully_visible(app, picker)
                    self.fully_visible(app, app.query_one("#workspace-picker"))
                    self.fully_visible(app, app.query_one("#log-search"))
                    for _, key in app.SECTIONS:
                        picker.value = key
                        await pilot.pause()
                        self.assertEqual(app.query_one("#content").active, key)
                self.assertIsNone(app.catalog)
                self.assertEqual((backend.samples, backend.log_samples), (1, 1))

    async def test_workspace_picker_changes_context_without_native_connection(self):
        backend = TwoWorkspaces()
        app = DashboardApp(backend, demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                initial = (backend.samples, backend.log_samples)
                app.action_tab("sessions")
                with patch.object(app, "connect_sessions") as connect:
                    app.query_one("#workspace-picker", Select).value = "b" * 32
                    await pilot.pause()
                self.assertEqual(app.selected, "b" * 32)
                self.assertEqual(app.query_one("#content").active, "sessions")
                self.assertEqual(app.query_one("#workspaces", DataTable).cursor_row, 1)
                self.assertIn("second", str(app.query_one("#nav-context").render()))
                self.assertEqual((backend.samples, backend.log_samples), initial)
                connect.assert_not_called()
                self.assertTrue(app.query_one("#delete", Button).disabled)
                self.assertTrue(app.query_one("#terminal-owner", Button).disabled)

    async def test_short_wide_terminal_keeps_every_section_reachable(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 24)) as pilot:
                await self.ready(app, pilot)
                self.assertTrue(app._compact_layout)
                self.assertFalse(app.query_one("#sidebar").display)
                picker = app.query_one("#section-picker", Select)
                self.fully_visible(app, picker)
                for _, key in app.SECTIONS:
                    picker.value = key
                    await pilot.pause()
                    self.assertEqual(app.query_one("#content").active, key)
                await pilot.resize_terminal(140, 44)
                await pilot.pause()
                last = app.query_one("#nav-backups", Button)
                self.fully_visible(app, last)
                hit, _ = app.screen.get_widget_at(last.region.x + 3, last.region.y)
                self.assertIs(hit, last, "Context card must not cover navigation")

    async def test_unchanged_reports_preserve_picker_options_and_selected_identity(
        self,
    ):
        backend = TwoWorkspaces()
        app = DashboardApp(backend, demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                picker = app.query_one("#workspace-picker", Select)
                picker.value = "b" * 32
                await pilot.pause()
                report = copy.deepcopy(app.report)
                with patch.object(
                    picker, "set_options", wraps=picker.set_options
                ) as options:
                    for _ in range(5):
                        app._apply_report(copy.deepcopy(report), False)
                    await pilot.pause()
                options.assert_not_called()
                self.assertEqual((app.selected, picker.value), ("b" * 32, "b" * 32))
                self.assertEqual(app.query_one("#workspaces").row_count, 2)

    async def test_inspector_resizes_without_weakening_details_or_mode(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                inspector = app.query_one("#workspace-inspector", Collapsible)
                table = app.query_one("#workspace-card")
                self.assertFalse(inspector.collapsed)
                self.assertGreater(inspector.region.x, table.region.x)
                self.assertEqual(len(app.query_one("#workspaces").columns), 5)
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertTrue(inspector.collapsed)
                self.assertEqual(inspector.region.x, table.region.x)
                inspector.collapsed = False
                app.query_one("#scan-storage").scroll_visible(animate=False)
                await pilot.pause()
                self.fully_visible(app, app.query_one("#scan-storage"))
                self.assertTrue(app.read_only)
                self.assertIsNone(app.catalog)

    async def test_private_shield_rejects_context_and_navigation_changes(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                selected = app.selected
                app._private_interaction = True
                with patch.object(app, "connect_sessions") as connect:
                    app.query_one("#workspace-picker", Select).value = "b" * 32
                    app.query_one("#section-picker", Select).value = "logs"
                    await pilot.pause()
                self.assertEqual(app.selected, selected)
                self.assertEqual(app.query_one("#workspace-picker").value, selected)
                self.assertEqual(app.query_one("#content").active, "status")
                self.assertEqual(app.query_one("#section-picker").value, "status")
                self.assertFalse(app.check_action("tab", ("logs",)))
                connect.assert_not_called()
                app._private_interaction = False

    def assert_picker_events_ignored(self, app, events, *, no_queries=False):
        selected, catalog = app.selected, app.catalog
        samples = (app.backend.samples, app.backend.log_samples)
        with ExitStack() as stack:
            calls = [
                stack.enter_context(patch.object(app, name))
                for name in (
                    "_clipboard_private",
                    "_sync_workspace_picker",
                    "_sync_navigation",
                    "_choose_workspace",
                    "_session_source_changed",
                    "action_tab",
                    "connect_sessions",
                    "pop_screen",
                )
            ]
            stack.enter_context(
                patch.object(app, "query_one", side_effect=AssertionError("late query"))
            )
            if no_queries:
                stack.enter_context(
                    patch.object(app, "query", side_effect=AssertionError("late query"))
                )
            for event in events:
                with self.subTest(picker=event.select.id):
                    app.on_select_changed(event)
            for call in calls:
                call.assert_not_called()
        self.assertEqual(app.selected, selected)
        self.assertIs(app.catalog, catalog)
        self.assertEqual((app.backend.samples, app.backend.log_samples), samples)

    async def test_picker_event_lifetime_matrix_ignores_detach_and_shutdown(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        retained = []
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                for picker_id in ("workspace-picker", "section-picker"):
                    picker = app.query_one(f"#{picker_id}", Select)
                    self.assertTrue(picker.is_attached)
                    retained.append(Select.Changed(picker, picker.value))
                    await picker.remove()
                    self.assertFalse(picker.is_attached)
                app._private_interaction = True
                self.assertTrue(app.is_running)
                self.assert_picker_events_ignored(app, retained, no_queries=True)
            self.assertFalse(app.is_running)
            self.assert_picker_events_ignored(app, retained, no_queries=True)

    async def test_picker_event_admission_matrix_ignores_stale_unmounted_missing_root(
        self,
    ):
        for state in ("stale value", "unmounted source", "missing root"):
            with self.subTest(state=state):
                app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
                with patch.object(app, "_tick"):
                    async with app.run_test(size=(140, 44)) as pilot:
                        await self.ready(app, pilot)
                        events = []
                        for picker_id, stale in (
                            ("workspace-picker", "b" * 32),
                            ("section-picker", "logs"),
                        ):
                            picker = app.query_one(f"#{picker_id}", Select)
                            events.append(
                                Select.Changed(
                                    picker,
                                    stale if state == "stale value" else picker.value,
                                )
                            )
                        if state == "missing root":
                            await app.query_one("#content").remove()
                        app._private_interaction = True
                        with ExitStack() as stack:
                            if state == "unmounted source":
                                stack.enter_context(
                                    patch.object(app, "is_mounted", return_value=False)
                                )
                            self.assert_picker_events_ignored(
                                app, events, no_queries=state != "missing root"
                            )

    async def test_picker_event_source_matrix_ignores_attached_foreign_same_ids(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                app._private_interaction = True
                foreign_screen = Screen()
                await app.push_screen(foreign_screen)
                events = []
                for picker_id, value in (
                    ("workspace-picker", "b" * 32),
                    ("section-picker", "logs"),
                ):
                    foreign = Select([(value, value)], value=value, id=picker_id)
                    with foreign.prevent(Select.Changed):
                        await foreign_screen.mount(foreign)
                        await pilot.pause()
                    self.assertTrue(foreign.is_attached)
                    self.assertTrue(app.is_mounted(foreign))
                    self.assertIsNot(app.query_one(f"#{picker_id}"), foreign)
                    events.append(Select.Changed(foreign, value))
                self.assert_picker_events_ignored(app, events)

    async def test_current_picker_private_event_matrix_restores_below_modal(self):
        app = DashboardApp(TwoWorkspaces(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                selected = app.selected
                private_screen = Screen()
                private_screen.PRIVATE_INPUT = True
                await app.push_screen(private_screen)
                for picker_id, value, original, restore in (
                    ("workspace-picker", "b" * 32, selected, "_sync_workspace_picker"),
                    ("section-picker", "logs", "status", "_sync_navigation"),
                ):
                    with self.subTest(picker=picker_id):
                        picker = app.query_one(f"#{picker_id}", Select)
                        self.assertIsNot(picker.screen, app.screen)
                        with picker.prevent(Select.Changed):
                            picker.value = value
                        with (
                            patch.object(
                                app, restore, wraps=getattr(app, restore)
                            ) as reset,
                            patch.object(app, "_session_source_changed") as source,
                            patch.object(app, "connect_sessions") as connect,
                        ):
                            app.on_select_changed(Select.Changed(picker, value))
                        reset.assert_called_once()
                        source.assert_not_called()
                        connect.assert_not_called()
                        self.assertEqual(picker.value, original)
                        self.assertEqual(app.selected, selected)
                        self.assertEqual(app.query_one("#content").active, "status")
                        self.assertIsNone(app.catalog)

    async def test_empty_inventory_keeps_host_controls_and_navigation_available(self):
        class Empty(DemoBackend):
            def collect(self, **kwargs):
                report = super().collect(**kwargs)
                report["workspaces"] = []
                return report

        app = DashboardApp(Empty(), demo=True, read_only=True)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                self.assertTrue(app.query_one("#workspace-picker").disabled)
                self.assertEqual(app.query_one("#workspaces").row_count, 0)
                app.query_one("#section-picker", Select).value = "settings"
                await pilot.pause()
                self.assertEqual(app.query_one("#content").active, "settings")
                self.assertTrue(app.query_one("#pref-auto-refresh").value)
                self.assertTrue(app._settings_snapshot()["auto_refresh"])
                self.assertIsNone(app.catalog)
