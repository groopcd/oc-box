import contextlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box.tui import DemoBackend
from oc_box import tui_operations, tui_storage

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box import tui_management
    from oc_box.tui_clipboard import ClipboardActions
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_management import ActionReview
    from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent


class MetadataDemo(DemoBackend):
    def __init__(self):
        self.connections = []

    def session_connection(self, workspace=None):
        self.connections.append(workspace)
        raise AssertionError("implicit native connection")


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class ManagementInteractionTests(unittest.IsolatedAsyncioTestCase):
    def test_idle_selection_adapter_ignores_removed_screen_during_teardown(self):
        for running, mounted, screens in (
            (False, True, [object()]),
            (True, False, [object()]),
            (True, True, []),
        ):
            target = SimpleNamespace(
                is_running=running,
                is_mounted=mounted,
                screen_stack=screens,
                enable_text_selection=Mock(),
            )
            with self.subTest(running=running, mounted=mounted, screens=bool(screens)):
                ClipboardActions.on_idle(target)
                target.enable_text_selection.assert_not_called()

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_seven_tabs_keep_existing_shortcuts_and_search_visible(self):
        backend = MetadataDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            for key, target in enumerate(
                (
                    "status",
                    "settings",
                    "sessions",
                    "terminals",
                    "logs",
                    "storage",
                    "backups",
                ),
                1,
            ):
                app.set_focus(None)
                await pilot.press(str(key))
                self.assertEqual(
                    app.query_one("#content", TabbedContent).active, target
                )
                self.assertTrue(app.query_one("#log-search", Input).display)
                self.assertGreater(app.query_one("#log-search", Input).region.width, 0)
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            self.assertTrue(app.query_one("#log-search", Input).display)
            self.assertGreater(app.query_one("#log-search", Input).region.width, 0)
        self.assertEqual(backend.connections, [])

    async def test_scope_and_backup_kind_changes_never_connect_or_execute(self):
        backend = MetadataDemo()
        app = DashboardApp(backend, demo=False)
        replacement = patch.object(tui_operations, "handoff")
        handoff = replacement.start()
        self.addCleanup(replacement.stop)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            for prefix in ("storage", "backup"):
                for value in ("host", "all", "workspace"):
                    app.query_one(f"#{prefix}-scope", Select).value = value
                    await pilot.pause()
            for value in ("session", "portable", "full"):
                app.query_one("#backup-kind", Select).value = value
                await pilot.pause()
            handoff.assert_not_called()
        self.assertEqual(backend.connections, [])

    async def test_read_only_and_demo_disable_destructive_controls(self):
        for readonly, demo in ((True, False), (False, True)):
            with self.subTest(readonly=readonly, demo=demo):
                app = DashboardApp(MetadataDemo(), read_only=readonly, demo=demo)
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.settle(app, pilot)
                    for ident in (
                        "storage-session-delete",
                        "storage-purge",
                        "backup-create",
                        "backup-restore",
                        "backup-delete",
                    ):
                        self.assertTrue(app.query_one("#" + ident, Button).disabled)
                    self.assertEqual(
                        app.query_one("#backup-inspect", Button).disabled, demo
                    )

    async def test_host_scope_explains_session_only_storage_boundary(self):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.query_one("#storage-scope", Select).value = "host"
            await pilot.pause()
            self.assertIn(
                "No whole-host database wipe",
                str(app.query_one("#storage-details", Static).render()),
            )
            self.assertTrue(app.query_one("#storage-purge", Button).disabled)
            self.assertTrue(app.query_one("#storage-scan", Button).disabled)

    async def test_all_workspace_inventory_does_not_authorize_bulk_removal(self):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            ident = app.selected
            row = tui_storage.StorageRecord(
                ident,
                "/synthetic/project",
                "debian",
                "synthetic-container",
                "synthetic-home",
                "synthetic-image",
                "2.2.6",
                Path("/synthetic/state"),
                Path("/synthetic/state/workspace.json"),
            )
            app._management_records = (row,)
            app.query_one("#storage-scope", Select).value = "all"
            await pilot.pause()
            self.assertEqual(app.query_one("#storage-table", DataTable).row_count, 1)
            self.assertIn(
                "not an unreviewed bulk-delete",
                str(app.query_one("#storage-details", Static).render()),
            )
            self.assertEqual(app._management_target("storage"), ident)

    async def test_management_refresh_retains_rows_and_cursor_when_metadata_unchanged(
        self,
    ):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            rows = tuple(
                tui_storage.StorageRecord(
                    f"{n:032x}",
                    f"/synthetic/project-{n}",
                    "debian",
                    f"container-{n}",
                    f"home-{n}",
                    "image",
                    "2.2.6",
                    Path("/synthetic/state"),
                    Path(f"/synthetic/state/{n}/workspace.json"),
                )
                for n in range(30)
            )
            app._management_records = rows
            app.query_one("#storage-scope", Select).value = "all"
            await pilot.pause()
            table = app.query_one("#storage-table", DataTable)
            table.move_cursor(row=20)
            before = table.cursor_row
            with patch.object(table, "clear", wraps=table.clear) as clear:
                app._render_management()
            clear.assert_not_called()
            self.assertEqual(table.cursor_row, before)

    async def test_review_wrong_text_and_cancel_never_dispatch(self):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            result = []
            app.push_screen(
                ActionReview("SYNTHETIC removal review", "DELETE WORKSPACE synthetic"),
                result.append,
            )
            await pilot.pause()
            app.screen.query_one("#operation-confirmation", Input).value = "wrong"
            await pilot.click("#operation-confirm")
            self.assertEqual(result, [])
            self.assertIsInstance(app.screen, ActionReview)
            await pilot.click("#operation-cancel")
            self.assertEqual(result, [None])

    async def test_review_requires_exact_operation_specific_phrase(self):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            result = []
            app.push_screen(
                ActionReview("SYNTHETIC restore review", "RESTORE"), result.append
            )
            await pilot.pause()
            app.screen.query_one("#operation-confirmation", Input).value = "RESTORE"
            await pilot.click("#operation-confirm")
            self.assertEqual(result, ["RESTORE"])

    async def test_review_clipboard_buttons_bubble_without_confirming_an_action(self):
        app = DashboardApp(MetadataDemo(), demo=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            result = []
            app.push_screen(
                ActionReview("SYNTHETIC management clipboard review", "CONFIRM"),
                result.append,
            )
            await pilot.pause()
            app.copy_to_clipboard("CONFIRM")
            field = app.screen.query_one("#operation-confirmation", Input)
            field.focus()
            await pilot.pause()
            await pilot.click("#paste-copied")
            self.assertEqual(field.value, "CONFIRM")
            self.assertEqual(result, [])
            await pilot.click("#copy-view")
            self.assertIn("SYNTHETIC management clipboard review", app.clipboard)
            self.assertEqual(result, [])
            await pilot.click("#operation-cancel")
            self.assertEqual(result, [None])

    async def test_child_failure_has_specific_visible_notification(self):
        from oc_box.tui_notifications import failure_notice

        app = DashboardApp(MetadataDemo())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            notice = failure_notice("workspace-purge", reason="owner-active")

            def child(runtime, request, *, on_result):
                on_result(notice)
                return 1

            terminal = SimpleNamespace(isatty=lambda: True)
            with (
                patch.object(
                    tui_management,
                    "sys",
                    SimpleNamespace(stdin=terminal, stdout=terminal, stderr=terminal),
                ),
                patch.object(app, "suspend", return_value=contextlib.nullcontext()),
                patch("builtins.input", return_value=""),
                patch.object(tui_operations, "handoff", side_effect=child),
                patch.object(app, "notify", wraps=app.notify) as notify,
            ):
                app._perform_management({"action": "workspace-purge"})
                notify.assert_called_once_with(
                    notice.message,
                    title=notice.title,
                    severity=notice.severity,
                    timeout=notice.timeout,
                    markup=False,
                )
            self.assertIn(
                "active owner", str(app.query_one("#notice", Static).render())
            )

    async def test_missing_child_result_is_explicitly_unverified(self):
        app = DashboardApp(MetadataDemo())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            terminal = SimpleNamespace(isatty=lambda: True)
            with (
                patch.object(
                    tui_management,
                    "sys",
                    SimpleNamespace(stdin=terminal, stdout=terminal, stderr=terminal),
                ),
                patch.object(app, "suspend", return_value=contextlib.nullcontext()),
                patch("builtins.input", return_value=""),
                patch.object(tui_operations, "handoff", return_value=1),
            ):
                app._perform_management({"action": "workspace-purge"})
            self.assertIn(
                "outcome unverified", str(app.query_one("#notice", Static).render())
            )

    async def test_completed_operation_refresh_failure_keeps_success_distinct(self):
        app = DashboardApp(MetadataDemo())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app._management_last_completed = "workspace-purge"
            app._management_failed()
            self.assertIn(
                "completed; refresh unavailable",
                str(app.query_one("#notice", Static).render()),
            )
            self.assertIsNone(app._management_last_completed)

    async def test_execution_time_read_only_and_pending_connection_fences(self):
        app = DashboardApp(MetadataDemo(), read_only=True)
        replacement = patch.object(tui_operations, "handoff")
        handoff = replacement.start()
        self.addCleanup(replacement.stop)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app._perform_management({"action": "workspace-backup"})
            handoff.assert_not_called()
            app.read_only = False
            app._session_busy = True
            app._perform_management({"action": "workspace-backup"})
            handoff.assert_not_called()
            self.assertIn(
                "still finishing", str(app.query_one("#notice", Static).render())
            )
            app._session_busy = False

    async def test_foreground_handoff_has_no_dashboard_output_capture(self):
        app = DashboardApp(MetadataDemo())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            request = {"action": "workspace-inspect", "synthetic": True}
            with contextlib.ExitStack() as stack:
                terminal = SimpleNamespace(isatty=lambda: True)
                stack.enter_context(
                    patch.object(
                        tui_management,
                        "sys",
                        SimpleNamespace(
                            stdin=terminal, stdout=terminal, stderr=terminal
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(app, "suspend", return_value=contextlib.nullcontext())
                )
                stack.enter_context(patch("builtins.input", return_value=""))
                handoff = stack.enter_context(
                    patch.object(tui_operations, "handoff", return_value=0)
                )
                app._perform_management(request)
                self.assertEqual(handoff.call_args.args, (app.backend.box, request))
                self.assertTrue(callable(handoff.call_args.kwargs["on_result"]))
            self.assertFalse(app._maintenance_busy)


if __name__ == "__main__":
    unittest.main()
