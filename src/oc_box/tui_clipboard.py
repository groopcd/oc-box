"""User-gesture clipboard selection for the optional Textual 8.2.x UI.

Selection copies only a custom widget's current visible viewport, not its
offscreen model or history. Copy view reads the active screen's rendered strips,
including control labels. Completed user selections and explicit copy actions
use Textual's best-effort OSC 52 writer, never refresh/programmatic selection.
Paste copied text uses Textual's local cache, never reads the desktop clipboard,
and goes through the recipient's existing Paste event handler.
"""

from types import MethodType

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.content import Content
from textual.containers import Horizontal
from textual.errors import NoWidget
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Input, RichLog
from textual.widgets._footer import FooterKey

MAX_CLIPBOARD_BYTES = 1024 * 1024


def _fits_clipboard(text):
    if not isinstance(text, str) or len(text) > MAX_CLIPBOARD_BYTES:
        return False
    try:
        return len(text.encode("utf-8")) <= MAX_CLIPBOARD_BYTES
    except UnicodeError:
        return False


def _render_native_selection(widget, y):
    """Highlight normal Textual text without losing its logical wrap offsets."""
    strip = widget._clipboard_native_render_line(y)
    selection = widget.text_selection
    if selection is None:
        return strip
    background = Style(
        bgcolor=widget.screen.get_component_rich_style("screen--selection").bgcolor,
        reverse=False,
    )
    segments = []
    for segment in strip:
        style = segment.style or Style()
        offset = style.meta.get("offset")
        if offset is None or (span := selection.get_span(offset[1])) is None:
            segments.append(segment)
            continue
        x, row = offset
        start, end = span
        left = max(0, min(len(segment.text), start - x))
        right = (
            len(segment.text) if end < 0 else max(left, min(len(segment.text), end - x))
        )
        if left == right:
            segments.append(segment)
            continue
        if left:
            segments.append(Segment(segment.text[:left], style))
        segments.append(
            Segment(
                segment.text[left:right],
                style + background + Style.from_meta({"offset": (x + left, row)}),
            )
        )
        if right < len(segment.text):
            segments.append(
                Segment(
                    segment.text[right:],
                    style + Style.from_meta({"offset": (x + right, row)}),
                )
            )
    return Strip(segments, strip.cell_length)


class SelectableViewport:
    """Selection adapter for literal-strip widgets, in viewport coordinates.

    Implement ``_selection_line(y)`` for a custom renderer. The default retains
    DataTable/RichLog rendering, scrolling, fixed headers and cursor styles.
    Selection offsets are character indexes; Strip cropping uses cell columns.
    Scrollbars and content hidden beyond the viewport are never extracted.
    """

    ALLOW_SELECT = True

    def _selection_line(self, y):
        return super().render_line(y)

    def _selection_viewport_line(self, y):
        width, height = self.scrollable_content_region.size
        if not 0 <= y < height:
            return Strip.blank(width)
        return self._selection_line(y).crop(0, width)

    def render_line(self, y):
        strip = self._selection_viewport_line(y)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            start, end = span
            length = len(strip.text)
            start = max(0, min(start, length))
            end = length if end < 0 else max(start, min(end, length))
            if start != end:
                left = strip.index_to_cell_position(start)
                right = strip.index_to_cell_position(end)
                style = self.screen.get_component_rich_style("screen--selection")
                # apply_style puts an existing segment background last. That
                # makes selection invisible on logs, row cursors and meters.
                # Override only the background, not the theme's potentially
                # identical selection foreground and background colors.
                selected = Strip(
                    Segment.apply_style(
                        strip.crop(left, right),
                        post_style=Style(bgcolor=style.bgcolor, reverse=False),
                    )
                )
                strip = Strip.join(
                    (
                        strip.crop(0, left),
                        selected,
                        strip.crop(right),
                    )
                )
        return strip.apply_offsets(0, y)

    def get_selection(self, selection):
        height = self.scrollable_content_region.height
        text = "\n".join(self._selection_viewport_line(y).text for y in range(height))
        try:
            return selection.extract(text), "\n"
        except IndexError:
            # A resize or cleared log can invalidate the last selected row.
            return "", "\n"


class SelectableDataTable(SelectableViewport, DataTable):
    """DataTable with visible-text selection, without changing row actions."""


class SelectableRichLog(SelectableViewport, RichLog):
    """RichLog with visible-text selection, without retaining another log."""


