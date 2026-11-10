"""Explicit, in-memory clipboard controls for the optional Textual 8.2.x UI.

Selection copies only a custom widget's current visible viewport, not its
offscreen model or history. Copy view reads the active screen's rendered strips,
including control labels that should not become draggable interactive widgets.
Only explicit copy actions call Textual's best-effort OSC 52 clipboard writer.
Paste copied text uses Textual's local cache, never reads the desktop clipboard,
and goes through the recipient's existing Paste event handler.
"""

from textual import events
from textual.containers import Horizontal
from textual.strip import Strip
from textual.widgets import Button, DataTable, Input, RichLog

MAX_CLIPBOARD_BYTES = 1024 * 1024


def _fits_clipboard(text):
    if not isinstance(text, str) or len(text) > MAX_CLIPBOARD_BYTES:
        return False
    try:
        return len(text.encode("utf-8")) <= MAX_CLIPBOARD_BYTES
    except UnicodeError:
        return False


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
                strip = Strip.join(
                    (
                        strip.crop(0, left),
                        strip.crop(left, right).apply_style(style),
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
    """App mixin: explicit copies and narrowly scoped local-cache pastes.

    No bindings are installed here. In particular ordinary Ctrl-C/Ctrl-V remain
    the focused input/terminal's keys. Native terminals must explicitly opt in
    with ``_accepts_clipboard_paste = True``; this never attaches a client.
    """

    def clipboard_text_selection(self):
        try:
            focused = self.focused
            if isinstance(focused, Input) and focused.selected_text:
                return focused.selected_text
            return self.screen.get_selected_text()
        except Exception:  # A changing renderer must not expose private locals.
            self._clipboard_notice("Selected text copy is unavailable.")
            return None

    def _clipboard_notice(self, message):
        self.notify(message, title="Clipboard")

    def copy_to_clipboard(self, text):
        # Input and Screen's standard copy bindings call this directly, not
        # our explicit action. Keep the same bound on every clipboard write.
        if not _fits_clipboard(text):
            self._clipboard_notice("Clipboard text exceeds the 1 MiB UTF-8 limit.")
            return False
        try:
            self._write_clipboard(text)
        except Exception:
            self._clipboard_notice("Clipboard copy is unavailable.")
            return False
        return True

    def _write_clipboard(self, text):
        super().copy_to_clipboard(text)

    def _copy_clipboard_text(self, text):
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
        self._clipboard_notice(
            "Copied text. Desktop clipboard delivery depends on your terminal."
        )

    def action_copy_selection(self, text=None):
        if text is None:
            text = self.clipboard_text_selection()
        self._copy_clipboard_text(text)

    def action_copy_view(self):
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


class ClipboardInput(Input):
    """Standard input keys, with non-destructive refusal of an oversized cut."""

    def action_cut(self):
        if self.selected_text and not _fits_clipboard(self.selected_text):
            self.notify(
                "Clipboard text exceeds the 1 MiB UTF-8 limit.", title="Clipboard"
            )
            return
        if self.app.copy_to_clipboard(self.selected_text) is not False:
            self.delete_selection()


class ClipboardButton(Button):
    """Clipboard control that preserves focus and an explicit copy candidate.

    Screen clears a selection on mouse-up, before Button.Pressed. Capture only
    the selection button's mouse-down candidate, and consume it once. There is
    no clipboard write on mouse-down or canceled/dragged clicks. Keeping normal
    Button selection disabled also avoids making action labels draggable.
    """

    FOCUS_ON_CLICK = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clipboard_selection = None
        self._clipboard_pointer = None
        self._clipboard_cancelled = False

    def consume_selection(self):
        text, self._clipboard_selection = self._clipboard_selection, None
        return text

    def on_mouse_down(self, event):
        self._clipboard_selection = None
        self._clipboard_pointer = event.screen_offset
        self._clipboard_cancelled = False
        if self.id == "copy-selection" and event.button == 1:
            self._clipboard_selection = self.app.clipboard_text_selection()

    def on_mouse_move(self, event):
        if (
            self._clipboard_pointer is not None
            and event.screen_offset != self._clipboard_pointer
        ):
            self._cancel_clipboard_capture()

    def on_leave(self):
        if self._clipboard_pointer is not None:
            self._cancel_clipboard_capture()

    def _cancel_clipboard_capture(self):
        self._clipboard_selection = None
        self._clipboard_pointer = None
        self._clipboard_cancelled = True

    def on_click(self, event):
        if (
            event.button != 1
            or self._clipboard_cancelled
            or (
                self._clipboard_pointer is not None
                and event.screen_offset != self._clipboard_pointer
            )
        ):
            self._cancel_clipboard_capture()
            event.stop()
            event.prevent_default()
        self._clipboard_pointer = None
        self._clipboard_cancelled = False

    def on_focus(self):
        self._cancel_clipboard_capture()

    def on_blur(self):
        self._cancel_clipboard_capture()

    def on_key(self, event):
        self._cancel_clipboard_capture()


class ClipboardBar(Horizontal):
    """Compact clipboard controls for the active screen, including modals."""

    DEFAULT_CSS = """
    ClipboardBar { height: 3; }
    ClipboardBar > ClipboardButton {
        width: auto; min-width: 6; padding: 0 1; margin-right: 1;
    }
    """

    def compose(self):
        yield ClipboardButton(
            "Copy",
            id="copy-selection",
            tooltip="Copy selected text; desktop clipboard support depends on the terminal.",
        )
        yield ClipboardButton(
            "Copy view",
            id="copy-view",
            tooltip="Copy the active screen's visible text, including control labels.",
        )
        yield ClipboardButton(
            "Paste copied",
            id="paste-copied",
            tooltip="Paste text copied in this app; use terminal paste for desktop text.",
        )
