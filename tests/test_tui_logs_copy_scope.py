"""Clipboard scope regressions using only synthetic dashboard log rows."""

import importlib.util
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import Select

    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_logs import JOURNAL_SOURCE, LogEntry


PRIVATE = "Synthetic dashboard started"
MIRROR = "Synthetic mirror record"


@unittest.skipUnless(AVAILABLE, "optional dashboard dependencies unavailable")
class LogCopyScopeTests(unittest.IsolatedAsyncioTestCase):
    async def open_logs(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()
        await pilot.press("5")
        await pilot.pause()
        app._apply_mirror([LogEntry("", JOURNAL_SOURCE, "info", MIRROR)])
        return app.query_one("#log-source", Select)

    async def test_copy_view_checks_composited_mode_and_source(self):
        app = DashboardApp(DemoBackend(), demo=True, read_only=True)
        async with app.run_test(size=(120, 35)) as pilot:
            source = await self.open_logs(app, pilot)
            source.value = "oc-box"
            for old, new, previous_marker, current_marker in (
                ("oc-box", JOURNAL_SOURCE, PRIVATE, MIRROR),
                (JOURNAL_SOURCE, "oc-box", MIRROR, PRIVATE),
            ):
                with self.subTest(old=old, new=new):
                    await pilot.pause()
                    self.assertEqual(source.value, old)
                    with patch.object(app, "_write_clipboard") as write:
                        app.action_copy_view()
                        self.assertIn(previous_marker, write.call_args.args[0])
                        write.reset_mock()

                        # The mode paints the new viewport before Textual
                        # removes the old viewport from the compositor layout.
                        source.value = new
                        app.action_copy_view()
                        write.assert_not_called()
                        app._switch_log_mode()
                        app.action_copy_view()
                        write.assert_not_called()

                        await pilot.pause()
                        app.action_copy_view()
                        write.assert_called_once()
                        copied = write.call_args.args[0]
                        self.assertIn(current_marker, copied)
                        self.assertNotIn(previous_marker, copied)

            with patch.object(app, "_write_clipboard") as write:
                # Leaving Logs also changes the tab before compositor layout.
                app.action_tab("storage")
                app.action_copy_view()
                write.assert_not_called()
                await pilot.pause()
                app.action_copy_view()
                write.assert_called_once()
                self.assertIn("Storage", write.call_args.args[0])
                self.assertNotIn(PRIVATE, write.call_args.args[0])

    async def test_native_copy_rejects_empty_stale_selection_without_writing(self):
        app = DashboardApp(DemoBackend(), demo=True, read_only=True)
        async with app.run_test(size=(120, 35)) as pilot:
            source = await self.open_logs(app, pilot)
            for viewport_id in ("log-table", "log-output"):
                with self.subTest(viewport=viewport_id):
                    if viewport_id == "log-output":
                        app._switch_log_mode()
                    source.value = "oc-box"
                    await pilot.pause()
                    viewport = app.query_one("#" + viewport_id)
                    app.screen.selections = {
                        viewport: Selection(Offset(0, 0), Offset(80, 2))
                    }
                    self.assertIn(PRIVATE, app.screen.get_selected_text())
                    with patch.object(
                        app, "_write_clipboard", wraps=app._write_clipboard
                    ) as write:
                        # Exercise the native Ctrl+C action, not the dashboard's
                        # separate explicit copy action.
                        app.screen.action_copy_text()
                        write.assert_called_once()
                        clipboard = app.clipboard
                        self.assertIn(PRIVATE, clipboard)
                        write.reset_mock()

                        source.value = JOURNAL_SOURCE
                        self.assertFalse(app.screen.get_selected_text())
                        app.screen.action_copy_text()
                        write.assert_not_called()
                        self.assertEqual(app.clipboard, clipboard)
