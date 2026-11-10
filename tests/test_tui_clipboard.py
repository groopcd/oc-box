"""Synthetic Textual apps only: no desktop clipboard, clients or user state."""

import unittest
from unittest.mock import patch

try:
    from rich.segment import Segment
    from rich.style import Style
    from textual import events
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.selection import SELECT_ALL, Selection
    from textual.geometry import Offset
    from textual.strip import Strip
    from textual.widget import Widget
    from textual.widgets import Button, Input, Select, Static
except ImportError:
    App = None


if App is not None:
    from oc_box.tui_clipboard import (
        MAX_CLIPBOARD_BYTES,
        ClipboardActions,
        ClipboardBar,
        ClipboardButton,
        ClipboardInput,
        SelectableDataTable,
        SelectableRichLog,
        SelectableViewport,
    )

    class SyntheticTerminal(SelectableViewport, Widget, can_focus=True):
        _accepts_clipboard_paste = True

        def __init__(self):
            super().__init__(id="terminal")
            self.state = "connected"
            self.pastes = []
            self.keys = []
            self.rows = ["hello 界e\u0301 world", "second terminal row", "last row"]

        def _selection_line(self, y):
            text = self.rows[y] if y < len(self.rows) else ""
            return Strip([Segment(text, Style(color="cyan"))]).adjust_cell_length(
                self.content_size.width
            )

        def on_paste(self, event):
            self.pastes.append(event.text)
            event.stop()

        def on_key(self, event):
            self.keys.append(event.key)
            event.stop()
            event.prevent_default()

    class ClipboardFixtureApp(ClipboardActions, App):
        AUTO_FOCUS = "#search"
        CSS = """
        Static { height: 1; }
        SyntheticTerminal { height: 3; }
        SelectableDataTable { height: 5; width: 50%; }
        SelectableRichLog { height: 4; width: 50%; }
        Input, Select, Button { height: 3; }
        """
        BINDINGS = [
            Binding("ctrl+shift+c", "copy_selection", priority=True),
            Binding("ctrl+shift+insert", "paste_copied_text", priority=True),
        ]

        def __init__(self, *, read_only=False):
            super().__init__()
            self.read_only = read_only
            self.copies = []
            self.notices = []

        def compose(self):
            yield Static("  first row  ", id="first")
            yield Static("second row", id="second")
            hidden = Static("HIDDEN_WIDGET_DATA", id="hidden")
            hidden.display = False
            yield hidden
            yield ClipboardInput("old searchable", id="search", select_on_focus=False)
            yield SyntheticTerminal()
            yield SelectableDataTable(
                id="table", fixed_rows=1, fixed_columns=1, cursor_type="row"
            )
            yield SelectableRichLog(
                id="log", min_width=80, max_lines=8, auto_scroll=False
            )
            yield Select([("Visible choice", "visible")], value="visible")
            yield ClipboardBar()
            yield Button("Disabled action label", id="action", disabled=True)

        def on_mount(self):
            table = self.query_one(SelectableDataTable)
            table.add_columns("Project", "State", "Notes")
            for index in range(12):
                table.add_row(
                    f"project-{index:02}",
                    "stopped",
                    f"note-{index:02} 界e\u0301 " + "detail " * 12,
                )
            log = self.query_one(SelectableRichLog)
            for index in range(8):
                log.write(f"LOG{index:02} prefix 界e\u0301 detail " + "content " * 8)

        def _write_clipboard(self, text):
            # A local test double: no driver writes or actual host clipboard.
            self._clipboard = text
            self.copies.append(text)

        def notify(self, message, **kwargs):
            self.notices.append(message)

        def on_button_pressed(self, event):
            if event.button.id == "copy-selection":
                self.action_copy_selection(event.button.consume_selection())
            elif event.button.id == "copy-view":
                self.action_copy_view()
            elif event.button.id == "paste-copied":
                self.action_paste_copied_text()

    class SyntheticModal(ModalScreen):
        CSS = """
        SyntheticModal { align: center middle; }
        SyntheticModal > Vertical { width: 52; height: 10; background: $surface; }
        """

        def compose(self):
            with Vertical():
                yield Static("Synthetic modal message", id="modal-message")
                yield ClipboardInput(
                    "modal input", id="modal-input", select_on_focus=False
                )
                yield ClipboardBar()


