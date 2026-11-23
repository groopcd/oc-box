"""Relay the normal TUI through a PTY, showing progress until it renders text."""

import base64
import codecs
import errno
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import pty
import re
import secrets
import select
import signal
import subprocess
import sys
import termios
import time
import tty


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


def _graphics_mark(input_fd, output_fd, environ):
    """Return one quiet, same-terminal upload and its targeted deletion."""
    profile = _graphics_profile(environ)
    if profile is None:
        return None
    try:
        if os.ttyname(input_fd) != os.ttyname(output_fd):
            return None
        with Path(__file__).with_name("oc-box-mark.png").open("rb") as image:
            png = image.read(1025)
    except OSError:
        return None
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 1024:
        return None
    number = 1 + secrets.randbelow(2**31 - 1)
    if profile == "kitty":
        # Allocate a fresh image via a number, not a shared client-chosen ID.
        upload = f"I={number}"
        delete = f"d=N,I={number}"
    else:
        # KonsolePart (also used by Yakuake) lacks deletion by image number.
        # ID 0 is not stored in its image cache; give the placement its own
        # random positive ID and remove only that image/placement pair.
        upload = f"i=0,p={number}"
        delete = f"d=i,i=0,p={number}"
    return (
        f"\x1b_Ga=T,t=d,f=100,{upload},q=2,c=2,r=1,C=1;".encode()
        + base64.b64encode(png)
        + b"\x1b\\",
        f"\x1b_Ga=d,{delete},q=2;\x1b\\".encode(),
    )


class VisibleOutput:
    """Ignore terminal setup, queries, titles, blank frames and control strings."""

    def __init__(self):
        self.state = "text"
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, output):
        visible = False
        for char in self.decoder.decode(output):
            if self.state == "text":
                if char == "\x1b":
                    self.state = "escape"
                elif char.isprintable() and not char.isspace():
                    visible = True
            elif self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]P_^X":
                    self.state = "string"
                elif char in "()*+-./":
                    self.state = "charset"
                else:
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "string":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if char == "\\" else "string"
            else:  # single-byte character-set designation
                self.state = "text"
        return visible

    def safe_to_draw(self):
        return self.state == "text" and not self.decoder.getstate()[0]


def write_all(fd, content):
    view = memoryview(content)
    while view:
        try:
            count = os.write(fd, view)
        except InterruptedError:
            continue
        view = view[count:]


