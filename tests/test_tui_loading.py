"""Bounded, event-driven tests of explicit session inventory loading UX."""

import asyncio
import copy
import importlib.util
import threading
import unittest
from collections import deque
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from oc_box.tui import DemoBackend
from oc_box.tui_backend import SessionConnection

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui_app import DashboardApp
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import (
        Button,
        DataTable,
        LoadingIndicator,
        Select,
        Static,
        Switch,
    )


class RequestGate:
    def __init__(self, *, fail=False):
        self.entered, self.release = threading.Event(), threading.Event()
        self.fail = fail

    def wait(self):
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError("Synthetic inventory gate timed out")
        if self.fail:
            raise RuntimeError("PRIVATE_NATIVE_ERROR synthetic-secret")


class InventoryAttempt:
    def __init__(self, *, connect=None, inventory=None):
        self.connect, self.inventory = connect, inventory
        self.closed = threading.Event()


class LoadingBackend(DemoBackend):
    """Only in-memory native-shaped data; never calls a host or workspace API."""

    def __init__(self, count=2):
        self.attempts, self.opened, self.calls = deque(), [], []
        self.records = [
            {
                "id": f"ses_loading_{index}",
                "projectID": "project_synthetic",
                "title": f"Synthetic session {index}",
                "location": {"directory": "/home/demo/projects/example"},
                "time": {"created": 1, "updated": index + 2},
            }
            for index in range(count)
        ]

    def session_connection(self, workspace=None):
        attempt = self.attempts.popleft() if self.attempts else InventoryAttempt()
        self.opened.append(attempt)
        if attempt.connect:
            attempt.connect.wait()

        def request(method, target, body=None):
            self.calls.append((method, target, body))
            if method != "GET" or urlparse(target).path != "/api/session":
                raise AssertionError("Unexpected synthetic inventory request")
            if attempt.inventory:
                attempt.inventory.wait()
            return {"data": copy.deepcopy(self.records), "cursor": {"next": None}}

        return SessionConnection(
            "host",
            "synthetic-host",
            request,
            lambda *args: "process-local",
            attempt.closed.set,
        )


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class SessionLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def wait_event(self, event):
        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(event.wait, 6), 7))

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def prepare(self, app, pilot):
        await self.settle(app, pilot)
        app.query_one("#auto-refresh", Switch).value = False
        app.query_one("#log-follow", Switch).value = False
        app.action_tab("sessions")
        await pilot.pause()

    def status(self, app):
        return app.query_one("#session-progress-text", Static).render().plain

    def assert_busy(self, app, *, cancelling=False):
        self.assertTrue(app._session_busy)
        connect = app.query_one("#connect-sessions", Button)
        self.assertTrue(connect.disabled)
        self.assertEqual(
            str(connect.label), "Cancelling..." if cancelling else "Loading..."
        )
        self.assertTrue(app.query_one("#session-spinner", LoadingIndicator).display)
        cancel = app.query_one("#disconnect-sessions", Button)
        self.assertFalse(cancel.disabled)
        self.assertEqual(str(cancel.label), "Cancel")

    def assert_idle(self, app):
        self.assertFalse(app._session_busy)
        self.assertFalse(app.query_one("#connect-sessions", Button).disabled)
        self.assertEqual(
            str(app.query_one("#connect-sessions", Button).label), "Connect / refresh"
        )
        self.assertEqual(
            str(app.query_one("#disconnect-sessions", Button).label), "Disconnect"
        )
        self.assertFalse(app.query_one("#session-spinner", LoadingIndicator).display)
        self.assertIsNone(app._pending_cancel)
        self.assertIsNone(app._pending_connection)
        self.assertIsNone(app._pending_generation)

    async def test_delayed_success_shows_immediate_selectable_source_and_each_stage(
        self,
    ):
        backend = LoadingBackend()
        connect, inventory = RequestGate(), RequestGate()
        attempt = InventoryAttempt(connect=connect, inventory=inventory)
        backend.attempts.append(attempt)
        app = DashboardApp(backend, read_only=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.prepare(app, pilot)
            try:
                app.connect_sessions("global", app.selected)
                self.assert_busy(app)
                self.assertIn("Loading Host · all projects", self.status(app))
                self.assertIn("Connecting to the native service", self.status(app))
                await self.wait_event(connect.entered)
                await pilot.pause()
                status = app.query_one("#session-progress-text", Static)
                app.screen.selections = {status: Selection(Offset(0, 0), Offset(7, 0))}
                await pilot.pause()
                self.assertEqual(app.screen.get_selected_text(), "Loading")
                app.screen.clear_selection()
                connect.release.set()
                await self.wait_event(inventory.entered)
                await pilot.pause()
                self.assert_busy(app)
                self.assertIn("Fetching session inventory", self.status(app))
            finally:
                connect.release.set()
                inventory.release.set()
            await self.settle(app, pilot)
            self.assert_idle(app)
            self.assertIn("Loaded Host · all projects: 2 sessions", self.status(app))
            self.assertEqual(app.query_one("#session-table", DataTable).row_count, 2)
            self.assertFalse(attempt.closed.is_set())
        self.assertTrue(attempt.closed.is_set())

    async def test_pending_refresh_keeps_existing_rows_cursor_and_scroll_untouched(
        self,
    ):
        backend = LoadingBackend(count=140)
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.prepare(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            table = app.query_one("#session-table", DataTable)
            table.move_cursor(row=80)
            table.scroll_to(y=60, animate=False, force=True)
            await pilot.pause()
            selected, scroll, cursor = (
                app._session()["id"],
                table.scroll_offset,
                table.cursor_row,
            )
            previous = app.catalog
            gate = RequestGate()
            backend.attempts.append(InventoryAttempt(inventory=gate))
            with patch.object(table, "clear", wraps=table.clear) as clear:
                try:
                    app.connect_sessions("global", app.selected)
                    await self.wait_event(gate.entered)
                    app._apply_report(backend.collect(), False)
                    await pilot.pause()
                    self.assert_busy(app)
                    clear.assert_not_called()
                    self.assertEqual(table.row_count, 140)
                    self.assertEqual(table.cursor_row, cursor)
                    self.assertEqual(table.scroll_offset, scroll)
                    self.assertEqual(app._session()["id"], selected)
                    self.assertIs(app.catalog, previous)
                finally:
                    gate.release.set()
                await self.settle(app, pilot)
            self.assert_idle(app)
            self.assertEqual(app._session()["id"], selected)

    async def test_delayed_failure_retains_rows_hides_private_errors_and_allows_retry(
        self,
    ):
        for phase in ("connect", "inventory"):
            with self.subTest(phase=phase):
                backend = LoadingBackend()
                app = DashboardApp(backend)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.prepare(app, pilot)
                    app.connect_sessions("global", app.selected)
                    await self.settle(app, pilot)
                    table = app.query_one("#session-table", DataTable)
                    table.move_cursor(row=1)
                    previous, selected = app.catalog, app._session()["id"]
                    gate = RequestGate(fail=True)
                    attempt = InventoryAttempt(**{phase: gate})
                    backend.attempts.append(attempt)
                    try:
                        app.connect_sessions("global", app.selected)
                        await self.wait_event(gate.entered)
                        self.assert_busy(app)
                        self.assertEqual(table.row_count, 2)
                    finally:
                        gate.release.set()
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertIs(app.catalog, previous)
                    self.assertEqual(app._session()["id"], selected)
                    self.assertEqual(table.row_count, 2)
                    for message in (
                        self.status(app),
                        app.query_one("#notice", Static).render().plain,
                    ):
                        self.assertIn("inventory unavailable or incomplete", message)
                        self.assertIn("Host · all projects", message)
                        self.assertNotIn("PRIVATE_NATIVE_ERROR", message)
                        self.assertNotIn("synthetic-secret", message)
                    if phase == "inventory":
                        self.assertTrue(attempt.closed.is_set())
                    app.connect_sessions("global", app.selected)
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertEqual(len(backend.opened), 3)
                    self.assertIn("Loaded Host", self.status(app))

    async def test_duplicate_click_and_refresh_are_visible_and_never_open_another_source(
        self,
    ):
        backend = LoadingBackend()
        gate = RequestGate()
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.prepare(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            backend.attempts.append(InventoryAttempt(inventory=gate))
            backend.calls.clear()
            try:
                await pilot.click("#connect-sessions")
                await self.wait_event(gate.entered)
                await pilot.click("#connect-sessions")
                self.assertIsNone(app.connect_sessions("global", app.selected))
                app.action_refresh()
                self.assert_busy(app)
                self.assertEqual(len(backend.opened), 2)
                self.assertEqual(len(backend.calls), 1)
                self.assertIn(
                    "already loading", app.query_one("#notice", Static).render().plain
                )
            finally:
                gate.release.set()
            await self.settle(app, pilot)
            self.assert_idle(app)

    async def test_scope_and_settings_changes_cancel_without_implicitly_connecting(
        self,
    ):
        for phase, via_settings in (
            ("connect", False),
            ("inventory", False),
            ("inventory", True),
        ):
            with self.subTest(phase=phase, via_settings=via_settings):
                backend = LoadingBackend()
                app = DashboardApp(backend)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.prepare(app, pilot)
                    app.connect_sessions("global", app.selected)
                    await self.settle(app, pilot)
                    previous, connection = app.catalog, app._session_connection
                    gate = RequestGate()
                    attempt = InventoryAttempt(**{phase: gate})
                    backend.attempts.append(attempt)
                    try:
                        app.connect_sessions("global", app.selected)
                        await self.wait_event(gate.entered)
                        cancel = app._pending_cancel
                        if via_settings:
                            app.action_settings()
                            await pilot.pause()
                            values = dict(app.screen.values)
                            await pilot.press("escape")
                            app.apply_settings({**values, "session_scope": "project"})
                        else:
                            app.query_one("#session-scope", Select).value = "project"
                        await pilot.pause()
                        self.assertTrue(cancel.is_set())
                        self.assert_busy(app, cancelling=True)
                        self.assertIn("Source selection changed", self.status(app))
                        self.assertIn(
                            "bounded request is still finishing", self.status(app)
                        )
                        self.assertIn("No replacement source", self.status(app))
                        self.assertEqual(len(backend.opened), 2)
                        self.assertIs(app.catalog, previous)
                        self.assertIs(app._session_connection, connection)
                        self.assertEqual(
                            app.query_one("#session-table", DataTable).row_count, 2
                        )
                    finally:
                        gate.release.set()
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertTrue(attempt.closed.is_set())
                    self.assertIs(app.catalog, previous)
                    self.assertIn("Cancelled Host", self.status(app))
                    self.assertEqual(len(backend.opened), 2)

    async def test_disconnect_shows_cancelling_until_bounded_call_finishes_then_reenables(
        self,
    ):
        for phase in ("connect", "inventory"):
            with self.subTest(phase=phase):
                backend = LoadingBackend()
                gate = RequestGate()
                attempt = InventoryAttempt(**{phase: gate})
                backend.attempts.append(attempt)
                app = DashboardApp(backend)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.prepare(app, pilot)
                    try:
                        app.connect_sessions("global", app.selected)
                        await self.wait_event(gate.entered)
                        await pilot.click("#disconnect-sessions")
                        self.assert_busy(app, cancelling=True)
                        self.assertIn("Disconnect requested", self.status(app))
                        self.assertIn(
                            "bounded request is still finishing", self.status(app)
                        )
                        self.assertIsNone(app.connect_sessions("project", app.selected))
                        self.assertIn(
                            "Cancellation is still finishing",
                            app.query_one("#notice", Static).render().plain,
                        )
                        self.assertEqual(len(backend.opened), 1)
                        self.assertIsNone(app.catalog)
                    finally:
                        gate.release.set()
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertTrue(attempt.closed.is_set())
                    self.assertIn("bounded request has finished", self.status(app))
                    self.assertIsNone(app.catalog)
                    app.connect_sessions("global", app.selected)
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertEqual(
                        app.query_one("#session-table", DataTable).row_count, 2
                    )

    async def test_stale_generation_and_wrong_cancel_token_cannot_clear_a_new_spinner(
        self,
    ):
        backend = LoadingBackend()
        first, second = RequestGate(), RequestGate()
        backend.attempts.extend(
            (InventoryAttempt(inventory=first), InventoryAttempt(inventory=second))
        )
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.prepare(app, pilot)
            try:
                app.connect_sessions("global", app.selected)
                await self.wait_event(first.entered)
                old_generation, old_cancel = (
                    app._pending_generation,
                    app._pending_cancel,
                )
                app._disconnect_sessions()
                first.release.set()
                await self.settle(app, pilot)
                app.connect_sessions("global", app.selected)
                await self.wait_event(second.entered)
                current = self.status(app)
                generation, cancel = app._pending_generation, app._pending_cancel
                for stale_generation, stale_cancel in (
                    (old_generation, old_cancel),
                    (generation, old_cancel),
                    (old_generation, cancel),
                ):
                    app._session_progress(stale_generation, stale_cancel, "Stale stage")
                    for outcome in ("failed", "complete", "cancelled"):
                        app._finish_session_request(
                            stale_generation, stale_cancel, outcome
                        )
                    self.assert_busy(app)
                    self.assertEqual(self.status(app), current)
                stale = Mock()
                self.assertFalse(
                    app._connected_sessions(
                        stale, None, [], [], old_generation, old_cancel
                    )
                )
                stale.close.assert_called_once()
                self.assertEqual(self.status(app), current)
                self.assert_busy(app)
            finally:
                first.release.set()
                second.release.set()
            await self.settle(app, pilot)
            self.assert_idle(app)

    async def test_cancel_event_alone_blocks_queued_progress_and_success(self):
        backend = LoadingBackend()
        gate = RequestGate()
        backend.attempts.append(InventoryAttempt(inventory=gate))
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.prepare(app, pilot)
            try:
                app.connect_sessions("global", app.selected)
                await self.wait_event(gate.entered)
                generation, cancel = app._pending_generation, app._pending_cancel
                app._cancel_pending_connection(reason="Cancel requested")
                previous = self.status(app)
                app._session_progress(generation, cancel, "Stale stage")
                stale = Mock()
                self.assertFalse(
                    app._connected_sessions(stale, None, [], [], generation, cancel)
                )
                stale.close.assert_called_once()
                self.assertEqual(self.status(app), previous)
                self.assert_busy(app, cancelling=True)
            finally:
                gate.release.set()
            await self.settle(app, pilot)
            self.assert_idle(app)
            self.assertIsNone(app.catalog)

    async def test_workspace_selection_change_cancels_pending_inventory_without_connecting(
        self,
    ):
        class TwoWorkspaces(LoadingBackend):
            def collect(self, *, scan_storage=False):
                report = super().collect(scan_storage=scan_storage)
                other = copy.deepcopy(report["workspaces"][0])
                other.update(workspace_id="b" * 32, project="/home/demo/projects/other")
                report["workspaces"].append(other)
                return report

        for change in ("cursor", "registration-removed"):
            with self.subTest(change=change):
                backend = TwoWorkspaces()
                gate = RequestGate()
                backend.attempts.append(InventoryAttempt(inventory=gate))
                app = DashboardApp(backend)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.prepare(app, pilot)
                    try:
                        app.connect_sessions("global", app.selected)
                        await self.wait_event(gate.entered)
                        if change == "cursor":
                            app.query_one("#workspaces", DataTable).move_cursor(row=1)
                        else:
                            report = backend.collect()
                            report["workspaces"].pop(0)
                            app._apply_report(report, False)
                        await pilot.pause()
                        self.assertEqual(app.selected, "b" * 32)
                        self.assertTrue(app._pending_cancel.is_set())
                        self.assert_busy(app, cancelling=True)
                        self.assertIn("Source selection changed", self.status(app))
                        self.assertEqual(len(backend.opened), 1)
                    finally:
                        gate.release.set()
                    await self.settle(app, pilot)
                    self.assert_idle(app)
                    self.assertIsNone(app.catalog)
                    self.assertEqual(len(backend.opened), 1)

    async def test_cancelled_textual_worker_cannot_leave_loading_controls_stuck(self):
        backend = LoadingBackend()
        gate = RequestGate()
        attempt = InventoryAttempt(inventory=gate)
        backend.attempts.append(attempt)
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.prepare(app, pilot)
            finished = asyncio.Event()
            original_finish = app._finish_session_request

            def finish(*args):
                original_finish(*args)
                finished.set()

            with patch.object(app, "_finish_session_request", side_effect=finish):
                try:
                    worker = app.connect_sessions("global", app.selected)
                    await self.wait_event(gate.entered)
                    worker.cancel()
                finally:
                    gate.release.set()
                await asyncio.wait_for(finished.wait(), 6)
            self.assert_idle(app)
            self.assertTrue(attempt.closed.is_set())
            self.assertIn("Cancelled Host", self.status(app))
            self.assertIsNone(app.catalog)

    async def test_shutdown_invalidates_pending_worker_and_ignores_late_ui_callbacks(
        self,
    ):
        backend = LoadingBackend()
        gate = RequestGate()
        attempt = InventoryAttempt(inventory=gate)
        backend.attempts.append(attempt)
        app = DashboardApp(backend)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await self.prepare(app, pilot)
                app.connect_sessions("global", app.selected)
                await self.wait_event(gate.entered)
                generation, cancel = app._pending_generation, app._pending_cancel
                self.assert_busy(app)
        finally:
            gate.release.set()
        await self.wait_event(attempt.closed)
        self.assertFalse(app.is_running)
        self.assertTrue(cancel.is_set())
        app._session_progress(generation, cancel, "Stale stage")
        app._finish_session_request(generation, cancel, "failed")
        stale = Mock()
        self.assertFalse(
            app._connected_sessions(stale, None, [], [], generation, cancel)
        )
        stale.close.assert_called_once()
        self.assertIsNone(app.catalog)

    async def test_demo_connect_and_scope_changes_never_start_loading_or_use_native_api(
        self,
    ):
        backend = LoadingBackend()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.prepare(app, pilot)
            app.connect_sessions("global", app.selected)
            app.query_one("#session-scope", Select).value = "workspace"
            await pilot.pause()
            self.assert_idle(app)
            self.assertFalse(backend.opened)
            self.assertFalse(backend.calls)
            self.assertTrue(app.query_one("#delete", Button).disabled)
            self.assertTrue(app.query_one("#terminal-owner", Button).disabled)


if __name__ == "__main__":
    unittest.main()
