"""Synthetic native-shaped transport and headless mutation notification tests."""

import asyncio
import copy
import importlib.util
import threading
import unittest
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from oc_box.tui import DemoBackend
from oc_box.tui_sessions import SessionCatalog

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui_app import Confirm, DashboardApp
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import Button, Input, Select, Static, Switch


class NativeStore:
    """In-memory transport only: no process, native service, lease or host state."""

    def __init__(self, *, count=1):
        self.records = {}
        for index in range(count):
            record = {
                "id": f"ses_notice_{index}",
                "projectID": "project_synthetic",
                "title": "Synthetic notification fixture",
                "location": {"directory": "/home/demo/projects/example"},
                "time": {"created": 1, "updated": index + 2},
            }
            if index:
                record["parentID"] = "ses_notice_0"
            self.records[record["id"]] = record
        self.calls = []
        self.active, self.jobs, self.terminals = {}, [], []
        self.delete_count = self.patch_count = 0
        self.delete_reply = "success"
        self.refresh_fails = False

    def request(self, method, target, body=None):
        self.calls.append((method, target, body))
        route = urlparse(target).path
        if route == "/api/session/active":
            return {"data": copy.deepcopy(self.active)}
        if route == "/api/experimental/job":
            return {"data": copy.deepcopy(self.jobs)}
        if route.startswith("/api/experimental/session/") and route.endswith(
            "/terminal"
        ):
            return {"data": copy.deepcopy(self.terminals)}
        if route == "/api/session":
            if self.refresh_fails and (self.delete_count or self.patch_count):
                raise TimeoutError("PRIVATE_REFRESH_ERROR synthetic-secret")
            return {
                "data": copy.deepcopy(list(self.records.values())),
                "cursor": {"next": None},
            }
        ident = route.rsplit("/", 1)[-1]
        if method == "GET":
            return {"data": copy.deepcopy(self.records[ident])}
        if method == "PATCH":
            self.patch_count += 1
            if body["archived"]:
                self.records[ident]["time"]["archived"] = 5
            else:
                self.records[ident]["time"].pop("archived", None)
            return None
        if method == "DELETE":
            self.delete_count += 1
            pending = [ident]
            while pending:
                current = pending.pop()
                pending.extend(
                    record["id"]
                    for record in self.records.values()
                    if record.get("parentID") == current
                )
                del self.records[current]
            if self.delete_reply == "lost":
                raise TimeoutError("PRIVATE_DELETE_REPLY synthetic-secret")
            if self.delete_reply == "malformed":
                return {"PRIVATE_ACK": "synthetic-secret"}
            return None
        raise AssertionError("Unexpected synthetic transport operation")


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class DeleteNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    @asynccontextmanager
    async def dashboard(self, store=None, *, read_only=False, demo=False, managed=()):
        store = store or NativeStore()
        catalog = SessionCatalog(
            store.request,
            "host",
            "synthetic-host-store",
            ownership=lambda *args: "process-local",
            managed_ids=managed,
        )
        app = DashboardApp(DemoBackend(), read_only=read_only, demo=demo)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.query_one("#auto-refresh", Switch).value = False
            app.query_one("#log-follow", Switch).value = False
            app.action_tab("sessions")
            app._apply_sessions(catalog, catalog.list())
            await pilot.pause()
            store.calls.clear()
            app._notifications.clear()
            yield app, pilot, catalog, store

    def assert_notice(self, app, title, phrase):
        notifications = list(app._notifications)
        self.assertTrue(notifications)
        notification = notifications[-1]
        self.assertEqual(notification.title, title)
        self.assertIn(phrase, notification.message)
        self.assertEqual(notification.timeout, 8.0)
        persistent = app.query_one("#notice", Static).render().plain
        self.assertIn(notification.title, persistent)
        self.assertIn(notification.message, persistent)
        for text in (notification.title, notification.message, persistent):
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("synthetic-secret", text)
            self.assertNotIn("/home/demo", text)
        return notification

    def assert_no_mutations(self, store):
        self.assertEqual(store.delete_count, 0)
        self.assertEqual(store.patch_count, 0)
        self.assertFalse(any(method != "GET" for method, _, _ in store.calls))

    async def test_native_preflight_refusal_matrix_has_visible_cause_and_never_deletes(
        self,
    ):
        cases = (
            ("managed-child", "managed host archive has native child"),
            ("active", "active session or job"),
            ("terminal", "running persistent shell"),
            ("ownership", "Ownership could not be verified"),
            ("invalid-activity", "activity could not be validated"),
            ("invalid-jobs", "background-job activity"),
            ("invalid-terminals", "persistent-shell status"),
        )
        for case, phrase in cases:
            with self.subTest(case=case):
                store = NativeStore(count=2 if case == "managed-child" else 1)
                managed = ("ses_notice_0",) if case == "managed-child" else ()
                async with self.dashboard(store, managed=managed) as (
                    app,
                    pilot,
                    catalog,
                    store,
                ):
                    if case == "active":
                        store.active = {"ses_notice_0": {"type": "running"}}
                    elif case == "terminal":
                        store.terminals = [
                            {"sessionID": "ses_notice_0", "status": "running"}
                        ]
                    elif case == "ownership":
                        catalog.ownership = None
                    elif case == "invalid-activity":
                        store.active = []
                    elif case == "invalid-jobs":
                        store.jobs = {"PRIVATE": "synthetic-secret"}
                    elif case == "invalid-terminals":
                        store.terminals = {"PRIVATE": "synthetic-secret"}
                    previous = list(app.sessions)
                    with patch.object(app, "_journal") as journal:
                        app.prepare_delete(store.records["ses_notice_0"], catalog)
                        await self.settle(app, pilot)
                        self.assert_notice(app, "Deletion refused", phrase)
                        journal.assert_not_called()
                    self.assert_no_mutations(store)
                    self.assertNotIsInstance(app.screen, Confirm)
                    self.assertEqual(app.sessions, previous)

    async def test_readonly_and_demo_execution_fences_notify_even_for_forced_events(
        self,
    ):
        for flag, phrase in (("read_only", "read-only"), ("demo", "Synthetic demo")):
            with self.subTest(flag=flag):
                async with self.dashboard(**{flag: True}) as (
                    app,
                    pilot,
                    catalog,
                    store,
                ):
                    app.on_button_pressed(
                        Button.Pressed(app.query_one("#delete", Button))
                    )
                    await self.settle(app, pilot)
                    self.assert_notice(app, "Deletion refused", phrase)
                    app.mutate_session(catalog, "ses_notice_0", preview={})
                    await self.settle(app, pilot)
                    self.assert_notice(app, "Deletion refused", phrase)
                    app._archive()
                    await pilot.pause()
                    self.assert_notice(app, "Archive refused", phrase)
                    self.assertEqual(store.calls, [])
                    self.assert_no_mutations(store)

    async def test_selection_and_stale_catalog_guards_notify_without_native_queries(
        self,
    ):
        async with self.dashboard() as (app, pilot, catalog, store):
            app.prepare_delete(None, None)
            await self.settle(app, pilot)
            self.assert_notice(app, "Deletion refused", "Select a session")
            foreign = Mock()
            app.prepare_delete(store.records["ses_notice_0"], foreign)
            await self.settle(app, pilot)
            self.assert_notice(
                app, "Deletion refused", "source changed or was disconnected"
            )
            app.mutate_session(foreign, "ses_notice_0", preview={})
            await self.settle(app, pilot)
            self.assert_notice(
                app, "Deletion refused", "source changed or was disconnected"
            )
            app._confirm_delete(foreign, "ses_notice_0", {})
            await pilot.pause()
            self.assert_notice(
                app, "Deletion refused", "source changed or was disconnected"
            )
            foreign.assert_not_called()
            self.assertEqual(foreign.method_calls, [])
            self.assertEqual(store.calls, [])
            self.assert_no_mutations(store)

    async def test_busy_action_and_pending_inventory_guards_notify_without_extra_requests(
        self,
    ):
        async with self.dashboard() as (app, pilot, catalog, store):
            for busy in ("operation", "inventory"):
                with self.subTest(busy=busy):
                    if busy == "operation":
                        app._operation_lock.acquire()
                    else:
                        app._session_busy = True
                    try:
                        app.prepare_delete(store.records["ses_notice_0"], catalog)
                        await self.settle(app, pilot)
                        self.assert_notice(
                            app, "Deletion refused", "Another action is still finishing"
                        )
                        app.mutate_session(catalog, "ses_notice_0", preview={})
                        await self.settle(app, pilot)
                        self.assert_notice(
                            app, "Deletion refused", "Another action is still finishing"
                        )
                        app._archive()
                        await pilot.pause()
                        self.assert_notice(
                            app, "Archive refused", "Another action is still finishing"
                        )
                        self.assertEqual(store.calls, [])
                    finally:
                        if busy == "operation":
                            app._operation_lock.release()
                        else:
                            app._session_busy = False

    async def test_scope_change_while_preparing_reports_stale_review_without_opening_modal(
        self,
    ):
        async with self.dashboard() as (app, pilot, catalog, store):
            entered, release = threading.Event(), threading.Event()
            original = catalog.prepare_delete

            def delayed(ident):
                entered.set()
                if not release.wait(8):
                    raise AssertionError("Synthetic review gate timed out")
                return original(ident)

            with patch.object(catalog, "prepare_delete", side_effect=delayed):
                try:
                    app.prepare_delete(store.records["ses_notice_0"], catalog)
                    self.assertTrue(
                        await asyncio.wait_for(asyncio.to_thread(entered.wait, 6), 7)
                    )
                    app.query_one("#session-scope", Select).value = "project"
                    await pilot.pause()
                finally:
                    release.set()
                await self.settle(app, pilot)
            self.assert_notice(
                app, "Deletion refused", "source changed or was disconnected"
            )
            self.assertNotIsInstance(app.screen, Confirm)
            self.assert_no_mutations(store)

    async def test_review_limit_and_invalid_preview_have_notifications_not_submissions(
        self,
    ):
        store = NativeStore(count=51)
        async with self.dashboard(store) as (app, pilot, catalog, store):
            app.prepare_delete(store.records["ses_notice_0"], catalog)
            await self.settle(app, pilot)
            self.assert_notice(app, "Deletion refused", "contains 51 sessions")
            self.assertNotIsInstance(app.screen, Confirm)
            self.assert_no_mutations(store)
            store.calls.clear()
            for preview in (None, {}, {"sessions": []}, {"sessions": [{}]}):
                app._confirm_delete(catalog, "ses_notice_0", preview)
                await pilot.pause()
                self.assert_notice(
                    app, "Deletion refused", "preview is missing, expired"
                )
            self.assertEqual(store.calls, [])

    async def test_wrong_typed_confirmation_shows_inline_toast_and_persistent_notice(
        self,
    ):
        async with self.dashboard() as (app, pilot, catalog, store):
            app.prepare_delete(store.records["ses_notice_0"], catalog)
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, Confirm)
            app.screen.query_one("#confirm", Button).active_effect_duration = 0
            app.screen.query_one("#confirmation", Input).value = "wrong"
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertIsInstance(app.screen, Confirm)
            notice = self.assert_notice(app, "Deletion refused", "Type DELETE exactly")
            self.assertEqual(
                app.screen.query_one("#confirmation-error", Static).render().plain,
                notice.message,
            )
            self.assert_no_mutations(store)
            app.screen.query_one("#confirmation", Input).value = "DELETE"
            await pilot.click("#confirm")
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, Confirm)
            self.assertEqual(store.delete_count, 1)
            self.assertEqual(app.sessions, [])

    async def test_cancel_confirmation_is_not_a_failure_and_does_not_dispatch(self):
        async with self.dashboard() as (app, pilot, catalog, store):
            app.prepare_delete(store.records["ses_notice_0"], catalog)
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, Confirm)
            with patch.object(app, "notify", wraps=app.notify) as notify:
                await pilot.click("#cancel")
                await self.settle(app, pilot)
                notify.assert_not_called()
            self.assert_no_mutations(store)

    async def test_lost_or_malformed_delete_acknowledgement_is_outcome_unverified(self):
        for reply in ("lost", "malformed"):
            with self.subTest(reply=reply):
                store = NativeStore()
                async with self.dashboard(store) as (app, pilot, catalog, store):
                    preview = catalog.prepare_delete("ses_notice_0")
                    store.delete_reply = reply
                    previous = list(app.sessions)
                    with patch.object(app, "_journal") as journal:
                        app.mutate_session(catalog, "ses_notice_0", preview=preview)
                        await self.settle(app, pilot)
                        journal.assert_not_called()
                    notice = self.assert_notice(
                        app, "Deletion outcome unverified", "before any retry"
                    )
                    self.assertEqual(notice.severity, "error")
                    self.assertEqual(store.delete_count, 1)
                    self.assertFalse(store.records)
                    self.assertEqual(app.sessions, previous)
                    self.assertNotIn("nothing changed", notice.message.lower())
                    self.assertEqual(
                        sum(method == "DELETE" for method, _, _ in store.calls), 1
                    )

    async def test_delete_completed_then_refresh_failed_has_completion_warning_and_success_journal(
        self,
    ):
        store = NativeStore()
        async with self.dashboard(store) as (app, pilot, catalog, store):
            preview = catalog.prepare_delete("ses_notice_0")
            previous = list(app.sessions)
            store.refresh_fails = True
            with patch.object(app, "_journal") as journal:
                app.mutate_session(catalog, "ses_notice_0", preview=preview)
                await self.settle(app, pilot)
                journal.assert_called_once_with("session.deleted", catalog)
            notice = self.assert_notice(
                app,
                "Deletion completed; refresh unavailable",
                "do not repeat the completed action",
            )
            self.assertEqual(notice.severity, "warning")
            self.assertEqual(store.delete_count, 1)
            self.assertFalse(store.records)
            self.assertEqual(app.sessions, previous)
            self.assertEqual(sum(method == "DELETE" for method, _, _ in store.calls), 1)

    async def test_archive_and_restore_use_their_own_context_for_refusal_and_refresh_failure(
        self,
    ):
        for archived, title, event in (
            (True, "Archive", "session.archived"),
            (False, "Restore", "session.unarchived"),
        ):
            with self.subTest(archived=archived):
                store = NativeStore()
                if not archived:
                    store.records["ses_notice_0"]["time"]["archived"] = 0
                async with self.dashboard(store) as (app, pilot, catalog, store):
                    store.active = {"ses_notice_0": {"type": "running"}}
                    app.mutate_session(catalog, "ses_notice_0", archived=archived)
                    await self.settle(app, pilot)
                    self.assert_notice(app, title + " refused", "active session or job")
                    self.assert_no_mutations(store)
                    store.active = {}
                    store.refresh_fails = True
                    with patch.object(app, "_journal") as journal:
                        app.mutate_session(catalog, "ses_notice_0", archived=archived)
                        await self.settle(app, pilot)
                        journal.assert_called_once_with(event, catalog)
                    self.assert_notice(
                        app,
                        title + " completed; refresh unavailable",
                        "Displayed rows may be stale",
                    )
                    self.assertEqual(store.patch_count, 1)
                    self.assertEqual(store.delete_count, 0)

    async def test_unknown_postdispatch_error_stays_private_and_persistent_text_is_selectable(
        self,
    ):
        class SecretError(Exception):
            def __str__(self):
                raise AssertionError("Must not render PRIVATE exception text")

        async with self.dashboard() as (app, pilot, catalog, store):
            app._operation_failure(
                SecretError("synthetic-secret"),
                operation="backup-file-delete",
                reason="path-required",
            )
            await pilot.pause()
            self.assert_notice(
                app, "Backup file deletion refused", "required file or destination path"
            )
            with patch.object(
                catalog, "delete", side_effect=SecretError("synthetic-secret")
            ):
                app.mutate_session(catalog, "ses_notice_0", preview={})
                await self.settle(app, pilot)
            self.assert_notice(
                app, "Deletion outcome unverified", "Inspect and refresh"
            )
            persistent = app.query_one("#notice", Static)
            app.screen.selections = {persistent: Selection(Offset(0, 0), Offset(8, 0))}
            await pilot.pause()
            self.assertEqual(app.screen.get_selected_text(), "Deletion")
            self.assert_no_mutations(store)

    async def test_completed_mutation_callback_for_replaced_source_never_replaces_rows(
        self,
    ):
        async with self.dashboard() as (app, pilot, catalog, store):
            previous = list(app.sessions)
            app._apply_mutation_result(Mock(), [], app._catalog_generation, "delete")
            await pilot.pause()
            self.assert_notice(
                app,
                "Deletion completed; refresh unavailable",
                "Displayed rows may be stale",
            )
            self.assertEqual(app.sessions, previous)
            self.assertEqual(store.calls, [])


if __name__ == "__main__":
    unittest.main()