def run_tui(
    command,
    *,
    env=None,
    input_fd=None,
    output_fd=None,
    refresh=1.0,
    show_loading=True,
    startup_logo="ascii",
    file_drops=None,
    preview_relay=None,
    preview_open=None,
):
    """Preserve terminal queries/input/resize; stop drawing at first UI text.

    No background writer ever touches a running OpenCode screen. The relay is
    byte-transparent apart from its temporary loading line before text renders,
    unless an explicitly opted-in host file-drop handler was supplied.
    """
    input_fd = sys.stdin.fileno() if input_fd is None else input_fd
    output_fd = sys.stdout.fileno() if output_fd is None else output_fd
    interactive = os.isatty(input_fd) and os.isatty(output_fd)
    if file_drops is not None and not interactive:
        raise ValueError("Host file drops require interactive terminal input/output.")
    if preview_relay is not None and (not interactive or not callable(preview_open)):
        raise ValueError("Host preview opening requires an interactive terminal.")
    relay_errors = ()
    if preview_relay is not None:
        if __package__:
            from .file_preview_host import RelayError, safe_open_failure
            from .host_drop_input import PasteInput
        else:
            from file_preview_host import RelayError, safe_open_failure
            from host_drop_input import PasteInput
        relay_errors = (RelayError,)
    master = slave = None
    process = None
    old_terminal = None
    preview_failures = set()
    preview_framing_failed = False
    worker = pending_open = None
    mark_cleanup = None
    handlers = {}
    try:
        if interactive:
            master, slave = pty.openpty()

            def resize(*_):
                try:
                    size = fcntl.ioctl(output_fd, termios.TIOCGWINSZ, b"\0" * 8)
                    fcntl.ioctl(master, termios.TIOCSWINSZ, size)
                except OSError:
                    pass

            resize()
            # A separate session provides the same terminal semantics as a
            # directly attached podman exec. Acquire a controlling terminal.
            child_code = "import fcntl,os,sys,termios; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execvp(sys.argv[1],sys.argv[1:])"
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, *command],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
            os.close(slave)
            slave = None
            old_terminal = termios.tcgetattr(input_fd)
            tty.setraw(input_fd)
            handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
        else:
            process = subprocess.Popen(command, env=env)
        handlers[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
        handlers[signal.SIGTERM] = signal.signal(
            signal.SIGTERM, lambda *_: process.terminate()
        )
        if not interactive:
            return process.wait()

        detector = VisibleOutput()
        if file_drops is not None:
            file_drops.start()
        loading = show_loading and os.environ.get("TERM", "") not in ("", "dumb")
        banner_drawn = False
        started = time.monotonic()
        next_update = started
        stdin_open = True

        def clear_mark():
            nonlocal mark_cleanup
            if mark_cleanup is not None:
                cleanup, mark_cleanup = mark_cleanup, None
                write_all(output_fd, cleanup)

        def banner(clear=False, mark=None):
            nonlocal banner_drawn, mark_cleanup
            try:
                size = os.get_terminal_size(output_fd)
            except OSError:
                return
            graphic = b""
            text = (
                ""
                if clear
                else f"oc-box: Loading OpenCode, plugins and MCPs... {int(time.monotonic() - started)}s"
            )
            if not clear and size.columns >= 16:
                if mark is not None and size.lines >= 2:
                    graphic, mark_cleanup = mark
                    text = "   " + text
                elif mark_cleanup is not None:
                    text = "   " + text
            text = text[: max(size.columns - 1, 0)]
            # DECSC/DECRC save and restore cursor and rendition. This runs only
            # before the first nonblank text from the app, never over its UI.
            write_all(
                output_fd,
                f"\x1b7\x1b[{max(size.lines, 1)};1H\x1b[0m\x1b[2K".encode()
                + graphic
                + text.encode()
                + b"\x1b8",
            )
            banner_drawn = not clear

        if preview_relay is not None:
            preview_input = (
                file_drops.input
                if file_drops is not None
                else PasteInput(lambda _: None)
            )

            preview_input.set_host_mouse(preview_relay.mouse, preview_relay.break_press)
            worker = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="oc-box-host-open"
            )

        # A PNG is only ever sent before the first native output. Do not probe,
        # redraw on resize, or leave a graphics placement for the native UI.
        if (
            loading
            and startup_logo == "graphics"
            and (mark := _graphics_mark(input_fd, output_fd, os.environ))
        ):
            banner(mark=mark)
            next_update = started + refresh

        while True:
            now = time.monotonic()
            if file_drops is not None:
                private_output = file_drops.pending_output()
                if private_output:
                    write_all(output_fd, private_output)
            if loading and now >= next_update and detector.safe_to_draw():
                banner()
                next_update = now + refresh
            readers = [master] + ([input_fd] if stdin_open else [])
            ready, _, _ = select.select(readers, [], [], refresh)
            if preview_relay is not None:
                preview_relay.check()
            if master in ready:
                try:
                    output = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    output = b""
                clear_mark()
                if not output:
                    if preview_relay is not None:
                        trailing = preview_relay.finish_output()
                        if trailing:
                            write_all(output_fd, trailing)
                    if file_drops is not None:
                        trailing = file_drops.finish_output()
                        if trailing:
                            write_all(output_fd, trailing)
                    break
                if preview_relay is not None:
                    output = preview_relay.output_bytes(output)
                if file_drops is not None:
                    output = file_drops.output_bytes(output)
                if loading and output:
                    # Erase before native output can begin a split control or
                    # switch buffers; never write an erase into an open string.
                    if banner_drawn and detector.safe_to_draw():
                        banner(clear=True)
                    if detector.feed(output):
                        loading = False
                write_all(output_fd, output)
            if input_fd in ready:
                content = os.read(input_fd, 65536)
                if content:
                    if file_drops is not None:
                        content = file_drops.input_bytes(content)
                    elif preview_relay is not None:
                        content = preview_input.feed(content)
                    write_all(master, content)
                else:
                    if file_drops is not None:
                        trailing = file_drops.finish_input()
                        if trailing:
                            write_all(master, trailing)
                    elif preview_relay is not None:
                        write_all(master, preview_input.finish())
                    stdin_open = False
            if preview_relay is not None:
                # A concurrently ready fresh click can supersede an old
                # arrival before handing it off to the opener worker.
                candidate = preview_relay.take()
                if pending_open is not None and pending_open.done():
                    if (error := pending_open.exception()) is not None:
                        preview_failures.add(safe_open_failure(error))
                    pending_open = None
                if (
                    candidate is not None
                    and pending_open is None
                    and stdin_open
                    and process.poll() is None
                ):
                    pending_open = worker.submit(preview_open, *candidate)
            if process.poll() is not None and not ready:
                break
        if banner_drawn and detector.safe_to_draw():
            banner(clear=True)
        return process.wait()
    except relay_errors:
        preview_framing_failed = True
        return 1
    finally:
        if mark_cleanup is not None:
            try:
                write_all(output_fd, mark_cleanup)
            except OSError:
                pass
        if old_terminal is not None:
            termios.tcsetattr(input_fd, termios.TCSADRAIN, old_terminal)
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if file_drops is not None:
            try:
                write_all(output_fd, file_drops.stop())
            except OSError:
                pass
        if preview_framing_failed:
            print(
                "oc-box: Host file transfer framing failed. No further opens "
                "will be accepted; an already submitted open can still finish.",
                file=sys.stderr,
            )
        if worker is not None:
            # A running worker can be inside an engine query or desktop
            # dispatch. Restore the TTY and reap its foreground client first.
            worker.shutdown(wait=True, cancel_futures=True)
            if pending_open is not None and not pending_open.cancelled():
                if (error := pending_open.exception()) is not None:
                    preview_failures.add(safe_open_failure(error))
        if preview_failures:
            print(
                "oc-box: A host file open was refused or did not complete "
                f"({', '.join(sorted(preview_failures))}).",
                file=sys.stderr,
            )
