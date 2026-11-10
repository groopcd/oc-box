"""Embedded display for the existing native persistent-session terminal client.

``LocalPTY`` is stdlib-only. ``NativeTerminal`` additionally requires Textual
8.2.x and pyte 0.8.x. No client starts on mount, focus, or tab selection: the
owner must explicitly call ``connect(command, env, on_close=lease.release)``
with a validated native-client command and a sanitized, complete environment.
That action may create/take control of a native terminal; browsing must not.

The local PTY is only the native client's stdio transport, never a replacement
workspace shell. Disconnect terminates/reaps that client, not the native shell.
If the command enters a container, it must preserve terminal stdio there (for
example, ``podman exec -it``); an exec command using only pipes cannot propagate
pane dimensions to the existing native helper. The widget never rewrites it.
No output is logged, interpreted as Rich markup, or forwarded to the host
terminal. All OSC/DCS/APC/PM/SOS strings (including clipboard and links) are
discarded before emulation. This is a bounded VT-style emulator, not a complete
xterm implementation; mouse, images, hyperlinks, complex grapheme clusters and
extended keyboard protocols are unsupported. Host acceptance remains separate.
"""

import codecs
import errno
import fcntl
import os
import re
import signal
import struct
import subprocess
import termios
import tty
import unicodedata
from collections.abc import Mapping, Sequence


MAX_READ = 64 * 1024
MAX_INPUT = 64 * 1024
MAX_COLUMNS = 400
MAX_ROWS = 160
SCROLLBACK = 1000
MAX_CELL_CODEPOINTS = 16


class TerminalError(RuntimeError):
    """Fixed, non-private diagnostic suitable for a dashboard status label."""


def _size(columns, rows):
    return max(1, min(MAX_COLUMNS, int(columns))), max(1, min(MAX_ROWS, int(rows)))