class ClipboardActions:
    """App mixin: completed user selections and scoped local-cache pastes.

    No bindings are installed here. In particular ordinary Ctrl-C/Ctrl-V remain
    the focused input/terminal's keys. Native terminals must explicitly opt in
    with ``_accepts_clipboard_paste = True``; this never attaches a client.
    """

    def _clipboard_private(self, widget=None):
        """Check trusted privacy markers without inspecting editor contents.

        The workflow's app-level shield covers submission and screen-transition
        gaps. A private screen also fences every action. On ordinary screens,
        the focused widget (or an explicit selection origin) inherits the fence
        from any marked ancestor. Private editors need not inherit Input.
        """
        if getattr(self, "_private_interaction", False):
            return True
        if not self.screen_stack:
            return False
        if getattr(self.screen, "PRIVATE_INPUT", False) is True:
            return True
        candidate = self.focused if widget is None else widget
        while candidate is not None:
            if getattr(candidate, "PRIVATE_INPUT", False) is True:
                return True
            candidate = candidate.parent
        return False

    def _clipboard_private_selection(self):
        return self._clipboard_private() or any(
            self._clipboard_private(widget) for widget in self.screen.selections
        )

    def enable_text_selection(self, screen=None):
        screen = self.screen if screen is None else screen
        previous = getattr(self, "_selection_adapted_screen", None)
        if previous is not None and previous is not screen:
            self._cancel_selection_gesture()
        if getattr(screen, "PRIVATE_INPUT", False) is True:
            screen.ALLOW_SELECT = False
        for widget in screen.walk_children(Widget):
            if self._clipboard_private(widget):
                # Never render/adapt private controls, including descendants
                # that do not carry the marker themselves.
                widget.ALLOW_SELECT = False
                continue
            if widget.has_class("-textual-system"):
                # Scrollbar drags must continue to scroll, not select content.
                continue
            widget.ALLOW_SELECT = True
            if isinstance(widget, SelectableViewport) or getattr(
                widget, "_clipboard_selection_adapter", False
            ):
                continue
            native_line = (
                getattr(widget.render_line, "__func__", None) is Widget.render_line
            )
            if native_line and widget.is_container:
                widget._clipboard_selection_adapter = True
                continue
            if (
                native_line
                and not isinstance(widget, Button)
                and isinstance(widget._render(), (Text, Content))
            ):
                # Standard text already has correct logical line/wrap offsets
                # and extraction, including meaningful trailing spaces.
                widget._clipboard_native_render_line = widget.render_line
                widget.render_line = MethodType(_render_native_selection, widget)
            else:
                # Custom renderers (e.g. dropdown options) need viewport
                # offsets/extraction in addition to the highlight overlay.
                widget._selection_line = widget.render_line
                for name in (
                    "_selection_viewport_line",
                    "render_line",
                    "get_selection",
                ):
                    setattr(
                        widget,
                        name,
                        MethodType(getattr(SelectableViewport, name), widget),
                    )
            widget._clipboard_selection_adapter = True
            widget.refresh()
        self._selection_adapted_screen = screen

    def on_mount(self):
        self.enable_text_selection()

    def on_idle(self):
        # A queued Idle may arrive after the final screen was removed. Native
        # TTY handoff/refresh teardown must not turn it into a fatal-error loop.
        if not self.is_running or not self.is_mounted or not self.screen_stack:
            return
        if getattr(self, "_selection_adapted_screen", None) is not self.screen:
            self.enable_text_selection()

    def _cancel_selection_gesture(self):
        # A screen can be pushed and popped before mouse-up. Disarm Textual's
        # pending synthetic click too, not only our clipboard gesture tracker.
        gesture = getattr(self, "_selection_gesture", None)
        if gesture is not None and gesture[0].is_attached:
            gesture[0].clear_selection()
        self._mouse_down_widget = None
        self._selection_gesture = None
        self._selection_dragged = False
        self._selection_keyboard_events = {}

    def on_unmount(self):
        self._cancel_selection_gesture()

    async def on_event(self, event):
        if self._clipboard_private():
            self._cancel_selection_gesture()
            if isinstance(event, events.Key) and event.key in {
                "ctrl+c",
                "super+c",
                "ctrl+x",
                "super+x",
                "ctrl+v",
                "super+v",
                "ctrl+a",
                "ctrl+shift+a",
                "super+a",
                "ctrl+insert",
                "shift+delete",
                "shift+insert",
                "ctrl+shift+c",
                "ctrl+shift+y",
                "ctrl+shift+insert",
                "ctrl+shift+v",
            }:
                # Fence native Screen/Input bindings as well as our actions,
                # before they can inspect selections or the clipboard cache.
                if event.key == "ctrl+c" and not event.is_forwarded:
                    # A trusted private workflow may cancel, but never copy.
                    cancel = getattr(self.screen, "action_cancel_private_input", None)
                    if callable(cancel):
                        cancel()
                event.stop()
                event.prevent_default()
                return
            await super().on_event(event)
            return
        completed = None
        keyboard_input = None
        forwarded = event.is_forwarded
        pending_keys = getattr(self, "_selection_keyboard_events", None)
        if pending_keys is None:
            pending_keys = self._selection_keyboard_events = {}
        if forwarded and isinstance(event, events.Key):
            pending = pending_keys.pop(id(event), None)
            if pending is not None and pending[0] is event:
                keyboard_input = pending[1:]
        if not forwarded:
            if isinstance(event, events.MouseDown) and event.button == 1:
                self.enable_text_selection()
                try:
                    widget, _ = self.screen.get_widget_at(*event.screen_offset)
                except NoWidget:
                    widget = None
                self._selection_gesture = (
                    (self.screen, event.screen_offset, widget)
                    if widget is not None and not widget.has_class("-textual-system")
                    else None
                )
                self._selection_dragged = False
            elif (
                isinstance(event, events.MouseMove)
                and getattr(self, "_selection_gesture", None) is not None
            ):
                screen, start, _ = self._selection_gesture
                if screen is not self.screen:
                    self._cancel_selection_gesture()
                elif event.screen_offset != start:
                    self._selection_dragged = True
            elif isinstance(event, events.MouseUp) and event.button == 1:
                completed = getattr(self, "_selection_gesture", None)
                self._selection_gesture = None
                if completed is not None and getattr(self, "_selection_dragged", False):
                    # Textual 8.2 synthesizes Click after a drag ending on the
                    # starting widget. Prevent selecting a label from pressing
                    # its button, tab, dropdown option or footer action.
                    self._mouse_down_widget = None
            elif isinstance(event, events.Key) and event.key == "escape":
                self._cancel_selection_gesture()
            elif isinstance(event, events.Key) and isinstance(self.focused, Input):
                parts = event.key.split("+")
                if event.key in ("ctrl+a", "ctrl+shift+a", "super+a") or (
                    "shift" in parts
                    and parts[-1] in ("left", "right", "up", "down", "home", "end")
                ):
                    # Non-priority Input bindings run when this same key
                    # bubbles back to App, not when it is first forwarded.
                    # Keep only a bounded set of real incoming key gestures.
                    self.screen.clear_selection()
                    if len(pending_keys) >= 32:
                        pending_keys.pop(next(iter(pending_keys)))
                    pending_keys[id(event)] = (event, self.screen, self.focused)
        await super().on_event(event)
        if completed is not None:
            screen, _, widget = completed
            dragged = getattr(self, "_selection_dragged", False)
            double_click = (
                getattr(self, "_click_chain_last_time", None) == event.time
                and getattr(self, "_chained_clicks", 0) >= 2
                and (
                    isinstance(widget, Input)
                    or getattr(getattr(widget, "_on_click", None), "__func__", None)
                    is Widget._on_click
                )
            )
            if dragged or double_click:
                self.call_after_refresh(self._copy_completed_selection, screen, widget)
        if keyboard_input is not None:
            self._copy_completed_selection(*keyboard_input)

    def _copy_completed_selection(self, screen, origin):
        # A queued completion cannot copy a different screen after a dialog or
        # action changed it. No selection watcher or refresh calls this method.
        if self.screen is not screen:
            return
        if origin is None or not origin.is_attached:
            return
        if self._clipboard_private(origin) or self._clipboard_private_selection():
            return
        try:
            if isinstance(origin, Input):
                if self.focused is not origin:
                    return
                text = self._completed_input_selection(origin)
            else:
                text = self.screen.get_selected_text()
        except Exception:
            self._clipboard_notice("Selected text copy is unavailable.")
            return
        if isinstance(text, str) and text:
            self._copy_clipboard_text(text)

    def _completed_input_selection(self, field):
        if self._clipboard_private(field) or self._clipboard_private_selection():
            return None
        if field.selected_text and not field.password:
            return field.selected_text
        selections = self.screen.selections
        selected = selections.get(field)
        if (
            not field.password
            and set(selections) == {field}
            and selected is not None
            and selected.start is None
            and selected.end is None
        ):
            # Widget double-click selection is a Screen selection, not the
            # Input's own editing range. Do not copy its viewport padding.
            return field.value
        return self.screen.get_selected_text()

    def clipboard_text_selection(self):
        if self._clipboard_private_selection():
            return None
        try:
            focused = self.focused
            selected = self.screen.get_selected_text()
            if isinstance(focused, Input) and (
                not self.screen.selections or set(self.screen.selections) == {focused}
            ):
                return self._completed_input_selection(focused)
            return selected
        except Exception:  # A changing renderer must not expose private locals.
            self._clipboard_notice("Selected text copy is unavailable.")
            return None

    def _clipboard_notice(self, message):
        self.notify(message, title="Clipboard")

    def copy_to_clipboard(self, text):
        # Input and Screen's standard copy bindings call this directly, not
        # our explicit action. Keep the same bound on every clipboard write.
        if self._clipboard_private():
            return False
        if not _fits_clipboard(text):
            self._clipboard_notice("Clipboard text exceeds the 1 MiB UTF-8 limit.")
            return False
        try:
            self._write_clipboard(text)
        except Exception:
            self._clipboard_notice("Clipboard copy is unavailable.")
            return False
        if text:
            self._clipboard_notice(
                "Copied text. Desktop clipboard delivery depends on your terminal."
            )
        return True

    def _write_clipboard(self, text):
        super().copy_to_clipboard(text)

    def _copy_clipboard_text(self, text):
        if self._clipboard_private():
            return
        if not isinstance(text, str) or not text:
            self._clipboard_notice("No text selected to copy.")
            return
        if not _fits_clipboard(text):
            self._clipboard_notice("Clipboard text exceeds the 1 MiB UTF-8 limit.")
            return
        try:
            if self.copy_to_clipboard(text) is False:
                return
        except Exception:  # Never expose private clipboard text or driver errors.
            self._clipboard_notice("Clipboard copy is unavailable.")
            return

    def action_copy_selection(self, text=None):
        if self._clipboard_private():
            return
        if text is None:
            text = self.clipboard_text_selection()
        self._copy_clipboard_text(text)

    def action_copy_view(self):
        if self._clipboard_private() or any(
            self._clipboard_private(widget)
            for widget in self.screen.walk_children(Widget)
        ):
            return
        try:
            # Textual 8.2.x has no public screen-to-plain-text API. This narrow
            # compositor call copies only the active screen's visible render,
            # not queried widgets, hidden screens or underlying model values.
            strips = self.screen._compositor.render_strips()
            text = "\n".join(strip.text.rstrip(" ") for strip in strips).rstrip("\n")
        except Exception:  # Refuse incompatible renderers with a fixed category.
            self._clipboard_notice("Visible screen copy is unavailable.")
            return
        self._copy_clipboard_text(text)

    def action_paste_copied_text(self):
        if self._clipboard_private():
            return
        target = self.focused
        if (
            target is None
            or not target.is_attached
            or not target.has_focus
            or target.is_disabled
        ):
            self._clipboard_notice("Focus an enabled text input to paste copied text.")
            return
        if not isinstance(target, Input) and not (
            getattr(target, "_accepts_clipboard_paste", False) is True
            and getattr(target, "state", None) == "connected"
            and not getattr(self, "read_only", False)
        ):
            self._clipboard_notice("Focus an enabled text input to paste copied text.")
            return
        text = self.clipboard
        if not isinstance(text, str) or not text:
            self._clipboard_notice(
                "No copied text in this app. Use your terminal's paste for desktop text."
            )
            return
        if not _fits_clipboard(text):
            self._clipboard_notice("Clipboard text exceeds the 1 MiB UTF-8 limit.")
            return
        target.post_message(events.Paste(text))


