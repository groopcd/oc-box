"""Retained-log search feedback and clear gestures, without host data."""

import importlib.util
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import Input, RichLog, Select, Static

    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp


@unittest.skipUnless(AVAILABLE, "optional dashboard dependencies unavailable")
class LogFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()

    def feedback(self, app):
        return str(app.query_one("#log-hits", Static).render())

    async def test_clear_search_restores_retained_entry_count_without_backend_work(
        self,
    ):
        for size in ((140, 44), (80, 24)):
            with self.subTest(size=size):
                app = DashboardApp(DemoBackend(), demo=True, read_only=True)
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    self.assertEqual(self.feedback(app), "2 log entries")
                    field = app.query_one("#log-search", Input)
                    for query, expected in (
                        ("ready", "1 log match"),
                        ("absent", "0 log matches"),
                    ):
                        with self.subTest(query=query):
                            field.value = query
                            await pilot.pause(0.2)
                            self.assertEqual(self.feedback(app), expected)
                            # The one-row compact header and tall input both
                            # expose the actual composited clear-cell location.
                            offset = field._clear_offset
                            self.assertIsNotNone(offset)
                            self.assertEqual(field._clear_region.width, 2)
                            with (
                                patch.object(app.backend, "collect") as collect,
                                patch.object(app.backend, "logs") as logs,
                                patch.object(app, "connect_sessions") as connect,
                            ):
                                await pilot.click(field, offset=tuple(offset))
                                await pilot.pause(0.2)
                            self.assertEqual((field.value, app.log_query), ("", ""))
                            self.assertEqual(self.feedback(app), "2 log entries")
                            self.assertEqual(len(app.logs.search()), 2)
                            self.assertIs(app.focused, field)
                            collect.assert_not_called()
                            logs.assert_not_called()
                            connect.assert_not_called()

    async def test_count_obeys_selected_source_and_never_treats_clear_as_delete(self):
        app = DashboardApp(DemoBackend(), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            source = app.query_one("#log-source", Select)
            source.value = "podman"
            await pilot.pause()
            self.assertEqual(self.feedback(app), "1 log entry")
            field = app.query_one("#log-search", Input)
            field.value = "absent"
            await pilot.pause(0.2)
            self.assertEqual(self.feedback(app), "0 log matches")
            field.value = ""
            await pilot.pause(0.2)
            self.assertEqual(self.feedback(app), "1 log entry")
            self.assertEqual(len(app.logs.search()), 2)
            source.value = "all"
            await pilot.pause()
            self.assertEqual(self.feedback(app), "2 log entries")

    async def test_selected_log_view_stays_frozen_but_feedback_follows_query(self):
        app = DashboardApp(DemoBackend(), demo=True, read_only=True)
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            app.action_tab("logs")
            await pilot.pause()
            await pilot.click("#log-mode")
            output = app.query_one("#log-output", RichLog)
            self.assertTrue(output.display)
            selection = Selection(Offset(0, 0), Offset(5, 0))
            app.screen.selections = {output: selection}
            await pilot.pause()
            self.assertIs(output.text_selection, selection)
            displayed = app._rendered_logs
            with (
                patch.object(app.logs, "search", wraps=app.logs.search) as search,
                patch.object(output, "clear", wraps=output.clear) as clear,
                patch.object(output, "write", wraps=output.write) as write,
                patch.object(app, "_write_clipboard") as copy,
            ):
                # Exercise rendering independently: assigning a native Input
                # value itself clears external selections in Textual.
                app.log_query = "absent"
                app._render_logs()
                await pilot.pause(0.2)
                self.assertEqual(self.feedback(app), "0 log matches")
                for _ in range(5):
                    app._render_logs()
                self.assertEqual(search.call_count, 1)
                app.log_query = ""
                app._render_logs()
                await pilot.pause(0.2)
                self.assertEqual(self.feedback(app), "2 log entries")
                self.assertEqual(search.call_count, 2)
                self.assertIs(app._rendered_logs, displayed)
                self.assertIs(output.text_selection, selection)
                clear.assert_not_called()
                write.assert_not_called()
                copy.assert_not_called()
            # The frozen render key must not hide a changed filter once the
            # user finishes copying and releases the viewport selection.
            app.log_query = "ready"
            app._render_logs()
            await pilot.pause(0.2)
            self.assertEqual(self.feedback(app), "1 log match")
            with patch.object(app.backend, "logs") as read:
                app.screen.clear_selection()
                await pilot.pause()
                read.assert_not_called()
            self.assertEqual(len(app._rendered_logs), 1)
            self.assertEqual(app._rendered_logs[0].message, "Synthetic container ready")
