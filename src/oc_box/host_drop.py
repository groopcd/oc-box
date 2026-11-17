"""Opt-in native terminal drops; host input is the only selection surface.

No model/server/MCP request can select a host pathname here. Structured Kitty
drops use terminal-offered data; the fallback deliberately authorizes qualifying
pathname pastes, not a purported proof that Spectacle originated the text.
"""

import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

if __package__:
    from . import host_drop_input, sharing
else:
    import host_drop_input
    import sharing

SUPPORTED = frozenset(("2.2.6", "2.2.7", "2.3.0"))
MAX_FILE = 8 * 1024 * 1024
MAX_BATCH = 4
MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_FILES = 32
MAX_CLIENT_BYTES = 64 * 1024 * 1024
MAX_PATH_PASTE = 16384
MIMES = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
)
SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}
SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
REFUSED = b"[oc-box: host file import refused]"
WARNING = (
    "Experimental host file drops enabled. A supported absolute pathname "
    "pasted anywhere in this native TUI connection authorizes a temporary "
    "container copy, not just a Spectacle drag. PNG/JPEG/GIF/WebP/PDF only, "
    "8 MiB each. Model support varies. Files may be read by workspace tools, "
    "retained in history/provider systems or survive a crash. This is not "
    "the separate confirmation-based attachment broker."
)


class DropError(ValueError):
    """Fixed categories only, never private paths, data or native errors."""


def preflight(version=None):
    if version is not None and (
        not isinstance(version, str) or version not in SUPPORTED
    ):
        raise DropError("Host file drops require OpenCode 2.2.6, 2.2.7 or 2.3.0.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DropError("Host file drops require a direct interactive host terminal.")
    if any(os.environ.get(key) for key in ("TMUX", "STY", "SSH_CONNECTION", "SSH_TTY")):
        raise DropError("Host file drops do not yet support multiplexers or SSH.")
    return terminal_profile()


def terminal_profile():
    """Compatibility selection by a trusted host operator, not authentication."""
    konsole = bool(re.fullmatch(r"[0-9]{4,10}", os.environ.get("KONSOLE_VERSION", "")))
    kitty = bool(
        re.fullmatch(r"[1-9][0-9]{0,9}", os.environ.get("KITTY_WINDOW_ID", ""))
    )
    if konsole == kitty:
        raise DropError(
            "Host drops require one direct Konsole or Kitty profile; unknown or inherited ambiguous terminal markers are refused."
        )
    return "konsole" if konsole else "kitty"


def mime_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    raise DropError("unsupported-content")


def valid_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or len(path.encode("utf-8")) > 4096
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
    ):
        raise DropError("invalid-path")
    return Path(path)


def pasted_paths(payload):
    """A narrow complete-frame grammar, never shell evaluation or expansion."""
    try:
        text = payload.decode("utf-8", errors="strict")
        if len(payload) > MAX_PATH_PASTE or any(
            ord(char) < 32 or 127 <= ord(char) <= 159 for char in text
        ):
            return None
        words = shlex.split(text, comments=False, posix=True)
        if not 1 <= len(words) <= MAX_BATCH:
            return None
        paths = [valid_path(word) for word in words]
        if any(path.suffix.lower() not in SUFFIXES for path in paths):
            return None
        return paths
    except (ValueError, UnicodeError):
        return None


def uri_paths(data):
    """Structured drop URIs are selection data, not general URL fetch requests."""
    try:
        text = data.decode("utf-8", errors="strict")
        if len(data) > MAX_PATH_PASTE:
            raise DropError("uri-list-limit")
        uris = [
            line.strip()
            for line in text.splitlines()
            if line and not line.startswith("#")
        ]
        if not 1 <= len(uris) <= MAX_BATCH:
            raise DropError("uri-list-limit")
        paths = []
        for uri in uris:
            parsed = urlsplit(uri)
            if (
                parsed.scheme != "file"
                or parsed.netloc not in ("", "localhost")
                or parsed.query
                or parsed.fragment
                or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
            ):
                raise DropError("non-local-uri")
            path = valid_path(unquote(parsed.path, encoding="utf-8", errors="strict"))
            if path.suffix.lower() not in SUFFIXES:
                raise DropError("unsupported-content")
            paths.append(path)
        return paths
    except (ValueError, UnicodeError):
        raise DropError("invalid-uri-list") from None


