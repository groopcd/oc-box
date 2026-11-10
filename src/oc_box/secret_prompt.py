"""Controlling-terminal editor and creation policy for masked secret input.

No input comes from stdin and no field is sent to stdout/stderr. Rendering is
single-line and bounded to the current terminal width to avoid normal wrapping.
Reveal is explicit, prompt-local, and cleared before return/error. Terminal
recordings, screen captures, resize reflow, or scrollback may retain revealed text.
"""

import json
import os
from pathlib import Path
import re
import select
import signal
import termios
import unicodedata

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
KEYS = {
    b"\x1b[D": "left",
    b"\x1b[C": "right",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1bOH": "home",
    b"\x1bOF": "end",
    b"\x1bOD": "left",
    b"\x1bOC": "right",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[7~": "home",
    b"\x1b[8~": "end",
    b"\x1b[3~": "delete",
}
MAX_PASSPHRASE_BYTES = 1024


def validate_minimum(length):
    if type(length) is not int or not 8 <= length <= MAX_PASSPHRASE_BYTES:
        raise ValueError("Minimum passphrase length must be an integer from 8 to 1024.")
    return length


def passphrase_minimum(kind, override=None):
    """Resolve public creation policy only; never restrict existing unlocks."""
    if override is not None:
        return validate_minimum(override)
    key = kind + "_min_passphrase_length"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    try:
        settings = json.loads((base / "oc-box/config.json").read_text())
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Cannot read passphrase policy from oc-box/config.json."
        ) from exc
    if not isinstance(settings, dict):
        raise ValueError("oc-box/config.json must be a JSON object.")
    try:
        return validate_minimum(settings.get(key, 8))
    except ValueError as exc:
        raise ValueError(f"Invalid {key} in oc-box/config.json: {exc}") from exc


def new_passphrase(kind, minimum, *, reader=None):
    minimum = validate_minimum(minimum)
    reader = read_secret if reader is None else reader
    first = reader(f"New {kind} passphrase ({minimum}+ characters): ")
    if (
        len(first) < minimum
        or any(char in first for char in ("\0", "\r", "\n"))
        or len(first.encode()) > MAX_PASSPHRASE_BYTES
    ):
        raise ValueError(
            f"Choose a single-line passphrase of {minimum} or more characters (maximum 1024 UTF-8 bytes)."
        )
    if first != reader(f"Confirm {kind} passphrase: "):
        raise ValueError("Passphrases do not match; no password was changed.")
    return first


def write(fd, text):
    remaining = memoryview(text.encode("utf-8"))
    while remaining:
        count = os.write(fd, remaining)
        if not count:
            raise OSError("Terminal closed during secret input")
        remaining = remaining[count:]


def safe_glyph(char):
    # Never render control/bidi/combining sequences supplied as credential data.
    if not char.isprintable() or unicodedata.category(char).startswith(("C", "M")):
        return ascii(char)[1:-1]
    return char


def width(text):
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text
    )


