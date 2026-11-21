"""One host-only confirmation while child output and input are suspended."""

import os
import secrets
import select
import termios
import time

# Do not accept a bare Enter, paste, old input, or any terminal reply. The
# challenge is generated after proposal completion and never enters the PTY.
_LETTERS = "abcdefghjkmnpqrstuvwxyz"
_LENGTH = 6
_TIMEOUT = 20


def _write(fd, data):
    while data:
        sent = os.write(fd, data)
        if sent <= 0:
            raise OSError("terminal-write")
        data = data[sent:]


def _thumbnail(fd, rows):
    width, height = os.get_terminal_size(fd)
    xstep = max(1, (len(rows[0]) // 6 + max(1, width - 2) - 1) // max(1, width - 2))
    ystep = max(
        1, (len(rows) + max(2, 2 * (height - 9)) - 1) // max(2, 2 * (height - 9))
    )
    rows = rows[::ystep]
    output = bytearray()
    for y in range(0, len(rows), 2):
        for x in range(0, len(rows[y]), 6 * xstep):
            top = bytes.fromhex(rows[y][x : x + 6])
            bottom = bytes.fromhex(rows[min(y + 1, len(rows) - 1)][x : x + 6])
            r, g, b = top
            R, G, B = bottom
            output.extend(f"\x1b[38;2;{r};{g};{b};48;2;{R};{G};{B}m▀".encode())
        output.extend(b"\x1b[0m\r\n")
    return bytes(output)


def confirm(input_fd, output_fd, *, raster=None):
    """Accept only a freshly typed challenge; never relay confirmation bytes."""
    challenge = "".join(secrets.choice(_LETTERS) for _ in range(_LENGTH)).encode(
        "ascii"
    )
    if not os.isatty(input_fd) or not os.isatty(output_fd):
        return False
    current = bytearray()
    try:
        thumbnail = _thumbnail(output_fd, raster) if raster else b""
    except (OSError, ValueError):
        return False
    prompt = (
        # Switch to the primary buffer without clearing the application's
        # alternate-screen cells.
        # Do not leave synchronized-output batching on during host input.
        b"\x1b[?2026l\x1b[0m\x1b[?47l\r\n"
        b"oc-box HOST: The workspace proposed opening one PNG in your default app.\r\n"
        + thumbnail
        + b"Type this code, then Enter, to export this snapshot: "
        + challenge
        + b"\r\n(20 seconds; Escape or Ctrl+C cancels.)\r\n"
    )
    try:
        termios.tcflush(input_fd, termios.TCIFLUSH)
        _write(output_fd, prompt)
        deadline = time.monotonic() + _TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([input_fd], [], [], remaining)[0]:
                return False
            data = os.read(input_fd, 256)
            if not data:
                return False
            for index, byte in enumerate(data):
                if byte in (10, 13):
                    return index + 1 == len(data) and secrets.compare_digest(
                        current, challenge
                    )
                if byte in (8, 127):
                    if current:
                        current.pop()
                        _write(output_fd, b"\b \b")
                    continue
                if byte not in b"abcdefghijklmnopqrstuvwxyz" or len(current) >= _LENGTH:
                    return False
                current.append(byte)
                _write(output_fd, b"*")
        return False
    except (OSError, termios.error):
        return False
    finally:
        try:
            # Drop only confirmation input. Never inject it or resend the
            # gesture to the native client. The relay requests a repaint.
            termios.tcflush(input_fd, termios.TCIFLUSH)
            _write(output_fd, b"\x1b[0m\r\n\x1b[?47h")
        except (OSError, termios.error):
            pass
