"""Synthetic headless performance contracts, not host/runtime acceptance."""

import asyncio
import copy
import importlib.util
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual import events
    from textual.widgets import DataTable, Input, RichLog, Select

    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_logs import LogEntry
    from oc_box.tui_status import MonitorPanel
    from tools.benchmark_tui_display import (
        SyntheticBackend,
        benchmark,
        synthetic_sessions,
    )


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class DisplayPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()

    async def paused_session_table(self, app, pilot):
        await self.ready(app, pilot)
        app.catalog = Mock(origin="host", store="synthetic-store")
        app.sessions = synthetic_sessions()
        app._render_sessions()
        app.action_tab("sessions")
        await pilot.pause()
        table = app.query_one("#session-table", DataTable)
        table.move_cursor(row=80)
        table.scroll_to(y=60, animate=False, force=True)
        await pilot.pause()
        return table

    @contextmanager
    def held_scroll_restores(self, app, table):
        """Delay only our restore, not Textual's automatic coordinate scroll."""
        pending = []
        original = table.call_after_refresh

        def schedule(callback, *args, **kwargs):
            if callback == app._restore_table_scroll:
                pending.append(lambda: callback(*args, **kwargs))
                return True
            return original(callback, *args, **kwargs)

        with patch.object(table, "call_after_refresh", side_effect=schedule):
            yield pending

    async def mouse(self, app, pilot, kind, point):
        # Pilot's mouse helpers bypass App.on_event; use the actual incoming
        # event route so stopped scrollbar events and capture are exercised.
        x, y = point
        widget, _ = app.screen.get_widget_at(x, y)
        await app.on_event(
            kind(
                widget,
                x,
                y,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=x,
                screen_y=y,
            )
        )
        await pilot.pause()

    async def test_deferred_restore_keeps_intervening_user_wheel_viewport(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            table = await self.paused_session_table(app, pilot)
            old_scroll = table.scroll_offset
            with self.held_scroll_restores(app, table) as pending:
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                cursor, automatic = table.cursor_row, table.scroll_offset
                self.assertEqual(len(pending), 1)
                point = table.content_region.offset + (3, 3)
                await self.mouse(app, pilot, events.MouseScrollDown, point)
                current = table.scroll_offset
                self.assertNotEqual(current, automatic)
                self.assertNotEqual(current, old_scroll)
                self.assertEqual(table.cursor_row, cursor)
                with patch.object(table, "scroll_to", wraps=table.scroll_to) as scroll:
                    pending[0]()
                    scroll.assert_not_called()
                self.assertEqual(table.scroll_offset, current)

    async def test_deferred_restore_keeps_intervening_captured_scrollbar_drag(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            table = await self.paused_session_table(app, pilot)
            with (
                self.held_scroll_restores(app, table) as pending,
                patch.object(app, "_write_clipboard") as copy,
            ):
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                scrollbar = table.vertical_scrollbar
                region = scrollbar.region
                point = next(
                    (region.x, y)
                    for y in range(region.y, region.bottom)
                    if app.screen.get_style_at(region.x, y).meta.get("@mouse.down")
                    == "grab"
                )
                await self.mouse(app, pilot, events.MouseDown, point)
                destination = point
                try:
                    self.assertIs(app.mouse_captured, scrollbar)
                    with patch.object(
                        table, "scroll_to", wraps=table.scroll_to
                    ) as scroll:
                        pending[0]()
                        scroll.assert_not_called()
                    # A second restore can be queued while dragging, after the
                    # mouse-down fence. Only real captured movement may fence it.
                    app.sessions[1]["time"]["updated"] = -2
                    app._render_sessions()
                    await pilot.pause()
                    automatic, cursor = table.scroll_offset, table.cursor_row
                    self.assertEqual(len(pending), 2)
                    destination = (point[0] - 3, min(region.bottom - 1, point[1] + 4))
                    await self.mouse(app, pilot, events.MouseMove, destination)
                    current = table.scroll_offset
                    self.assertNotEqual(current, automatic)
                    self.assertEqual(table.cursor_row, cursor)
                    with patch.object(
                        table, "scroll_to", wraps=table.scroll_to
                    ) as scroll:
                        for callback in pending:
                            callback()
                        scroll.assert_not_called()
                    self.assertEqual(table.scroll_offset, current)
                    copy.assert_not_called()
                finally:
                    await self.mouse(app, pilot, events.MouseUp, destination)

    async def test_deferred_restore_rejects_view_aba_even_with_same_content_cursor(
        self,
    ):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            table = await self.paused_session_table(app, pilot)
            with self.held_scroll_restores(app, table) as pending:
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                state_a = app._table_snapshots["#session-table"]
                cursor = table.cursor_row
                title = app.sessions[2]["title"]
                app.sessions[2]["title"] = "Changed synthetic metadata"
                app._render_sessions()
                app.sessions[2]["title"] = title
                app._render_sessions()
                await pilot.pause()
                self.assertEqual(app._table_snapshots["#session-table"], state_a)
                self.assertEqual(table.cursor_row, cursor)
                self.assertEqual(len(pending), 1)
                table.scroll_to(y=30, animate=False, force=True)
                await pilot.pause()
                current = table.scroll_offset
                with patch.object(table, "scroll_to", wraps=table.scroll_to) as scroll:
                    pending[0]()
                    scroll.assert_not_called()
                self.assertEqual(table.scroll_offset, current)

    async def test_deferred_restore_corrects_internal_scroll_without_user_gesture(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            table = await self.paused_session_table(app, pilot)
            previous = table.scroll_offset
            with self.held_scroll_restores(app, table) as pending:
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                self.assertEqual(len(pending), 1)
                self.assertNotEqual(table.scroll_offset, previous)
                with patch.object(table, "scroll_to", wraps=table.scroll_to) as scroll:
                    pending[0]()
                    scroll.assert_called_once()
                self.assertEqual(table.scroll_offset, previous)

    async def test_deferred_restore_respects_navigation_returning_to_same_cursor(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            table = await self.paused_session_table(app, pilot)
            with self.held_scroll_restores(app, table) as pending:
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                cursor = table.cursor_row
                table.focus()
                await pilot.press("down", "up")
                self.assertEqual(table.cursor_row, cursor)
                current = table.scroll_offset
                with patch.object(table, "scroll_to", wraps=table.scroll_to) as scroll:
                    pending[0]()
                    scroll.assert_not_called()
                self.assertEqual(table.scroll_offset, current)

    async def test_fixed_workload_bounds_work_not_wall_clock(self):
        result = await benchmark()
        for scenario in (
            "ten_unchanged_reports",
            "ten_unchanged_session_renders",
            "five_unchanged_log_snapshots",
        ):
            with self.subTest(scenario=scenario):
                counts = result[scenario]
                for key in (
                    "workspace_clear",
                    "workspace_add",
                    "workspace_update",
                    "session_clear",
                    "session_add",
                    "session_update",
                    "favorite_reads",
                    "log_search",
                    "log_clear",
                    "log_write",
                    "backend_queries",
                ):
                    self.assertEqual(counts[key], 0, (scenario, key))
        sessions = result["session_typing_burst"]
        self.assertEqual(sessions["session_clear"], 1)
        self.assertEqual(sessions["session_add"], 10)
        self.assertEqual(sessions["favorite_reads"], 0)
        logs = result["log_typing_burst"]
        self.assertEqual(logs["log_search"], 1)
        self.assertEqual(logs["log_clear"], 1)
        self.assertEqual(logs["log_write"], 10)
        self.assertEqual(logs["backend_queries"], 0)
        # Histories continue recording every sample, even identical readings.
        self.assertGreaterEqual(result["ten_unchanged_reports"]["monitor_update"], 10)

    async def test_initial_workspace_selector_below_viewport_is_visible(self):
        class ManyDemo(DemoBackend):
            def __init__(self):
                self.samples = 0

            def collect(self, *, scan_storage=False):
                self.samples += 1
                report = super().collect(scan_storage=scan_storage)
                template = report["workspaces"][0]
                report["workspaces"] = []
                for index in range(50):
                    item = copy.deepcopy(template)
                    item["workspace_id"] = f"{index + 1:032x}"
                    item["project"] = f"/synthetic/project-{index:02}"
                    report["workspaces"].append(item)
                return report

        backend = ManyDemo()
        selected = f"{50:032x}"
        app = DashboardApp(backend, read_only=True, selected=selected, demo=True)
        # This checks first population, not how long layout takes under load.
        # Control only periodic queries before mount; initial workers and
        # Textual's frame/layout timers must still reveal the selected row.
        with patch.object(app, "_tick") as scheduled_tick:
            async with app.run_test(size=(100, 30)) as pilot:
                await self.ready(app, pilot)
                scheduled_tick()
                table = app.query_one("#workspaces", DataTable)
                self.assertEqual(app.selected, selected)
                self.assertEqual(table.cursor_row, 49)
                self.assertGreater(table.scroll_y, 0)
                region = table.scrollable_content_region.intersection(app.screen.region)
                strips = app.screen._compositor.render_strips()
                visible = "\n".join(
                    strips[y].crop(region.x, region.right).text
                    for y in range(region.y, region.bottom)
                )
                self.assertIn("project-49", visible)
                self.assertEqual(backend.samples, 1)

    async def test_changed_cells_and_structural_refresh_preserve_cursor_and_scroll(
        self,
    ):
        backend = SyntheticBackend()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.catalog = Mock(origin="host", store="synthetic-store")
            app.sessions = synthetic_sessions()
            app._render_sessions()
            app.action_tab("sessions")
            await pilot.pause()
            table = app.query_one("#session-table", DataTable)
            table.move_cursor(row=80)
            table.scroll_to(y=60, animate=False, force=True)
            await pilot.pause()
            selected = app._session()["id"]
            scroll, cursor = table.scroll_offset, table.cursor_row
            with (
                patch.object(table, "clear", wraps=table.clear) as clear,
                patch.object(table, "update_cell", wraps=table.update_cell) as update,
            ):
                app.sessions[80]["title"] = "Updated synthetic title"
                app._render_sessions()
                await pilot.pause()
                clear.assert_not_called()
                update.assert_called_once()
                self.assertEqual(app._session()["id"], selected)
                self.assertEqual(table.cursor_row, cursor)
                self.assertEqual(table.scroll_offset, scroll)
                self.assertEqual(
                    table.get_row(selected)[2].plain, "Updated synthetic title"
                )
                # Sorting changes are structural, but the selected native ID
                # and paused viewport must not jump to the beginning.
                app.sessions[0]["time"]["updated"] = -1
                app._render_sessions()
                await pilot.pause()
                clear.assert_called_once()
                self.assertEqual(app._session()["id"], selected)
                self.assertEqual(table.scroll_offset, scroll)
                # Identical native IDs from a different store are not the
                # previous selection namespace.
                app.catalog = Mock(origin="host", store="another-synthetic-store")
                app._render_sessions()
                await pilot.pause()
                self.assertEqual(table.cursor_row, 0)

    async def test_status_changes_update_only_changed_cells_and_keep_unknowns_and_sampling(
        self,
    ):
        backend = SyntheticBackend()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#workspaces", DataTable)
            table.move_cursor(row=20)
            table.scroll_to(y=15, animate=False, force=True)
            await pilot.pause()
            selected, scroll = app.selected, table.scroll_offset
            panel = app.query_one("#full-diagnostics", MonitorPanel)
            report = copy.deepcopy(app.report)
            report["workspaces"][20]["state"] = "stopped"
            report["workspaces"][20]["metrics"]["cpu_percent"] = None
            report["host"]["cpu_percent"] = None
            samples = len(app.cpu_history)
            with (
                patch.object(table, "clear", wraps=table.clear) as clear,
                patch.object(table, "update_cell", wraps=table.update_cell) as update,
            ):
                app._apply_report(report, False)
                await pilot.pause()
                clear.assert_not_called()
                self.assertEqual(update.call_count, 3)
                self.assertEqual(
                    {call.args[0] for call in update.call_args_list}, {selected}
                )
                self.assertEqual(app.selected, selected)
                self.assertEqual(table.scroll_offset, scroll)
                self.assertEqual(table.get_row(selected)[1].plain, "stopped")
                self.assertIsNone(app.cpu_history[-1])
                self.assertEqual(len(app.cpu_history), samples + 1)
                self.assertIsNone(panel.snapshot["metrics"]["cpu_percent"])
                self.assertIn("n/a", panel.render().plain)

    async def test_debounce_rejects_queued_stale_callbacks_and_uses_current_source(
        self,
    ):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.catalog = Mock(origin="host", store="synthetic-store")
            app.sessions = synthetic_sessions()
            app._render_sessions()
            field = app.query_one("#session-search", Input)
            callbacks, timers = [], []

            def timer(_delay, callback):
                callbacks.append(callback)
                handle = Mock()
                timers.append(handle)
                return handle

            with patch.object(app, "set_timer", side_effect=timer):
                for value in ("Synthetic", "Synthetic 09", "Synthetic 099"):
                    with field.prevent(Input.Changed):
                        field.value = value
                    app.on_input_changed(Input.Changed(field, value))
            for handle in timers[:-1]:
                handle.stop.assert_called_once()
            table = app.query_one("#session-table", DataTable)
            with patch.object(table, "clear", wraps=table.clear) as clear:
                for callback in callbacks[:-1]:
                    callback()
                clear.assert_not_called()
                callbacks[-1]()
                clear.assert_called_once()
                self.assertEqual(table.row_count, 10)
                # A disconnected source cannot be resurrected by a queued
                # callback, which reads current metadata rather than capturing it.
                app._disconnect_sessions()
                callbacks[-1]()
                self.assertIsNone(app.catalog)
                self.assertEqual(table.row_count, 0)

    async def test_pilot_typing_and_clear_leave_final_value_and_caret_live(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.catalog = Mock(origin="host", store="synthetic-store")
            app.sessions = synthetic_sessions()
            app._render_sessions()
            app.action_tab("sessions")
            await pilot.pause()
            field = app.query_one("#session-search", Input)
            field.focus()
            await pilot.press(*"Synthetic 099")
            await pilot.pause(0.25)
            self.assertEqual(field.value, "Synthetic 099")
            self.assertEqual(field.cursor_position, len(field.value))
            self.assertTrue(field.has_focus)
            self.assertEqual(len(app.visible_sessions), 10)
            await pilot.press("ctrl+a", "backspace")
            await pilot.pause(0.25)
            self.assertEqual(field.value, "")
            self.assertEqual(field.cursor_position, 0)
            self.assertEqual(len(app.visible_sessions), 1000)
            self.assertTrue(field.has_focus)

    async def test_late_table_event_matrix_ignores_pruned_default_screen(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        default_screen = None
        late_events, dispatched = [], []
        original_prune = app._prune
        pruned = False

        def record_dispatch(message):
            if any(message is event for event in late_events):
                dispatched.append(message)

        async def prune(*nodes, **kwargs):
            nonlocal pruned
            result = await original_prune(*nodes, **kwargs)
            if default_screen is None or default_screen not in nodes:
                return result
            pruned = True
            self.assertFalse(app.is_running)
            self.assertEqual(len(default_screen.children), 0)
            catalog, visible = app.catalog, list(app.visible_sessions)
            with (
                patch.object(app, "_session_source_changed") as source_changed,
                patch.object(app, "_render_workspace") as workspace,
                patch.object(app, "_render_sessions") as sessions,
                patch.object(app, "_session_details") as details,
            ):
                for event in late_events:
                    with self.subTest(
                        table=event.data_table.id, event=type(event).__name__
                    ):
                        self.assertFalse(event.data_table.is_attached)
                        acknowledged = asyncio.Event()
                        self.assertTrue(app.post_message(event))
                        self.assertTrue(app.call_later(acknowledged.set))
                        await asyncio.wait_for(acknowledged.wait(), timeout=5)
                        self.assertIs(dispatched[-1], event)
                        source_changed.assert_not_called()
                        workspace.assert_not_called()
                        sessions.assert_not_called()
                        details.assert_not_called()
                        self.assertIsNone(app.selected)
                        self.assertIs(app.catalog, catalog)
                        self.assertEqual(app.visible_sessions, visible)
                        self.assertFalse(app._exception_event.is_set())
            return result

        # Textual 8.2.8 (supported <9) prunes the default screen before closing
        # the app message queue. Hold only that boundary; native dispatch and a
        # queued acknowledgement determine ordering, not sleeps or retries.
        with patch.object(app, "_prune", prune):
            async with app.run_test(
                size=(120, 35), message_hook=record_dispatch
            ) as pilot:
                await self.ready(app, pilot)
                app.catalog = Mock(origin="host", store="synthetic-store")
                app.sessions = synthetic_sessions()
                app._render_sessions()
                app.action_tab("sessions")
                await pilot.pause()
                default_screen = app.default_screen
                for selector in ("#workspaces", "#session-table"):
                    table = app.query_one(selector, DataTable)
                    key = table.ordered_rows[table.cursor_row].key
                    for message_type in (
                        DataTable.RowHighlighted,
                        DataTable.RowSelected,
                    ):
                        late_events.append(message_type(table, table.cursor_row, key))
                # A valid workspace row would change this selection if its
                # late handler were allowed to run.
                app.selected = None
        self.assertTrue(pruned)
        self.assertEqual(len(late_events), 4)
        self.assertEqual(dispatched, late_events)

    async def test_slow_favorites_are_off_ui_thread_cached_and_store_qualified(self):
        app = DashboardApp(SyntheticBackend())
        entered, finish = threading.Event(), threading.Event()
        ui_thread = threading.get_ident()
        store = Mock()

        def read():
            self.assertNotEqual(threading.get_ident(), ui_thread)
            entered.set()
            finish.wait(5)
            return [{"origin": "host", "store": "synthetic-store", "id": "ses_0000"}]

        store.list.side_effect = read
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.favorites = store
            app.catalog = Mock(origin="host", store="synthetic-store")
            app.sessions = synthetic_sessions()
            app._render_sessions()
            app._refresh_favorites()
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
                field = app.query_one("#log-search", Input)
                field.focus()
                await pilot.press("r", "e", "a", "d", "y")
                await pilot.pause(0.2)
                self.assertEqual(field.value, "ready")
                self.assertEqual(len(app.logs.search(app.log_query)), 0)
                for _ in range(3):
                    app._render_sessions()
                    app._refresh_favorites()
                store.list.assert_called_once()
            finally:
                finish.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#session-table", DataTable)
            self.assertEqual(table.get_row("ses_0000")[0].plain, "★")
            app.catalog = Mock(origin="host", store="other-synthetic-store")
            app._render_sessions()
            self.assertEqual(table.get_row("ses_0000")[0].plain, "")

    async def test_favorite_toggle_runs_off_ui_and_late_result_cannot_cross_stores(
        self,
    ):
        app = DashboardApp(SyntheticBackend())
        entered, finish = threading.Event(), threading.Event()
        store = Mock()
        ui_thread = threading.get_ident()

        def toggle(origin, source, ident):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(
                (origin, source, ident), ("host", "synthetic-store", "ses_0000")
            )
            entered.set()
            finish.wait(5)

        store.toggle.side_effect = toggle
        store.list.return_value = [
            {"origin": "host", "store": "synthetic-store", "id": "ses_0000"}
        ]
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.favorites = store
            app.catalog = Mock(origin="host", store="synthetic-store")
            app.sessions = synthetic_sessions()
            app._render_sessions()
            app._favorite()
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
                app.catalog = Mock(origin="host", store="different-synthetic-store")
                app._render_sessions()
                app._favorite()
                store.toggle.assert_called_once()
            finally:
                finish.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(
                app.query_one("#session-table", DataTable).get_row("ses_0000")[0].plain,
                "",
            )
            # CLI read-only remains a hard fence before starting any I/O worker.
            app.read_only = True
            app._favorite()
            store.toggle.assert_called_once()

    async def test_pending_filters_are_invalidated_and_stopped_at_unmount(self):
        app = DashboardApp(SyntheticBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            callbacks, timers = [], []

            def timer(_delay, callback):
                callbacks.append(callback)
                handle = Mock()
                timers.append(handle)
                return handle

            with patch.object(app, "set_timer", side_effect=timer):
                app._schedule_filter("sessions")
                app._schedule_filter("logs")
        for handle in timers:
            handle.stop.assert_called_once()
        with (
            patch.object(app, "_render_sessions") as sessions,
            patch.object(app, "_render_logs") as logs,
        ):
            for callback in callbacks:
                callback()
            sessions.assert_not_called()
            logs.assert_not_called()

    async def test_appended_logs_do_not_rebuild_or_move_a_paused_viewport(self):
        backend = SyntheticBackend()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            app.action_logs()
            await pilot.pause()
            output = app.query_one("#log-output", RichLog)
            # Replace the source with a short tail first; then append one line
            # while staying under the existing bounded retention limit.
            entries = backend.logs()[:100]
            app._apply_logs(entries)
            await pilot.pause()
            output.scroll_to(y=20, animate=False, force=True)
            await pilot.pause()
            scroll = output.scroll_offset
            with (
                patch.object(output, "clear", wraps=output.clear) as clear,
                patch.object(output, "write", wraps=output.write) as write,
            ):
                app._apply_logs(
                    entries
                    + [LogEntry("2026-01-01T13:00:00Z", "oc-box", "info", "New")]
                )
                await pilot.pause()
                clear.assert_not_called()
                write.assert_called_once()
                self.assertEqual(output.scroll_offset, scroll)
                self.assertIn("New", "".join(line.text for line in output.lines))

    async def test_background_queries_remain_serial_while_input_is_usable(self):
        class HeldBackend(SyntheticBackend):
            hold = False

            def __init__(self):
                super().__init__()
                self.entered = {"status": threading.Event(), "logs": threading.Event()}
                self.finish = threading.Event()
                self.counts = {"status": 0, "logs": 0}

            def collect(self, *, scan_storage=False):
                if self.hold:
                    self.counts["status"] += 1
                    self.entered["status"].set()
                    self.finish.wait(5)
                return super().collect(scan_storage=scan_storage)

            def logs(self, workspace=None):
                if self.hold:
                    self.counts["logs"] += 1
                    self.entered["logs"].set()
                    self.finish.wait(5)
                return super().logs(workspace)

        backend = HeldBackend()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.ready(app, pilot)
            self.assertEqual(app._settings_snapshot()["interval"], 1)
            backend.hold = True
            app.refresh_report()
            app.refresh_logs()
            try:
                for event in backend.entered.values():
                    await asyncio.wait_for(asyncio.to_thread(event.wait), 3)
                for _ in range(5):
                    app.refresh_report()
                    app.refresh_logs()
                    app._tick()
                field = app.query_one("#log-search", Input)
                field.focus()
                await pilot.press("1", "9", "9")
                await pilot.pause(0.25)
                self.assertEqual(field.value, "199")
                self.assertTrue(field.has_focus)
                self.assertEqual(backend.counts, {"status": 1, "logs": 1})
            finally:
                backend.finish.set()
            await app.workers.wait_for_complete()


if __name__ == "__main__":
    unittest.main()
