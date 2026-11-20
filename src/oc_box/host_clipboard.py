"""Host clipboard images/file references, read only on a host input gesture.

Preflight discovers a local helper without reading the clipboard. PNG signature
checks identify a format only; they do not decode or sanitize image content.
"""

import os
import select
import shutil
import subprocess
import time

MAX_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 16384
MAX_TARGETS = 128
FILE_TYPES = ("text/uri-list", "x-special/gnome-copied-files")
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
        return Reader(command, environment, helper=helper)
    except ClipboardError:
        raise
    except (OSError, ValueError):
        raise missing_helper(helper) from None


def _reap(process):
    """Bounded cleanup of this helper only; return verified reaping."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    verified = False
    try:
        verified = type(process.wait(timeout=REAP_TIMEOUT)) is int
    except (OSError, subprocess.SubprocessError):
        pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    return verified


class Reader:
    """A discovered command and sanitized environment, not a clipboard cache."""

    __slots__ = ("command", "env", "helper", "cleanup_verified")

    def __init__(self, command, environment, *, helper):
        if helper not in ("wl-paste", "xclip"):
            raise ClipboardError("Host clipboard helper is unsupported.")
        self.command = tuple(command)
        self.env = dict(environment)
        self.helper = helper
        self.cleanup_verified = True

    def _command(self, mime=None):
        if self.helper == "wl-paste":
            arguments = (
                ("--list-types",)
                if mime is None
                else (
                    "--no-newline",
                    "--type",
                    mime,
                )
            )
        else:
            arguments = (
                "-selection",
                "clipboard",
                "-target",
                mime or "TARGETS",
                "-out",
            )
        return (self.command[0], *arguments)

    def read_selection(self):
        """One bounded type negotiation and selected payload read; no retries."""
        deadline = time.monotonic() + READ_TIMEOUT
        offered = self._read(self._command(), MAX_METADATA_BYTES, deadline)
        try:
            targets = offered.decode("ascii").split()
        except UnicodeError:
            raise ClipboardError(
                "Host clipboard offered invalid content types."
            ) from None
        if len(targets) > MAX_TARGETS:
            raise ClipboardError("Host clipboard offered too many content types.")
        # Explicit copied files win over an application's optional PNG thumbnail.
        mime = next(
            (kind for kind in (*FILE_TYPES, "image/png") if kind in targets), None
        )
        if mime is None:
            raise ClipboardError(
                "Host clipboard contains no supported image or copied files."
            )
        limit = MAX_BYTES if mime == "image/png" else MAX_METADATA_BYTES
        data = self._read(self._command(mime), limit, deadline)
        if mime == "image/png" and not data.startswith(_PNG_SIGNATURE):
            raise ClipboardError("Host clipboard does not contain a PNG image.")
        return mime, data

    def read_png(self) -> bytes:
        """Explicit PNG-only read, retained for the bounded image reader."""
        data = self._read(self.command, MAX_BYTES, time.monotonic() + READ_TIMEOUT)
        if not data.startswith(_PNG_SIGNATURE):
            raise ClipboardError("Host clipboard does not contain a PNG image.")
        return data

    def _read(self, command, limit, deadline):
        process = None
        try:
            if not self.cleanup_verified:
                raise ClipboardError("Host clipboard helper cleanup is unverified.")
            if time.monotonic() >= deadline:
                raise ClipboardError("Host clipboard read timed out.")
            self.cleanup_verified = False
            process = subprocess.Popen(
                command,
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
                    chunk = os.read(descriptor, min(65536, limit + 1 - len(output)))
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ClipboardError("Host clipboard data exceeds its size limit.")
            if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise ClipboardError("Host clipboard read failed.")
        except ClipboardError:
            raise
        except subprocess.TimeoutExpired:
            raise ClipboardError("Host clipboard read timed out.") from None
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ClipboardError("Host clipboard read failed.") from None
        finally:
            if process is not None:
                self.cleanup_verified = _reap(process)
        if not self.cleanup_verified:
            raise ClipboardError("Host clipboard helper cleanup is unverified.")
        return bytes(output)
