"""Real dashboard gestures with a local clipboard double, never a desktop driver.

These are synthetic headless regressions, not interactive host acceptance. Mouse
events enter App.on_event so Textual's native Click synthesis is exercised too.
"""

from contextlib import asynccontextmanager
import importlib.util
import re
import unittest
from unittest.mock import patch

from oc_box.tui import DemoBackend
from oc_box.tui_logs import LogEntry

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from rich.cells import cell_len
    from textual import events
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import Collapsible, Input, Select, Switch, TabbedContent

    from oc_box.tui_app import Confirm, DashboardApp
    from oc_box.tui_settings import SettingsScreen


class SelectionDemo(DemoBackend):
    """Only fixed sample data; counts refreshes without any real collectors."""

    def __init__(self):
        self.samples = 0
        self.entries = super().logs()

    def collect(self, *, scan_storage=False):
        self.samples += 1
        return super().collect(scan_storage=scan_storage)

    def logs(self, workspace=None):
        return list(self.entries)


@unittest.skipUnless(AVAILABLE, "optional Textual/pyte dependencies are not installed")
class DashboardSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    @asynccontextmanager
    async def dashboard(self, *, size=(120, 40)):
        backend = SelectionDemo()
        app = DashboardApp(backend, read_only=True, demo=True)
        copies = []

        def write(text):
            # Keep ClipboardActions.copy_to_clipboard's byte/error guards. Only
            # its final driver write is replaced, including Textual's local cache.
            app._clipboard = text
            copies.append(text)

        with (
            patch.object(app, "_write_clipboard", side_effect=write),
            patch.object(app, "notify"),
        ):
            async with app.run_test(size=size) as pilot:
                await self.settle(app, pilot)
                app._poll_timer.pause()
                await self.settle(app, pilot)
                self.assertEqual(copies, [], "mount/initial refresh must not copy")
                yield app, pilot, copies, backend

    def visible_span(self, app, widget, text=None, *, pattern=None):
        """Locate actual rendered cells, not guessed layout or hidden model text."""
        region = widget.region.intersection(app.screen.region)
        strips = app.screen._compositor.render_strips()
        for y in range(region.y, region.bottom):
            row = strips[y].crop(region.x, region.right).text
            match = re.search(pattern or re.escape(text), row)
            if match is not None:
                x = region.x + cell_len(row[: match.start()])
                value = match.group()
                return (x, y), (x + cell_len(value) - 1, y), value
        self.fail(f"No visible {text or pattern!r} in {type(widget).__name__}")

    async def mouse(self, app, pilot, kind, point, *, button=1, when=None):
        x, y = point
        widget, _ = app.screen.get_widget_at(x, y)
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
        if when is not None:
            event.time = when
        await app.on_event(event)
        await pilot.pause()

    async def drag(self, app, pilot, copies, span):
        start, end, _ = span
        before = list(copies)
        await self.mouse(app, pilot, events.MouseDown, start)
        self.assertTrue(
            app.is_running, "selecting a control label must not exit the app"
        )
        self.assertEqual(copies, before, "mouse-down is not a completed selection")
        await self.mouse(app, pilot, events.MouseMove, end)
        self.assertEqual(copies, before, "dragging must not write the clipboard")
        await self.mouse(app, pilot, events.MouseUp, end)
        return before

    async def click(self, app, pilot, widget, text):
        start, _, _ = self.visible_span(app, widget, text)
        await self.mouse(app, pilot, events.MouseDown, start)
        await self.mouse(app, pilot, events.MouseUp, start)

    def assert_copied(self, app, copies, before, expected):
        self.assertEqual(
            copies,
            before + [expected],
            "a completed user selection must copy exactly once, without Copy",
        )
        self.assertEqual(app.clipboard, expected)

    def assert_highlight(self, app, span):
        start, end, _ = span
        self.assertEqual(start[1], end[1])
        strip = app.screen._compositor.render_strips()[start[1]].crop(
            start[0], end[0] + 1
        )
        selected = [segment for segment in strip if segment.text.strip()]
        self.assertTrue(selected, "the selected visible cells must contain text/glyphs")
        background = app.screen.get_component_rich_style("screen--selection").bgcolor
        self.assertIsNotNone(background)
        for segment in selected:
            self.assertIsNotNone(segment.style)
            self.assertEqual(
                segment.style.bgcolor,
                background,
                "selection must override opaque log/table/meter backgrounds",
            )
            self.assertIsNotNone(segment.style.color)
            self.assertNotEqual(
                segment.style.color,
                segment.style.bgcolor,
                "selection must not make foreground and background identical",
            )

    async def test_real_log_drag_highlights_and_copies_at_both_dashboard_sizes(self):
        for size in ((120, 40), (80, 24)):
            with self.subTest(size=size):
                async with self.dashboard(size=size) as (app, pilot, copies, backend):
                    # The normal tab binding must work, not only direct widget access.
                    await pilot.press("5")
                    self.assertEqual(app.query_one(TabbedContent).active, "logs")
                    log = app.query_one("#log-output")
                    span = self.visible_span(app, log, "2026-01-01T12:00:00Z")
                    original = app.screen._compositor.render_strips()[span[0][1]]
                    self.assertTrue(
                        any(
                            segment.style and segment.style.bgcolor
                            for segment in original
                        ),
                        "exercise the actual opaque RichLog renderer",
                    )
                    before = await self.drag(app, pilot, copies, span)
                    self.assertEqual(app.screen.get_selected_text(), span[2])
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, span[2])

                    copied = list(copies)
                    self.assertTrue(app.query_one("#log-follow", Switch).value)
                    backend.entries.append(
                        LogEntry(
                            "2026-01-01T12:00:02Z",
                            "selection-fixture",
                            "info",
                            "new followed sample",
                        )
                    )
                    app.refresh_logs()
                    await self.settle(app, pilot)
                    self.assertTrue(app.logs.search("new followed sample"))
                    self.assertEqual(copies, copied, "follow refresh must not re-copy")
                    self.assertEqual(app.clipboard, span[2])
                    self.assertEqual(app.screen.get_selected_text(), span[2])
                    self.assert_highlight(app, span)

    async def test_table_meter_bars_and_history_have_selectable_plain_text(self):
        async with self.dashboard() as (app, pilot, copies, _):
            surfaces = (
                ("table", "#workspaces", "example", None),
                ("workspace RAM", "#full-diagnostics", None, r"[█░]{4,}"),
                ("system RAM", "#system-report", None, r"[█░]{4,}"),
                ("history", "#system-report", None, r"CPU [▁▂▃▄▅▆▇█]+"),
            )
            for name, selector, text, pattern in surfaces:
                with self.subTest(surface=name):
                    app.screen.clear_selection()
                    widget = app.query_one(selector)
                    span = self.visible_span(app, widget, text, pattern=pattern)
                    if name.endswith("RAM"):
                        self.assertIn("█", span[2])
                        self.assertIn("░", span[2])
                    before = await self.drag(app, pilot, copies, span)
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, span[2])

    async def test_panel_and_settings_captions_copy_their_actual_title_text(self):
        async with self.dashboard() as (app, pilot, copies, _):
            for selector, title in (
                ("#full-diagnostics", "Full workspace diagnostics"),
                ("#system-report", "System (OS-reported)"),
            ):
                with self.subTest(caption=title):
                    span = self.visible_span(app, app.query_one(selector), title)
                    before = await self.drag(app, pilot, copies, span)
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, title)
            app.action_settings()
            await pilot.pause()
            modal = app.screen
            for title in ("Monitoring", "Resources · information only"):
                with self.subTest(caption=title):
                    label = next(
                        widget
                        for widget in modal.query(".settings-topic-title")
                        if str(widget.render()) == title
                    )
                    label.scroll_visible(animate=False)
                    await pilot.pause()
                    span = self.visible_span(app, label, title)
                    before = await self.drag(app, pilot, copies, span)
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, title)
                    self.assertIs(app.screen, modal)

    async def test_fresh_widget_selection_beats_stale_input_and_keyboard_copy_is_bounded(
        self,
    ):
        async with self.dashboard() as (app, pilot, copies, _):
            field = app.query_one("#log-search", Input)
            field.value = "previous input selection"
            field.focus()
            field.selection = (0, 8)
            await pilot.pause()
            self.assertEqual(copies, [], "programmatic Input selection must not copy")

            with self.subTest(gesture="fresh static selection"):
                span = self.visible_span(app, app.query_one("#log-hits"), "0 matches")
                before = await self.drag(app, pilot, copies, span)
                self.assertEqual(app.screen.get_selected_text(), "0 matches")
                self.assert_copied(app, copies, before, "0 matches")

            with self.subTest(gesture="real select-all key"):
                app.screen.clear_selection()
                field.focus()
                field.selection = (0, 0)
                await pilot.pause()
                before = list(copies)
                await pilot.press("ctrl+a")
                self.assertEqual(field.selected_text, field.value)
                self.assert_copied(app, copies, before, field.value)

            with self.subTest(gesture="UTF-8 bound and recovery"):
                # 17 code points, but 34 UTF-8 bytes: exercise bytes, not len().
                app.screen.clear_selection()
                field.value = "é" * 17
                field.selection = (0, 0)
                field.focus()
                app._clipboard = "previous cached copy"
                await pilot.pause()
                before = list(copies)
                with patch("oc_box.tui_clipboard.MAX_CLIPBOARD_BYTES", 32):
                    await pilot.press("ctrl+a")
                    self.assertEqual(field.selected_text, "é" * 17)
                    self.assertEqual(field.value, "é" * 17)
                    self.assertEqual(copies, before)
                    self.assertEqual(app.clipboard, "previous cached copy")

                    field.value = "é" * 16
                    field.selection = (0, 0)
                    await pilot.pause()
                    self.assertEqual(copies, before, "value changes must not copy")
                    await pilot.press("ctrl+a")
                    self.assertEqual(field.selected_text, "é" * 16)
                    self.assert_copied(app, copies, before, "é" * 16)

    async def test_control_label_drags_do_not_activate_and_ordinary_clicks_still_do(
        self,
    ):
        for control in ("button", "tab", "select", "collapsible", "follow label"):
            with self.subTest(control=control):
                async with self.dashboard() as (app, pilot, copies, _):
                    tabs = app.query_one(TabbedContent)
                    if control == "button":
                        widget = app.query_one("#show-logs")
                        text = "Logs"
                    elif control == "tab":
                        widget = app.query_one("#--content-tab-settings")
                        text = "Settings"
                    elif control == "collapsible":
                        widget = app.query_one("#workspace-details CollapsibleTitle")
                        text = "Workspace details"
                    else:
                        await pilot.press("5")
                        if control == "select":
                            widget = app.query_one("#log-source SelectCurrent")
                            text = "All sources"
                        else:
                            widget = next(
                                label
                                for label in app.query("#logs Label")
                                if "Follow" in str(label.render())
                            )
                            text = "Follow"
                    span = self.visible_span(app, widget, text)
                    before = await self.drag(app, pilot, copies, span)

                    if control in ("button", "tab"):
                        self.assertEqual(
                            tabs.active, "status", "drag must not switch tabs"
                        )
                    elif control == "select":
                        select = app.query_one("#log-source", Select)
                        self.assertEqual(select.value, "all")
                        self.assertFalse(
                            select.expanded, "drag must not open the selector"
                        )
                    elif control == "collapsible":
                        self.assertTrue(app.query_one(Collapsible).collapsed)
                    else:
                        self.assertTrue(app.query_one("#log-follow", Switch).value)
                        self.assertTrue(
                            app.query_one("#log-follow", Switch).allow_select
                        )
                    self.assertTrue(widget.allow_select)
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, text)

                    if control == "follow label":
                        continue
                    copied = list(copies)
                    await self.click(app, pilot, widget, text)
                    if control == "button":
                        self.assertEqual(tabs.active, "logs")
                    elif control == "tab":
                        self.assertEqual(tabs.active, "settings")
                    elif control == "select":
                        self.assertTrue(select.expanded)
                        await pilot.press("escape")
                    else:
                        self.assertFalse(app.query_one(Collapsible).collapsed)
                    self.assertEqual(
                        copies, copied, "an ordinary click is not a text drag"
                    )

    async def test_settings_action_label_drags_neither_reset_nor_apply(self):
        for action, text in (
            ("preferences-reset", "Reset to defaults"),
            ("preferences-apply", "Apply"),
        ):
            with self.subTest(action=action):
                async with self.dashboard() as (app, pilot, copies, _):
                    app.action_settings()
                    await pilot.pause()
                    modal = app.screen
                    self.assertIsInstance(modal, SettingsScreen)
                    modal.query_one("#pref-interval", Select).value = 5
                    await pilot.pause()
                    self.assertEqual(copies, [])
                    widget = modal.query_one("#" + action)
                    span = self.visible_span(app, widget, text)
                    before = await self.drag(app, pilot, copies, span)
                    self.assertIs(app.screen, modal, "drag must not Apply/dismiss")
                    self.assertEqual(modal.query_one("#pref-interval", Select).value, 5)
                    self.assertEqual(
                        app.query_one("#refresh-interval", Select).value, 1
                    )
                    self.assertTrue(app.read_only)
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, text)
                    await self.click(
                        app, pilot, modal.query_one("#preferences-cancel"), "Cancel"
                    )
                    self.assertNotIsInstance(app.screen, SettingsScreen)
                    self.assertEqual(
                        app.query_one("#refresh-interval", Select).value, 1
                    )

    async def test_dropdown_option_text_drag_copies_without_selecting_option(self):
        async with self.dashboard() as (app, pilot, copies, _):
            await pilot.press("5")
            selector = app.query_one("#log-source", Select)
            current = selector.query_one("SelectCurrent")
            await self.click(app, pilot, current, "All sources")
            self.assertTrue(selector.expanded)
            overlay = selector.query_one("SelectOverlay")
            span = self.visible_span(app, overlay, "oc-box activity")
            before = await self.drag(app, pilot, copies, span)
            self.assertTrue(selector.expanded)
            self.assertEqual(selector.value, "all")
            self.assert_highlight(app, span)
            self.assert_copied(app, copies, before, "oc-box activity")
            await pilot.press("escape")
            self.assertEqual(selector.value, "all")

    async def test_native_terminal_visible_unicode_output_copies_on_user_release(self):
        async with self.dashboard() as (app, pilot, copies, _):
            app.action_tab("terminals")
            await pilot.pause()
            terminal = app.query_one("#native-terminal")
            terminal.emulator.feed("\x1b[37;44mNative 界e\u0301 output\x1b[0m".encode())
            terminal.refresh()
            await pilot.pause()
            self.assertEqual(copies, [], "native output alone must not copy")
            span = self.visible_span(app, terminal, "Native 界é")
            before = await self.drag(app, pilot, copies, span)
            self.assert_highlight(app, span)
            self.assert_copied(app, copies, before, "Native 界é")

    async def test_typed_delete_review_and_confirm_label_drags_never_confirm(self):
        async with self.dashboard() as (app, pilot, copies, _):
            results = []
            modal = Confirm("Review only: synthetic DELETE target", destructive=True)
            await app.push_screen(modal, results.append)
            modal.query_one("#confirmation", Input).value = "DELETE"
            await pilot.pause()
            self.assertEqual(copies, [])

            for selector, text in (
                ("#confirmation-review Static", "synthetic DELETE target"),
                ("#confirm", "Confirm"),
            ):
                with self.subTest(surface=selector):
                    widget = modal.query_one(selector)
                    span = self.visible_span(app, widget, text)
                    before = await self.drag(app, pilot, copies, span)
                    self.assertIs(
                        app.screen, modal, "text drag must not confirm DELETE"
                    )
                    self.assertEqual(results, [])
                    self.assert_highlight(app, span)
                    self.assert_copied(app, copies, before, text)

            if app.screen is modal:
                # No backend operation is attached: only this synthetic callback.
                await self.click(app, pilot, modal.query_one("#confirm"), "Confirm")
                self.assertEqual(results, [True], "a deliberate click must still work")

    async def test_footer_quit_label_is_selectable_without_exit_and_refresh_still_works(
        self,
    ):
        async with self.dashboard() as (app, pilot, copies, backend):
            footer = app.query_one("Footer")
            quit_key = next(key for key in footer.query("FooterKey") if key.key == "q")
            self.assertTrue(
                quit_key.allow_select, "Quit must not be a selection barrier"
            )
            span = self.visible_span(app, quit_key, "Quit")
            before = await self.drag(app, pilot, copies, span)
            self.assertTrue(
                app.is_running, "footer must not execute Quit on mouse-down"
            )
            self.assert_highlight(app, span)
            self.assert_copied(app, copies, before, "Quit")

            refresh = next(key for key in footer.query("FooterKey") if key.key == "r")
            copied, samples = list(copies), backend.samples
            await self.click(app, pilot, refresh, "Refresh")
            await self.settle(app, pilot)
            self.assertGreater(backend.samples, samples)
            samples = backend.samples
            await pilot.press("r")
            await self.settle(app, pilot)
            self.assertGreater(backend.samples, samples)
            self.assertEqual(
                copies, copied, "refresh/click/key must not re-copy selection"
            )

    async def test_programmatic_selection_focus_refresh_and_container_osc_never_copy(
        self,
    ):
        async with self.dashboard() as (app, pilot, copies, backend):
            app._clipboard = "retained synthetic copy"
            app.action_tab("logs")
            await pilot.pause()
            log = app.query_one("#log-output")
            app.screen.selections = {log: Selection(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()
            self.assertTrue(app.screen.get_selected_text())
            self.assertEqual(copies, [], "programmatic Screen.selections must not copy")

            field = app.query_one("#log-search", Input)
            field.value = "programmatically assigned text"
            field.selection = (0, 8)
            field.focus()
            await pilot.pause()
            self.assertEqual(copies, [], "value/selection/focus must not copy")

            app.screen.clear_selection()
            field.value = ""
            backend.entries.append(
                LogEntry(
                    "2026-01-01T12:00:03Z",
                    "selection-fixture",
                    "info",
                    "passive sample",
                )
            )
            app.refresh_logs()
            app.refresh_report()
            await self.settle(app, pilot)
            self.assertTrue(app.logs.search("passive sample"))
            self.assertEqual(copies, [], "changed log/report refresh must not copy")

            terminal = app.query_one("#native-terminal")
            terminal.emulator.feed(
                b"visible output\x1b]52;c;Tk9UX0FfQ0xJUEJPQVJEX1dSSVRF\x07 remains"
            )
            terminal.refresh()
            await pilot.pause()
            rendered = "\n".join(terminal.emulator.screen.display)
            self.assertIn("visible output remains", rendered)
            self.assertNotIn("Tk9UX0FfQ0xJUEJPQVJEX1dSSVRF", rendered)
            self.assertEqual(
                copies, [], "container OSC 52 must never reach the clipboard"
            )
            self.assertEqual(app.clipboard, "retained synthetic copy")

    async def test_screen_change_cancels_pending_drag_and_late_mouse_up_cannot_copy(
        self,
    ):
        async with self.dashboard() as (app, pilot, copies, _):
            span = self.visible_span(app, app.query_one("#host-summary"), "oc-box")
            await self.mouse(app, pilot, events.MouseDown, span[0])
            await self.mouse(app, pilot, events.MouseMove, span[1])
            self.assertTrue(app.screen.get_selected_text())
            self.assertEqual(copies, [], "unfinished gesture must not copy")

            modal = Confirm("A new screen cancels the previous selection gesture")
            await app.push_screen(modal)
            await pilot.pause()
            await self.mouse(app, pilot, events.MouseUp, span[1])
            self.assertIs(app.screen, modal)
            self.assertEqual(
                copies, [], "new screen must not receive the old selection"
            )
            app.pop_screen()
            await pilot.pause()
            await self.mouse(app, pilot, events.MouseUp, span[1])
            self.assertEqual(
                copies, [], "stray mouse-up must not revive a canceled gesture"
            )

    async def test_modal_roundtrip_before_mouse_up_disarms_the_original_action(self):
        for route in ("push/pop", "F2/Escape"):
            with self.subTest(route=route):
                async with self.dashboard() as (app, pilot, copies, _):
                    span = self.visible_span(app, app.query_one("#show-logs"), "Logs")
                    await self.mouse(app, pilot, events.MouseDown, span[0])
                    await self.mouse(app, pilot, events.MouseMove, span[1])
                    self.assertEqual(copies, [])
                    if route == "push/pop":
                        await app.push_screen(Confirm("Interrupt an unfinished drag"))
                        await pilot.pause()
                        app.pop_screen()
                        await pilot.pause()
                    else:
                        await pilot.press("f2")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, SettingsScreen)
                        await pilot.press("escape")
                        await pilot.pause()
                    await self.mouse(app, pilot, events.MouseUp, span[1])
                    self.assertEqual(app.query_one(TabbedContent).active, "status")
                    self.assertEqual(copies, [])

    async def test_input_double_triple_click_and_existing_select_all_key_auto_copy(
        self,
    ):
        import time

        async with self.dashboard() as (app, pilot, copies, _):
            field = app.query_one("#log-search", Input)
            field.value = "select this input"
            field.focus()
            field.selection = (0, 0)
            await pilot.pause()
            point = self.visible_span(app, field, "select this input")[0]
            clock = time.monotonic()
            for index in range(2):
                await self.mouse(
                    app, pilot, events.MouseDown, point, when=clock + index * 0.1
                )
                await self.mouse(
                    app, pilot, events.MouseUp, point, when=clock + index * 0.1 + 0.02
                )
            self.assertEqual(copies, [field.value])
            before = list(copies)
            await self.mouse(app, pilot, events.MouseDown, point, when=clock + 0.2)
            await self.mouse(app, pilot, events.MouseUp, point, when=clock + 0.22)
            self.assertTrue(app.screen.get_selected_text())
            self.assert_copied(app, copies, before, app.screen.get_selected_text())

            app.screen.clear_selection()
            field.selection = (0, 0)
            before = list(copies)
            await pilot.press("ctrl+shift+a")
            self.assertEqual(field.selected_text, field.value)
            self.assert_copied(app, copies, before, field.value)

            before = list(copies)
            await pilot.press("shift+left")
            self.assertTrue(field.selected_text)
            self.assert_copied(app, copies, before, field.selected_text)

    async def test_repeated_input_select_all_clears_stale_screen_copy_candidate(self):
        async with self.dashboard() as (app, pilot, copies, _):
            field = app.query_one("#log-search", Input)
            field.value = "already selected input"
            field.focus()
            field.action_select_all()
            await pilot.pause()
            self.assertEqual(copies, [])
            span = self.visible_span(app, app.query_one("#log-hits"), "0 matches")
            await self.drag(app, pilot, copies, span)
            self.assertEqual(field.selected_text, field.value)
            self.assertEqual(app.clipboard, "0 matches")
            before = list(copies)
            await pilot.press("ctrl+a")
            self.assert_copied(app, copies, before, field.value)
            self.assertEqual(field.selected_text, field.value)
            self.assertIsNone(app.screen.get_selected_text())
            await self.click(app, pilot, app.query_one("#copy-selection"), "Copy")
            self.assertEqual(copies[-2:], [field.value, field.value])
            self.assertEqual(field.selected_text, field.value)


if __name__ == "__main__":
    unittest.main()
