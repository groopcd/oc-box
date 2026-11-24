"""Badges for explicit launcher output, not a filter on terminal streams."""

import base64
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import sys


_KITTY_WINDOW = re.compile(r"[1-9][0-9]{0,9}")
_KONSOLE_VERSION = re.compile(r"[0-9]{2}(?:0[1-9]|1[0-2])[0-9]{2}")
_GRAPHICS_BLOCKERS = (
    "TMUX",
    "STY",
    "ZELLIJ",
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "XTERM_VERSION",
    "VTE_VERSION",
    "WEZTERM_PANE",
    "ALACRITTY_WINDOW_ID",
)


def _graphics_profile(environ):
    if any(environ.get(key) for key in _GRAPHICS_BLOCKERS):
        return None
    term = environ.get("TERM", "")
    kitty_window = environ.get("KITTY_WINDOW_ID", "")
    konsole = environ.get("KONSOLE_VERSION", "")
    if kitty_window and konsole:
        return None
    if term == "xterm-kitty" and _KITTY_WINDOW.fullmatch(kitty_window):
        return "kitty"
    if (
        not kitty_window
        and term in ("xterm", "xterm-256color", "konsole", "konsole-256color")
        and _KONSOLE_VERSION.fullmatch(konsole)
        and int(konsole) >= 220400
    ):
        return "konsole"
    return None


def _image():
    try:
        with Path(__file__).with_name("oc-box-mark.png").open("rb") as image:
            png = image.read(1025)
    except OSError:
        return None
    if png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 1024:
        return base64.b64encode(png).decode("ascii")
    return None


@dataclass(frozen=True)
class Badge:
    wire: str = ""
    cells: int = 0
    erase: str = ""

    def fit(self, columns):
        return self if columns >= 16 else _NONE


_NONE = Badge()
_ASCII = Badge("[/] ", 4)


class _Display:
    def __init__(self, ttyname, profile):
        self.ttyname = ttyname
        self.profile = profile
        self.image = None

    def accepts(self, fd):
        try:
            return os.ttyname(fd) == self.ttyname
        except OSError:
            return False

    def select(self, style):
        if style == "graphics" and self.profile is not None:
            self.image = _image()

    def badge(self, *, temporary=False):
        if self.image is None:
            return _ASCII
        upload = erase = ""
        if temporary:
            number = 1 + secrets.randbelow(2**31 - 1)
            if self.profile == "kitty":
                # Let Kitty allocate the image ID; never reuse an ID a native
                # client could have chosen. Delete only this numbered image.
                upload = f"I={number},"
                delete = f"d=N,I={number}"
            else:
                # KonsolePart (including Yakuake) does not delete by image
                # number. ID 0 is not cached; target our own placement ID.
                upload = f"i=0,p={number},"
                delete = f"d=i,i=0,p={number}"
            erase = f"\x1b_Ga=d,{delete},q=2;\x1b\\"
        # No query/reply. Static placements use terminal-managed lifetimes.
        # Do not print a space over the image cell: advance the cursor by
        # one cell for the image and one for the gap, independently of APC.
        return Badge(
            f"\x1b_Ga=T,t=d,f=100,{upload}q=2,c=1,r=1,C=1;{self.image}\x1b\\\x1b[2C",
            2,
            erase,
        )


def _display(input_fd, environ):
    if environ.get("TERM", "") in ("", "dumb"):
        return None
    try:
        ttyname = os.ttyname(input_fd)
    except OSError:
        return None
    return _Display(ttyname, _graphics_profile(environ))


def _fd(stream):
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return -1


@dataclass
class _Invocation:
    display: _Display | None = None


_INVOCATION: ContextVar[_Invocation | None] = ContextVar(
    "oc_box_startup_output", default=None
)


@contextlib.contextmanager
def invocation():
    # entrypoint owns the scope, including the final status; main() also has
    # a scope for direct callers.
    if _INVOCATION.get() is not None:
        yield
        return
    token = _INVOCATION.set(_Invocation())
    try:
        yield
    finally:
        _INVOCATION.reset(token)


def configure(style):
    """Resolve the saved style only if graphics are possible on the host TTY."""
    invocation = _INVOCATION.get()
    display = _display(_fd(sys.stdin), os.environ)
    if (
        invocation is None
        or display is None
        or not any(display.accepts(_fd(stream)) for stream in (sys.stderr, sys.stdout))
    ):
        return
    if display.profile is not None:
        display.select(style())
    invocation.display = display


def _configured_display():
    invocation = _INVOCATION.get()
    return None if invocation is None else invocation.display


def badge_for(stream, *, temporary=False):
    display = _configured_display()
    if display is not None and display.accepts(_fd(stream)):
        return display.badge(temporary=temporary)
    return _NONE


def relay_badge(input_fd, output_fd, style):
    if style is None:
        display = _configured_display()
    else:
        # Standalone relay/demo callers can select a badge explicitly.
        display = _display(input_fd, os.environ)
        if display is not None:
            display.select(style)
    if display is not None and display.accepts(input_fd) and display.accepts(output_fd):
        return display.badge(temporary=True)
    return _NONE


def line(text, *, file=None, flush=False):
    """Print launcher-authored lines; leave redirected output byte-for-byte."""
    stream = sys.stdout if file is None else file
    badge = badge_for(stream)
    if badge.cells:
        try:
            badge = badge.fit(os.get_terminal_size(_fd(stream)).columns)
        except OSError:
            badge = _NONE
    if badge.cells:
        text = "\n".join(badge.wire + row if row else row for row in text.split("\n"))
    print(text, file=stream, flush=flush)