class LocalPTY:
    """Nonblocking, bounded stdio for one explicitly supplied native client.

    Poll ``read()`` on the UI timer and pass returned bytes only to an emulator.
    Slow readers apply kernel backpressure rather than accumulate/drop output.
    ``send`` returns False if its bounded input queue is full; callers must not
    split a rejected paste. The lease callback runs once on exit, startup failure
    or disconnect; callback errors are reduced to a fixed failure reason.
    """

    def __init__(self):
        self.state = "disconnected"
        self.reason = ""
        self.returncode = None
        self.process = None
        self.fd = None
        self._pending = bytearray()
        self._on_close = None

    def connect(self, command, env, on_close=None, *, columns=80, rows=24):
        if self.fd is not None or self.process is not None:
            raise TerminalError("Disconnect the current terminal client first.")
        self._on_close = on_close
        self.returncode = None
        self.reason = ""
        master = slave = None
        try:
            if (
                isinstance(command, (str, bytes))
                or not isinstance(command, Sequence)
                or not command
                or not all(isinstance(part, str) and part for part in command)
                or not isinstance(env, Mapping)
            ):
                raise ValueError("Invalid client specification")
            child_env = dict(env)  # Never inherit ambient provider/vault values.
            child_env.setdefault("TERM", "xterm-256color")
            master, slave = os.openpty()
            tty.setraw(slave)
            columns, rows = _size(columns, rows)
            fcntl.ioctl(
                slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
            )
            self.process = subprocess.Popen(
                list(command),
                stdin=slave,
                stdout=slave,
                stderr=subprocess.DEVNULL,
                env=child_env,
                close_fds=True,
                start_new_session=True,
            )
            os.set_blocking(master, False)
            self.fd, master = master, None
            self.state = "connected"
        except (OSError, ValueError, TypeError):
            self.disconnect()
            self.state, self.reason = (
                "failed",
                "Native terminal client could not start.",
            )
            raise TerminalError(self.reason) from None
        finally:
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)

    def resize(self, columns, rows):
        if self.fd is None:
            return
        columns, rows = _size(columns, rows)
        try:
            fcntl.ioctl(
                self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
            )
            # start_new_session gives the native client its own process group.
            # An explicit signal also works without a controlling local tty.
            if self.process is not None:
                os.killpg(self.process.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass
        except OSError:
            self._fail("Native terminal resize failed.")

    def _flush(self):
        while self.fd is not None and self._pending:
            try:
                written = os.write(self.fd, self._pending)
                if not written:
                    break
                del self._pending[:written]
            except BlockingIOError:
                break
            except OSError:
                self._fail("Native terminal input closed.")
                break

    def send(self, content):
        if self.fd is None or not isinstance(content, bytes):
            return False
        if len(content) + len(self._pending) > MAX_INPUT:
            return False
        self._pending.extend(content)
        self._flush()
        return self.state == "connected"

    def read(self, limit=MAX_READ):
        """Return at most MAX_READ bytes; no blocking reads or signal handlers."""
        self._flush()
        if self.fd is None:
            return b""
        result = bytearray()
        limit = max(1, min(MAX_READ, int(limit)))
        try:
            while len(result) < limit:
                try:
                    chunk = os.read(self.fd, min(8192, limit - len(result)))
                except BlockingIOError:
                    break
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""  # Linux PTY EOF after the slave closes.
                if not chunk:
                    self.disconnect(_state="exited")
                    break
                result.extend(chunk)
        except OSError:
            self._fail("Native terminal output closed.")
        # A dead client can leave an inherited slave open in a descendant.
        if self.process is not None and self.process.poll() is not None and not result:
            self.disconnect(_state="exited")
        return bytes(result)

    def _fail(self, reason):
        self.disconnect(_state="failed")
        self.reason = reason

    def disconnect(self, *, _state="disconnected"):
        """Release local resources only; never send ``exit`` or remove a shell."""
        process, self.process = self.process, None
        if process is not None:
            if _state == "exited":
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            # Kill this client's group even if its leader already exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            self.returncode = process.returncode
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self._pending.clear()
        self.state = _state
        if _state == "exited" and self.returncode not in (None, 0):
            self.state, self.reason = (
                "failed",
                "Native terminal client exited unsuccessfully.",
            )
        callback, self._on_close = self._on_close, None
        if callback is not None:
            try:
                callback()
            except Exception:
                self.state, self.reason = "failed", "Terminal lease release failed."


class ControlFilter:
    """Incremental UTF-8/control filter with constant-sized escape storage."""

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"
        self.escape = ""

    def feed(self, content):
        output = []
        for char in self.decoder.decode(content):
            if self.state in ("string", "string_escape"):
                if char in ("\a", "\x9c") or (
                    self.state == "string_escape" and char == "\\"
                ):
                    self.state = "text"
                else:
                    self.state = "string_escape" if char == "\x1b" else "string"
                continue
            if char == "\x1b":
                self.state, self.escape = "escape", char
                continue
            if char in ("\x90", "\x98", "\x9d", "\x9e", "\x9f"):
                self.state, self.escape = "string", ""
                continue
            if char == "\x9b":
                self.state, self.escape = "csi", "\x1b["
                continue
            if self.state == "escape":
                if char in "]P_X^":
                    self.state, self.escape = "string", ""
                elif char == "[":
                    self.state, self.escape = "csi", "\x1b["
                elif char in "()%#":
                    self.state, self.escape = "charset", self.escape + char
                else:
                    if "0" <= char <= "~":
                        output.append(self.escape + char)
                    self.state, self.escape = "text", ""
                continue
            if self.state == "charset":
                if "0" <= char <= "~":
                    output.append(self.escape + char)
                self.state, self.escape = "text", ""
                continue
            if self.state in ("csi", "discard_csi"):
                if "@" <= char <= "~":
                    if self.state == "csi":
                        output.append(self.escape + char)
                    self.state, self.escape = "text", ""
                elif " " <= char <= "?":
                    if self.state == "csi":
                        self.escape += char
                        if len(self.escape) > 128:
                            self.state, self.escape = "discard_csi", ""
                else:
                    self.state, self.escape = "text", ""
                continue
            if char in "\b\t\n\v\f\r" or (char >= " " and not "\x7f" <= char <= "\x9f"):
                output.append(char)
        return "".join(output)


try:
    import pyte
except ImportError:
    pyte = None


if pyte is not None:

    class _Screen(pyte.HistoryScreen):
        def draw(self, data):
            # pyte appends combining marks to a cell. Bound that cell too, not
            # just the number of rows: a combining-only stream has no width.
            start = 0
            for index, char in enumerate(data):
                if not unicodedata.combining(char):
                    continue
                if start < index:
                    super().draw(data[start:index])
                cursor = self.cursor
                if cursor.x:
                    previous = self.buffer[cursor.y][cursor.x - 1]
                elif cursor.y:
                    previous = self.buffer[cursor.y - 1][self.columns - 1]
                else:
                    previous = self.default_char
                if len(previous.data) < MAX_CELL_CODEPOINTS:
                    super().draw(char)
                start = index + 1
            if start < len(data):
                super().draw(data[start:])

        def save_cursor(self):
            # Repeated ESC 7 must not grow pyte's savepoint stack forever.
            if len(self.savepoints) >= 16:
                del self.savepoints[0]
            super().save_cursor()

    class TerminalEmulator:
        """Bounded screen model with a separate DEC alternate-screen buffer."""

        def __init__(self, columns=80, rows=24, *, history=SCROLLBACK, send=None):
            columns, rows = _size(columns, rows)
            self.main = _Screen(columns, rows, history=max(0, min(SCROLLBACK, history)))
            self.alternate = _Screen(columns, rows, history=0)
            self.screen = self.main
            self.application_cursor = False
            self.bracketed_paste = False
            self.send = send
            self.filter = ControlFilter()
            self.stream = pyte.Stream(self)

        def __getattr__(self, name):
            if name not in pyte.Stream.events:
                raise AttributeError(name)
            # pyte caches dispatch functions, so resolve the active buffer when
            # called, not when Stream attaches its listener.
            return lambda *args, **kwargs: getattr(self.screen, name)(*args, **kwargs)

        def set_mode(self, *modes, **kwargs):
            self._mode(True, modes, kwargs.get("private", False))

        def reset_mode(self, *modes, **kwargs):
            self._mode(False, modes, kwargs.get("private", False))

        def _mode(self, enabled, modes, private):
            for mode in modes:
                if private and mode in (47, 1047, 1049):
                    if enabled and self.screen is self.main:
                        if mode != 47:
                            self.alternate.reset()
                        self.screen = self.alternate
                    elif not enabled:
                        self.screen = self.main
                elif private and mode == 1:
                    self.application_cursor = enabled
                elif private and mode == 2004:
                    self.bracketed_paste = enabled
                elif mode in ((5, 6, 7, 25) if private else (4, 20)):
                    method = self.screen.set_mode if enabled else self.screen.reset_mode
                    method(mode, private=private)

        def reset(self):
            self.main.reset()
            self.alternate.reset()
            self.screen = self.main
            self.application_cursor = self.bracketed_paste = False

        def report_device_status(self, mode, **kwargs):
            # Replies go to the local client, never to the host terminal.
            if self.send is not None:
                if mode == 5:
                    self.send(b"\x1b[0n")
                elif mode == 6:
                    private = "?" if kwargs.get("private") else ""
                    self.send(
                        f"\x1b[{private}{self.screen.cursor.y + 1};{self.screen.cursor.x + 1}R".encode()
                    )

        def report_device_attributes(self, *args, **kwargs):
            if self.send is not None:
                self.send(b"\x1b[?6c")

        def feed(self, content):
            try:
                self.stream.feed(self.filter.feed(content))
            except (TypeError, ValueError, IndexError, KeyError, AssertionError):
                # Unsupported/malformed terminal commands must not produce a
                # Textual error report containing private emulator locals.
                raise TerminalError("Native terminal output is unsupported.") from None

        def resize(self, columns, rows):
            columns, rows = _size(columns, rows)
            for screen in (self.main, self.alternate):
                screen.resize(lines=rows, columns=columns)

        def key(self, key, character=None):
            return key_bytes(key, character, application_cursor=self.application_cursor)

        def paste(self, text):
            # Reserve Ctrl-] for detach even in paste and disallow pasted ESC
            # from terminating the bracketed-paste wrapper early.
            content = text.replace("\x1b", "").replace("\x1d", "").replace("\r\n", "\n")
            content = content.replace("\n", "\r").encode("utf-8", "replace")
            return (
                b"\x1b[200~" + content + b"\x1b[201~"
                if self.bracketed_paste
                else content
            )

else:
    TerminalEmulator = None


def key_bytes(key, character=None, *, application_cursor=False):
    """Translate Textual key names, without consuming dashboard keys globally."""
    special = {
        "enter": b"\r",
        "tab": b"\t",
        "backspace": b"\x7f",
        "escape": b"\x1b",
        "shift+tab": b"\x1b[Z",
        "insert": b"\x1b[2~",
        "delete": b"\x1b[3~",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
        "f1": b"\x1bOP",
        "f2": b"\x1bOQ",
        "f3": b"\x1bOR",
        "f4": b"\x1bOS",
        "f5": b"\x1b[15~",
        "f6": b"\x1b[17~",
        "f7": b"\x1b[18~",
        "f8": b"\x1b[19~",
        "f9": b"\x1b[20~",
        "f10": b"\x1b[21~",
        "f11": b"\x1b[23~",
        "f12": b"\x1b[24~",
        "ctrl+space": b"\x00",
        "ctrl+at": b"\x00",
        "ctrl+left_square_bracket": b"\x1b",
        "ctrl+backslash": b"\x1c",
        "ctrl+circumflex_accent": b"\x1e",
        "ctrl+underscore": b"\x1f",
    }
    if key in ("ctrl+]", "ctrl+right_square_bracket"):
        return None  # Local detach, never sent to the native shell.
    if key in special:
        return special[key]
    arrows = {
        "up": "A",
        "down": "B",
        "right": "C",
        "left": "D",
        "home": "H",
        "end": "F",
    }
    parts = key.split("+")
    base = parts[-1]
    if base in arrows:
        modifiers = set(parts[:-1])
        if modifiers:
            number = (
                1
                + (1 if "shift" in modifiers else 0)
                + (2 if "alt" in modifiers else 0)
                + (4 if "ctrl" in modifiers else 0)
            )
            return f"\x1b[1;{number}{arrows[base]}".encode()
        return ("\x1bO" if application_cursor else "\x1b[").encode() + arrows[
            base
        ].encode()
    if key.startswith("ctrl+") and len(base) == 1 and "a" <= base.lower() <= "z":
        return bytes([ord(base.lower()) - 96])
    if key.startswith("alt+") and len(base) == 1:
        return b"\x1b" + base.encode("utf-8")
    if character is not None and len(character) == 1:
        return character.encode("utf-8", "replace")
    return None


try:
    from rich.segment import Segment
    from rich.style import Style
    from textual import events
    from textual.message import Message
    from textual.strip import Strip
    from textual.widget import Widget
except ImportError:
    Widget = None


if Widget is not None and pyte is not None:

    class NativeTerminal(Widget, can_focus=True):
        """Explicit-connect embedded terminal; Textual alone owns UI signals.

        ``state`` / ``reason`` are fixed transport status, not proof of native
        protocol readiness. Listen for ``StateChanged`` and ``Detached``. Mount
        only installs a nonblocking poll timer. Ctrl-] disconnects and blurs;
        the dashboard should restore focus on ``Detached``. Dashboard priority
        shortcuts must be disabled while this widget has focus, since priority
        app bindings run before widget input handlers.
        """

        DEFAULT_CSS = (
            "NativeTerminal { width: 1fr; height: 1fr; overflow: hidden hidden; }"
        )

        class StateChanged(Message):
            def __init__(self, terminal):
                self.terminal = terminal
                self.state, self.reason = terminal.state, terminal.reason
                super().__init__()

        class Detached(Message):
            pass

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.transport = LocalPTY()
            self.emulator = TerminalEmulator(send=self.transport.send)
            self._last_status = (self.state, self.reason)

        @property
        def state(self):
            return self.transport.state

        @property
        def reason(self):
            return self.transport.reason

        def on_mount(self):
            self.set_interval(1 / 30, self._poll)

        def connect(self, command, env, on_close=None):
            columns, rows = _size(
                self.content_size.width or 80, self.content_size.height or 24
            )
            if self.transport.fd is not None:
                raise TerminalError("Disconnect the current terminal client first.")
            self.emulator = TerminalEmulator(columns, rows, send=self.transport.send)
            try:
                self.transport.connect(
                    command, env, on_close, columns=columns, rows=rows
                )
            finally:
                self._notify_state()
                self.refresh()

        def disconnect(self):
            self.transport.disconnect()
            self._notify_state()
            self.refresh()

        def on_unmount(self):
            self.disconnect()

        def _notify_state(self):
            status = (self.state, self.reason)
            if status != self._last_status:
                self._last_status = status
                self.post_message(self.StateChanged(self))

        def _poll(self):
            content = self.transport.read()
            if content:
                try:
                    self.emulator.feed(content)
                except TerminalError:
                    self.transport._fail("Native terminal output is unsupported.")
                self.refresh()
            self._notify_state()

        def on_resize(self, event):
            columns, rows = _size(self.content_size.width, self.content_size.height)
            self.emulator.resize(columns, rows)
            self.transport.resize(columns, rows)
            self.refresh()

        def on_focus(self):
            self.refresh()

        def on_blur(self):
            self.refresh()

        def on_key(self, event: events.Key):
            if not self.has_focus or self.state != "connected":
                return
            # Focused terminal owns input, including Ctrl-C and Tab. None of
            # these bindings are installed globally or on the dashboard.
            event.stop()
            event.prevent_default()
            if event.key in ("ctrl+]", "ctrl+right_square_bracket"):
                self.disconnect()
                self.blur()
                self.post_message(self.Detached())
            elif event.key in ("shift+pageup", "shift+pagedown"):
                method = (
                    self.emulator.screen.prev_page
                    if event.key == "shift+pageup"
                    else self.emulator.screen.next_page
                )
                method()
                self.refresh()
            else:
                content = self.emulator.key(event.key, event.character)
                if content is not None:
                    self.transport.send(content)

        def on_paste(self, event: events.Paste):
            if self.has_focus and self.state == "connected":
                event.stop()
                event.prevent_default()
                # Bound before encoding/copying a large clipboard payload.
                if len(event.text) <= MAX_INPUT:
                    self.transport.send(self.emulator.paste(event.text))

        @staticmethod
        def _color(value):
            if value == "default":
                return None
            if re.fullmatch("[0-9a-fA-F]{6}", value):
                return "#" + value
            return {
                "brown": "yellow",
                "brightblack": "bright_black",
                "brightred": "bright_red",
                "brightgreen": "bright_green",
                "brightbrown": "bright_yellow",
                "brightblue": "bright_blue",
                "brightmagenta": "bright_magenta",
                "brightcyan": "bright_cyan",
                "brightwhite": "bright_white",
            }.get(value, value)

        def render_line(self, y):
            width = self.content_size.width
            screen = self.emulator.screen
            if y >= screen.lines:
                return Strip.blank(width)
            line = screen.buffer[y]
            segments = []
            for x in range(min(width, screen.columns)):
                cell = line[x]
                if not cell.data:  # The trailing cell of a wide code point.
                    continue
                style = Style(
                    color=self._color(cell.fg),
                    bgcolor=self._color(cell.bg),
                    bold=cell.bold,
                    italic=cell.italics,
                    underline=cell.underscore,
                    strike=cell.strikethrough,
                    reverse=cell.reverse,
                )
                if (
                    self.has_focus
                    and not screen.cursor.hidden
                    and (x, y)
                    == (min(screen.cursor.x, screen.columns - 1), screen.cursor.y)
                ):
                    style += Style(reverse=not cell.reverse)
                # Segment text is literal; no markup, OSC links, or raw ANSI.
                segments.append(Segment(cell.data, style))
            return Strip(segments).adjust_cell_length(width)

else:

    class NativeTerminal:
        def __init__(self, *args, **kwargs):
            raise TerminalError(
                "Embedded terminal requires textual>=8.2.8,<9 and pyte>=0.8.2,<0.9."
            )
