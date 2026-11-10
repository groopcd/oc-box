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


_STOP_GRACE = 1.0
_POLL_INTERVAL = 0.05


class _TUIProcessError(RuntimeError):
    pass


class _ChildGroup:
    """The relay alone waits for this private child; its PID pins the group.

    An exited but unreaped session leader prevents numeric PID/PGID reuse. Never
    poll/reap while future group signals are possible. Outside reaping invalidates
    that anchor: refuse further numeric signals, not merely a failed killpg(0).
    """

    def __init__(self, process):
        self.process = process
        self.lost = False
        self.closed = False

    def observe(self):
        if self.lost or self.closed or self.process.returncode is not None:
            self.lost = True
        else:
            try:
                result = os.waitid(
                    os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except OSError:
                self.lost = True
            else:
                if result is None:
                    return None
                if result.si_pid == self.process.pid:
                    return (
                        result.si_status
                        if result.si_code == os.CLD_EXITED
                        else -result.si_status
                    )
                self.lost = True
        raise _TUIProcessError(
            "TUI child ownership lost; process-group signaling refused."
        ) from None

    def signal(self, sig):
        # Verify the retained anchor immediately before each numeric group use.
        self.observe()
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            raise _TUIProcessError("TUI child process cleanup failed.") from None

    def reap(self):
        try:
            self.observe()
        finally:
            # Close signaling BEFORE wait(), including signal-handler reentry.
            self.closed = True
        try:
            self.process.wait(timeout=_STOP_GRACE)
        except (OSError, subprocess.SubprocessError):
            raise _TUIProcessError("TUI child process cleanup failed.") from None


def _stop_group(group, deadline, term_sent):
    deadline = time.monotonic() + _STOP_GRACE if deadline is None else deadline
    failure = None
    try:
        if not term_sent:
            group.signal(signal.SIGTERM)
        while time.monotonic() < deadline:
            # A zombie leader remains our ownership anchor, not evidence that
            # every descendant has exited. Retain the bounded TERM grace.
            group.observe()
            time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))
    except BaseException as exc:
        failure = exc
    # Finish ALL group signaling before the one reaping stage. Still attempt
    # independent cleanup after a failure, but a lost anchor stays refused.
    for action in (lambda: group.signal(signal.SIGKILL), group.reap):
        try:
            action()
        except BaseException as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


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
    group = None
    old_terminal = None
    handlers = {}
    stop_deadline = None
    term_sent = False
    interrupt_pending = False

    def request_stop(*_):
        nonlocal stop_deadline
        if stop_deadline is None:
            stop_deadline = time.monotonic() + _STOP_GRACE

    def interrupt(*_):
        nonlocal interrupt_pending
        # Interactive Ctrl-C travels as a raw input byte to the child PTY. In
        # pipe mode preserve foreground SIGINT delivery despite the owned group.
        # Handlers only record intent: none may signal during/after final reap.
        if not interactive:
            interrupt_pending = True

    def advance_stop():
        nonlocal term_sent, interrupt_pending
        if interrupt_pending:
            interrupt_pending = False
            group.signal(signal.SIGINT)
        if stop_deadline is None:
            return False
        if not term_sent:
            group.signal(signal.SIGTERM)
            term_sent = True
        if time.monotonic() >= stop_deadline:
            group.signal(signal.SIGKILL)
            return True
        return False

    def wait_for_child():
        exit_deadline = None
        while True:
            if advance_stop() and exit_deadline is None:
                exit_deadline = time.monotonic() + _STOP_GRACE
            code = group.observe()
            if code is not None:
                return code
            if exit_deadline is not None and time.monotonic() >= exit_deadline:
                raise _TUIProcessError("TUI child termination timed out.")
            time.sleep(_POLL_INTERVAL)

    try:
        # Install before spawning or entering raw mode, closing the setup window
        # where SIGTERM could otherwise strand the caller's terminal settings.
        handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, request_stop)
        handlers[signal.SIGINT] = signal.signal(signal.SIGINT, interrupt)
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
            child_code = "import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execvp(sys.argv[1],sys.argv[1:])"
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, *command],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            group = _ChildGroup(process)
            os.close(slave)
            slave = None
            old_terminal = termios.tcgetattr(input_fd)
            tty.setraw(input_fd)
            handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
        else:
            process = subprocess.Popen(command, env=env, start_new_session=True)
            group = _ChildGroup(process)
        if not interactive:
            return wait_for_child()

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
            if group.observe() is not None:
                request_stop()
            if advance_stop():
                break
            now = time.monotonic()
            if loading and now >= next_update and stream.safe_to_draw():
                banner()
                next_update = now + refresh
            readers = [master] + ([input_fd] if stdin_open else [])
            ready, _, _ = select.select(readers, [], [], min(refresh, _POLL_INTERVAL))
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
            if group.observe() is not None and not ready:
                break
        stream.finish(forward)
        if banner_drawn and stream.safe_to_draw():
            banner(clear=True)
        return wait_for_child()
    finally:
        primary_error = sys.exc_info()[1]
        try:
            if group is not None:
                _stop_group(group, stop_deadline, term_sent)
        except BaseException:
            # In particular, anchor-loss cleanup must not replace cancellation
            # or another primary failure; terminal/handler/FD cleanup still runs.
            if primary_error is None:
                raise
        finally:
            try:
                if old_terminal is not None:
                    termios.tcsetattr(input_fd, termios.TCSANOW, old_terminal)
            finally:
                try:
                    for sig, previous in handlers.items():
                        signal.signal(sig, previous)
                finally:
                    for fd in (master, slave):
                        if fd is not None:
                            os.close(fd)