class Editor:
    def __init__(self, maximum, allow_reveal):
        self.chars = []
        self.cursor = 0
        self.bytes = 0
        self.maximum = maximum
        self.allow_reveal = allow_reveal
        self.visible = False
        self.notice = ""

    def insert(self, text):
        length = len(text.encode("utf-8"))
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            self.notice = "Paste rejected: single line, no control characters"
        elif self.bytes + length > self.maximum:
            self.notice = "Input limit reached"
        else:
            self.chars[self.cursor : self.cursor] = text
            self.cursor += len(text)
            self.bytes += length
            self.notice = ""

    def erase(self, first, last):
        self.bytes -= sum(len(char.encode("utf-8")) for char in self.chars[first:last])
        del self.chars[first:last]
        self.cursor = first
        self.notice = ""

    def key(self, key):
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.chars), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.chars)
        elif key == "delete" and self.cursor < len(self.chars):
            self.erase(self.cursor, self.cursor + 1)
        elif key == "backspace" and self.cursor:
            self.erase(self.cursor - 1, self.cursor)
        elif key == "clear":
            self.erase(0, len(self.chars))
        elif key == "tail":
            self.erase(self.cursor, len(self.chars))
        elif key == "word":
            start = self.cursor
            while start and self.chars[start - 1].isspace():
                start -= 1
            while start and not self.chars[start - 1].isspace():
                start -= 1
            self.erase(start, self.cursor)
        elif key == "reveal" and self.allow_reveal:
            self.visible = not self.visible

    def line(self, prompt, columns):
        budget = max(1, columns - 1)  # never autowrap the last column
        mode = "visible" if self.visible else "masked"
        toggle = (
            ", Ctrl+R " + ("hide" if self.visible else "show")
            if self.allow_reveal
            else ""
        )
        suffix = f" [{len(self.chars)} chars, {mode}{toggle}]"
        if len(suffix) + min(len(prompt), 14) + 8 > budget:
            suffix = f" [{len(self.chars)} {'vis' if self.visible else 'mask'}]"
        suffix = suffix[: max(0, budget - 1)]
        prefix = prompt[: max(0, min(len(prompt), budget - len(suffix) - 8))]
        available = max(0, budget - len(prefix) - len(suffix))
        units = [safe_glyph(char) if self.visible else "*" for char in self.chars]
        # A horizontal viewport keeps the insertion cursor visible, including
        # for wide Unicode. Untrusted values never become terminal sequences.
        start = self.cursor
        used = 0
        while start and used + width(units[start - 1]) <= max(0, available - 2):
            start -= 1
            used += width(units[start])
        left = "<" if start else ""
        end = start
        used = len(left)
        while end < len(units):
            extra = width(units[end])
            reserve = 1 if end + 1 < len(units) else 0
            if used + extra + reserve > available:
                break
            used += extra
            end += 1
        field = left + "".join(units[start:end])
        if width(field) > available:
            field = ""
        if end < len(units) and width(field) < available:
            field += ">"
        position = min(
            budget - 1,
            len(prefix)
            + len(left)
            + sum(width(unit) for unit in units[start : self.cursor]),
        )
        return prefix + field + suffix, max(0, position)


