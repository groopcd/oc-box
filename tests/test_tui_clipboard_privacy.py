"""Trusted private-editor fences; no backup workflow or real secret material."""

import unittest
from unittest.mock import PropertyMock, patch

try:
    from textual import events
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.selection import SELECT_ALL
    from textual.widget import Widget
    from textual.widgets import Input, Static
except ImportError:
    App = None


if App is not None:
    from oc_box.tui_clipboard import ClipboardActions, ClipboardInput

    class OpaqueEditor(Widget, can_focus=True):
        """A custom masked editor, deliberately not a Textual Input."""

        ALLOW_SELECT = False
        BINDINGS = [
            Binding(
                "ctrl+c,ctrl+x,ctrl+v,ctrl+a,ctrl+shift+a,super+c,super+x,super+v,super+a",
                "forbidden",
            )
        ]
        DEFAULT_CSS = "OpaqueEditor { height: 3; }"

        def __init__(self):
            super().__init__(id="private-editor")
            self.keys = []
            self.pastes = []
            self.forbidden = []

        def render(self):
            return "••••••••"

        @property
        def selected_text(self):
            raise AssertionError("private contents must not be inspected")

        def get_selection(self, selection):
            raise AssertionError("private selection must not be extracted")

        def action_forbidden(self):
            self.forbidden.append(True)

        def on_key(self, event):
            self.keys.append(event.key)
            event.stop()

        def on_paste(self, event):
            self.pastes.append(event.text)
            event.stop()

    class PrivateEditor(OpaqueEditor):
        PRIVATE_INPUT = True

    class PrivateGroup(Vertical):
        PRIVATE_INPUT = True
        ALLOW_SELECT = False

    class PrivateScreen(ModalScreen):
        PRIVATE_INPUT = True
        ALLOW_SELECT = False

        def compose(self):
            yield Static("Private entry")
            yield PrivateEditor()

    class SubtreeScreen(ModalScreen):
        def compose(self):
            yield ClipboardInput(
                "public text", id="public-input", select_on_focus=False
            )
            with PrivateGroup(id="private-group"):
                yield OpaqueEditor()

    class PrivacyFixtureApp(ClipboardActions, App):
        BINDINGS = [
            Binding("ctrl+shift+c", "copy_selection", priority=True),
            Binding("ctrl+shift+y", "copy_view", priority=True),
            Binding("ctrl+shift+insert", "paste_copied_text", priority=True),
        ]

        def __init__(self):
            super().__init__()
            self.copies, self.notices = [], []

        def compose(self):
            yield ClipboardInput("public text", id="ordinary", select_on_focus=False)

        def _write_clipboard(self, text):
            self._clipboard = text
            self.copies.append(text)

        def _clipboard_notice(self, message):
            self.notices.append(message)


