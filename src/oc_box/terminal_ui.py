"""Relay the normal TUI through a PTY, showing progress until it renders text."""

import codecs
import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty


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


class _LoadingOutput:
    """Keep banner controls outside application fragments, only during startup.

    Retain at most 4 KiB of an incomplete control/UTF-8 fragment. Longer strings
    stream through unchanged; drawing waits for their actual closing boundary.
    This is not a terminal emulator, and even unfinished fragments flush at EOF.
    """

    def __init__(self):
        self.detector = VisibleOutput()
        self.pending = bytearray()
        self.fragment_forwarded = False
        self.visible = False

    def safe_to_draw(self):
        return not self.visible and self.detector.safe_to_draw()

    def feed(self, output, forward, clear):
        ready = bytearray()
        for index, value in enumerate(output):
            self.pending.append(value)
            self.visible |= self.detector.feed(bytes((value,)))
            if self.detector.safe_to_draw():
                if self.visible:
                    forward(ready)
                    if self.fragment_forwarded:
                        # Its prefix is already on the physical terminal. Finish
                        # it before inserting any of our own control bytes.
                        forward(self.pending)
                        clear()
                    else:
                        clear()
                        forward(self.pending)
                    self.pending.clear()
                    forward(output[index + 1 :])
                    return True
                ready.extend(self.pending)
                self.pending.clear()
                self.fragment_forwarded = False
            elif len(self.pending) >= 4096:
                ready.extend(self.pending)
                self.pending.clear()
                self.fragment_forwarded = True
        forward(ready)
        return False

    def finish(self, forward):
        forward(self.pending)
        self.pending.clear()


def write_all(fd, content):
    view = memoryview(content)
    while view:
        try:
            count = os.write(fd, view)
        except InterruptedError:
            continue
        view = view[count:]


def run_tui(
    command, *, env=None, input_fd=None, output_fd=None, refresh=1.0, show_loading=True
):
    """Preserve terminal queries/input/resize; stop drawing at first UI text.

    No background writer ever touches a running OpenCode screen. The relay is
    byte-transparent apart from its temporary loading line before text renders.
    """
    input_fd = sys.stdin.fileno() if input_fd is None else input_fd
    output_fd = sys.stdout.fileno() if output_fd is None else output_fd
    interactive = os.isatty(input_fd) and os.isatty(output_fd)
    master = slave = None
    process = None
    old_terminal = None
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

        stream = _LoadingOutput()
        loading = show_loading
        banner_drawn = False
        started = time.monotonic()
        next_update = started
        stdin_open = True

        def banner(clear=False):
            nonlocal banner_drawn
            try:
                size = os.get_terminal_size(output_fd)
            except OSError:
                return
            text = (
                ""
                if clear
                else f"oc-box: Loading OpenCode, plugins and MCPs... {int(time.monotonic() - started)}s"
            )
            text = text[: max(size.columns - 1, 0)]
            # DECSC/DECRC save and restore cursor and rendition. This runs only
            # before the first nonblank text from the app, never over its UI.
            write_all(
                output_fd,
                f"\x1b7\x1b[{max(size.lines, 1)};1H\x1b[0m\x1b[2K{text}\x1b8".encode(),
            )
            banner_drawn = not clear

        def forward(content):
            write_all(output_fd, content)

        def clear_banner():
            if banner_drawn:
                banner(clear=True)

        while True:
            now = time.monotonic()
            if loading and now >= next_update and stream.safe_to_draw():
                banner()
                next_update = now + refresh
            readers = [master] + ([input_fd] if stdin_open else [])
            ready, _, _ = select.select(readers, [], [], refresh)
            if master in ready:
                try:
                    output = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    output = b""
                if not output:
                    break
                if loading:
                    loading = not stream.feed(output, forward, clear_banner)
                else:
                    forward(output)
            if input_fd in ready:
                content = os.read(input_fd, 65536)
                if content:
                    write_all(master, content)
                else:
                    stdin_open = False
            if process.poll() is not None and not ready:
                break
        stream.finish(forward)
        if banner_drawn and stream.safe_to_draw():
            banner(clear=True)
        return process.wait()
    finally:
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
