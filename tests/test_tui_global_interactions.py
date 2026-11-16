"""Global dashboard controls: synthetic headless input, never a host clipboard."""

import importlib.util
import unittest
from unittest.mock import patch

from oc_box.tui import DemoBackend

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from oc_box.tui_app import Confirm, DashboardApp
    from oc_box.tui_clipboard import ClipboardHelp
    from oc_box.tui_management import ActionReview
    from textual.widgets import Button, Input, Static
    from textual.screen import ModalScreen
    from textual.widget import Widget

    class SyntheticPrivateScreen(ModalScreen):
        PRIVATE_INPUT = True
        ALLOW_SELECT = False

        def compose(self):
            yield Static("Private entry")
            yield Widget(id="opaque-editor")


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class GlobalInteractionMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_private_interaction_fences_navigation_on_normal_screen(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(100, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            ordinary = app.screen
            app._private_interaction = object()
            with patch.object(app, "_write_clipboard") as write:
                app.action_clipboard_help()
                for key in ("f1", "f2", "ctrl+p", "ctrl+q", "q", "s", "1", "r"):
                    await pilot.press(key)
                    self.assertIs(app.screen, ordinary)
                    self.assertTrue(app.is_running)
                write.assert_not_called()
            app._private_interaction = False
            await pilot.press("f1")
            self.assertIsInstance(app.screen, ClipboardHelp)

    async def test_private_entry_fences_global_navigation_palette_and_help(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(100, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            modal = SyntheticPrivateScreen()
            await app.push_screen(modal)
            await pilot.pause()
            for action in (
                "quit",
                "help_quit",
                "command_palette",
                "clipboard_help",
                "settings",
                "logs",
                "tab",
                "refresh",
                "copy_selection",
                "copy_view",
                "paste_copied_text",
            ):
                with self.subTest(action=action):
                    self.assertFalse(app.check_action(action, ()))
            self.assertTrue(app.check_action("focus_next", ()))
            with patch.object(app, "_write_clipboard") as write:
                app.action_clipboard_help()
                for key in ("f1", "f2", "ctrl+p", "ctrl+q", "q", "s", "1", "r"):
                    await pilot.press(key)
                    self.assertIs(app.screen, modal)
                    self.assertTrue(app.is_running)
                write.assert_not_called()
            app.pop_screen()
            await pilot.pause()
            await pilot.press("f1")
            self.assertIsInstance(app.screen, ClipboardHelp)

    async def test_confirmation_classification_matrix(self):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(100, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for destructive, approve in (
                (False, False),
                (False, True),
                (True, False),
                (True, True),
            ):
                with self.subTest(destructive=destructive, approve=approve):
                    result = []
                    modal = Confirm(
                        "Synthetic explicit review", destructive=destructive
                    )
                    app.push_screen(modal, result.append)
                    await pilot.pause()
                    self.assertFalse(
                        modal.query("#copy-selection, #copy-view, #paste-copied")
                    )
                    self.assertEqual(bool(modal.query("#confirmation")), destructive)
                    self.assertEqual(result, [])
                    if destructive:
                        await pilot.click("#confirm")
                        self.assertEqual(result, [])
                        self.assertIn(
                            "Type DELETE exactly",
                            str(
                                modal.query_one("#confirmation-error", Static).render()
                            ),
                        )
                        modal.query_one("#confirmation", Input).value = "DELETE"
                    await pilot.click("#confirm" if approve else "#cancel")
                    self.assertEqual(result, [approve])

    async def test_help_exposes_shortcuts_without_copying_or_changing_underlying_review(
        self,
    ):
        app = DashboardApp(DemoBackend(), demo=True)
        async with app.run_test(size=(100, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            result = []
            review = ActionReview("Synthetic reversible review")
            app.push_screen(review, result.append)
            await pilot.pause()
            with patch.object(app, "_write_clipboard") as write:
                await pilot.press("f1")
                self.assertIsInstance(app.screen, ClipboardHelp)
                content = str(app.screen.query_one(Static).render())
                for phrase in (
                    "Ctrl+Shift+C",
                    "Ctrl+Shift+Y",
                    "Ctrl+Shift+Insert",
                    "in-app cache",
                    "not the current desktop clipboard",
                    "programmatically selecting text never copies",
                    "Ctrl+C / Ctrl+V",
                ):
                    self.assertIn(phrase, content)
                self.assertFalse(app.screen.query(Button))
                write.assert_not_called()
                await pilot.press("escape")
                self.assertIs(app.screen, review)
                self.assertEqual(result, [])
                await pilot.press("f1")
                self.assertIsInstance(app.screen, ClipboardHelp)
                await pilot.press("f1")
                self.assertIs(app.screen, review)
                self.assertEqual(result, [])
                await pilot.click("#operation-cancel")
                self.assertEqual(result, [None])


if __name__ == "__main__":
    unittest.main()