@unittest.skipIf(App is None, "optional Textual not installed")
class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def mouse(self, app, pilot, kind, widget, offset=(0, 0), button=1):
        """Use App routing, including its real same-widget Click synthesis."""
        x = widget.content_region.x + offset[0]
        y = widget.content_region.y + offset[1]
        event = kind(
            widget,
            x,
            y,
            0,
            0,
            button,
            False,
            False,
            False,
            screen_x=x,
            screen_y=y,
        )
        await app.on_event(event)
        await pilot.pause()

    async def drag(self, app, pilot, first, start, last, end):
        await self.mouse(app, pilot, events.MouseDown, first, start)
        await self.mouse(app, pilot, events.MouseMove, last, end)
        await self.mouse(app, pilot, events.MouseUp, last, end)

    async def click(self, app, pilot, widget, offset=(1, 0)):
        await self.mouse(app, pilot, events.MouseDown, widget, offset)
        await self.mouse(app, pilot, events.MouseUp, widget, offset)

    async def test_cross_widget_drag_copies_only_on_explicit_action(self):
        app = ClipboardFixtureApp(read_only=True)
        async with app.run_test(size=(80, 36)) as pilot:
            await self.drag(
                app,
                pilot,
                app.query_one("#first"),
                (2, 0),
                app.query_one("#second"),
                (3, 0),
            )
            self.assertEqual(app.clipboard_text_selection(), "first row  \nsec")
            self.assertEqual(app.copies, [])
            await pilot.press("ctrl+shift+c")
            self.assertEqual(app.copies, ["first row  \nsec"])

    async def test_custom_unicode_selection_has_offsets_and_highlighting(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            terminal = app.query_one(SyntheticTerminal)
            before = terminal.render_line(0)
            await self.drag(app, pilot, terminal, (6, 0), terminal, (9, 0))
            self.assertEqual(app.screen.get_selected_text(), "界e\u0301")
            after = terminal.render_line(0)
            self.assertEqual(after.text, before.text)
            style = app.screen.get_component_rich_style("screen--selection")
            self.assertTrue(
                any(
                    "界e\u0301" in segment.text
                    and segment.style.bgcolor == style.bgcolor
                    for segment in after
                )
            )
            self.assertTrue(all("offset" in segment.style.meta for segment in after))
            self.assertEqual(app.copies, [])
            app.action_copy_selection()
            self.assertEqual(app.clipboard, "界e\u0301")

    async def test_table_and_log_keep_scrolling_fixed_headers_and_viewport_bounds(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            table = app.query_one(SelectableDataTable)
            log = app.query_one(SelectableRichLog)
            first, _ = table.get_selection(SELECT_ALL)
            self.assertIn("Project", first)
            self.assertIn("project-00", first)
            self.assertNotIn("project-11", first)
            await self.drag(app, pilot, table, (1, 1), table, (8, 1))
            self.assertEqual(app.screen.get_selected_text(), "project-")
            self.assertEqual(table.cursor_type, "row")
            table.scroll_to(x=15, y=4, animate=False, force=True)
            log.scroll_to(x=7, y=3, animate=False, force=True)
            await pilot.pause()
            scrolled, _ = table.get_selection(SELECT_ALL)
            self.assertIn("Project", scrolled)
            self.assertIn("project-00", scrolled)
            self.assertNotIn("project-01", scrolled)
            self.assertNotEqual(scrolled, first)
            logged, _ = log.get_selection(SELECT_ALL)
            self.assertIn("界e\u0301", logged)
            self.assertNotIn("LOG00", logged)
            self.assertNotIn("LOG07", logged)
            self.assertGreater(table.scroll_x, 0)
            self.assertGreater(log.scroll_y, 0)
            old_width = table.scrollable_content_region.width
            await pilot.resize_terminal(54, 36)
            self.assertLess(table.scrollable_content_region.width, old_width)
            for widget in (table, log):
                selected, _ = widget.get_selection(SELECT_ALL)
                self.assertLessEqual(
                    len(selected.splitlines()), widget.scrollable_content_region.height
                )
                for y in range(widget.scrollable_content_region.height):
                    self.assertLessEqual(
                        widget.render_line(y).cell_length,
                        widget.scrollable_content_region.width,
                    )
            self.assertEqual(app.copies, [])

    async def test_log_eviction_empty_rows_and_stale_resize_selection_are_safe(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            log = app.query_one(SelectableRichLog)
            for index in range(8, 18):
                log.write(f"replacement-{index:02} 界e\u0301")
            log.scroll_home(animate=False)
            await pilot.pause()
            selected, _ = log.get_selection(SELECT_ALL)
            self.assertNotIn("LOG", selected)
            self.assertIn("replacement-10", selected)
            log.clear()
            await pilot.pause()
            self.assertFalse(log.get_selection(SELECT_ALL)[0].strip())
            terminal = app.query_one(SyntheticTerminal)
            terminal.rows = []
            stale = Selection(Offset(0, 3), Offset(5, 3))
            self.assertEqual(terminal.get_selection(stale), ("", "\n"))
            self.assertEqual(app.copies, [])

    async def test_copy_button_captures_before_mouse_up_and_preserves_input_focus(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            search = app.query_one("#search", Input)
            button = app.query_one("#copy-selection", ClipboardButton)
            await self.drag(
                app,
                pilot,
                app.query_one("#first"),
                (2, 0),
                app.query_one("#second"),
                (3, 0),
            )
            await self.mouse(app, pilot, events.MouseDown, button, (1, 0))
            self.assertEqual(app.copies, [])
            await self.mouse(app, pilot, events.MouseUp, button, (1, 0))
            self.assertIsNone(app.screen.get_selected_text())
            self.assertEqual(app.copies, ["first row  \nsec"])
            self.assertIsNone(button.consume_selection())
            await pilot.pause(0.25)
            search.focus()
            await pilot.pause()
            search.selection = (4, 10)
            await self.click(app, pilot, button)
            self.assertEqual(app.clipboard, "search")
            self.assertIs(app.focused, search)
            self.assertFalse(button.allow_select)

    async def test_canceled_real_drag_and_keyboard_clear_button_candidate(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            search = app.query_one("#search", Input)
            search.selection = (0, 3)
            button = app.query_one("#copy-selection", ClipboardButton)
            await self.drag(app, pilot, button, (1, 0), button, (4, 0))
            self.assertEqual(app.copies, [])
            self.assertIsNone(button.consume_selection())
            search.selection = (0, 3)
            await self.mouse(app, pilot, events.MouseDown, button, (1, 0))
            button.focus()
            await pilot.pause()
            self.assertIsNone(button.consume_selection())
            button.post_message(events.Key("escape", None))
            await pilot.pause()
            self.assertIsNone(button.consume_selection())
            self.assertEqual(app.copies, [])
            view = app.query_one("#copy-view", ClipboardButton)
            await self.mouse(app, pilot, events.MouseDown, view, (1, 0), button=3)
            await self.mouse(app, pilot, events.MouseUp, view, (1, 0), button=3)
            self.assertEqual(app.copies, [])

    async def test_copy_view_includes_control_labels_but_not_hidden_model_data(self):
        app = ClipboardFixtureApp(read_only=True)
        async with app.run_test(size=(80, 36)):
            app.action_copy_view()
            copied = app.clipboard
            for visible in (
                "first row",
                "Visible choice",
                "Disabled action label",
                "Copy view",
            ):
                self.assertIn(visible, copied)
            for hidden in ("HIDDEN_WIDGET_DATA", "project-11", "LOG07"):
                self.assertNotIn(hidden, copied)
            self.assertTrue(copied.startswith("  first row\n"))
            self.assertTrue(all(not row.endswith(" ") for row in copied.splitlines()))
            self.assertFalse(copied.endswith("\n"))

    async def test_modal_clipboard_bar_routes_only_the_active_visible_screen(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            log = app.query_one(SelectableRichLog)
            log.clear()
            log.write("backdrop-left COVERED_BACKGROUND")
            await pilot.pause()
            app.action_copy_view()
            self.assertIn("COVERED_BACKGROUND", app.clipboard)
            await app.push_screen(SyntheticModal())
            await pilot.pause()
            self.assertEqual(len(app.screen.query(ClipboardButton)), 3)
            for button in app.screen.query(ClipboardButton):
                self.assertTrue(button.tooltip)
                self.assertFalse(button.allow_select)
            await self.click(app, pilot, app.screen.query_one("#copy-view"))
            self.assertIn("Synthetic modal message", app.clipboard)
            self.assertIn("Paste copied", app.clipboard)
            self.assertNotIn("COVERED_BACKGROUND", app.clipboard)
            self.assertNotIn("HIDDEN_WIDGET_DATA", app.clipboard)
            field = app.screen.query_one("#modal-input", Input)
            field.focus()
            await pilot.pause()
            field.selection = (0, 5)
            app._clipboard = "pasted"
            await self.click(app, pilot, app.screen.query_one("#paste-copied"))
            self.assertEqual(field.value, "pasted input")
            self.assertIs(app.focused, field)

    async def test_copy_limits_are_utf8_atomic_and_errors_have_fixed_notices(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)):
            app.action_copy_selection()
            self.assertEqual(app.copies, [])
            boundary = "é" * (MAX_CLIPBOARD_BYTES // 2)
            app.action_copy_selection(boundary)
            self.assertEqual(app.clipboard, boundary)
            self.assertEqual(len(app.copies), 1)
            for rejected in (
                boundary + "é",
                "x" * (MAX_CLIPBOARD_BYTES + 1),
                "\ud800",
                True,
            ):
                app.action_copy_selection(rejected)
                self.assertEqual(app.clipboard, boundary)
                self.assertEqual(len(app.copies), 1)
            with patch.object(
                app.screen._compositor,
                "render_strips",
                side_effect=RuntimeError("PRIVATE_RENDER_DETAIL"),
            ):
                app.action_copy_view()
            with patch.object(
                app,
                "copy_to_clipboard",
                side_effect=RuntimeError("PRIVATE_DRIVER_DETAIL"),
            ):
                app.action_copy_selection("PRIVATE_COPIED_TEXT")
            with patch.object(
                app.screen,
                "get_selected_text",
                side_effect=RuntimeError("PRIVATE_SELECTION_DETAIL"),
            ):
                app.action_copy_selection()
            self.assertIn("Visible screen copy is unavailable.", app.notices)
            self.assertIn("Clipboard copy is unavailable.", app.notices)
            self.assertIn("Selected text copy is unavailable.", app.notices)
            self.assertTrue(all("PRIVATE" not in notice for notice in app.notices))

    async def test_read_only_search_paste_replaces_selection_and_keeps_builtin_keys(
        self,
    ):
        app = ClipboardFixtureApp(read_only=True)
        async with app.run_test(size=(80, 36)) as pilot:
            search = app.query_one("#search", Input)
            search.selection = (0, 3)
            app._clipboard = "paste 界\nnot a second input line"
            app.action_paste_copied_text()
            await pilot.pause()
            self.assertEqual(search.value, "paste 界 searchable")
            self.assertNotIn("\n", search.value)
            search.selection = (0, 5)
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, "paste")
            search.selection = (0, len(search.value))
            await pilot.press("ctrl+v")
            self.assertEqual(search.value, "paste")
            app.post_message(events.Paste("external synthetic 界\nignored"))
            await pilot.pause()
            self.assertIn("external synthetic 界", search.value)
            self.assertEqual(app.clipboard, "paste")

    async def test_paste_capability_connection_and_read_only_checks_never_start_clients(
        self,
    ):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            terminal = app.query_one(SyntheticTerminal)
            terminal.focus()
            await pilot.pause()
            app._clipboard = "synthetic 界\nsecond"
            await pilot.press("ctrl+shift+insert")
            await pilot.pause()
            self.assertEqual(terminal.pastes, ["synthetic 界\nsecond"])
            await pilot.press("ctrl+c", "ctrl+v")
            self.assertEqual(terminal.keys[-2:], ["ctrl+c", "ctrl+v"])
            for read_only, state, capability in (
                (True, "connected", True),
                (False, "disconnected", True),
                (False, "connected", False),
            ):
                app.read_only, terminal.state = read_only, state
                terminal._accepts_clipboard_paste = capability
                app.action_paste_copied_text()
                await pilot.pause()
                self.assertEqual(len(terminal.pastes), 1)
            self.assertEqual(app.copies, [])

    async def test_builtin_input_copy_and_cut_enforce_utf8_limit_without_deletion(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            search = app.query_one("#search", Input)
            search.value = "界" * 11
            search.selection = (0, len(search.value))
            app._clipboard = "previous copy"
            with patch("oc_box.tui_clipboard.MAX_CLIPBOARD_BYTES", 32):
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual(app.clipboard, "previous copy")
                self.assertEqual(app.copies, [])
                await pilot.press("ctrl+x")
                await pilot.pause()
                self.assertEqual(search.value, "界" * 11)
                self.assertEqual(app.clipboard, "previous copy")
                self.assertEqual(app.copies, [])
                search.value = "ok界"
                search.selection = (0, len(search.value))
                await pilot.press("ctrl+x")
                self.assertEqual(app.clipboard, "ok界")
                self.assertEqual(search.value, "")
                search.value = "keep this selection"
                search.selection = (0, len(search.value))
                with patch.object(
                    app,
                    "_write_clipboard",
                    side_effect=RuntimeError("PRIVATE_DRIVER_DETAIL"),
                ):
                    await pilot.press("ctrl+x")
                self.assertEqual(search.value, "keep this selection")
                self.assertTrue(all("PRIVATE" not in notice for notice in app.notices))

    async def test_paste_rejects_empty_oversize_disabled_and_non_input_targets(self):
        app = ClipboardFixtureApp()
        async with app.run_test(size=(80, 36)) as pilot:
            search = app.query_one("#search", Input)
            before = search.value
            for cached in ("", False, "é" * (MAX_CLIPBOARD_BYTES // 2 + 1)):
                app._clipboard = cached
                app.action_paste_copied_text()
                await pilot.pause()
                self.assertEqual(search.value, before)
            app._clipboard = "must not insert"
            search.disabled = True
            app.action_paste_copied_text()
            await pilot.pause()
            self.assertEqual(search.value, before)
            app.query_one(Select).focus()
            await pilot.pause()
            app.action_paste_copied_text()
            await pilot.pause()
            self.assertEqual(app.query_one(Select).value, "visible")
            self.assertEqual(app.copies, [])
            self.assertTrue(
                all("must not insert" not in notice for notice in app.notices)
            )


if __name__ == "__main__":
    unittest.main()