@unittest.skipIf(App is None, "optional Textual not installed")
class PrivateClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_private_interaction_fences_an_ordinary_screen(self):
        app = PrivacyFixtureApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = app.screen
            field = app.query_one(Input)
            field.focus()
            field.selection = (0, len(field.value))
            app._clipboard = "public cache"
            # A truthy workflow owner, not just literal True, keeps the fence
            # active after a private modal disappears and until closure clears it.
            app._private_interaction = object()
            self.assertTrue(app._clipboard_private())
            self.assertTrue(app._clipboard_private(field))
            with (
                patch.object(screen, "get_selected_text") as selection,
                patch.object(screen._compositor, "render_strips") as strips,
                patch.object(field, "_render") as render,
                patch.object(
                    type(app),
                    "clipboard",
                    new_callable=PropertyMock,
                    side_effect=AssertionError(
                        "private interaction must not read cache"
                    ),
                ),
            ):
                app.enable_text_selection()
                self.assertIsNone(app.clipboard_text_selection())
                self.assertIsNone(app._completed_input_selection(field))
                app._copy_completed_selection(screen, field)
                app.action_copy_selection()
                app.action_copy_view()
                app.action_paste_copied_text()
                self.assertFalse(app.copy_to_clipboard("synthetic private value"))
                selection.assert_not_called()
                strips.assert_not_called()
                render.assert_not_called()
            for key in (
                "ctrl+c",
                "ctrl+x",
                "ctrl+v",
                "ctrl+a",
                "ctrl+shift+c",
                "ctrl+shift+y",
                "ctrl+shift+insert",
            ):
                await pilot.press(key)
            self.assertEqual(field.value, "public text")
            self.assertEqual(app.copies, [])
            self.assertEqual(app.notices, [])
            self.assertEqual(app.clipboard, "public cache")
            app._private_interaction = None
            app.enable_text_selection()
            await pilot.press("ctrl+a")
            self.assertEqual(app.copies, ["public text"])

    async def test_private_screen_shortcuts_and_callbacks_never_read_or_copy(self):
        app = PrivacyFixtureApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app._clipboard = "public cache"
            modal = PrivateScreen()
            await app.push_screen(modal)
            editor = modal.query_one(PrivateEditor)
            editor.focus()
            await pilot.pause()
            self.assertNotIsInstance(editor, Input)
            self.assertTrue(app._clipboard_private())
            app.enable_text_selection()
            self.assertFalse(modal.allow_select)
            for widget in modal.walk_children(Widget):
                self.assertFalse(widget.allow_select)
                self.assertFalse(getattr(widget, "_clipboard_selection_adapter", False))
            modal.selections = {editor: SELECT_ALL}
            with (
                patch.object(modal, "get_selected_text") as selection,
                patch.object(modal._compositor, "render_strips") as strips,
            ):
                self.assertIsNone(app.clipboard_text_selection())
                self.assertIsNone(app._completed_input_selection(editor))
                app._copy_completed_selection(modal, editor)
                app.action_copy_selection()
                app.action_copy_selection("synthetic private value")
                app._copy_clipboard_text("synthetic private value")
                app.action_copy_view()
                self.assertFalse(app.copy_to_clipboard("synthetic private value"))
                app.action_paste_copied_text()
                selection.assert_not_called()
                strips.assert_not_called()
            for key in (
                "ctrl+c",
                "ctrl+x",
                "ctrl+v",
                "ctrl+a",
                "ctrl+shift+a",
                "super+c",
                "super+x",
                "super+v",
                "super+a",
                "ctrl+shift+c",
                "ctrl+shift+y",
                "ctrl+shift+insert",
                "ctrl+insert",
                "shift+insert",
                "shift+delete",
            ):
                with self.subTest(key=key):
                    await pilot.press(key)
            self.assertEqual(editor.keys, [])
            self.assertEqual(editor.forbidden, [])
            self.assertEqual(editor.pastes, [])
            await pilot.press("a")
            self.assertEqual(editor.keys, ["a"], "private editor still receives typing")
            # A completed drag is also fenced, not just explicit hotkeys.
            point = editor.content_region.offset
            for kind, x in (
                (events.MouseDown, point.x),
                (events.MouseMove, point.x + 3),
                (events.MouseUp, point.x + 3),
            ):
                await app.on_event(
                    kind(
                        editor,
                        x,
                        point.y,
                        0,
                        0,
                        1,
                        False,
                        False,
                        False,
                        screen_x=x,
                        screen_y=point.y,
                    )
                )
                await pilot.pause()
            self.assertEqual(app.copies, [])
            self.assertEqual(app.notices, [])
            self.assertEqual(app.clipboard, "public cache")
            self.assertIsNone(app._selection_gesture)
            app.pop_screen()
            await pilot.pause()
            ordinary = app.query_one("#ordinary", Input)
            ordinary.focus()
            await pilot.press("ctrl+a")
            self.assertEqual(app.copies, ["public text"])

    async def test_private_ancestor_fences_focus_selection_and_whole_screen_copy(self):
        app = PrivacyFixtureApp()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = SubtreeScreen()
            await app.push_screen(modal)
            editor = modal.query_one(OpaqueEditor)
            field = modal.query_one(Input)
            field.focus()
            await pilot.pause()
            app.enable_text_selection()
            self.assertFalse(app._clipboard_private())
            self.assertTrue(app._clipboard_private(editor))
            self.assertFalse(editor.allow_select)
            self.assertFalse(editor.parent.allow_select)
            self.assertFalse(getattr(editor, "_clipboard_selection_adapter", False))
            self.assertTrue(field.allow_select)
            # Even an unfocused marked subtree blocks full-screen extraction.
            with patch.object(modal._compositor, "render_strips") as strips:
                app.action_copy_view()
                strips.assert_not_called()
            modal.selections = {editor: SELECT_ALL}
            with patch.object(modal, "get_selected_text") as selection:
                self.assertIsNone(app.clipboard_text_selection())
                app._copy_completed_selection(modal, editor)
                selection.assert_not_called()
            modal.clear_selection()
            editor.focus()
            await pilot.pause()
            self.assertTrue(app._clipboard_private())
            app.action_paste_copied_text()
            app.action_copy_selection()
            self.assertEqual(editor.pastes, [])
            self.assertEqual(app.copies, [])
            field.focus()
            await pilot.pause()
            await pilot.press("ctrl+a")
            self.assertEqual(app.copies, ["public text"])


if __name__ == "__main__":
    unittest.main()
