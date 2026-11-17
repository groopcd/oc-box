"""Card layout and explicit-action checks with synthetic dashboard metadata."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box import tui_operations, tui_storage
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_backup_workflow import BackupWorkflow
    from oc_box.tui_clipboard import ClipboardInput, SelectableDataTable
    from textual.widgets import Button, Collapsible, Input, ProgressBar, Select, Static


SIZES = ((80, 24), (100, 32), (140, 44))


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class ManagementCardTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def reveal(self, pilot, widget):
        widget.scroll_visible(animate=False)
        await pilot.pause()
        self.assertGreater(widget.content_region.width, 0)
        self.assertTrue(widget.visible)
        x, y = widget.region.center
        self.assertIs(widget.screen.get_widget_at(x, y)[0], widget)

    async def test_path_first_cards_keep_all_fields_and_actions_reachable(self):
        app = DashboardApp(DemoBackend(), read_only=True)
        async with app.run_test(size=SIZES[0]) as pilot:
            await self.settle(app, pilot)
            app.action_tab("backups")
            await pilot.pause()
            fields = [
                app.query_one("#" + name, ClipboardInput)
                for name in (
                    "backup-file",
                    "backup-destination",
                    "backup-recipients",
                    "backup-identities",
                )
            ]
            self.assertEqual(len(app.query("BackupPanel Input")), 4)
            self.assertTrue(
                app.query_one("BackupPanel Collapsible", Collapsible).collapsed
            )
            fields[0].value = "/synthetic/archive.ocbox"
            fields[1].value = "/synthetic/new-project"
            for size, theme in zip(
                SIZES, ("textual-dark", "textual-light", "textual-dark")
            ):
                with self.subTest(size=size, theme=theme):
                    app.theme = theme
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    for field in fields[:2]:
                        await self.reveal(pilot, field)
                        self.assertGreaterEqual(field.content_region.width, 14)
                    self.assertLess(
                        fields[0].region.y,
                        app.query_one("#backup-scope", Select).region.y,
                    )
                    advanced = app.query_one("BackupPanel Collapsible", Collapsible)
                    advanced.collapsed = False
                    await pilot.pause()
                    for field in fields[2:]:
                        await self.reveal(pilot, field)
                    advanced.collapsed = True
                    await pilot.pause()
                    inspect = app.query_one("#backup-inspect", Button)
                    await self.reveal(pilot, inspect)
                    with patch.object(app, "_review_backup") as review:
                        self.assertTrue(await pilot.click("#backup-inspect"))
                        review.assert_called_once_with("backup-inspect")
                    for name in ("backup-create", "backup-restore", "backup-delete"):
                        button = app.query_one("#" + name, Button)
                        await self.reveal(pilot, button)
                        self.assertTrue(button.disabled)
                    delete = app.query_one("#backup-delete", Button)
                    self.assertEqual(delete.variant, "error")
                    self.assertGreater(delete.styles.margin.left, 0)
            self.assertEqual(fields[0].value, "/synthetic/archive.ocbox")
            self.assertEqual(fields[1].value, "/synthetic/new-project")

    async def test_card_scopes_preserve_one_explicit_target_without_dispatch(self):
        app = DashboardApp(DemoBackend(), demo=True)
        with (
            patch.object(tui_operations, "handoff") as handoff,
            patch("oc_box.tui_operation_io.OperationProcess.start") as start,
            patch.object(app, "suspend") as suspend,
        ):
            async with app.run_test(size=SIZES[0]) as pilot:
                await self.settle(app, pilot)
                app._management_records = tuple(
                    tui_storage.StorageRecord(
                        f"{index:032x}",
                        f"/synthetic/project-{index}",
                        "debian",
                        f"container-{index}",
                        f"home-{index}",
                        "image",
                        "2.2.6",
                        Path("/synthetic/state"),
                        Path(f"/synthetic/state/{index}/workspace.json"),
                    )
                    for index in (1, 2)
                )
                for pane, prefix in (("storage", "storage"), ("backups", "backup")):
                    app.action_tab(pane)
                    for size in SIZES:
                        await pilot.resize_terminal(*size)
                        for scope in ("host", "all", "workspace"):
                            app.query_one(f"#{prefix}-scope", Select).value = scope
                            await pilot.pause()
                            table = app.query_one(
                                f"#{prefix}-table", SelectableDataTable
                            )
                            if scope == "host":
                                self.assertEqual(table.row_count, 0)
                            elif scope == "all":
                                self.assertEqual(table.row_count, 2)
                                table.move_cursor(row=1)
                                self.assertEqual(
                                    app._management_target(prefix), f"{2:032x}"
                                )
                    for button in app.query(f"#{pane} Button"):
                        if button.disabled:
                            button.press()
                    if prefix == "storage":
                        for name in ("storage-session-delete", "storage-purge"):
                            await self.reveal(pilot, app.query_one("#" + name, Button))
                self.assertEqual(app.query_one("#backup-kind", Select).value, "session")
                self.assertFalse(app.query("BackupPanel #copy-selection"))
                handoff.assert_not_called()
                start.assert_not_called()
                suspend.assert_not_called()

    async def test_running_card_is_indeterminate_and_cancel_waits_for_real_result(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=SIZES[0]) as pilot:
            await self.settle(app, pilot)
            for size in SIZES:
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    workflow = BackupWorkflow(None, {"action": "workspace-inspect"})
                    result = []
                    with (
                        patch.object(BackupWorkflow, "_run") as worker,
                        patch.object(workflow.transport, "start") as start,
                        patch.object(app, "suspend") as suspend,
                    ):
                        app.push_screen(workflow, result.append)
                        await pilot.pause()
                        worker.assert_called_once()
                        workflow._apply({"kind": "stage", "stage": "encrypting"})
                        workflow._apply(
                            {
                                "kind": "inspection",
                                "metadata": {
                                    "format": "session",
                                    "source_store": "synthetic-store",
                                },
                            }
                        )
                        await pilot.pause()
                        stage = workflow.query_one("#backup-operation-stage", Static)
                        self.assertIn("Encrypting", str(stage.render()))
                        self.assertGreaterEqual(stage.size.height, 1)
                        self.assertIn(
                            "indeterminate",
                            str(
                                workflow.query_one(
                                    "#backup-operation-elapsed", Static
                                ).render()
                            ),
                        )
                        progress = workflow.query_one(ProgressBar)
                        self.assertIsNone(progress.total)
                        self.assertFalse(progress.show_eta)
                        self.assertFalse(progress.show_percentage)
                        self.assertGreater(progress.region.width, 20)
                        summary = str(
                            workflow.query_one(
                                "#backup-operation-summary", Static
                            ).render()
                        )
                        self.assertIn("synthetic-store", summary)
                        self.assertFalse(workflow.query(Input))
                        cancel = workflow.query_one("#backup-operation-cancel", Button)
                        close = workflow.query_one("#backup-operation-close", Button)
                        self.assertFalse(cancel.disabled)
                        self.assertTrue(close.disabled)
                        self.assertTrue(await pilot.click("#backup-operation-cancel"))
                        self.assertTrue(workflow.transport._cancelled)
                        self.assertFalse(workflow.finished)
                        self.assertTrue(close.disabled)
                        self.assertEqual(result, [])
                        workflow._finished({"code": 0})
                        await pilot.pause()
                        self.assertFalse(close.disabled)
                        self.assertTrue(cancel.disabled)
                        self.assertFalse(progress.display)
                        self.assertTrue(await pilot.click("#backup-operation-close"))
                        self.assertEqual(result, [{"code": 0}])
                        start.assert_not_called()
                        suspend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