def read_secret(prompt, *, allow_reveal=False, maximum=1024):
    """Mask input with a count; Ctrl+R toggles values only when allowed."""
    fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
    attributes = None
    handlers = {}
    enabled = False
    editor = Editor(maximum, allow_reveal)
    pending = bytearray()
    pasted = None
    paste_too_large = False
    success = False

    def columns():
        size = os.get_terminal_size(fd).columns
        return size if size > 0 else 80

    def cancel(*_):
        raise KeyboardInterrupt

    def draw():
        text, position = editor.line(prompt, columns())
        write(fd, "\r\x1b[2K" + text + "\r" + (f"\x1b[{position}C" if position else ""))

    try:
        if not os.isatty(fd):
            raise OSError("Secret input needs a controlling terminal")
        attributes = termios.tcgetattr(fd)
        raw = list(attributes)
        raw[6] = list(attributes[6])
        raw[0] &= ~(
            termios.ICRNL
            | termios.INLCR
            | termios.IGNCR
            | termios.IXON
            | termios.ISTRIP
        )
        raw[3] &= ~(
            termios.ECHO
            | termios.ECHONL
            | termios.ICANON
            | termios.IEXTEN
            | termios.ISIG
        )
        raw[6][termios.VMIN] = 1
        raw[6][termios.VTIME] = 0
        for sig in (
            signal.SIGINT,
            signal.SIGTERM,
            signal.SIGHUP,
            signal.SIGQUIT,
            signal.SIGTSTP,
        ):
            previous = signal.signal(sig, cancel)
            handlers[sig] = previous
        termios.tcsetattr(fd, termios.TCSAFLUSH, raw)
        enabled = True
        write(fd, "\x1b[?2004h")
        draw()
        previous_columns = columns()
        while True:
            if not select.select([fd], [], [], 0.1)[0]:
                if columns() != previous_columns:
                    draw()
                    previous_columns = columns()
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                raise EOFError
            pending.extend(chunk)
            while pending:
                if pasted is not None:
                    end = pending.find(PASTE_END)
                    take = (
                        end if end >= 0 else max(0, len(pending) - len(PASTE_END) + 1)
                    )
                    if len(pasted) + take > maximum:
                        paste_too_large = True
                    if not paste_too_large:
                        pasted.extend(pending[:take])
                    del pending[:take]
                    if end < 0:
                        break
                    del pending[: len(PASTE_END)]
                    if paste_too_large:
                        editor.notice = "Paste rejected: input limit"
                    else:
                        try:
                            editor.insert(pasted.decode("utf-8"))
                        except UnicodeError:
                            editor.notice = "Paste rejected: invalid UTF-8"
                    pasted = None
                    paste_too_large = False
                    continue
                if pending.startswith(PASTE_START):
                    del pending[: len(PASTE_START)]
                    pasted = bytearray()
                    continue
                if pending[0] == 27:
                    key = next((key for key in KEYS if pending.startswith(key)), None)
                    if key is not None:
                        editor.key(KEYS[key])
                        del pending[: len(key)]
                    elif any(key.startswith(pending) for key in (*KEYS, PASTE_START)):
                        break
                    else:
                        # Ignore unsupported cursor/function-key sequences.
                        sequence = re.match(rb"\x1b\[[0-?]*[ -/]*[@-~]", pending)
                        if sequence:
                            del pending[: sequence.end()]
                        elif pending.startswith(b"\x1bO") and len(pending) >= 3:
                            del pending[:3]
                        elif len(pending) < 32 and pending.startswith(b"\x1b["):
                            break
                        else:
                            # Drop an unrecognized Alt/key sequence as a unit,
                            # rather than insert its trailing character as data.
                            del pending[: min(2, len(pending))]
                    continue
                char = pending[0]
                if char in (10, 13):
                    if editor.notice:
                        draw()
                        pending.clear()
                        break  # require correcting/retyping rejected input
                    success = True
                    return "".join(editor.chars)
                if char in (
                    3,
                    26,
                    28,
                ):  # Ctrl-C/Z/\: cancel, never suspend/dump raw tty
                    raise KeyboardInterrupt
                if char == 4:
                    raise EOFError
                controls = {
                    1: "home",
                    5: "end",
                    8: "backspace",
                    11: "tail",
                    18: "reveal",
                    21: "clear",
                    23: "word",
                    127: "backspace",
                }
                if char < 32 or char == 127:
                    if char in controls:
                        editor.key(controls[char])
                    del pending[0]
                    continue
                length = (
                    1
                    if char < 128
                    else 2
                    if 194 <= char <= 223
                    else 3
                    if 224 <= char <= 239
                    else 4
                    if 240 <= char <= 244
                    else 0
                )
                if not length:
                    raise ValueError(
                        "Secret input rejected: invalid UTF-8; re-enter the complete value."
                    )
                if len(pending) < length:
                    break
                try:
                    text = bytes(pending[:length]).decode("utf-8")
                except UnicodeError:
                    raise ValueError(
                        "Secret input rejected: invalid UTF-8; re-enter the complete value."
                    ) from None
                else:
                    editor.insert(text)
                del pending[:length]
            # Repaint per read, not per byte, so a large paste stays responsive.
            draw()
            if editor.notice:
                message = editor.notice[: max(0, columns() - 1)]
                write(fd, "\r\x1b[2K" + message + "\a")
    finally:
        try:
            if enabled:
                # Erase even deliberately revealed text before a prompt returns
                # or errors. Flush queued input before the shell regains echo.
                final = prompt + ("[accepted]" if success else "[cancelled]")
                write(
                    fd, "\r\x1b[2K" + final[: max(0, columns() - 1)] + "\x1b[?2004l\n"
                )
        finally:
            try:
                if attributes is not None:
                    termios.tcsetattr(fd, termios.TCSAFLUSH, attributes)
            finally:
                for sig, previous in handlers.items():
                    signal.signal(sig, previous)
                os.close(fd)