class ClipboardInput(SelectableViewport, Input):
    """Native editing plus a two-cell clear action in reserved right padding.

    The line-API Input isn't a container. Composite a double-width glyph after its
    normal border/padding rendering instead of wrapping/replacing native editing.
    Native right padding reserves room; the action is not selectable field content.
    Passwords, disabled fields and paddingless/empty fields have no clear action.
    """

    BINDINGS = [Binding("ctrl+a,super+a", "select_all", show=False)]
    CLEAR_GLYPH = "❌"
    CLEAR_WIDTH = 2
    COMPONENT_CLASSES = Input.COMPONENT_CLASSES | {"input--clear"}
    DEFAULT_CSS = """
    ClipboardInput .input--clear { color: $error; text-style: bold; }
    """

    @property
    def _clear_region(self):
        if (
            self.password
            or self.is_disabled
            or not self.value
            or self.styles.padding.right < self.CLEAR_WIDTH
            or not self.content_region.width
            or not self.content_region.height
        ):
            return None
        return Region(
            self.content_region.right
            + self.styles.padding.right
            - self.CLEAR_WIDTH
            - self.region.x,
            self.content_region.y - self.region.y,
            self.CLEAR_WIDTH,
            1,
        )

    @property
    def _clear_offset(self):
        region = self._clear_region
        return region.offset if region is not None else None

    def render_lines(self, crop):
        lines = super().render_lines(crop)
        region = self._clear_region
        visible = region.intersection(crop) if region is not None else None
        if visible:
            x, y = visible.offset - crop.offset
            line = lines[y]
            style = self.get_component_rich_style("input--clear")
            icon = Strip([Segment(self.CLEAR_GLYPH, style)]).crop(
                visible.x - region.x, visible.right - region.x
            )
            lines = list(lines)
            lines[y] = Strip.join((line.crop(0, x), icon, line.crop(x + visible.width)))
        return lines

    def on_mouse_down(self, event):
        self._clear_pointer = None
        region = self._clear_region
        if region is not None and region.contains(*event.offset):
            if event.button == 1:
                self._clear_pointer = event.screen_offset
            # Do not start a native selection on either cell of the clear icon.
            event.stop()
            event.prevent_default()

    def on_mouse_move(self, event):
        if event.screen_offset != getattr(self, "_clear_pointer", None):
            self._clear_pointer = None

    def on_leave(self):
        self._clear_pointer = None

    def on_click(self, event):
        pointer = getattr(self, "_clear_pointer", None)
        self._clear_pointer = None
        region = self._clear_region
        on_clear = region is not None and region.contains(*event.offset)
        if pointer is not None or on_clear:
            event.stop()
            event.prevent_default()
            if event.button == 1 and event.screen_offset == pointer and on_clear:
                # The native reactive setter validates/clamps selection and
                # posts exactly one ordinary Input.Changed; never submit.
                self.value = ""
                self.focus()

    def action_cut(self):
        if self.selected_text and not _fits_clipboard(self.selected_text):
            self.notify(
                "Clipboard text exceeds the 1 MiB UTF-8 limit.", title="Clipboard"
            )
            return
        if self.app.copy_to_clipboard(self.selected_text) is not False:
            self.delete_selection()


