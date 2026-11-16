"""Headless Input clear controls; no real clipboard, shell or host services."""

import importlib.util
import unittest

AVAILABLE = importlib.util.find_spec("textual") is not None
if AVAILABLE:
    from textual.app import App
    from textual.geometry import Region
    from textual.selection import SELECT_ALL
    from textual.widgets import Button, Input

    from oc_box.tui_clipboard import ClipboardActions, ClipboardInput

    class InputControlsApp(ClipboardActions, App):
        AUTO_FOCUS = "#search"

        def __init__(self):
            super().__init__()
            self.read_only = True
            self.changes, self.submissions, self.copies, self.buttons = [], [], [], []

        def compose(self):
            yield ClipboardInput("filter 界", id="search")
            yield ClipboardInput("ordinary path", id="path", select_on_focus=False)
            yield ClipboardInput("masked synthetic value", password=True, id="secret")
            yield ClipboardInput("disabled value", disabled=True, id="disabled")
            yield ClipboardInput("no padding", compact=True, id="compact")
            yield Input("native reference", id="native", select_on_focus=False)
            yield Button("Unrelated action", id="unrelated")

        def on_input_changed(self, event):
            self.changes.append((event.input.id, event.value))

        def on_input_submitted(self, event):
            self.submissions.append((event.input.id, event.value))

        def on_button_pressed(self, event):
            self.buttons.append(event.button.id)

        def _write_clipboard(self, value):
            self.copies.append(value)


@unittest.skipUnless(AVAILABLE, "optional Textual not installed")
class InputControlsTests(unittest.IsolatedAsyncioTestCase):
    def rendered(self, field):
        return field.render_lines(Region(0, 0, field.region.width, field.region.height))

    async def click_clear(self, pilot, field, **kwargs):
        # Native tall border is one cell; × is the last right-padding cell.
        await pilot.click(field, offset=(field.region.width - 2, 1), **kwargs)
        await pilot.pause()

    async def test_mouse_clear_is_one_changed_event_not_submit_and_keeps_native_input(
        self,
    ):
        app = InputControlsApp()
        async with app.run_test(size=(80, 28)) as pilot:
            search = app.query_one("#search", Input)
            native = app.query_one("#native", Input)
            self.assertEqual(search.region.width, native.region.width)
            self.assertEqual(search.content_region.width, native.content_region.width)
            self.assertIsInstance(search, ClipboardInput)
            self.assertEqual(self.rendered(search)[1].text[-2], "×")
            self.assertNotIn("×", search.get_selection(SELECT_ALL)[0])
            native.focus()
            await pilot.pause()
            app.changes.clear()

            await self.click_clear(pilot, search)

            self.assertEqual(search.value, "")
            self.assertEqual(app.changes, [("search", "")])
            self.assertEqual(search.cursor_position, 0)
            self.assertEqual(tuple(search.selection), (0, 0))
            self.assertIs(app.focused, search)
            self.assertNotIn("×", self.rendered(search)[1].text)
            self.assertEqual(app.submissions, [])
            self.assertEqual(app.buttons, [])
            self.assertEqual(app.copies, [])
            await self.click_clear(pilot, search)
            self.assertEqual(app.changes, [("search", "")])
            await pilot.press("n", "e", "w", "left", "X")
            self.assertEqual(search.value, "neXw")
            self.assertEqual(search.cursor_position, 3)
            await pilot.press("ctrl+a", "r")
            self.assertEqual(search.value, "r")
            self.assertEqual(self.rendered(search)[1].text[-2], "×")
            self.assertEqual(app.submissions, [])

    async def test_normal_inputs_resize_scroll_and_crop_without_consuming_text_columns(
        self,
    ):
        app = InputControlsApp()
        async with app.run_test(size=(100, 28)) as pilot:
            field = app.query_one("#path", ClipboardInput)
            native = app.query_one("#native", Input)
            for width in (100, 36, 16):
                with self.subTest(width=width):
                    await pilot.resize_terminal(width, 28)
                    field.value = "界e\u0301/" * 40
                    field.focus()
                    field.cursor_position = len(field.value)
                    await pilot.pause()
                    self.assertEqual(field.region.width, native.region.width)
                    self.assertEqual(
                        field.content_region.width, native.content_region.width
                    )
                    self.assertGreater(field.scroll_x, 0)
                    full = self.rendered(field)
                    self.assertTrue(all(line.cell_length == width for line in full))
                    self.assertEqual(full[1].text[-2], "×")
                    crop = Region(width - 4, 1, 4, 1)
                    self.assertEqual(
                        field.render_lines(crop)[0].text,
                        full[1].crop(width - 4, width).text,
                    )
                    await self.click_clear(pilot, field)
                    self.assertEqual(field.value, "")
                    self.assertEqual(field.cursor_position, 0)
                    await pilot.press("a", "b", "left", "c")
                    self.assertEqual(field.value, "acb")
            self.assertEqual(app.submissions, [])
            self.assertEqual(app.copies, [])

    async def test_password_disabled_and_paddingless_inputs_do_not_gain_an_overlay(
        self,
    ):
        app = InputControlsApp()
        async with app.run_test(size=(80, 28)) as pilot:
            for ident in ("secret", "disabled", "compact"):
                with self.subTest(ident=ident):
                    field = app.query_one(f"#{ident}", Input)
                    value = field.value
                    self.assertNotIn("×", "".join(s.text for s in self.rendered(field)))
                    if ident != "compact":
                        await self.click_clear(pilot, field)
                    self.assertEqual(field.value, value)
            secret = app.query_one("#secret", Input)
            self.assertNotIn(secret.value, secret.get_selection(SELECT_ALL)[0])
            app.action_copy_view()
            self.assertNotIn(secret.value, app.copies[-1])
            self.assertEqual(app.submissions, [])
            self.assertEqual(app.buttons, [])

    async def test_nonprimary_click_and_drag_to_clear_cell_do_not_clear(self):
        app = InputControlsApp()
        async with app.run_test(size=(80, 28)) as pilot:
            field = app.query_one("#path", Input)
            await self.click_clear(pilot, field, button=3)
            self.assertEqual(field.value, "ordinary path")
            await pilot.mouse_down(field, offset=(3, 1))
            await pilot.hover(field, offset=(field.region.width - 2, 1))
            await pilot.mouse_up(field, offset=(field.region.width - 2, 1))
            await pilot.pause()
            self.assertEqual(field.value, "ordinary path")
            await pilot.mouse_down(field, offset=(field.region.width - 2, 1))
            await pilot.hover(field, offset=(field.region.width - 3, 1))
            await pilot.hover(field, offset=(field.region.width - 2, 1))
            await pilot.mouse_up(field, offset=(field.region.width - 2, 1))
            await pilot.pause()
            self.assertEqual(field.value, "ordinary path")
            self.assertEqual(app.submissions, [])
            self.assertEqual(app.buttons, [])


if __name__ == "__main__":
    unittest.main()
