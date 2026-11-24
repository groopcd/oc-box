"""Relay the normal TUI through a PTY, showing progress until it renders text."""

import codecs
import errno
from concurrent.futures import ThreadPoolExecutor
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

if __package__:
    from . import startup_output
else:
    import startup_output


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
                elif " " <= char <= "/":
                    self.state = "esc_intermediate"
                else:
                    self.state = "text"
            elif self.state == "esc_intermediate":
                # ISO-2022 ESC sequences can contain multiple intermediates.
                # A designation's final byte is not rendered UI text.
                if char == "\x1b":
                    self.state = "escape"
                elif char in "\x18\x1a" or "0" <= char <= "~":
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
    startup_logo=None,
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
    clear_banner = None
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
        badge = (
            startup_output.relay_badge(input_fd, output_fd, startup_logo)
            if loading
            else startup_output.Badge()
        )
        banner_row = 0
        mark_cleanup = ""
        started = time.monotonic()
        next_update = started
        stdin_open = True

        def banner(clear=False):
            nonlocal banner_row, mark_cleanup
            try:
                size = os.get_terminal_size(output_fd)
            except OSError:
                return
            row = max(size.lines, 1)
            controls = "\x1b7" + mark_cleanup
            if banner_row and (clear or row != banner_row):
                controls += f"\x1b[{min(banner_row, row)};1H\x1b[2K"
            mark_cleanup = ""
            banner_row = 0
            if not clear:
                prefix = badge.fit(size.columns)
                text = (
                    "oc-box: Loading OpenCode, plugins and MCPs... "
                    f"{int(time.monotonic() - started)}s"
                )
                controls += (
                    f"\x1b[{row};1H\x1b[0m\x1b[2K"
                    + prefix.wire
                    + text[: max(size.columns - 1 - prefix.cells, 0)]
                )
                mark_cleanup = prefix.erase
                banner_row = row
            # Repaint badge and text together. DECSC/DECRC restore the cursor;
            # no launcher placement is left for the native app to erase.
            write_all(output_fd, (controls + "\x1b8").encode())

        def safe_to_paint():
            return detector.safe_to_draw() and (
                file_drops is None or file_drops.safe_to_inject()
            )

        def clear_if_drawn():
            if banner_row and safe_to_paint():
                banner(clear=True)

        clear_banner = clear_if_drawn

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

        while True:
            now = time.monotonic()
            if file_drops is not None:
                private_output = file_drops.pending_output()
                if private_output:
                    write_all(output_fd, private_output)
            if loading and now >= next_update and safe_to_paint():
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
                if not output:
                    clear_if_drawn()
                    if preview_relay is not None:
                        trailing = preview_relay.finish_output()
                        if trailing:
                            write_all(output_fd, trailing)
                    if file_drops is not None:
                        trailing = file_drops.finish_output()
                        if trailing:
                            write_all(output_fd, trailing)
                    break
                # Clear in the previous, already-forwarded state. The Kitty
                # boundary advances while filtering the next chunk, even when
                # that chunk is still waiting to be written to the terminal.
                clear_if_drawn()
                if preview_relay is not None:
                    output = preview_relay.output_bytes(output)
                if file_drops is not None:
                    output = file_drops.output_bytes(output)
                if loading and detector.feed(output):
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
        clear_if_drawn()
        return process.wait()
    except relay_errors:
        preview_framing_failed = True
        return 1
    finally:
        if clear_banner is not None:
            try:
                clear_banner()
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
        elif preview_relay is not None and preview_relay.startup_unverified():
            print(
                "oc-box: Ctrl-click host opening stayed off (native-hook-unverified). "
                "The native TUI hook did not confirm startup. If the owner predates "
                "this launcher, exit it normally and start it again.",
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
