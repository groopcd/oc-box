"""Dashboard host backup form behavior, without dispatching archive operations."""

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box import tui_operations
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_management import ActionReview, BackupPanel
    from oc_box.tui_settings import RoundedSwitch
    from textual.widgets import Button, Input, Select, Static


WORKSPACE_COMPONENTS = ("project.age", "home.age", "registry.age")


def envelope(path, *components):
    with tarfile.open(path, "w") as archive:
        for name in ("key.age", "manifest.age", *components):
            entry = tarfile.TarInfo(name)
            entry.size = 9
            archive.addfile(entry, io.BytesIO(b"synthetic"))
    return str(path)


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class HostBackupSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def open_backups(self, app, pilot):
        app.action_tab("backups")
        await self.settle(app, pilot)
        return app.query_one(BackupPanel)

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    def controller(self, app):
        controller = Mock()
        controller.prepare.return_value = SimpleNamespace(
            review="Reviewed synthetic options", confirmation=""
        )
        app._management_controller = controller
        return controller

    async def dismiss_review(self, app, pilot):
        self.assertIsInstance(app.screen, ActionReview)
        app.screen.query_one("#operation-cancel", Button).press()
        await pilot.pause()

    async def test_switch_defaults_dependencies_and_mutation_fences(self):
        for read_only, demo in ((False, False), (True, False), (False, True)):
            with self.subTest(read_only=read_only, demo=demo):
                app = DashboardApp(DemoBackend(), read_only=read_only, demo=demo)
                async with app.run_test(size=(80, 24)) as pilot:
                    panel = await self.open_backups(app, pilot)
                    switches = {item.id: item for item in panel.query(RoundedSwitch)}
                    self.assertEqual(
                        {
                            name: switch.render().plain
                            for name, switch in switches.items()
                        },
                        {
                            "backup-include-workspace": "ON  ●",
                            "backup-portable": "● OFF",
                            "backup-include-host": "● OFF",
                            "backup-include-auth-export": "● OFF",
                            "backup-restore-workspace": "ON  ●",
                            "backup-restore-host": "● OFF",
                            "backup-restore-opencode": "● OFF",
                        },
                    )
                    self.assertEqual(
                        panel.query_one("#backup-inspect", Button).disabled, demo
                    )
                    self.assertFalse(
                        panel.query_one("#backup-auth-export-field").display
                    )
                    if read_only or demo:
                        self.assertTrue(
                            all(window.disabled for window in switches.values())
                        )
                        continue
                    export = panel.switch("include-auth-export")
                    native = panel.switch("restore-opencode")
                    self.assertTrue(export.disabled)
                    self.assertTrue(native.disabled)
                    with patch.object(tui_operations, "archive_kind") as detect:
                        panel.query_one(
                            "#backup-file", Input
                        ).value = "/synthetic/archive"
                        panel.switch("include-host").value = True
                        panel.switch("restore-host").value = True
                        await pilot.pause()
                        self.assertFalse(export.disabled)
                        self.assertFalse(native.disabled)
                        export.value = native.value = True
                        await pilot.pause()
                        self.assertTrue(
                            panel.query_one("#backup-auth-export-field").display
                        )
                        panel.switch("include-host").value = False
                        panel.switch("restore-host").value = False
                        await pilot.pause()
                        self.assertTrue(export.disabled)
                        self.assertTrue(native.disabled)
                        self.assertFalse(export.value or native.value)
                        self.assertFalse(
                            panel.query_one("#backup-auth-export-field").display
                        )
                        detect.assert_not_called()

    async def test_host_only_and_combined_create_dispatch_distinct_options(self):
        app = DashboardApp(DemoBackend())
        async with app.run_test(size=(110, 40)) as pilot:
            panel = await self.open_backups(app, pilot)
            controller = self.controller(app)
            panel.query_one("#backup-file", Input).value = "/synthetic/output.ocbox"
            panel.switch("include-workspace").value = False
            panel.switch("include-host").value = True
            await pilot.pause()
            self.assertTrue(panel.switch("portable").disabled)
            with patch.object(app, "_management_target") as target:
                app._review_backup("backup-create")
                await pilot.pause()
                target.assert_not_called()
                self.assertEqual(
                    controller.prepare.call_args.args, ("workspace-backup",)
                )
                kwargs = controller.prepare.call_args.kwargs
                self.assertIsNone(kwargs["workspace"])
                self.assertTrue(kwargs["host"])
                self.assertFalse(kwargs["portable"])
                self.assertIsNone(kwargs["auth_export"])
                await self.dismiss_review(app, pilot)
            panel.switch("include-workspace").value = True
            panel.switch("portable").value = True
            panel.switch("include-auth-export").value = True
            await pilot.pause()
            controller.prepare.reset_mock()
            with patch.object(app, "_management_target", return_value="a" * 32):
                app._review_backup("backup-create")
                controller.prepare.assert_not_called()  # explicitly enabled, path missing
                panel.query_one(
                    "#backup-auth-export-path", Input
                ).value = "/synthetic/auth.json"
                app._review_backup("backup-create")
                await pilot.pause()
                kwargs = controller.prepare.call_args.kwargs
                self.assertEqual(kwargs["workspace"], "a" * 32)
                self.assertTrue(kwargs["host"])
                self.assertTrue(kwargs["portable"])
                self.assertEqual(kwargs["auth_export"], "/synthetic/auth.json")
                await self.dismiss_review(app, pilot)
                panel.switch("include-auth-export").value = False
                app._review_backup("backup-create")
                await pilot.pause()
                self.assertIsNone(controller.prepare.call_args.kwargs["auth_export"])
                await self.dismiss_review(app, pilot)

    async def test_host_only_restore_requires_explicit_host_and_ignores_destination(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-switch-") as directory:
            path = envelope(Path(directory) / "host.ocbox", "host.age", "host-auth.age")
            app = DashboardApp(DemoBackend())
            async with app.run_test(size=(110, 40)) as pilot:
                panel = await self.open_backups(app, pilot)
                controller = self.controller(app)
                panel.query_one("#backup-file", Input).value = path
                panel.query_one(
                    "#backup-destination", Input
                ).value = "/synthetic/unused"
                app._review_backup("backup-restore")
                await self.settle(app, pilot)
                controller.prepare.assert_not_called()
                self.assertTrue(panel.switch("restore-workspace").disabled)
                self.assertFalse(panel.switch("restore-workspace").value)
                self.assertFalse(panel.switch("restore-host").value)
                self.assertIn(
                    "Unverified archive layout: Host settings only",
                    str(panel.query_one("#backup-archive-layout", Static).render()),
                )
                panel.switch("restore-host").value = True
                await pilot.pause()
                self.assertFalse(panel.switch("restore-opencode").disabled)
                panel.switch("restore-opencode").value = True
                app._review_backup("backup-restore")
                await self.settle(app, pilot)
                self.assertEqual(
                    controller.prepare.call_args.args, ("workspace-restore",)
                )
                kwargs = controller.prepare.call_args.kwargs
                self.assertTrue(kwargs["host"])
                self.assertTrue(kwargs["include_opencode"])
                self.assertIsNone(kwargs["workspace"])
                self.assertIsNone(kwargs["destination"])
                await self.dismiss_review(app, pilot)
                panel.query_one("#backup-file", Input).value = "/synthetic/other"
                await pilot.pause()
                self.assertTrue(panel.switch("restore-workspace").value)
                self.assertFalse(panel.switch("restore-workspace").disabled)
                self.assertFalse(panel.switch("restore-host").value)
                self.assertFalse(panel.switch("restore-opencode").value)

    async def test_combined_restore_can_target_workspace_host_or_both(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-switch-") as directory:
            root = Path(directory)
            path = envelope(root / "both.ocbox", *WORKSPACE_COMPONENTS, "host.age")
            destination = str(root / "new-project")
            app = DashboardApp(DemoBackend())
            async with app.run_test(size=(110, 40)) as pilot:
                panel = await self.open_backups(app, pilot)
                controller = self.controller(app)
                panel.query_one("#backup-file", Input).value = path
                panel.query_one("#backup-destination", Input).value = destination
                for workspace, host in ((True, False), (True, True), (False, True)):
                    with self.subTest(workspace=workspace, host=host):
                        panel.switch("restore-workspace").value = workspace
                        panel.switch("restore-host").value = host
                        app._review_backup("backup-restore")
                        await self.settle(app, pilot)
                        self.assertEqual(
                            controller.prepare.call_args.args, ("workspace-restore",)
                        )
                        kwargs = controller.prepare.call_args.kwargs
                        self.assertEqual(kwargs["host"], host)
                        self.assertFalse(kwargs["include_opencode"])
                        self.assertEqual(
                            kwargs["destination"], destination if workspace else None
                        )
                        await self.dismiss_review(app, pilot)

    async def test_workspace_archive_does_not_silently_discard_host_restore_request(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-switch-") as directory:
            path = envelope(Path(directory) / "workspace.ocbox", *WORKSPACE_COMPONENTS)
            app = DashboardApp(DemoBackend())
            async with app.run_test(size=(110, 40)) as pilot:
                panel = await self.open_backups(app, pilot)
                controller = self.controller(app)
                panel.query_one("#backup-file", Input).value = path
                panel.switch("restore-host").value = True
                app._review_backup("backup-restore")
                await self.settle(app, pilot)
                controller.prepare.assert_not_called()
                self.assertTrue(panel.switch("restore-host").disabled)
                self.assertFalse(panel.switch("restore-host").value)
                app._review_backup("backup-restore")
                await self.settle(app, pilot)
                self.assertTrue(
                    controller.prepare.call_args.kwargs["destination"].endswith(
                        "-restored"
                    )
                )
                self.assertFalse(controller.prepare.call_args.kwargs["host"])
                await self.dismiss_review(app, pilot)

    async def test_session_type_and_layout_never_route_host_settings(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-switch-") as directory:
            path = envelope(Path(directory) / "misleading.ocbox", "sessions.age")
            app = DashboardApp(DemoBackend())
            async with app.run_test(size=(110, 40)) as pilot:
                panel = await self.open_backups(app, pilot)
                controller = self.controller(app)
                panel.switch("include-host").value = True
                panel.switch("include-auth-export").value = True
                panel.query_one(
                    "#backup-auth-export-path", Input
                ).value = "/synthetic/auth.json"
                panel.query_one("#backup-scope", Select).value = "host"
                await pilot.pause()
                self.assertEqual(
                    panel.query_one("#backup-kind", Select).value, "session"
                )
                self.assertTrue(panel.switch("include-host").disabled)
                self.assertFalse(panel.switch("include-host").value)
                session = {"id": "sesSynthetic", "location": {"directory": directory}}
                panel.query_one("#backup-file", Input).value = path
                panel.query_one("#backup-destination", Input).value = directory
                with (
                    patch.object(
                        app,
                        "_management_session",
                        return_value=("host", None, "host-store"),
                    ),
                    patch.object(app, "_session", return_value=session),
                ):
                    app._review_backup("backup-create")
                    await pilot.pause()
                    self.assertEqual(
                        controller.prepare.call_args.args, ("session-backup",)
                    )
                    self.assertFalse(controller.prepare.call_args.kwargs["host"])
                    self.assertIsNone(
                        controller.prepare.call_args.kwargs["auth_export"]
                    )
                    await self.dismiss_review(app, pilot)
                    app._review_backup("backup-restore")
                    await self.settle(app, pilot)
                    self.assertEqual(
                        controller.prepare.call_args.args, ("session-restore",)
                    )
                    self.assertFalse(controller.prepare.call_args.kwargs["host"])
                    self.assertEqual(
                        controller.prepare.call_args.kwargs["destination"], directory
                    )
                    self.assertTrue(panel.switch("restore-workspace").disabled)
                    self.assertTrue(panel.switch("restore-host").disabled)
                    await self.dismiss_review(app, pilot)

    async def test_pending_detection_refuses_changed_restore_inputs(self):
        app = DashboardApp(DemoBackend())
        async with app.run_test(size=(110, 40)) as pilot:
            panel = await self.open_backups(app, pilot)
            controller = self.controller(app)
            panel.query_one("#backup-file", Input).value = "/synthetic/both.ocbox"
            options = (
                (panel.switch("restore-workspace"), False),
                (panel.switch("restore-host"), True),
                (panel.query_one("#backup-destination", Input), "/synthetic/new"),
                (panel.query_one("#backup-identities", Input), "/synthetic/other-key"),
                (panel.query_one("#backup-scope", Select), "all"),
            )
            for widget, changed in options:
                with self.subTest(option=widget.id):
                    previous = widget.value
                    with (
                        patch.object(app, "_detect_backup") as detect,
                        patch.object(app, "_operation_failure") as failure,
                        patch.object(tui_operations, "file_identity") as identify,
                    ):
                        app._review_backup("backup-restore")
                        widget.value = changed
                        await pilot.pause()
                        app._backup_detected(
                            *detect.call_args.args, [1, 2, 3], "workspace-host"
                        )
                        error = failure.call_args.args[0]
                        self.assertEqual(error.reason, "selection-changed")
                        controller.prepare.assert_not_called()
                        identify.assert_not_called()
                    widget.value = previous
                    await pilot.pause()

    async def test_readonly_host_envelope_can_be_inspected_without_activation(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-host-switch-") as directory:
            path = envelope(Path(directory) / "host.ocbox", "host.age")
            app = DashboardApp(DemoBackend(), read_only=True)
            async with app.run_test(size=(110, 40)) as pilot:
                panel = await self.open_backups(app, pilot)
                controller = self.controller(app)
                panel.query_one("#backup-file", Input).value = path
                with patch.object(app, "_perform_management") as perform:
                    app._review_backup("backup-inspect")
                    await self.settle(app, pilot)
                    controller.prepare.assert_called_once()
                    self.assertEqual(
                        controller.prepare.call_args.args, ("workspace-inspect",)
                    )
                    self.assertFalse(controller.prepare.call_args.kwargs["host"])
                    self.assertFalse(
                        controller.prepare.call_args.kwargs["include_opencode"]
                    )
                    await self.dismiss_review(app, pilot)
                    controller.cancel.assert_called_once_with(
                        controller.prepare.return_value
                    )
                    perform.assert_not_called()
