import copy
import importlib.util
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from oc_box.tui import DemoBackend
from oc_box.tui_backend import SessionConnection
from oc_box.tui_logs import LogEntry

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui_app import DashboardApp
    from textual.widgets import Button, DataTable, Input, Switch, TabbedContent


class SessionDemo(DemoBackend):
    def __init__(self):
        self.calls, self.closed = [], 0
        self.active = False
        self.records = {
            "ses_root": {
                "id": "ses_root",
                "projectID": "project_fixture",
                "location": {"directory": "/home/demo/projects/example"},
                "time": {"created": 1, "updated": 2},
                "title": "Root",
            },
            "ses_child": {
                "id": "ses_child",
                "parentID": "ses_root",
                "projectID": "project_fixture",
                "location": {"directory": "/home/demo/projects/other"},
                "time": {"created": 2, "updated": 3},
                "title": "Child",
            },
        }

    def session_connection(self, workspace=None):
        def request(method, target, body=None):
            self.calls.append((method, target, body))
            path = urlparse(target).path
            if path == "/api/session/active":
                return {
                    "data": {"ses_root": {"type": "running"}} if self.active else {}
                }
            if path == "/api/experimental/job":
                return {"data": []}
            if path == "/api/session":
                return {
                    "data": copy.deepcopy(list(self.records.values())),
                    "cursor": {"next": None},
                }
            ident = path.rsplit("/", 1)[-1]
            if method == "GET":
                return {"data": copy.deepcopy(self.records[ident])}
            if method == "PATCH":
                if body["archived"]:
                    self.records[ident]["time"]["archived"] = 4
                else:
                    self.records[ident]["time"].pop("archived", None)
                return None
            if method == "DELETE":
                pending = [ident]
                while pending:
                    current = pending.pop()
                    pending.extend(
                        s["id"]
                        for s in self.records.values()
                        if s.get("parentID") == current
                    )
                    del self.records[current]
                return None
            raise AssertionError("Unexpected fixture operation")

        def close():
            self.closed += 1

        return SessionConnection(
            "host", "fixture-host", request, lambda *args: "process-local", close
        )


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class DashboardInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_five_tabs_demo_has_no_connections_and_small_terminal_works(self):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.query_one("#workspaces", DataTable).row_count, 1)
            self.assertEqual(len(app.logs.search()), 2)
            self.assertFalse(backend.calls)
            self.assertTrue(app.query_one("#delete", Button).disabled)
            self.assertTrue(app.query_one("#terminal-owner", Button).disabled)
            for name in ("status", "settings", "sessions", "terminals", "logs"):
                app.action_tab(name)
                await pilot.pause()
                self.assertEqual(app.query_one("#content", TabbedContent).active, name)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()

    async def test_log_search_is_available_outside_logs_and_survives_follow_pause(self):
        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.query_one("#log-search", Input).value = "READY"
            await pilot.pause()
            self.assertEqual(app.query_one("#content", TabbedContent).active, "status")
            self.assertEqual(len(app.logs.search(app.log_query)), 1)
            app.query_one("#log-follow", Switch).value = False
            app.action_logs()
            await pilot.pause()
            self.assertEqual(app.query_one("#content", TabbedContent).active, "logs")
            self.assertTrue(app.query_one("#log-search", Input).has_focus)
            app._apply_logs(
                [
                    LogEntry(
                        "2026-01-01T12:00:02Z", "podman", "info", "More ready output"
                    )
                ]
            )
            self.assertEqual(app.log_query, "READY")
            self.assertEqual(len(app.logs.search(app.log_query)), 1)
            app.query_one("#log-search", Input).value = "dashboard"
            await pilot.pause()
            self.assertEqual(len(app.logs.search(app.log_query)), 1)

    async def test_global_inventory_includes_children_and_archive_uses_native_patch(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend)
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            app.action_tab("sessions")
            await pilot.pause()
            await pilot.click("#connect-sessions")
            await self.settle(app, pilot)
            self.assertEqual(app.query_one("#session-table", DataTable).row_count, 2)
            self.assertEqual(
                {s["id"] for s in app.visible_sessions}, {"ses_root", "ses_child"}
            )
            await pilot.click("#archive")
            await pilot.pause()
            await pilot.click("#confirm")
            await self.settle(app, pilot)
            self.assertTrue(
                any(
                    method == "PATCH" and body == {"archived": True}
                    for method, _, body in backend.calls
                )
            )
            self.assertEqual(app.catalog.origin, "host")
            app._disconnect_sessions()
            self.assertEqual(backend.closed, 1)
        self.assertEqual(backend.closed, 1)

    async def test_delete_requires_typed_confirmation_and_previews_other_directory_child(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend)
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            root = next(s for s in app.sessions if s["id"] == "ses_root")
            app.prepare_delete(root, app.catalog)
            await self.settle(app, pilot)
            app.screen.query_one("#confirmation", Input).value = "wrong"
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertFalse(any(method == "DELETE" for method, _, _ in backend.calls))
            app.screen.query_one("#confirmation", Input).value = "DELETE"
            await pilot.click("#confirm")
            await self.settle(app, pilot)
            self.assertEqual(backend.records, {})

    async def test_epoch_zero_archive_is_shown_and_restores_instead_of_archiving_again(
        self,
    ):
        backend = SessionDemo()
        backend.records["ses_root"]["time"]["archived"] = 0
        app = DashboardApp(backend)
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            app.action_tab("sessions")
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            table = app.query_one("#session-table", DataTable)
            self.assertEqual(table.get_row("ses_root")[3].plain, "archived")
            index = next(
                i
                for i, row in enumerate(app.visible_sessions)
                if row["id"] == "ses_root"
            )
            table.move_cursor(row=index)
            app._archive()
            await pilot.pause()
            await pilot.click("#confirm")
            await self.settle(app, pilot)
            self.assertTrue(
                any(
                    method == "PATCH" and body == {"archived": False}
                    for method, _, body in backend.calls
                )
            )

    async def test_active_session_archive_refuses_without_native_mutation(self):
        backend = SessionDemo()
        backend.active = True
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            app.mutate_session(app.catalog, "ses_root", archived=True)
            await self.settle(app, pilot)
            self.assertFalse(
                any(method in ("PATCH", "DELETE") for method, _, _ in backend.calls)
            )

    async def test_refresh_preserves_selected_session_and_stale_connect_is_discarded(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend)
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            table = app.query_one("#session-table", DataTable)
            index = next(
                i
                for i, row in enumerate(app.visible_sessions)
                if row["id"] == "ses_root"
            )
            table.move_cursor(row=index)
            app._render_workspace()
            self.assertEqual(app._session()["id"], "ses_root")
            stale = backend.session_connection()
            app._connected_sessions(
                stale, app.catalog, [], [], app._catalog_generation - 1
            )
            self.assertEqual(backend.closed, 1)
            self.assertEqual(table.row_count, 2)

    async def test_manual_storage_scan_does_not_block_live_refresh_or_log_search(self):
        class SlowDemo(DemoBackend):
            def __init__(self):
                self.entered, self.finish = threading.Event(), threading.Event()
                self.samples = 0

            def collect(self, *, scan_storage=False):
                self.samples += 1
                return super().collect(scan_storage=scan_storage)

            def scan_storage(self, workspace):
                self.entered.set()
                self.finish.wait(5)
                return super().scan_storage(workspace)

        backend = SlowDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.scan_storage(app.selected)
            try:
                for _ in range(100):
                    await pilot.pause(0.01)
                    if backend.entered.is_set():
                        break
                self.assertTrue(backend.entered.is_set())
                app.refresh_report()
                for _ in range(100):
                    await pilot.pause(0.01)
                    if backend.samples >= 2:
                        break
                self.assertGreaterEqual(backend.samples, 2)
                self.assertTrue(app._storage_lock.locked())
                app.query_one("#log-search", Input).value = "ready"
                await pilot.pause()
                self.assertEqual(len(app.logs.search(app.log_query)), 1)
            finally:
                backend.finish.set()
            await self.settle(app, pilot)
            self.assertEqual(app._workspace()["storage"]["project_bytes"], 2 * 1024**3)
            self.assertTrue(app._workspace().get("storage_sampled_at"))

    async def test_terminal_owns_control_keys_but_log_search_remains_clickable(self):
        app = DashboardApp(DemoBackend())
        callback = Mock()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.action_tab("terminals")
            await pilot.pause()
            widget = app._terminal_widget
            widget.connect(
                [sys.executable, "-u", "-c", "import time; time.sleep(20)"],
                {},
                callback,
            )
            widget.focus()
            with patch.object(widget.transport, "send", return_value=True) as send:
                await pilot.press("ctrl+q", "ctrl+c", "ctrl+l", "q")
                self.assertEqual(
                    [call.args[0] for call in send.call_args_list],
                    [b"\x11", b"\x03", b"\x0c", b"q"],
                )
                self.assertTrue(app.is_running)
                await pilot.click("#log-search")
                app.query_one("#log-search", Input).value = "ready"
                await pilot.pause()
                self.assertEqual(len(app.logs.search(app.log_query)), 1)
                self.assertEqual(widget.state, "connected")
            widget.disconnect()
            callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
