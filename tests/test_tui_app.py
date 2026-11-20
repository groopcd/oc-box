import copy
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from oc_box import activity_log
from oc_box import oc_box as box
from oc_box.tui import DemoBackend
from oc_box.tui_backend import (
    DashboardBackend,
    SessionConnection,
    TerminalAttachmentError,
    TerminalHandle,
)
from oc_box.tui_logs import LogEntry

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui_app import DashboardApp, Confirm
    from textual.geometry import Offset
    from textual.screen import ModalScreen
    from textual.selection import Selection
    from textual.widgets import (
        Button,
        Collapsible,
        DataTable,
        Input,
        Select,
        Static,
        Switch,
        TabbedContent,
    )
    from oc_box.tui_settings import SettingsPanel, DEFAULT_SETTINGS
    from oc_box.tui_clipboard import ClipboardInput
    from oc_box.tui_status import MonitorPanel


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
            if path.startswith("/api/experimental/session/") and path.endswith(
                "/terminal"
            ):
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
            app._poll_timer.pause()
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
                ],
                private=False,
            )
            self.assertEqual(app.log_query, "READY")
            self.assertEqual(
                [entry.message for entry in app.logs.search(app.log_query)],
                ["More ready output"],
            )
            app.query_one("#log-search", Input).value = "dashboard"
            await pilot.pause()
            self.assertEqual(
                [entry.message for entry in app.logs.search(app.log_query)],
                ["Synthetic dashboard started"],
            )

    def test_session_journal_refuses_resolved_state_symlink_without_changing_ui_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            linked = state.with_name("linked-state")
            linked.symlink_to(state, target_is_directory=True)
            catalog = SimpleNamespace(origin="workspace", store="a" * 32)
            private_config = str(Path(temporary) / "config")
            with patch.dict(
                os.environ,
                {"OC_BOX_STATE_HOME": str(linked), "XDG_CONFIG_HOME": private_config},
            ):
                app = DashboardApp(DashboardBackend(box))
                self.assertEqual(app.favorites.root, state / "ui")
                app._journal("session.archived", catalog)
                self.assertFalse((state / "log").exists())
            with patch.dict(
                os.environ,
                {"OC_BOX_STATE_HOME": str(state), "XDG_CONFIG_HOME": private_config},
            ):
                app._journal("session.archived", catalog)
                [record] = activity_log.read_events()
            self.assertEqual(record["event"], "session.archived")
            self.assertEqual(record["command"], "tui")
            self.assertEqual(record["workspace"], catalog.store)

    async def test_session_disconnect_is_beside_connect_and_search_clear_is_local(self):
        for size in ((140, 45), (80, 24)):
            with self.subTest(size=size):
                backend = SessionDemo()
                app = DashboardApp(backend, read_only=True)
                async with app.run_test(size=size) as pilot:
                    await self.settle(app, pilot)
                    app.action_tab("sessions")
                    await pilot.pause()
                    connect = app.query_one("#connect-sessions", Button)
                    disconnect = app.query_one("#disconnect-sessions", Button)
                    self.assertIs(connect.parent, disconnect.parent)
                    self.assertEqual(connect.region.y, disconnect.region.y)
                    self.assertLess(connect.region.right, disconnect.region.x)
                    self.assertLessEqual(
                        disconnect.region.right, disconnect.parent.content_region.right
                    )
                    self.assertEqual(backend.calls, [])
                    await pilot.click("#connect-sessions")
                    await self.settle(app, pilot)
                    self.assertIsNotNone(app.catalog)
                    search = app.query_one("#session-search", Input)
                    search.value = "Root"
                    for _ in range(100):
                        await pilot.pause(0.02)
                        if len(app.visible_sessions) == 1:
                            break
                    self.assertEqual(len(app.visible_sessions), 1)
                    calls = list(backend.calls)
                    generation = app._catalog_generation
                    await pilot.click(search, offset=(search.region.width - 2, 1))
                    for _ in range(100):
                        await pilot.pause(0.02)
                        if len(app.visible_sessions) == 2:
                            break
                    self.assertEqual(search.value, "")
                    self.assertIs(app.focused, search)
                    self.assertEqual(len(app.visible_sessions), 2)
                    self.assertEqual(backend.calls, calls)
                    self.assertEqual(backend.closed, 0)
                    self.assertEqual(app._catalog_generation, generation)
                    self.assertTrue(app.query_one("#delete", Button).disabled)
                    self.assertTrue(app.query_one("#terminal-owner", Button).disabled)
                    await pilot.click("#disconnect-sessions")
                    await self.settle(app, pilot)
                    self.assertIsNone(app.catalog)
                    self.assertEqual(backend.closed, 1)
                    self.assertEqual(backend.calls, calls)
                self.assertEqual(backend.closed, 1)

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
            # Exercise wrong -> valid clicks without the purely visual button
            # cooldown swallowing the second synthetic click on faster runners.
            app.screen.query_one("#confirm", Button).active_effect_duration = 0
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

    async def test_disconnect_cancels_pending_connect_and_pagination_without_more_pages(
        self,
    ):
        for phase in ("connect", "page"):
            with self.subTest(phase=phase):

                class PendingDemo(SessionDemo):
                    def __init__(self):
                        super().__init__()
                        self.entered, self.finish, self.closed_event = (
                            threading.Event(),
                            threading.Event(),
                            threading.Event(),
                        )

                    def session_connection(self, workspace=None):
                        base = super().session_connection(workspace)
                        if phase == "connect":
                            self.entered.set()
                            self.finish.wait(5)

                        def request(method, target, body=None):
                            if (
                                phase == "page"
                                and urlparse(target).path == "/api/session"
                            ):
                                self.calls.append((method, target, body))
                                self.entered.set()
                                self.finish.wait(5)
                                return {
                                    "data": [copy.deepcopy(self.records["ses_root"])],
                                    "cursor": {"next": "next-page"},
                                }
                            return base.request(method, target, body)

                        return SessionConnection(
                            base.origin,
                            base.store,
                            request,
                            base.ownership,
                            self.closed_event.set,
                        )

                backend = PendingDemo()
                app = DashboardApp(backend)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.settle(app, pilot)
                    app.connect_sessions("global", app.selected)
                    try:
                        for _ in range(100):
                            await pilot.pause(0.01)
                            if backend.entered.is_set():
                                break
                        self.assertTrue(backend.entered.is_set())
                        app._disconnect_sessions()
                    finally:
                        backend.finish.set()
                    await self.settle(app, pilot)
                    self.assertTrue(backend.closed_event.is_set())
                    self.assertIsNone(app.catalog)
                    self.assertIsNone(app._pending_connection)
                    self.assertIsNone(app._pending_cancel)
                    self.assertFalse(
                        any("cursor=" in target for _, target, _ in backend.calls)
                    )
                    self.assertEqual(len(backend.calls), 0 if phase == "connect" else 1)

    async def test_late_mutation_callback_cannot_restore_disconnected_or_replaced_source(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            old_catalog, old_rows, old_generation = (
                app.catalog,
                list(app.sessions),
                app._catalog_generation,
            )
            app._disconnect_sessions()
            app._apply_mutation_result(old_catalog, old_rows, old_generation)
            self.assertIsNone(app.catalog)
            self.assertEqual(app.visible_sessions, [])
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            current, connection = app.catalog, app._session_connection
            app._apply_mutation_result(old_catalog, [], old_generation)
            self.assertIs(app.catalog, current)
            self.assertIs(app._session_connection, connection)
            self.assertEqual(len(app.visible_sessions), 2)

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

    async def test_settings_shortcuts_navigate_inline_topics_and_apply_without_backend_actions(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app._settings_snapshot()["interval"], 1)
            await pilot.press("s")
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertEqual(
                app.query_one("#content", TabbedContent).active, "settings"
            )
            panel = app.query_one(SettingsPanel)
            self.assertEqual(len(app.screen.query(".settings-topic")), 7)
            self.assertTrue(app.screen.query("#host-logging-settings.settings-topic"))
            self.assertEqual(app.screen.query_one("#pref-interval", Input).value, "1")
            app.screen.query_one("#pref-interval", Input).value = "17"
            app.screen.query_one("#pref-auto-refresh", Switch).value = False
            app.screen.query_one("#pref-log-follow", Switch).value = False
            app.screen.query_one("#pref-details", Switch).value = True
            app.screen.query_one("#pref-log-query", Input).value = "ready"
            app.screen.query_one("#pref-session-filter", Input).value = "example"
            await pilot.click("#preferences-apply")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertEqual(app._settings_snapshot()["interval"], 17)
            self.assertFalse(app._settings_snapshot()["auto_refresh"])
            self.assertFalse(panel.dirty)
            self.assertFalse(app.query_one("#log-follow", Switch).value)
            self.assertFalse(app.query_one("#workspace-details", Collapsible).collapsed)
            self.assertEqual(app.log_query, "ready")
            self.assertEqual(app.query_one("#session-search", Input).value, "example")
            self.assertTrue(app.read_only)
            self.assertEqual(backend.calls, [])
            await pilot.press("f2")
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertEqual(
                app.query_one("#content", TabbedContent).active, "settings"
            )
            app.screen.query_one("#pref-interval", Input).value = "59"
            app.screen.query_one("#pref-interval", Input).focus()
            await pilot.press("escape")
            self.assertEqual(app._settings_snapshot()["interval"], 17)
            self.assertEqual(panel.query_one("#pref-interval", Input).value, "17")

    async def test_refresh_interval_apply_accepts_all_integers_and_rejects_bad_types_atomically(
        self,
    ):
        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.action_settings()
            await pilot.pause()
            values = dict(app.query_one(SettingsPanel).values)
            await pilot.press("escape")
            for interval in range(1, 61):
                with self.subTest(valid_interval=interval):
                    app.apply_settings({**values, "interval": interval})
                    self.assertEqual(app._settings_snapshot()["interval"], interval)
            for name, interval in (
                ("zero", 0),
                ("negative", -1),
                ("above maximum", 61),
                ("large", 999),
                ("true", True),
                ("false", False),
                ("fraction", 1.5),
                ("integral float", 7.0),
                ("nan", float("nan")),
                ("infinity", float("inf")),
                ("string", "7"),
                ("blank", ""),
                ("none", None),
                ("list", [7]),
                ("mapping", {"value": 7}),
            ):
                with self.subTest(invalid_interval=name):
                    with patch.object(app, "_message") as message:
                        app.apply_settings(
                            {
                                **values,
                                "interval": interval,
                                "auto_refresh": False,
                                "session_filter": "must not apply",
                                "log_query": "must not apply",
                            }
                        )
                    self.assertEqual(app._settings_snapshot()["interval"], 60)
                    self.assertTrue(app._settings_snapshot()["auto_refresh"])
                    self.assertEqual(app.query_one("#session-search", Input).value, "")
                    self.assertEqual(app.query_one("#log-search", Input).value, "")
                    message.assert_called_once_with(
                        "Unsupported dashboard setting; no changes applied."
                    )

    async def test_settings_interval_input_named_validation_matrix_preserves_draft(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        apply_messages = []
        apply_inputs = {}
        invalid_intervals = (
            ("blank", ""),
            ("fraction", "1.5"),
            ("zero", "0"),
            ("negative", "-1"),
            ("above maximum", "61"),
            ("nan", "NaN"),
            ("infinity", "Infinity"),
            ("boolean", "True"),
            ("exponent", "1e1"),
            ("whitespace", " 7 "),
            ("huge number", "9" * 5000),
        )
        valid_intervals = (
            ("custom middle", "17"),
            ("custom near maximum", "59"),
            ("upper boundary", "60"),
            ("lower boundary", "1"),
        )

        def record_apply(message):
            if (
                isinstance(message, Button.Pressed)
                and message.button.id == "preferences-apply"
                and not any(message is previous for previous, _ in apply_messages)
            ):
                apply_messages.append(
                    (
                        message,
                        apply_inputs[message.button].value,
                    )
                )

        async with app.run_test(size=(120, 40), message_hook=record_apply) as pilot:
            await self.settle(app, pilot)
            app.action_settings()
            await pilot.pause()
            panel = app.query_one(SettingsPanel)
            # This matrix tests validation, not Textual's timer-based suppression
            # of repeated mouse clicks while the button has its -active effect.
            panel.query_one("#preferences-apply", Button).active_effect_duration = 0
            interval = panel.query_one("#pref-interval", Input)
            apply_inputs[panel.query_one("#preferences-apply", Button)] = interval
            self.assertIsInstance(interval, ClipboardInput)
            self.assertEqual(interval.type, "integer")
            panel.query_one("#pref-session-filter", Input).value = "retained draft"
            panel.query_one("#pref-auto-refresh", Switch).value = False
            panel.query_one("#pref-details", Switch).value = True
            panel.query_one("#pref-log-follow", Switch).value = False
            panel.query_one("#pref-log-query", Input).value = "retained log draft"
            applied = app._settings_snapshot()
            for name, raw in invalid_intervals:
                with self.subTest(invalid_interval=name):
                    interval.value = raw
                    with patch.object(app, "apply_settings") as apply:
                        await pilot.click("#preferences-apply")
                        await pilot.pause()
                    apply.assert_not_called()
                    self.assertNotIsInstance(app.screen, ModalScreen)
                    self.assertEqual(app._settings_snapshot(), applied)
                    self.assertEqual(app.query_one("#session-search", Input).value, "")
                    self.assertEqual(
                        panel.query_one("#pref-session-filter", Input).value,
                        "retained draft",
                    )
                    self.assertIn(
                        "1 to 60 seconds",
                        str(panel.query_one("#pref-interval-error", Static).render()),
                    )
                    self.assertTrue(interval.has_focus)
            interval.value = "7"
            await pilot.click("#preferences-apply")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertEqual(app._settings_snapshot()["interval"], 7)
            self.assertEqual(
                app.query_one("#session-search", Input).value, "retained draft"
            )
            for name, value in valid_intervals:
                with self.subTest(valid_interval=name):
                    app.action_settings()
                    await pilot.pause()
                    interval = app.screen.query_one("#pref-interval", Input)
                    apply_inputs[app.screen.query_one("#preferences-apply", Button)] = (
                        interval
                    )
                    interval.value = value
                    await pilot.click("#preferences-apply")
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ModalScreen)
                    self.assertEqual(app._settings_snapshot()["interval"], int(value))
                    self.assertEqual(
                        app.query_one("#session-search", Input).value, "retained draft"
                    )
            self.assertTrue(app.read_only)
            self.assertEqual(backend.calls, [])
            self.assertEqual(len(apply_messages), 16)
            self.assertEqual(
                [value for _, value in apply_messages],
                [
                    *(value for _, value in invalid_intervals),
                    "7",
                    *(value for _, value in valid_intervals),
                ],
            )

    async def test_custom_refresh_interval_applies_to_tick_without_overlapping_or_queuing(
        self,
    ):
        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app._poll_timer.pause()
            app.apply_settings({**DEFAULT_SETTINGS, "interval": 17})
            app._last_report = app._last_logs = 100
            with (
                patch("oc_box.tui_app.time.monotonic", return_value=116),
                patch.object(app, "refresh_report") as report,
                patch.object(app, "refresh_logs") as logs,
            ):
                app._tick()
                report.assert_not_called()
                logs.assert_not_called()
            with (
                patch("oc_box.tui_app.time.monotonic", return_value=117),
                patch.object(app, "refresh_report") as report,
                patch.object(app, "refresh_logs") as logs,
            ):
                app._status_lock.acquire()
                app._log_lock.acquire()
                try:
                    app._tick()
                    app._tick()
                    report.assert_not_called()
                    logs.assert_not_called()
                finally:
                    app._status_lock.release()
                    app._log_lock.release()
                report.assert_not_called()
                logs.assert_not_called()
                app._tick()
                report.assert_called_once_with()
                logs.assert_called_once_with()

    async def test_diagnostics_and_controls_stay_aligned_across_screen_sizes(self):
        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            app.apply_settings({**app._settings_snapshot(), "auto_refresh": False})
            for dimensions in ((80, 24), (100, 30), (140, 45), (220, 70)):
                with self.subTest(dimensions=dimensions):
                    was_compact = app._compact_layout
                    prior_fold = app.query_one(
                        "#workspace-inspector", Collapsible
                    ).collapsed
                    await pilot.resize_terminal(*dimensions)
                    app.action_tab("status")
                    await pilot.pause()
                    full = app.query_one("#full-diagnostics", MonitorPanel)
                    system = app.query_one("#system-report", MonitorPanel)
                    inspector = app.query_one("#workspace-inspector", Collapsible)
                    inventory = app.query_one("#workspace-card")
                    if dimensions[0] < 118:
                        self.assertEqual(
                            inspector.collapsed, prior_fold if was_compact else True
                        )
                        self.assertEqual(inspector.region.x, inventory.region.x)
                        inspector.collapsed = False
                        await pilot.pause()
                    else:
                        self.assertGreater(inspector.region.x, inventory.region.x)
                    self.assertLessEqual(full.region.right, system.region.right)
                    self.assertLessEqual(full.region.height, 12)
                    self.assertLessEqual(system.region.height, 8)
                    for panel in (full, system):
                        self.assertTrue(
                            all(
                                row.cell_length <= panel.content_size.width
                                for row in panel.render().split("\n")
                            )
                        )
                    header = app.query_one("#host-summary")
                    region = app.screen.find_widget(header)
                    self.assertEqual(
                        region.region.intersection(region.clip), region.region
                    )
                    self.assertEqual(header.content_size.height, 1)
                    app.action_tab("settings")
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one("#pref-auto-refresh").region.x,
                        app.query_one("#pref-interval").region.x,
                    )
                    app.action_tab("sessions")
                    await pilot.pause()
                    for scope in ("global", "project", "workspace"):
                        app.query_one("#session-scope", Select).value = scope
                        await pilot.pause()
                        self.assertEqual(
                            app.query_one("#session-scope").region.height, 3
                        )
                    button = app.query_one("#disconnect-sessions", Button)
                    button.scroll_visible(animate=False, force=True)
                    await pilot.pause()
                    self.assertEqual(app.query_one("#body").scroll_offset, (0, 0))
                    self.assertEqual(app.query_one("#content").scroll_offset, (0, 0))
                    for tab in ("status", "settings", "sessions", "terminals", "logs"):
                        app.action_tab(tab)
                        app.action_logs()
                        await pilot.pause()
                        search = app.query_one("#log-search", Input)
                        region = app.screen.find_widget(search)
                        self.assertEqual(
                            region.region.intersection(region.clip), region.region
                        )
                        self.assertTrue(search.has_focus)

    async def test_reset_defaults_stages_all_preferences_and_cancel_preserves_live_settings(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            modified = {
                **DEFAULT_SETTINGS,
                "theme": next(
                    name
                    for name in app.available_themes
                    if name != DEFAULT_SETTINGS["theme"]
                ),
                "interval": 59,
                "auto_refresh": False,
                "details": True,
                "session_scope": "project",
                "session_filter": "example",
                "log_follow": False,
                "log_source": "oc-box",
                "log_query": "ready",
            }
            app.apply_settings(modified)
            await pilot.pause()
            retained = list(app.logs.search())
            for apply in (False, True):
                app.action_settings()
                await pilot.pause()
                await pilot.click("#preferences-reset")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ModalScreen)
                self.assertTrue(
                    app.screen.query_one("#pref-auto-refresh", Switch).value
                )
                self.assertEqual(
                    app.screen.query_one("#pref-interval", Input).value, "1"
                )
                self.assertEqual(
                    app.screen.query_one("#pref-theme", Select).value,
                    DEFAULT_SETTINGS["theme"],
                )
                self.assertFalse(app.screen.query_one("#pref-details", Switch).value)
                self.assertEqual(
                    app.screen.query_one("#pref-session-scope", Select).value, "global"
                )
                self.assertEqual(
                    app.screen.query_one("#pref-session-filter", Input).value, ""
                )
                self.assertTrue(app.screen.query_one("#pref-log-follow", Switch).value)
                self.assertEqual(
                    app.screen.query_one("#pref-log-source", Select).value, "all"
                )
                self.assertEqual(
                    app.screen.query_one("#pref-log-query", Input).value, ""
                )
                self.assertEqual(app._settings_snapshot()["interval"], 59)
                self.assertTrue(app.read_only)
                if apply:
                    await pilot.click("#preferences-apply")
                else:
                    await pilot.click("#preferences-cancel")
                await pilot.pause()
                self.assertEqual(
                    app._settings_snapshot()["interval"], 1 if apply else 59
                )
            app.action_settings()
            await pilot.pause()
            self.assertEqual(
                app.query_one(SettingsPanel).values, dict(DEFAULT_SETTINGS)
            )
            await pilot.press("escape")
            self.assertEqual(app.logs.search(), retained)
            self.assertEqual(backend.calls, [])
            self.assertTrue(app.read_only)
            self.assertTrue(app.query_one("#delete", Button).disabled)

    async def test_settings_reset_action_is_visible_in_a_small_terminal(self):
        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            app.action_settings()
            await pilot.pause()
            for ident in (
                "preferences-apply",
                "preferences-reset",
                "preferences-cancel",
            ):
                button = app.screen.query_one("#" + ident, Button)
                geometry = app.screen.find_widget(button)
                self.assertEqual(
                    geometry.region.intersection(geometry.clip), geometry.region
                )
            await pilot.click("#preferences-reset")
            await pilot.click("#preferences-cancel")
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertFalse(app.query_one(SettingsPanel).dirty)

    async def test_background_refresh_keeps_session_rows_scroll_and_cursor_static(self):
        backend = SessionDemo()
        template = copy.deepcopy(backend.records["ses_root"])
        for index in range(150):
            backend.records[f"ses_many_{index}"] = {
                **copy.deepcopy(template),
                "id": f"ses_many_{index}",
                "title": f"Synthetic session {index}",
                "time": {"created": 1, "updated": index + 10},
            }
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 35)) as pilot:
            await self.settle(app, pilot)
            app.action_tab("sessions")
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            table = app.query_one("#session-table", DataTable)
            table.move_cursor(row=80)
            table.scroll_to(y=60, animate=False, force=True)
            await pilot.pause()
            scroll, cursor, selected = (
                table.scroll_offset,
                table.cursor_row,
                app._session()["id"],
            )
            self.assertGreater(scroll.y, 0)
            count, calls = table.row_count, len(backend.calls)
            backend.records["ses_added_later"] = {
                **copy.deepcopy(template),
                "id": "ses_added_later",
            }
            with patch.object(table, "clear", wraps=table.clear) as clear:
                for _ in range(3):
                    app._apply_report(backend.collect(), False)
                app._apply_storage(backend.scan_storage(app.selected))
                await pilot.pause(1.25)
                await self.settle(app, pilot)
                clear.assert_not_called()
                self.assertEqual(table.row_count, count)
                self.assertEqual(table.scroll_offset, scroll)
                self.assertEqual(table.cursor_row, cursor)
                self.assertEqual(app._session()["id"], selected)
                self.assertEqual(len(backend.calls), calls)
            table.focus()
            await pilot.press("r")
            await self.settle(app, pilot)
            self.assertEqual(table.row_count, count + 1)
            self.assertGreater(len(backend.calls), calls)

    async def test_explicit_workspace_change_updates_the_cached_project_view(self):
        class TwoWorkspaces(SessionDemo):
            def collect(self, *, scan_storage=False):
                report = super().collect(scan_storage=scan_storage)
                other = copy.deepcopy(report["workspaces"][0])
                other.update(workspace_id="b" * 32, project="/home/demo/projects/other")
                report["workspaces"].append(other)
                return report

        backend = TwoWorkspaces()
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.connect_sessions("global", app.selected)
            await self.settle(app, pilot)
            app.query_one("#session-scope", Select).value = "project"
            await pilot.pause()
            self.assertEqual([s["id"] for s in app.visible_sessions], ["ses_root"])
            calls = len(backend.calls)
            app.query_one("#workspaces", DataTable).move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(app.selected, "b" * 32)
            self.assertEqual([s["id"] for s in app.visible_sessions], ["ses_child"])
            self.assertEqual(len(backend.calls), calls)

    async def test_terminal_failure_shows_safe_category_and_never_raw_errors(self):
        backend = SessionDemo()
        backend.terminal = Mock(
            side_effect=TerminalAttachmentError("owner-unavailable")
        )
        app = DashboardApp(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.open_terminal(app.selected, None)
            await self.settle(app, pilot)
            self.assertIn("owner-unavailable", app.query_one("#notice").render().plain)
            close = Mock()
            backend.terminal.side_effect = None
            backend.terminal.return_value = TerminalHandle(
                ["synthetic-client"],
                {"PRIVATE": "synthetic-private"},
                close,
                "ses_demo",
            )
            with patch.object(
                app._terminal_widget,
                "connect",
                side_effect=RuntimeError("PRIVATE_OUTPUT synthetic-private"),
            ):
                app.open_terminal(app.selected, None)
                await self.settle(app, pilot)
            message = app.query_one("#notice").render().plain
            self.assertIn("client-launch", message)
            self.assertNotIn("PRIVATE_OUTPUT", message)
            self.assertNotIn("synthetic-private", message)
            close.assert_called_once()

    async def test_shortcut_only_clipboard_keeps_search_space_and_input_focus(self):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            for ident in ("copy-selection", "copy-view", "paste-copied"):
                self.assertFalse(app.query("#" + ident))
            search = app.query_one("#log-search", Input)
            geometry = app.screen.find_widget(search)
            self.assertEqual(
                geometry.region.intersection(geometry.clip), geometry.region
            )
            search.value = "copy 界é"
            search.focus()
            search.action_select_all()
            await pilot.press("ctrl+shift+c")
            await pilot.pause()
            self.assertEqual(app.clipboard, "copy 界é")
            self.assertTrue(search.has_focus)

            app.copy_to_clipboard("pasted 界\nnot a second search line")
            search.value = "prefix "
            search.cursor_position = len(search.value)
            await pilot.press("ctrl+shift+insert")
            await pilot.pause()
            self.assertEqual(search.value, "prefix pasted 界")
            self.assertTrue(search.has_focus)
            self.assertTrue(app.read_only)
            self.assertFalse(backend.calls)

    async def test_copy_view_inline_settings_and_modal_clipboard_do_not_apply_or_delete(
        self,
    ):
        backend = SessionDemo()
        app = DashboardApp(backend, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("ctrl+shift+y")
            await pilot.pause()
            self.assertIn("WORKSPACES", app.clipboard)
            self.assertIn("Clipboard help", app.clipboard)
            self.assertFalse(app.query("#copy-selection, #copy-view, #paste-copied"))

            app.action_settings()
            await pilot.pause()
            self.assertFalse(
                app.screen.query("#copy-selection, #copy-view, #paste-copied")
            )
            await pilot.press("ctrl+shift+y")
            await pilot.pause()
            self.assertIn("Dashboard settings", app.clipboard)
            self.assertIn("Reset to defaults", app.clipboard)
            self.assertNotIsInstance(app.screen, ModalScreen)
            self.assertEqual(
                app.query_one("#content", TabbedContent).active, "settings"
            )
            await pilot.press("escape")

            app.push_screen(Confirm("Synthetic deletion review", destructive=True))
            await pilot.pause()
            app.screen.query_one("#confirmation", Input).value = "DELETE"
            self.assertFalse(
                app.screen.query("#copy-selection, #copy-view, #paste-copied")
            )
            await pilot.press("ctrl+shift+y")
            await pilot.pause()
            self.assertIn("Synthetic deletion review", app.clipboard)
            self.assertIsInstance(app.screen, Confirm)
            self.assertFalse(backend.calls)
            await pilot.click(app.screen.query_one("#cancel", Button))

    async def test_log_refresh_preserves_highlight_and_unchanged_snapshot(self):
        from textual.widgets import RichLog

        app = DashboardApp(DemoBackend(), read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app._poll_timer.pause()
            app.action_tab("logs")
            await pilot.pause()
            await pilot.click("#log-mode")
            output = app.query_one("#log-output", RichLog)
            self.assertTrue(output.display)
            with (
                patch.object(output, "clear", wraps=output.clear) as clear,
                patch.object(output, "write", wraps=output.write) as write,
            ):
                app._render_logs()
                clear.assert_not_called()
                app.screen.selections = {output: Selection(Offset(0, 0), Offset(5, 0))}
                await pilot.pause()
                selected = app.screen.get_selected_text()
                app._apply_logs(
                    [
                        *app.backend.logs(),
                        LogEntry(
                            "2026-01-01T12:00:02Z",
                            "podman",
                            "info",
                            "New event",
                        ),
                    ]
                )
                await pilot.pause()
                clear.assert_not_called()
                write.assert_not_called()
                self.assertEqual(app.screen.get_selected_text(), selected)
                self.assertEqual(len(app.logs.search("New event")), 1)
                app.screen.clear_selection()
                app._render_logs()
                clear.assert_not_called()
                write.assert_called_once()

    async def test_clipboard_shortcuts_preserve_native_control_keys(self):
        app = DashboardApp(SessionDemo())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.action_tab("terminals")
            await pilot.pause()
            terminal = app._terminal_widget
            terminal.transport.state = "connected"
            terminal.emulator.feed(b"READY")
            terminal.focus()
            app.screen.selections = {terminal: Selection(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()
            with patch.object(terminal.transport, "send", return_value=True) as send:
                await pilot.press("ctrl+shift+c")
                await pilot.pause()
                self.assertEqual(app.clipboard, "READY")
                send.assert_not_called()
                await pilot.press("ctrl+c", "ctrl+v", "ctrl+a")
                self.assertEqual(
                    [call.args[0] for call in send.call_args_list],
                    [b"\x03", b"\x16", b"\x01"],
                )
                app.copy_to_clipboard("copied\ncommand")
                await pilot.press("ctrl+shift+insert")
                await pilot.pause()
                self.assertEqual(send.call_args.args[0], b"copied\rcommand")
                self.assertTrue(terminal.has_focus)


if __name__ == "__main__":
    unittest.main()
