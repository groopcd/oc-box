"""Headless log-view actions with synthetic data and no real external reads."""

import importlib.util
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import (
        DataTable,
        Input,
        RichLog,
        SelectionList,
        Select,
        Static,
        Switch,
    )

    from oc_box import journal_output
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_logs import JOURNAL_SOURCE, LogEntry, journal_entries


if AVAILABLE:

    class LogFixture(DemoBackend):
        def __init__(self, entries):
            super().__init__()
            self.entries = entries

        def logs(self, workspace=None):
            return self.entries

        def journal_mirror_logs(self):
            return journal_entries()


@unittest.skipUnless(AVAILABLE, "optional dashboard dependencies unavailable")
class LogViewTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()
        await pilot.press("5")
        await pilot.pause()

    def messages(self, table):
        return [str(table.get_row_at(i)[-1]) for i in range(table.row_count)]

    async def test_table_sort_columns_details_and_filter_survive_tab_switch(self):
        entries = [
            LogEntry("2026-01-01T12:01:00Z", "oc-box", "error", "second [bold]"),
            LogEntry("", "podman", "", "untimed"),
            LogEntry("2026-01-01T12:00:00Z", "oc-box", "debug", "first"),
        ]
        app = DashboardApp(LogFixture(entries), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#log-table", DataTable)
            self.assertTrue(table.display)
            self.assertFalse(app.query_one("#log-output", RichLog).display)
            self.assertEqual(
                [str(column.label) for column in table.columns.values()],
                ["Time", "Source", "Severity", "Message"],
            )
            self.assertEqual(
                self.messages(table), ["first", "second [bold]", "untimed"]
            )
            self.assertEqual(
                [str(v) for v in table.get_row_at(2)[:3]], ["—", "podman", "—"]
            )
            # Use the actual column-header click, not a direct sort helper call.
            await pilot.click(table, offset=(2, 0))
            self.assertEqual(
                self.messages(table), ["second [bold]", "first", "untimed"]
            )
            table.move_cursor(row=2)
            await pilot.click("#log-details")
            detail = str(app.screen.query_one("#log-detail-text", Static).render())
            self.assertIn("Time: Unavailable", detail)
            self.assertIn("Severity: Unavailable", detail)
            self.assertIn("untimed", detail)
            await pilot.press("escape")
            await pilot.click("#log-columns")
            options = app.screen.query_one(SelectionList)
            options.focus()
            await pilot.press("space", "down", "space", "down", "space")
            self.assertEqual(options.selected, [])
            await pilot.click("#log-columns-apply")
            self.assertEqual(
                [str(c.label) for c in table.columns.values()], ["Message"]
            )
            self.assertEqual(table.cursor_row, 2)

            app.query_one("#log-level", Select).value = "info"
            await pilot.pause()
            self.assertEqual(self.messages(table), ["second [bold]"])
            app.action_tab("status")
            app.query_one("#log-search", Input).value = "[BOLD]"
            await pilot.pause(0.2)
            self.assertIn(
                "1 log match", str(app.query_one("#log-hits", Static).render())
            )
            await pilot.press("5")
            self.assertEqual(self.messages(table), ["second [bold]"])
            await pilot.click("#log-columns")
            await pilot.press("escape")
            self.assertEqual(
                [str(c.label) for c in table.columns.values()], ["Message"]
            )

    async def test_explicit_raw_copy_has_all_filtered_lines_in_source_order(self):
        records = [
            LogEntry(
                f"2026-01-01T12:00:{second:02}Z",
                "oc-box",
                "info",
                f"Match [{second}]",
                f"safe line {second} [literal]",
            )
            for second in reversed(range(45))
        ]
        entries = [
            *records,
            LogEntry(
                "2026-01-01T12:01:00Z",
                "oc-box",
                "debug",
                "match debug",
                "excluded debug",
            ),
            LogEntry(
                "2026-01-01T12:01:01Z",
                "oc-box",
                "error",
                "another row",
                "excluded query",
            ),
        ]
        app = DashboardApp(LogFixture(entries), demo=True, read_only=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#log-table", DataTable)
            self.assertEqual(self.messages(table)[0], "Match [0]")
            app.query_one("#log-level", Select).value = "info"
            app.query_one("#log-search", Input).value = "match"
            await pilot.pause(0.2)
            with patch.object(app, "_write_clipboard") as write:
                await pilot.click("#log-mode")
                output = app.query_one("#log-output", RichLog)
                self.assertGreater(
                    len(output.lines), output.scrollable_content_region.height
                )
                write.assert_not_called()
                await pilot.click("#log-copy-all")
                write.assert_called_once_with(
                    "\n".join(entry.raw_line() for entry in records)
                )
            with (
                patch("oc_box.tui_clipboard.MAX_CLIPBOARD_BYTES", 64),
                patch.object(app, "_write_clipboard") as write,
                patch.object(app, "notify") as notify,
            ):
                await pilot.click("#log-copy-all")
                write.assert_not_called()
                notify.assert_called_once_with(
                    "Clipboard text exceeds the 1 MiB UTF-8 limit.", title="Clipboard"
                )
            await pilot.click("#log-mode")
            self.assertEqual(self.messages(table), [f"Match [{i}]" for i in range(45)])

    async def test_mirror_is_load_only_separate_and_fails_without_fallback(self):
        record = {"event": "history.wait", "timestamp": "2026-01-01T12:00:00.000Z"}
        backend = LogFixture(
            [LogEntry(record["timestamp"], "oc-box", "debug", record["event"])]
        )
        app = DashboardApp(backend, read_only=True)
        with patch.object(journal_output, "read_events", return_value=[record]) as read:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                source = app.query_one("#log-source", Select)
                table = app.query_one("#log-table", DataTable)
                self.assertEqual(self.messages(table), ["history.wait"])
                source.value = JOURNAL_SOURCE
                await pilot.pause()
                self.assertEqual(table.row_count, 0)
                read.assert_not_called()
                await pilot.click("#load-journald")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(str(table.get_row_at(0)[1]), "Journal mirror")
                self.assertEqual(self.messages(table), ["history.wait"])
                source.value = "all"
                await pilot.pause()
                self.assertEqual(table.row_count, 1)
                self.assertEqual(str(table.get_row_at(0)[1]), "oc-box")
                source.value = JOURNAL_SOURCE
                app.action_refresh()
                await app.workers.wait_for_complete()
                await pilot.pause()
                read.assert_called_once_with(limit=1000, level=None)

                with patch.object(backend, "logs", wraps=backend.logs) as normal:
                    read.side_effect = journal_output.JournalError("missing-tool")
                    await pilot.click("#load-journald")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertEqual(source.value, JOURNAL_SOURCE)
                    self.assertEqual(self.messages(table), ["history.wait"])
                    self.assertIn(
                        "Journal mirror unavailable.",
                        str(app.query_one("#log-mirror-status", Static).render()),
                    )
                    normal.assert_not_called()
                self.assertEqual(read.call_count, 2)

    async def test_demo_load_mirror_never_reads_host_data(self):
        app = DashboardApp(LogFixture([]), read_only=True, demo=True)
        with patch.object(journal_output, "read_events") as read:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                await pilot.click("#load-journald")
                await pilot.pause()
                self.assertIn(
                    "Synthetic demo does not read",
                    str(app.query_one("#log-mirror-status", Static).render()),
                )
                read.assert_not_called()

    async def test_unchanged_and_selected_table_snapshots_do_not_redraw_or_copy(self):
        entries = [LogEntry("2026-01-01T12:00:00Z", "oc-box", "debug", "first")]
        app = DashboardApp(LogFixture(entries), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#log-table", DataTable)
            with (
                patch.object(table, "clear", wraps=table.clear) as clear,
                patch.object(table, "add_row", wraps=table.add_row) as add,
                patch.object(app, "_write_clipboard") as copy,
            ):
                for _ in range(5):
                    app._apply_logs(list(entries))
                clear.assert_not_called()
                add.assert_not_called()
                app.screen.selections = {table: Selection(Offset(1, 1), Offset(5, 1))}
                await pilot.pause()
                selected = app.screen.get_selected_text()
                self.assertTrue(selected)
                entries.append(
                    LogEntry("2026-01-01T12:00:01Z", "oc-box", "info", "second")
                )
                app._apply_logs(entries)
                await pilot.pause()
                self.assertEqual(self.messages(table), ["first"])
                self.assertEqual(app.screen.get_selected_text(), selected)
                self.assertIn(
                    "2 log entries", str(app.query_one("#log-hits", Static).render())
                )
                add.assert_not_called()
                app.screen.clear_selection()
                app._render_logs()
                self.assertEqual(self.messages(table), ["first", "second"])
                add.assert_called_once()
                clear.assert_not_called()
                copy.assert_not_called()

    async def test_new_table_rows_follow_at_tail_without_rebuilding_paused_view(self):
        entries = [
            LogEntry(f"2026-01-01T12:00:{i:02}Z", "oc-box", "debug", str(i))
            for i in range(40)
        ]
        app = DashboardApp(LogFixture(entries), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#log-table", DataTable)
            table.scroll_to(y=8, animate=False, force=True)
            await pilot.pause()
            self.assertTrue(app.query_one("#log-follow", Switch).value)
            with patch.object(table, "clear", wraps=table.clear) as clear:
                entries.append(
                    LogEntry("2026-01-01T12:01:00Z", "oc-box", "debug", "new")
                )
                app._apply_logs(entries)
                await pilot.pause()
                self.assertEqual(table.scroll_offset.y, 8)
                self.assertEqual(self.messages(table)[-1], "new")
                table.scroll_to(y=table.max_scroll_y, animate=False, force=True)
                await pilot.pause()
                entries.append(
                    LogEntry("2026-01-01T12:01:01Z", "oc-box", "debug", "latest")
                )
                app._apply_logs(entries)
                await pilot.pause()
                self.assertEqual(table.scroll_offset.y, table.max_scroll_y)
                self.assertEqual(self.messages(table)[-1], "latest")
                clear.assert_not_called()
