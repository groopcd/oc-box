"""Private editor and embedded workflow presentation, without lifecycle I/O."""

import importlib.util
import json
import os
import signal
from pathlib import Path
import tempfile
import tarfile
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box import tui_management, tui_operations
    from oc_box import tui_backup_workflow as workflow_ui
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_backup_workflow import (
        BackupWorkflow,
        MetadataReview,
        PrivateSecretScreen,
        SecretEditor,
    )
    from textual import events
    from textual.widgets import Button, Collapsible, Input, Select, Static

VALUE = "synthetic-Private-0987"


@unittest.skipUnless(AVAILABLE, "optional UI dependencies unavailable")
class PrivateEditorTests(unittest.IsolatedAsyncioTestCase):
    def test_textual_event_logging_or_devtools_refuses_private_input(self):
        for log, debug, devtools in (
            ("configured", False, None),
            (None, True, None),
            (None, False, object()),
        ):
            with self.subTest(log=bool(log), debug=debug, devtools=bool(devtools)):
                with patch.object(
                    workflow_ui, "constants", SimpleNamespace(LOG_FILE=log, DEBUG=debug)
                ):
                    with self.assertRaises(Exception) as caught:
                        workflow_ui.private_ui_allowed(
                            SimpleNamespace(devtools=devtools)
                        )
                self.assertNotIn(VALUE, str(caught.exception))

    async def begin(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_password_has_no_input_value_message_or_visible_selection_and_no_global_keys(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        transport = SimpleNamespace(submit_secret=Mock(), cancel=Mock())
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app.copy_to_clipboard("EXISTING_NONSECRET_CACHE")
            app._private_interaction = True
            screen = PrivateSecretScreen(transport, {"id": 1, "purpose": "backup-new"})
            app.push_screen(screen)
            await pilot.pause()
            editor = screen.query_one(SecretEditor)
            self.assertFalse(isinstance(editor, Input))
            self.assertTrue(screen.PRIVATE_INPUT and editor.PRIVATE_INPUT)
            self.assertFalse(hasattr(editor, "value"))
            for action in (
                "quit",
                "settings",
                "copy_selection",
                "copy_view",
                "paste_copied_text",
                "command_palette",
                "tab",
            ):
                self.assertFalse(app.check_action(action, ()))
            await pilot.press(*VALUE)
            self.assertEqual("".join(editor._private_editor.chars), VALUE)
            self.assertNotIn(VALUE, editor.render().plain)
            self.assertEqual(editor.get_selection(None), ("", "\n"))
            self.assertEqual(app.clipboard, "EXISTING_NONSECRET_CACHE")
            screen.submit()
            screen.submit()
            self.assertEqual(transport.submit_secret.call_count, 1)
            self.assertEqual(editor._private_editor.chars, [])
            # The argument is the same promptly cleared bytearray, not a copied
            # Textual message/value or retained worker request.
            self.assertEqual(transport.submit_secret.call_args.args[1], bytearray())
            self.assertTrue(app._private_interaction)
            screen.dismiss()
            app._private_interaction = False

    async def test_paste_bound_control_rejection_and_local_clear_never_clear_other_fields(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        transport = SimpleNamespace(submit_secret=Mock(), cancel=Mock())
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app.query_one("#log-search", Input).value = "KEEP_SEARCH"
            app.query_one("#backup-file", Input).value = "/synthetic/KEEP_PATH"
            app._private_interaction = True
            screen = PrivateSecretScreen(
                transport, {"id": 1, "purpose": "backup-unlock"}
            )
            app.push_screen(screen)
            await pilot.pause()
            editor = screen.query_one(SecretEditor)
            editor.post_message(events.Paste("first\nsecond"))
            await pilot.pause()
            self.assertEqual(editor._private_editor.chars, [])
            screen.submit()
            transport.submit_secret.assert_not_called()
            await pilot.press("ctrl+u")
            editor.post_message(events.Paste("猫" * 400))
            await pilot.pause()
            self.assertEqual(editor._private_editor.chars, [])
            await pilot.press("ctrl+u")
            editor.post_message(events.Paste(VALUE))
            await pilot.pause()
            screen.cancel()
            self.assertEqual(editor._private_editor.chars, [])
            self.assertEqual(app.query_one("#log-search", Input).value, "KEEP_SEARCH")
            self.assertEqual(
                app.query_one("#backup-file", Input).value, "/synthetic/KEEP_PATH"
            )
            self.assertTrue(app._private_interaction)
            screen.dismiss()
            app._private_interaction = False

    async def test_confirmation_starts_empty_unfocused_and_post_submit_input_is_discarded(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        transport = SimpleNamespace(submit_secret=Mock(), cancel=Mock())
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app._private_interaction = True
            screen = PrivateSecretScreen(
                transport, {"id": 2, "purpose": "backup-confirm"}
            )
            app.push_screen(screen)
            await pilot.pause()
            editor = screen.query_one(SecretEditor)
            self.assertFalse(editor.has_focus)
            await pilot.press(*VALUE, "enter", "enter")
            editor.post_message(events.Paste(VALUE))
            await pilot.pause()
            self.assertEqual(editor._private_editor.chars, [])
            transport.submit_secret.assert_not_called()
            editor.focus()
            await pilot.press(*VALUE)
            screen.submit()
            await pilot.press(*VALUE, "enter")
            editor.post_message(events.Paste(VALUE))
            await pilot.pause()
            self.assertEqual(editor._private_editor.chars, [])
            transport.submit_secret.assert_called_once()
            screen.dismiss()
            app._private_interaction = False

    async def test_repeated_password_enter_cannot_approve_next_decrypted_review(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app._private_interaction = True
            result = []
            review = MetadataReview({"format": "session", "session_count": 1})
            app.push_screen(review, result.append)
            await pilot.pause()
            review.query_one("#metadata-approve", Button).focus()
            await pilot.press("enter", "enter")
            self.assertEqual(result, [])
            await pilot.press("space")
            self.assertEqual(result, [True])
            app._private_interaction = False

    async def test_native_review_requires_exact_phrase_and_explicit_approval(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app._private_interaction = True
            result = []
            review = MetadataReview(
                {
                    "format": "host",
                    "kind": "host",
                    "restore_host": True,
                    "restore_opencode": True,
                }
            )
            app.push_screen(review, result.append)
            await pilot.pause()
            approve = review.query_one("#metadata-approve", Button)
            phrase = review.query_one("#metadata-opencode-confirmation", Input)
            self.assertTrue(approve.disabled)
            self.assertEqual(phrase.value, "")
            phrase.focus()
            await pilot.press("enter")
            phrase.value = "restore-opencode "
            await pilot.pause()
            self.assertTrue(approve.disabled)
            self.assertEqual(result, [])
            phrase.value = "restore-opencode"
            await pilot.pause()
            self.assertFalse(approve.disabled)
            approve.focus()
            await pilot.press("enter")
            self.assertEqual(result, [])
            await pilot.press("space")
            self.assertEqual(result, [True])
            app._private_interaction = False

    async def test_metadata_cancel_private_input_alias_declines_once_never_approves(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app._private_interaction = True
            result = []
            review = MetadataReview({"format": "session", "session_count": 1})
            app.push_screen(review, result.append)
            await pilot.pause()
            review.action_cancel_private_input()
            review.action_cancel_private_input()
            # A queued approval cannot reverse the already emitted cancellation.
            review.on_button_pressed(
                SimpleNamespace(
                    button=SimpleNamespace(id="metadata-approve"),
                    stop=Mock(),
                )
            )
            await pilot.pause()
            self.assertEqual(result, [False])
            app._private_interaction = False

    async def test_workflow_cancel_private_input_alias_preserves_cleanup_wait_without_start(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            workflow = BackupWorkflow(None, {"action": "workspace-inspect"})
            with (
                patch.object(BackupWorkflow, "_run"),
                patch.object(workflow.transport, "start") as start,
                patch.object(app, "suspend") as suspend,
                patch.object(
                    workflow,
                    "action_cancel_operation",
                    wraps=workflow.action_cancel_operation,
                ) as cancel,
            ):
                app.push_screen(workflow)
                await pilot.pause()
                workflow.action_cancel_private_input()
                cancel.assert_called_once_with()
                self.assertTrue(workflow.transport._cancelled)
                self.assertFalse(workflow.finished)
                self.assertTrue(app._private_interaction)
                self.assertTrue(
                    workflow.query_one("#backup-operation-close", Button).disabled
                )
                self.assertIn(
                    "Cancellation requested",
                    str(workflow.query_one("#backup-operation-stage", Static).render()),
                )
                start.assert_not_called()
                suspend.assert_not_called()
                workflow._finished(None)
                workflow.dismiss()

    async def test_advanced_keys_collapsed_and_host_creation_context_is_session_only(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            self.assertTrue(app.query_one(Collapsible).collapsed)
            app.query_one("#backup-scope", Select).value = "host"
            await pilot.pause()
            self.assertEqual(app.query_one("#backup-kind", Select).value, "session")

    async def test_demo_and_readonly_mutation_refusal_precedes_format_probe(self):
        for read_only, demo in ((False, True), (True, False)):
            app = DashboardApp(DemoBackend(), read_only=read_only, demo=demo)
            async with app.run_test(size=(110, 40)) as pilot:
                await self.begin(app, pilot)
                with patch.object(app, "_detect_backup") as probe:
                    with self.assertRaises(tui_operations.OperationError):
                        app._review_backup("backup-restore")
                probe.assert_not_called()

    async def test_embedded_inspect_never_suspends_dashboard_or_calls_input_handoff(
        self,
    ):
        app = DashboardApp(DemoBackend())
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            with (
                patch.object(app, "suspend") as suspend,
                patch.object(tui_operations, "handoff") as handoff,
                patch("builtins.input") as terminal_input,
                patch.object(BackupWorkflow, "_run") as worker,
            ):
                previous_interrupt = signal.getsignal(signal.SIGINT)
                app._perform_management({"action": "workspace-inspect"})
                await pilot.pause()
                self.assertIsInstance(app.screen, BackupWorkflow)
                self.assertTrue(app._maintenance_busy)
                self.assertTrue(app._private_interaction)
                suspend.assert_not_called()
                handoff.assert_not_called()
                terminal_input.assert_not_called()
                worker.assert_called_once()
                workflow = app.screen
                os.kill(os.getpid(), signal.SIGINT)
                self.assertTrue(workflow.transport._cancelled)
                workflow._finished(None)
                self.assertIs(signal.getsignal(signal.SIGINT), previous_interrupt)
                self.assertFalse(app._private_interaction)
                self.assertIn(
                    "unverified",
                    str(workflow.query_one("#backup-operation-stage", Static).render()),
                )
                app._embedded_operation = None
                app._maintenance_busy = False
                workflow.dismiss()

    async def test_real_private_process_roundtrip_stays_inside_dashboard(self):
        from tests.test_tui_operation_io import FIXTURE, SOURCE, VALUE as CHILD_VALUE

        with tempfile.TemporaryDirectory(
            prefix="oc-box-private-dashboard-"
        ) as temporary:
            root = Path(temporary)
            (root / "tui_operations.py").write_text(
                FIXTURE.replace("SOURCE", repr(str(SOURCE))).replace(
                    "VALUE", repr(CHILD_VALUE)
                )
            )
            runtime = SimpleNamespace(
                PACKAGE=root,
                state_root=lambda: root / "state",
                maintenance_host_env=lambda: {
                    "PATH": os.environ["PATH"],
                    "HOME": str(root),
                },
            )
            app = DashboardApp(DemoBackend(), demo=True)
            async with app.run_test(size=(110, 40)) as pilot:
                await self.begin(app, pilot)
                workflow = BackupWorkflow(runtime, {"scenario": "prompt"})
                app.push_screen(workflow)
                for _ in range(100):
                    await pilot.pause(0.03)
                    if isinstance(app.screen, PrivateSecretScreen):
                        screen = app.screen
                        if not screen._submitted:
                            await (
                                pilot.pause()
                            )  # private focus shield must finish mounting
                            screen.query_one(SecretEditor).focus()
                            await pilot.pause()
                            screen.query_one(SecretEditor).post_message(
                                events.Paste(CHILD_VALUE)
                            )
                            await pilot.pause()
                            self.assertEqual(
                                len(
                                    screen.query_one(SecretEditor)._private_editor.chars
                                ),
                                len(CHILD_VALUE),
                            )
                            screen.submit()
                    elif isinstance(app.screen, MetadataReview):
                        await pilot.pause()  # pushed screens compose asynchronously
                        app.screen.query_one("#metadata-approve", Button).focus()
                        await pilot.press("space")
                    if workflow.finished:
                        break
                self.assertTrue(workflow.finished)
                self.assertEqual(workflow.result["code"], 0)
                self.assertNotIn(CHILD_VALUE, "\n".join(workflow._details))
                self.assertFalse(app._private_interaction)
                self.assertEqual(app.screen, workflow)
                workflow.dismiss()

    async def test_explicit_readonly_inspect_routes_session_envelope_despite_ocbox_suffix(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="oc-box-dashboard-routing-"
        ) as temporary:
            archive = Path(temporary) / "misleading.ocbox"
            with tarfile.open(archive, "w") as output:
                for name in ("key.age", "manifest.age", "sessions.age"):
                    info = tarfile.TarInfo(name)
                    info.size = 10
                    output.addfile(info, io.BytesIO(b"ciphertext"))
            app = DashboardApp(DemoBackend(), read_only=True)
            async with app.run_test(size=(110, 40)) as pilot:
                await self.begin(app, pilot)
                plan = SimpleNamespace(
                    review="SYNTHETIC metadata review", confirmation=""
                )
                controller = Mock()
                controller.prepare.return_value = plan
                app._management_controller = controller
                app.query_one("#backup-file", Input).value = str(archive)
                self.assertEqual(app.query_one("#backup-kind", Select).value, "full")
                with patch.object(tui_operations, "handoff") as handoff:
                    app._review_backup("backup-inspect")
                    await self.begin(app, pilot)
                    controller.prepare.assert_called_once()
                    self.assertEqual(
                        controller.prepare.call_args.args, ("session-inspect",)
                    )
                    handoff.assert_not_called()
                    app.screen.query_one("#operation-cancel", Button).press()
                    await pilot.pause()
                    controller.cancel.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
