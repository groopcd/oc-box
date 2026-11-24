"""Display and confirm the exact uninstall review on the controlling terminal."""

import os
import termios

if __package__:
    from . import uninstall_plan
else:
    import uninstall_plan


_PROMPT = b"Type the required confirmation, then press Enter: "


def _write(fd, text):
    with memoryview(text) as data:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if not written:
                raise OSError("Uninstall review was not displayed.")
            offset += written


def confirm(snapshot):
    """Return one local approval, not an execution permit or identity recheck."""
    if (
        type(snapshot) is not uninstall_plan.UninstallSnapshot
        or snapshot.request.dry_run
        or snapshot.blockers
        or snapshot.installation is None
        or (snapshot.request.full and snapshot.workspace is None)
    ):
        return False
    review = snapshot.render().encode("ascii")
    expected = snapshot.request.phrase.encode("ascii") + b"\n"
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
        try:
            pgrp = os.getpgrp()
            if (
                not os.isatty(fd)
                or os.tcgetpgrp(fd) != pgrp
                or not termios.tcgetattr(fd)[3] & termios.ICANON
            ):
                return False
            _write(fd, review + b"\n")
            # A line queued before the operator saw the review is not consent.
            termios.tcflush(fd, termios.TCIFLUSH)
            _write(fd, _PROMPT)
            answer = os.read(fd, len(expected) + 1)
            return answer == expected and os.tcgetpgrp(fd) == pgrp
        finally:
            os.close(fd)
    except (OSError, termios.error):
        return False
