"""Synthetic layout and explicit-action contracts for the card dashboard."""

import copy
import importlib.util
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
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
                self.assertEqual(app.query_one("#workspaces", DataTable).cursor_row, 1)
                self.assertIn("second", str(app.query_one("#nav-context").render()))
                self.assertEqual((backend.samples, backend.log_samples), initial)
                connect.assert_not_called()
                self.assertTrue(app.query_one("#delete", Button).disabled)
                self.assertTrue(app.query_one("#terminal-owner", Button).disabled)

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
                self.assertTrue(app.query_one("#auto-refresh").value)
                self.assertIsNone(app.catalog)