class ClipboardBar(Horizontal):
    """Invisible compatibility placeholder; clipboard actions use shortcuts."""

    DEFAULT_CSS = """
    ClipboardBar { display: none; height: 0; }
    """

    def on_mount(self):
        self.app.enable_text_selection(self.screen)


class ClipboardHelp(ModalScreen):
    """Discoverable shortcuts without clipboard reads or action buttons."""

    BINDINGS = [("escape,f1", "close", "Close help")]
    CSS = """
    ClipboardHelp { align: center middle; }
    ClipboardHelp > Static { width: 76; max-width: 95%; height: auto;
      max-height: 95%; overflow-y: auto; padding: 1 2;
      border: round $accent; background: $surface; }
    """

    def compose(self):
        from textual.widgets import Static

        yield Static(
            "Clipboard shortcuts\n\n"
            "Ctrl+Shift+C       Copy selected text\n"
            "Ctrl+Shift+Y       Copy screen (visible text only)\n"
            "Ctrl+Shift+Insert  Paste copied text into the focused input or connected shell\n\n"
            "Completed user text-selection gestures copy selected text and show "
            "a confirmation. Merely "
            "refreshing, focusing or programmatically selecting text never copies it.\n\n"
            "Paste copied uses only this dashboard's in-app cache, not the current "
            "desktop clipboard. For desktop text, use your terminal's paste command. "
            "Copy delivery to the desktop depends on terminal clipboard support.\n\n"
            "Ordinary Ctrl+C / Ctrl+V and shell control keys keep their focused "
            "input or shell behavior. Shortcuts require terminal forwarding.\n\n"
            "Escape or F1 closes help.",
            markup=False,
        )

    def action_close(self):
        self.dismiss()


class _SelectableFooterKey(SelectableViewport, FooterKey):
    """Footer actions activate on click, never at the start of a label drag."""

    def on_mouse_down(self, event):
        event.prevent_default()

    def _on_click(self, event):
        event.stop()
        super().on_mouse_down()


class ClipboardFooter(Footer):
    """Retain Textual's binding layout with drag-safe selectable footer keys."""

    ALLOW_SELECT = True

    def compose(self):
        # Textual 8.2 FooterKey executes on MouseDown. Reuse its presentation
        # and binding construction, replacing only that activation boundary.
        for widget in super().compose():
            if isinstance(widget, FooterKey):
                yield _SelectableFooterKey(
                    widget.key,
                    widget.key_display,
                    widget.description,
                    widget.action,
                    disabled=widget._disabled,
                    tooltip=widget.tooltip or "",
                    classes=" ".join(widget.classes - {"-disabled"}),
                ).data_bind(compact=Footer.compact)
            else:
                yield widget
