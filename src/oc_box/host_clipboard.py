"""Host-only PNG snapshots, read solely on an explicit host input gesture.

Preflight discovers a local helper without reading the clipboard. PNG signature
checks identify a format only; they do not decode or sanitize image content.
"""

import os
import select
import shutil
import subprocess
import time

MAX_BYTES = 8 * 1024 * 1024
READ_TIMEOUT = 2.0
REAP_TIMEOUT = 0.25
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DESKTOP_ENV = frozenset(
    (
        "HOME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XAUTHORITY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    )
)


class ClipboardError(ValueError):
    """Fixed messages only, never clipboard data, helper errors or paths."""


def missing_helper(helper):
    # These host package names are shared by all seven supported distros.
    # Guidance only: no package manager or clipboard helper is executed.
    package = "wl-clipboard" if helper == "wl-paste" else "xclip"
    return ClipboardError(
        f"Host clipboard utility '{helper}' is missing or unavailable on PATH. "
        f"Install the '{package}' package on the host, not inside oc-box."
    )


def preflight() -> "Reader":
    """Discover the selected desktop helper; never execute it or read data."""
    if "WAYLAND_DISPLAY" in os.environ:
        helper, arguments = "wl-paste", ("--no-newline", "--type", "image/png")
    elif os.environ.get("DISPLAY"):
        helper, arguments = (
            "xclip",
            ("-selection", "clipboard", "-target", "image/png", "-out"),
        )
    else:
        raise ClipboardError("Host clipboard requires a desktop session.")
    try:
        executable = shutil.which(helper)
        if executable is None:
            raise missing_helper(helper)
        command = (os.path.abspath(executable), *arguments)
        environment = {
            key: value for key, value in os.environ.items() if key in _DESKTOP_ENV
        }
        environment["PATH"] = os.defpath
        return Reader(command, environment)
    except ClipboardError:
        raise
    except (OSError, ValueError):
        raise missing_helper(helper) from None


def _reap(process):
    """Bounded cleanup of this helper only, never a process group."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=REAP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass


class Reader:
    """A discovered command and sanitized environment, not a clipboard cache."""

    __slots__ = ("command", "env")

    def __init__(self, command, environment):
        self.command = tuple(command)
        self.env = dict(environment)

    def read_png(self) -> bytes:
        """Read one bounded snapshot after the caller's explicit user gesture."""
        process = None
        try:
            deadline = time.monotonic() + READ_TIMEOUT
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                shell=False,
                close_fds=True,
                bufsize=0,
            )
            descriptor = process.stdout.fileno()
            os.set_blocking(descriptor, False)
            output = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClipboardError("Host clipboard read timed out.")
                try:
                    ready, _, _ = select.select([descriptor], [], [], remaining)
                    if not ready:
                        raise ClipboardError("Host clipboard read timed out.")
                    chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(output)))
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_BYTES:
                    raise ClipboardError("Host clipboard image exceeds 8 MiB.")
            if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise ClipboardError("Host clipboard read failed.")
            if not output.startswith(_PNG_SIGNATURE):
                raise ClipboardError("Host clipboard does not contain a PNG image.")
            return bytes(output)
        except ClipboardError:
            raise
        except subprocess.TimeoutExpired:
            raise ClipboardError("Host clipboard read timed out.") from None
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ClipboardError("Host clipboard read failed.") from None
        finally:
            if process is not None:
                _reap(process)
