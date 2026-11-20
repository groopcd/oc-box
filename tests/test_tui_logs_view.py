"""Headless log-view actions with synthetic data and no real external reads."""

import asyncio
import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    from oc_box import activity_log, journal_output, tui_logs
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_logs import (
        JOURNAL_SOURCE,
        PODMAN_UNAVAILABLE,
        LogEntry,
        journal_entries,
    )
    from oc_box.tui_settings import SettingsPanel


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

    def select_log_text(self, app, viewport):
        app.screen.selections = {viewport: Selection(Offset(1, 0), Offset(7, 1))}
        self.assertTrue(app.screen.get_selected_text())

    def viewport_messages(self, viewport):
        if isinstance(viewport, RichLog):
            return [line.text.rstrip() for line in viewport.lines]
        return self.messages(viewport)

    async def test_source_switch_revokes_old_selection_and_details_in_both_modes(self):
        private = LogEntry("", "oc-box", "info", "old private")
        mirror = LogEntry("", JOURNAL_SOURCE, "info", "new mirror")
        app = DashboardApp(LogFixture([private]), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            source = app.query_one("#log-source", Select)
            app._apply_mirror([mirror])
            with patch.object(app, "_write_clipboard") as write:
                for mode in ("table", "raw"):
                    with self.subTest(mode=mode):
                        if mode == "raw":
                            await pilot.click("#log-mode")
                        viewport = app.query_one(
                            "#log-table" if mode == "table" else "#log-output"
                        )
                        source.value = "oc-box"
                        await pilot.pause()
                        self.select_log_text(app, viewport)
                        screen = app.screen
                        source.value = JOURNAL_SOURCE
                        # The control value changes before Select.Changed runs.
                        self.assertFalse(app.screen.get_selected_text())
                        app.action_copy_selection()
                        app.action_copy_view()
                        app._show_log_details()
                        self.assertIs(app.screen, screen)
                        await pilot.pause()
                        self.assertIsNone(viewport.text_selection)
                        self.assertEqual(
                            self.viewport_messages(viewport), ["new mirror"]
                        )
                write.assert_not_called()
                await pilot.click("#log-copy-all")
                write.assert_called_once_with("new mirror")

    async def test_empty_private_snapshot_clears_selected_rows_in_paused_all_view(self):
        entries = [
            LogEntry("", "oc-box", "info", "private row"),
            LogEntry("", "podman", "info", "other source"),
        ]
        app = DashboardApp(LogFixture(entries), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            app.query_one("#log-follow", Switch).value = False
            with patch.object(app, "_write_clipboard") as write:
                for mode in ("table", "raw"):
                    with self.subTest(mode=mode):
                        if mode == "raw":
                            await pilot.click("#log-mode")
                        app._apply_logs(entries)
                        viewport = app.query_one(
                            "#log-table" if mode == "table" else "#log-output"
                        )
                        await pilot.pause()
                        self.select_log_text(app, viewport)
                        app._apply_logs(entries[1:])
                        self.assertIsNone(viewport.text_selection)
                        self.assertEqual(
                            self.viewport_messages(viewport), ["other source"]
                        )
                        self.assertIn(
                            "No retained oc-box activity",
                            str(app.query_one("#overview-activity", Static).render()),
                        )
                write.assert_not_called()

    async def test_saving_directory_clears_paused_private_rows_and_drops_inflight_read(
        self,
    ):
        class HeldPrivate(LogFixture):
            hold = False

            def __init__(self):
                super().__init__([])
                self.entered, self.finish = threading.Event(), threading.Event()

            def logs(self, workspace=None):
                entries = tui_logs.activity_entries()
                if self.hold:
                    self.hold = False
                    self.entered.set()
                    if not self.finish.wait(5):
                        raise TimeoutError
                return entries

            def podman_logs(self, workspace):
                raise AssertionError("No Podman target was requested")

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                XDG_CONFIG_HOME=temporary + "/config",
                OC_BOX_STATE_HOME=temporary + "/state",
            ),
        ):
            self.assertTrue(activity_log.record_event("history.wait"))
            backend = HeldPrivate()
            app = DashboardApp(backend)
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                table = app.query_one("#log-table", DataTable)
                self.assertEqual(self.messages(table), ["history.wait"])
                stale = app.logs.search(source="oc-box")
                request = app._private_log_request
                app.query_one("#log-follow", Switch).value = False
                self.select_log_text(app, table)
                backend.hold = True
                worker = app.refresh_logs()
                try:
                    self.assertTrue(await asyncio.to_thread(backend.entered.wait, 3))
                    app.action_settings()
                    panel = app.query_one(SettingsPanel)
                    new = Path(temporary) / "new-log"
                    panel.query_one("#host-logging-directory", Input).value = str(new)
                    app.on_settings_panel_save_host_logging_requested(
                        SettingsPanel.SaveHostLoggingRequested(panel)
                    )
                    self.assertFalse(new.exists())
                    self.assertFalse(app.logs.search(source="oc-box"))
                    self.assertIsNone(table.text_selection)
                    app.action_tab("logs")
                    self.assertEqual(table.row_count, 0)
                    app._apply_private_logs(stale, new / "activity.jsonl", request)
                    self.assertEqual(table.row_count, 0)
                    self.assertTrue(activity_log.record_event("session.archived"))
                finally:
                    backend.finish.set()
                accepted = asyncio.Event()
                apply = app._apply_logs

                def check_applied(entries, *args, **kwargs):
                    self.assertNotIn("history.wait", [e.message for e in entries])
                    apply(entries, *args, **kwargs)
                    if [e.message for e in app.logs.search(source="oc-box")] == [
                        "session.archived"
                    ]:
                        accepted.set()

                with patch.object(app, "_apply_logs", side_effect=check_applied):
                    await worker.wait()
                    await asyncio.wait_for(accepted.wait(), 3)
                    await pilot.pause()
                    app.action_tab("logs")
                    self.assertEqual(self.messages(table), ["session.archived"])
                    self.assertFalse(app.query_one("#log-follow", Switch).value)

    async def test_unavailable_podman_clears_selected_rows_without_blocking_private_refresh(
        self,
    ):
        workspace = "a" * 32

        class HeldTarget(LogFixture):
            hold = False
            container = "b" * 64

            def __init__(self):
                super().__init__([LogEntry("", "oc-box", "info", "private before")])
                self.entered, self.finish = threading.Event(), threading.Event()

            def podman_logs(self, target):
                if target != workspace:
                    raise AssertionError("Unexpected Podman target")
                if self.hold:
                    self.entered.set()
                    self.finish.wait(5)
                    return [PODMAN_UNAVAILABLE]
                return tui_logs._PodmanEntries(
                    [LogEntry("", "podman", "info", "container " + self.container[0])],
                    self.container,
                )

        backend = HeldTarget()
        app = DashboardApp(backend, read_only=True)
        with patch("oc_box.tui_app.activity_path", return_value=Path("/synthetic/log")):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                app.query_one("#log-follow", Switch).value = False
                app.query_one("#log-source", Select).value = "podman"
                app._load_podman(workspace)
                await pilot.pause()
                await app.workers.wait_for_complete()
                table = app.query_one("#log-table", DataTable)
                self.assertEqual(self.messages(table), ["container b"])
                self.select_log_text(app, table)
                backend.container = "c" * 64
                await app.refresh_logs().wait()
                await app.workers.wait_for_complete()
                self.assertEqual(self.messages(table), ["container c"])
                self.assertIsNone(table.text_selection)
                self.select_log_text(app, table)
                backend.hold = True
                # Private refresh schedules the optional Podman query.
                await app.refresh_logs().wait()
                try:
                    self.assertTrue(await asyncio.to_thread(backend.entered.wait, 3))
                    backend.entries = [LogEntry("", "oc-box", "info", "private after")]
                    await app.refresh_logs().wait()
                    self.assertEqual(
                        [e.message for e in app.logs.search(source="oc-box")],
                        ["private after"],
                    )
                finally:
                    backend.finish.set()
                await app.workers.wait_for_complete()
                self.assertIsNone(table.text_selection)
                self.assertEqual(self.messages(table), [PODMAN_UNAVAILABLE.message])
                self.assertIn(
                    "unavailable; no current logs",
                    str(app.query_one("#log-target", Static).render()),
                )

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
                patch.object(app, "action_copy_selection") as copy,
                patch.object(app, "_write_clipboard") as write,
                patch.object(app, "notify") as notify,
            ):
                await pilot.click("#log-copy-all")
                copy.assert_not_called()
                write.assert_not_called()
                notify.assert_called_once_with(
                    "Clipboard text exceeds the 1 MiB UTF-8 limit.", title="Clipboard"
                )
            await pilot.click("#log-mode")
            self.assertEqual(self.messages(table), [f"Match [{i}]" for i in range(45)])

    async def test_podman_raw_keeps_one_line_per_record_and_copies_only_safe_text(self):
        project = "a" * 32
        trusted = SimpleNamespace(
            maintenance_host_env=lambda: {},
            inspect_owned=lambda data, env: {
                "Id": "b" * 64,
                "Config": {"Labels": {"io.oc-box.workspace": project}},
            },
        )
        first = "2026-01-01T12:00:00Z " + "wide " * 120 + "END"
        second = "2026-01-01T12:00:01Z "
        third = "2026-01-01T12:00:02Z safe end"
        output = (
            f"{first}\n{second}Authorization:\u2028Bearer synthetic-private\n{third}\n"
        )
        with patch.object(tui_logs, "_podman_output", return_value=(output, False)):
            entries = tui_logs.podman_entries(trusted, {"id": project})
        app = DashboardApp(LogFixture(entries), demo=True, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.click("#log-mode")
            raw = app.query_one("#log-output", RichLog)
            expected = [first, second + "[REDACTED]", third]
            self.assertEqual([line.text.rstrip() for line in raw.lines], expected)
            self.assertGreater(raw.max_scroll_x, 0)
            with patch.object(app, "_write_clipboard") as write:
                await pilot.click("#log-copy-all")
                write.assert_called_once_with("\n".join(expected))

    async def test_bulk_copy_refuses_multiple_matching_podman_scopes(self):
        app = DashboardApp(LogFixture([]), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            for scope, message in (
                ("a" * 32, "first scope"),
                ("b" * 32, "current scope"),
            ):
                app.podman_workspace = scope
                app._apply_logs([LogEntry("", "podman", "", message, message)], scope)
            await pilot.click("#log-mode")
            self.assertEqual(
                [entry.raw_line() for entry in app._rendered_logs],
                ["first scope", "current scope"],
            )
            with (
                patch.object(app, "_write_clipboard") as write,
                patch.object(app, "notify") as notify,
            ):
                await pilot.click("#log-copy-all")
                write.assert_not_called()
                notify.assert_called_once()
                self.assertIn(
                    "Choose Selected Podman container", notify.call_args.args[0]
                )
                app.query_one("#log-source", Select).value = "podman"
                await pilot.pause()
                await pilot.click("#log-copy-all")
                write.assert_called_once_with("current scope")

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
            app.query_one("#log-follow", Switch).value = False
            with (
                patch.object(table, "clear", wraps=table.clear) as clear,
                patch.object(table, "add_row", wraps=table.add_row) as add,
                patch.object(app.backend, "logs") as read,
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
                await pilot.pause()
                self.assertEqual(self.messages(table), ["first", "second"])
                add.assert_called_once()
                clear.assert_not_called()
                read.assert_not_called()
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

    async def test_saturated_private_tail_retains_row_keys_and_idle_viewport(self):
        def entry(second):
            return LogEntry(
                f"2026-01-01T12:{second // 60:02}:{second % 60:02}Z",
                "oc-box",
                "debug",
                f"private event {second}",
            )

        # A duplicate at the cutoff must keep the second occurrence's key.
        original = [entry(0), entry(0), *(entry(i) for i in range(2, 1000))]
        app = DashboardApp(LogFixture(original), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#log-table", DataTable)
            self.assertEqual(table.row_count, 1000)
            table.move_cursor(row=310, scroll=False)
            await pilot.pause()
            table.scroll_to(y=300, animate=False, force=True)
            await pilot.pause()
            old_rows = app._table_snapshots["#log-table"][1]
            with (
                patch.object(table, "clear", wraps=table.clear) as clear,
                patch.object(table, "remove_row", wraps=table.remove_row) as remove,
                patch.object(table, "add_row", wraps=table.add_row) as add,
            ):
                updated = [*original[1:], entry(1000)]
                app._apply_logs(updated)
                await pilot.pause()
                current = app._table_snapshots["#log-table"][1]
                self.assertEqual(current[:-1], old_rows[1:])
                self.assertEqual(current[table.cursor_row][0], old_rows[310][0])
                self.assertEqual(table.scroll_offset.y, 299)
                self.assertEqual(str(table.get_row_at(999)[-1]), "private event 1000")
                remove.assert_called_once_with(old_rows[0][0])
                add.assert_called_once()
                clear.assert_not_called()
                table.scroll_to(y=table.max_scroll_y, animate=False, force=True)
                await pilot.pause()
                app._apply_logs([*updated[1:], entry(1001)])
                await pilot.pause()
                self.assertEqual(table.scroll_offset.y, table.max_scroll_y)
                self.assertEqual(remove.call_count, 2)
                self.assertEqual(add.call_count, 2)
                clear.assert_not_called()