def capture(paths, protected):
    """Pin every source first; validate/read bounded snapshots before staging."""
    try:
        if not 1 <= len(paths) <= MAX_BATCH:
            raise DropError("batch-limit")
        images, total = [], 0
        with contextlib.ExitStack() as stack:
            exports = [
                stack.enter_context(sharing.Export(path, protected=protected))
                for path in paths
            ]
            for export in exports:
                with export.open(".") as (fd, before):
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or not 0 < before.st_size <= MAX_FILE
                    ):
                        raise DropError("file-limit")
                    data = bytearray()
                    while len(data) <= MAX_FILE:
                        chunk = os.pread(
                            fd, min(65536, MAX_FILE + 1 - len(data)), len(data)
                        )
                        if not chunk:
                            break
                        data.extend(chunk)
                    after = os.fstat(fd)
                    if len(data) > MAX_FILE or any(
                        getattr(before, key) != getattr(after, key)
                        for key in (
                            "st_dev",
                            "st_ino",
                            "st_nlink",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                    ):
                        raise DropError("source-changed")
                content = bytes(data)
                mime = mime_type(content)
                if SUFFIX_MIME.get(export.path.suffix.lower()) != mime:
                    raise DropError("extension-content-mismatch")
                total += len(content)
                if total > MAX_BATCH_BYTES:
                    raise DropError("batch-limit")
                images.append((mime, content))
        return images
    except DropError:
        raise
    except (sharing.ShareError, OSError, ValueError, UnicodeError):
        raise DropError("host-source-refused") from None


class Transfer:
    """One private non-PTY helper with pinned temporary storage, no listener."""

    def __init__(self, box, data, connection):
        self.box, self.data, self.connection = box, data, connection
        self.token = secrets.token_hex(16)
        self.root = "/tmp/oc-box-drop-" + self.token
        self.process = None
        self.buffer = bytearray()
        self.files = self.total = 0
        self.closed = self.failed = False
        self.cleanup_verified = True

    def _line(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(self.buffer) > 4096:
                raise DropError("transfer-timeout")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if ready:
                chunk = os.read(
                    self.process.stdout.fileno(), min(4096, 4097 - len(self.buffer))
                )
                if not chunk:
                    raise DropError("transfer-closed")
                self.buffer.extend(chunk)
        line, _, self.buffer = self.buffer.partition(b"\n")
        if len(line) > 4096:
            raise DropError("transfer-response")
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError):
            raise DropError("transfer-response") from None
        if not isinstance(result, dict):
            raise DropError("transfer-response")
        return result

    def exchange(self, value):
        deadline = time.monotonic() + 30
        content = memoryview((json.dumps(value) + "\n").encode())
        while content:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DropError("transfer-timeout")
            _, ready, _ = select.select([], [self.process.stdin], [], remaining)
            if ready:
                try:
                    written = os.write(self.process.stdin.fileno(), content[:65536])
                    content = content[written:]
                except BlockingIOError:
                    continue
        return self._line(deadline)

    def __enter__(self):
        try:
            command = self.box.workspace_service.client_command(
                self.box,
                self.data,
                self.connection,
                [
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    (self.box.PACKAGE / "host_drop_io.py").read_text(),
                    self.token,
                ],
            )
            self.process = subprocess.Popen(
                command,
                env=command.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            if self._line(time.monotonic() + 10) != {"ok": True, "root": self.root}:
                raise DropError("transfer-start")
            return self
        except (DropError, OSError, ValueError, subprocess.SubprocessError):
            self.failed = True
            self.close()
            raise DropError("Private host-drop transfer could not start.") from None

    def put(self, images):
        if (
            self.closed
            or self.failed
            or not 1 <= len(images) <= MAX_BATCH
            or self.files + len(images) > MAX_FILES
            or self.total + sum(len(data) for _, data in images) > MAX_CLIENT_BYTES
        ):
            raise DropError("staging-limit")
        paths, total = [], 0
        try:
            for mime, data in images:
                if not 0 < len(data) <= MAX_FILE or mime_type(data) != mime:
                    raise DropError("unsupported-content")
                total += len(data)
                if total > MAX_BATCH_BYTES:
                    raise DropError("batch-limit")
            for mime, data in images:
                name = "drop_" + secrets.token_hex(16) + "." + mime.split("/", 1)[1]
                expected = self.root + "/" + name
                response = self.exchange(
                    {
                        "op": "put",
                        "name": name,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
                if response != {"ok": True, "path": expected}:
                    raise DropError("transfer-response")
                paths.append(expected)
            self.files += len(images)
            self.total += total
            return " ".join(shlex.quote(path) for path in paths).encode("utf-8")
        except (DropError, OSError, ValueError, subprocess.SubprocessError):
            self.failed = True
            self.close()
            raise DropError("temporary-import-failed") from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None and not self.failed:
                response = self.exchange({"op": "close"})
                self.cleanup_verified = response == {"ok": True, "closed": True}
            else:
                self.cleanup_verified = False
        except (DropError, OSError, ValueError, subprocess.SubprocessError):
            self.cleanup_verified = False
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.cleanup_verified = False
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            process.stdout.close()
            if process.returncode != 0:
                self.cleanup_verified = False

    def __exit__(self, *_):
        self.close()


class Relay:
    def __init__(self, box, data, connection, *, profile=None):
        self.box, self.data, self.connection = box, data, connection
        self.profile = profile or terminal_profile()
        if self.profile not in ("konsole", "kitty"):
            raise DropError("unsupported-terminal-profile")
        self.protected = sharing.protected_paths(box)
        self.transfer = Transfer(box, data, connection)
        self.refused = 0
        self.protocol = None
        self.ident = None
        self.pending = bytearray()
        self.broken = False
        self.output = (
            host_drop_input.OutputFilter() if self.profile == "kitty" else None
        )
        self.input = host_drop_input.PasteInput(
            self.paste,
            on_osc72=self.control if self.profile == "kitty" else None,
            max_paste=MAX_PATH_PASTE,
            reserve_osc72=self.profile == "kitty",
        )

    def __enter__(self):
        self.transfer.__enter__()
        return self

    def stage(self, images):
        return self.transfer.put(images)

    def paste(self, payload):
        paths = pasted_paths(payload)
        if paths is None:
            return None
        if self.broken:
            return REFUSED
        try:
            return self.stage(capture(paths, self.protected))
        except DropError:
            self.refused += 1
            return REFUSED

    def start(self):
        if self.profile != "kitty":
            return
        self.protocol = KittyDrop(self, self.emit)
        self.ident = self.protocol.ident
        self.protocol.start()

    def emit(self, data):
        if len(self.pending) + len(data) > 4096:
            raise DropError("protocol-output-limit")
        self.pending.extend(data)

    def pending_output(self):
        if self.output is None:
            return b""
        try:
            if self.protocol:
                self.protocol.tick()
        except DropError:
            self.disable()
        if not self.output.safe_to_inject():
            return b""
        data = bytes(self.pending)
        self.pending.clear()
        return data

    def control(self, payload):
        # Protocol 72 is reserved by this opted-in host connection. Never expose
        # its raw file-data replies to the container as keyboard input.
        try:
            return self.protocol.receive(payload) if self.protocol else True
        except DropError:
            self.disable()
            return True

    def disable(self):
        self.broken = True
        self.refused += 1
        self.pending.clear()
        if self.protocol:
            ident = self.protocol.ident
            self.protocol = None
            self.pending.extend(b"\x1b]72;t=A:i=" + ident.encode() + b";\x1b\\")

    def input_bytes(self, data):
        return self.input.feed(data)

    def output_bytes(self, data):
        if self.output is None:
            return data
        try:
            return self.output.feed(data)
        except host_drop_input.ReservationError:
            raise DropError(
                "Terminal output exceeded the supported file-drop framing limits."
            ) from None

    def finish_input(self):
        return self.input.finish()

    def finish_output(self):
        try:
            return self.output.finish() if self.output is not None else b""
        except host_drop_input.ReservationError:
            raise DropError(
                "Terminal output exceeded the supported file-drop framing limits."
            ) from None

    def stop(self):
        # Called only after the native client has exited. Do not send queued
        # data requests during teardown. ST safely closes any truncated native
        # control string before withdrawing our terminal drop registration.
        self.pending.clear()
        protocol, self.protocol = self.protocol, None
        if self.ident is None:
            return b""
        prefix = b"\x1b\\"
        if protocol is not None and protocol.index is not None:
            prefix += b"\x1b]72;t=r:o=0:i=" + self.ident.encode() + b";\x1b\\"
        return prefix + b"\x1b]72;t=A:i=" + self.ident.encode() + b";\x1b\\"

    def __exit__(self, *_):
        self.transfer.__exit__()
        if self.refused:
            print(
                f"oc-box: {self.refused} host file drop(s) refused; no automatic retry was made.",
                file=sys.stderr,
            )
        if not self.transfer.cleanup_verified:
            print(
                "oc-box: Temporary import cleanup could not be verified; container files may remain. No workspace was restarted for cleanup.",
                file=sys.stderr,
            )


class KittyDrop:
    """Bounded OSC 72 DnD client, not the unrelated Kitty graphics protocol."""

    def __init__(self, relay, write, *, clock=time.monotonic):
        self.relay, self.write, self.clock = relay, write, clock
        self.ident = str(secrets.randbelow(2**31 - 1) + 1)
        self.registered = False
        self.index = None
        self.mime = None
        self.data = bytearray()
        self.chain = None
        self.chain_data = bytearray()
        self.deadline = None

    def emit(self, fields, data=b""):
        header = ":".join(
            f"{key}={value}" for key, value in {**fields, "i": self.ident}.items()
        ).encode("ascii")
        self.write(b"\x1b]72;" + header + b";" + data + b"\x1b\\")

    def start(self):
        self.emit({"t": "q"})

    def finish(self, operation=0):
        if self.index is not None:
            self.emit({"t": "r", "o": operation})
        self.index = self.mime = self.deadline = self.chain = None
        self.data.clear()
        self.chain_data.clear()

    def close(self):
        self.finish()
        if self.registered:
            self.emit({"t": "A"})
            self.registered = False

    def tick(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.relay.refused += 1
            self.finish()

    @staticmethod
    def metadata(value):
        fields = {}
        for item in value.decode("ascii").split(":"):
            key, separator, data = item.partition("=")
            if (
                not separator
                or key in fields
                or key
                not in {
                    "t",
                    "i",
                    "m",
                    "o",
                    "x",
                    "y",
                    "X",
                    "Y",
                }
            ):
                raise DropError("drop-metadata")
            fields[key] = data
        return fields

    def receive(self, content):
        try:
            if not content.startswith(b"72;") or len(content) > 8192:
                return True
            header, _, payload = content[3:].partition(b";")
            fields = self.metadata(header)
            if fields.get("i") != self.ident:
                return True
            kind = fields.get("t")
            if kind == "q":
                if not self.registered:
                    accepted = b" ".join(
                        mime.encode() for mime in (*MIMES, "text/uri-list")
                    )
                    self.emit({"t": "a"}, accepted)
                    self.registered = True
                return True
            if self.chain is not None:
                # Compliant senders may repeat metadata or omit everything
                # except i/m. Only the FIRST chunk's MIME/index/type applies.
                fields = {**self.chain, "m": fields.get("m", "0")}
                kind = fields.get("t")
            if fields.get("m", "0") not in ("0", "1") or len(payload) > 4096:
                raise DropError("drop-chunk-limit")
            if fields.get("m") == "1" or self.chain is not None:
                if self.chain is None:
                    self.chain = dict(fields)
                self.chain_data.extend(payload)
                maximum = (MAX_FILE + 2) // 3 * 4
                if len(self.chain_data) > maximum:
                    raise DropError("drop-file-limit")
                if fields.get("m") == "1":
                    return True
                payload = bytes(self.chain_data)
                fields, self.chain = self.chain, None
                self.chain_data.clear()
                kind = fields.get("t")
            if kind == "m":
                if fields.get("x") == "-1" and fields.get("y") == "-1":
                    self.finish()
                    return True
                offered = payload.decode("ascii").split()
                choice = next(
                    (mime for mime in (*MIMES, "text/uri-list") if mime in offered),
                    None,
                )
                self.emit(
                    {"t": "m", "o": 1 if choice else 0},
                    choice.encode() if choice else b"",
                )
                return True
            if kind == "M":
                if not self.registered:
                    return True
                self.finish()
                offered = payload.decode("ascii").split()
                if len(offered) > 64:
                    raise DropError("drop-offer-limit")
                self.mime = next(
                    (mime for mime in (*MIMES, "text/uri-list") if mime in offered),
                    None,
                )
                if self.mime is None:
                    return True
                self.index = offered.index(self.mime) + 1
                self.deadline = self.clock() + 30
                self.emit({"t": "r", "x": self.index})
                return True
            if kind in ("R", "E"):
                self.finish()
                self.relay.refused += 1
                return True
            if kind != "r" or self.index is None:
                return True
            if fields.get("x") != str(self.index):
                raise DropError("drop-index")
            if payload:
                try:
                    decoded = base64.b64decode(
                        payload + b"=" * (-len(payload) % 4),
                        validate=True,
                    )
                except (ValueError, UnicodeError):
                    raise DropError("drop-base64") from None
                self.data.extend(decoded)
                limit = MAX_PATH_PASTE if self.mime == "text/uri-list" else MAX_FILE
                if len(self.data) > limit:
                    raise DropError("drop-file-limit")
                return True
            if self.mime == "text/uri-list":
                images = capture(uri_paths(bytes(self.data)), self.relay.protected)
            else:
                image = bytes(self.data)
                mime = mime_type(image)
                if mime != self.mime:
                    raise DropError("offered-content-mismatch")
                images = [(mime, image)]
            replacement = self.relay.stage(images)
            self.finish(operation=1)
            return b"\x1b[200~" + replacement + b"\x1b[201~"
        except (DropError, OSError, ValueError, UnicodeError):
            self.relay.refused += 1
            self.finish()
            return b"\x1b[200~" + REFUSED + b"\x1b[201~"
