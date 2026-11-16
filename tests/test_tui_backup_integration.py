"""Combined dashboard privacy fences around the real private backup widgets."""

import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_backup_workflow import (
        MetadataReview,
        PrivateSecretScreen,
        SecretEditor,
    )
    from textual import events
    from textual.widgets import Button, Input


SECRET = "synthetic-only-private-123"


@unittest.skipUnless(AVAILABLE, "optional UI dependencies unavailable")
class BackupPrivacyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def begin(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_real_private_editor_fences_clipboard_and_transition_gap(self):
        app = DashboardApp(DemoBackend(), demo=True)
        transport = SimpleNamespace(
            submit_secret=Mock(), cancel=Mock(return_value=True)
        )
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app.copy_to_clipboard("EXISTING_PUBLIC_CACHE")
            search = app.query_one("#log-search", Input)
            search.value = "KEEP_SEARCH"
            app.query_one("#backup-file", Input).value = "/synthetic/KEEP_PATH"
            app._private_interaction = True
            screen = PrivateSecretScreen(
                transport, {"id": 1, "purpose": "backup-unlock"}
            )
            await app.push_screen(screen)
            await pilot.pause()
            editor = screen.query_one(SecretEditor)
            await pilot.press(*SECRET)
            self.assertEqual(len(editor._private_editor.chars), len(SECRET))
            self.assertNotIn(SECRET, editor.render().plain)
            self.assertFalse(screen.query(Input))

            app.enable_text_selection(screen)
            self.assertTrue(all(not w.ALLOW_SELECT for w in screen.walk_children()))
            with (
                patch.object(app, "_write_clipboard") as write,
                patch.object(editor, "get_selection") as extract,
                patch.object(
                    DashboardApp,
                    "clipboard",
                    new_callable=PropertyMock,
                    side_effect=AssertionError("Private cache read"),
                ) as cache,
            ):
                app.action_copy_selection("EXPLICIT_PUBLIC_TEXT")
                app.action_copy_view()
                app.action_paste_copied_text()
                app.copy_to_clipboard("DIRECT_PUBLIC_TEXT")
                app._copy_completed_selection(screen, editor)
                await pilot.press(
                    "ctrl+shift+c", "ctrl+shift+y", "ctrl+shift+insert", "ctrl+a"
                )
                write.assert_not_called()
                extract.assert_not_called()
                cache.assert_not_called()
            self.assertEqual(app.clipboard, "EXISTING_PUBLIC_CACHE")
            self.assertEqual(getattr(app, "_selection_keyboard_events", {}), {})

            # A click outside the private modal cannot reach the underlying ×.
            await pilot.click(offset=(search.region.right - 2, search.region.y + 1))
            self.assertEqual(search.value, "KEEP_SEARCH")
            self.assertEqual(
                app.query_one("#backup-file", Input).value, "/synthetic/KEEP_PATH"
            )
            screen.dismiss()
            await pilot.pause()
            self.assertEqual(editor._private_editor.chars, [])
            self.assertTrue(app._private_interaction)
            with patch.object(app, "_write_clipboard") as write:
                # The child has not acknowledged exit: ordinary screen/focus
                # must not reopen clipboard access between private dialogs.
                app.action_copy_selection("PUBLIC_BETWEEN_SCREENS")
                app.action_copy_view()
                app.action_paste_copied_text()
                write.assert_not_called()
                app._private_interaction = False
                app.action_copy_selection("PUBLIC_AFTER_EXIT")
                write.assert_called_once_with("PUBLIC_AFTER_EXIT")
            transport.submit_secret.assert_not_called()

    async def test_private_focus_traversal_and_deliberate_metadata_approval(self):
        app = DashboardApp(DemoBackend(), demo=True)
        transport = SimpleNamespace(
            submit_secret=Mock(), cancel=Mock(return_value=True)
        )
        async with app.run_test(size=(110, 40)) as pilot:
            await self.begin(app, pilot)
            app._private_interaction = True
            screen = PrivateSecretScreen(
                transport, {"id": 2, "purpose": "backup-confirm"}
            )
            await app.push_screen(screen)
            await pilot.pause()
            editor = screen.query_one(SecretEditor)
            self.assertIs(
                app.focused, screen.query_one("#private-secret-cancel", Button)
            )
            await pilot.press(*SECRET, "enter", "enter", "f1", "ctrl+p")
            editor.post_message(events.Paste(SECRET))
            await pilot.pause()
            self.assertIs(app.screen, screen)
            self.assertEqual(editor._private_editor.chars, [])
            transport.submit_secret.assert_not_called()
            self.assertTrue(app.check_action("focus_previous", ()))
            self.assertTrue(app.check_action("focus_next", ()))
            await pilot.press("shift+tab")
            self.assertIs(app.focused, editor)
            editor.post_message(events.Paste(SECRET))
            await pilot.pause()
            self.assertEqual(len(editor._private_editor.chars), len(SECRET))
            screen.submit()
            transport.submit_secret.assert_called_once()
            self.assertEqual(transport.submit_secret.call_args.args[0], 2)
            self.assertEqual(transport.submit_secret.call_args.args[1], bytearray())
            screen.dismiss()
            await pilot.pause()

            result = []
            review = MetadataReview({"format": "session", "session_count": 1})
            await app.push_screen(review, result.append)
            await pilot.pause()
            review.query_one("#metadata-approve", Button).focus()
            await pilot.press("enter", "enter")
            self.assertEqual(result, [])
            self.assertIs(app.screen, review)
            self.assertTrue(app._private_interaction)
            await pilot.press("space")
            for _ in range(100):
                await pilot.pause(0.02)
                if result:
                    break
            self.assertEqual(result, [True])
            self.assertTrue(app._private_interaction)
            app._private_interaction = False
