"""Container-only, stdin-fed image/PDF staging with a pinned private lifetime.

Run this file's source with ``python3 -I -S -B -c SOURCE TOKEN`` on a private
non-PTY pipe. TOKEN is exactly 32 lowercase hexadecimal characters. There is no
host-path reader, native OpenCode integration, service discovery or execution.

The root and files are private from other users, not from the same container
user/root or its tools. Cleanup is bounded, best-effort and limited to recorded
file identities; a crash, replacement or changed ownership/mode can retain data.
Namespace checks are not atomic against same-user rename/unlink races.
Signature checks identify the same four image formats as the image broker and
the PDF header. They are not decoding, sanitization, proof of well-formed/safe
content or a guarantee of support by a selected model.
"""

import base64
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_LINE = 12 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 32
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENCODED = 4 * ((MAX_FILE + 2) // 3)
ROOT_PREFIX = "oc-box-drop-"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
NAME_PATTERN = re.compile(r"drop_[0-9a-f]{32}\.(png|jpeg|gif|webp|pdf)", re.ASCII)
MIME_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
}
CATEGORIES = frozenset(
    (
        "invalid-request",
        "root-unavailable",
        "root-changed",
        "quota-exceeded",
        "file-exists",
        "io-failure",
    )
)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(ValueError):
    """A fixed, content-free refusal, never a native exception or input."""


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _content_extension(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    if content.startswith(b"%PDF-"):
        return "pdf"
    return None


class DropStore:
    """One exclusive root and lifetime quotas; ``put`` takes decoded bytes.

    ``root`` is an absolute string; ``put(name, content)`` returns the generated
    absolute path. ``close()`` is idempotent and returns whether cleanup finished.
    ``parent`` exists only for ordinary-import filesystem fixtures. The executable
    entry point always uses /tmp and never accepts a parent/source path.
    """

    def __init__(self, token, *, parent=Path("/tmp")):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise Refused("invalid-request")
        self._parent = Path(parent)
        if (
            self._parent.anchor != "/"
            or ".." in self._parent.parts
            or len(os.fsencode(self._parent)) > 4096
        ):
            raise Refused("root-unavailable")
        self._basename = ROOT_PREFIX + token
        self.root = str(self._parent / self._basename)
        self._owner = os.geteuid()
        self._parent_fd = None
        self._root_fd = None
        self._root_identity = None
        self._root_group = None
        self._parent_identity = None
        self._files = {}
        self._used_files = 0
        self._used_bytes = 0
        self._closed = None
        try:
            self._parent_fd = os.open(self._parent, DIRECTORY_FLAGS)
            self._parent_identity = _identity(os.fstat(self._parent_fd))
            os.mkdir(self._basename, 0o700, dir_fd=self._parent_fd)
            created = os.stat(
                self._basename, dir_fd=self._parent_fd, follow_symlinks=False
            )
            self._root_identity = _identity(created)
            self._root_group = created.st_gid
            self._root_fd = os.open(
                self._basename, DIRECTORY_FLAGS, dir_fd=self._parent_fd
            )
            self._verify_root()
        except Exception:
            self.close()
            raise Refused("root-unavailable") from None

    def _verify_root(self):
        if self._closed is not None or self._parent_fd is None or self._root_fd is None:
            raise Refused("root-unavailable")
        try:
            parent = os.stat(self._parent, follow_symlinks=False)
            entry = os.stat(
                self._basename, dir_fd=self._parent_fd, follow_symlinks=False
            )
            pinned = os.fstat(self._root_fd)
            if (
                _identity(parent) != self._parent_identity
                or _identity(os.fstat(self._parent_fd)) != self._parent_identity
                or _identity(entry) != self._root_identity
                or _identity(pinned) != self._root_identity
                or not stat.S_ISDIR(pinned.st_mode)
                or entry.st_uid != self._owner
                or pinned.st_uid != self._owner
                or entry.st_gid != self._root_group
                or pinned.st_gid != self._root_group
                or os.geteuid() != self._owner
                or stat.S_IMODE(entry.st_mode) != 0o700
                or stat.S_IMODE(pinned.st_mode) != 0o700
            ):
                raise Refused("root-changed")
        except OSError:
            raise Refused("root-changed") from None

    def put(self, name, content):
        """Create one signature-matched file, never overwriting or reusing quota."""
        match = NAME_PATTERN.fullmatch(name) if isinstance(name, str) else None
        if (
            match is None
            or not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_FILE
            or _content_extension(content) != match.group(1)
        ):
            raise Refused("invalid-request")
        self._verify_root()
        if name in self._files:
            raise Refused("file-exists")
        if self._used_files >= MAX_FILES or self._used_bytes + len(content) > MAX_TOTAL:
            raise Refused("quota-exceeded")
        fd = None
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._root_fd,
            )
            # Reserve the complete file at creation, including a partial write.
            # External removal never frees a filename, file count or byte budget.
            self._files[name] = _identity(os.fstat(fd))
            self._used_files += 1
            self._used_bytes += len(content)
            os.fchmod(fd, 0o600)
            remaining = memoryview(content)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise Refused("io-failure")
                remaining = remaining[written:]
            info = os.fstat(fd)
            entry = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if (
                _identity(info) != self._files[name]
                or _identity(entry) != self._files[name]
                or info.st_uid != self._owner
                or entry.st_uid != self._owner
                or info.st_gid != self._root_group
                or entry.st_gid != self._root_group
                or stat.S_IMODE(info.st_mode) != 0o600
                or stat.S_IMODE(entry.st_mode) != 0o600
                or info.st_nlink != 1
                or entry.st_nlink != 1
                or info.st_size != len(content)
            ):
                raise Refused("io-failure")
            self._verify_root()
        except FileExistsError:
            raise Refused("file-exists") from None
        except OSError:
            raise Refused("io-failure") from None
        finally:
            if fd is not None:
                os.close(fd)
        return self.root + "/" + name

    def close(self):
        """Remove only recorded file identities; never crawl or follow links."""
        if self._closed is not None:
            return self._closed
        clean = True
        try:
            if self._root_fd is not None:
                self._verify_root()
                for name, identity in self._files.items():
                    self._verify_root()
                    try:
                        info = os.stat(
                            name, dir_fd=self._root_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if (
                        _identity(info) != identity
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != self._owner
                        or info.st_gid != self._root_group
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                    ):
                        clean = False
                        continue
                    os.unlink(name, dir_fd=self._root_fd)
                self._verify_root()
                # rmdir is relative to the original parent, after verifying the
                # canonical entry still names the pinned root. Never recurse.
                os.rmdir(self._basename, dir_fd=self._parent_fd)
            elif self._root_identity is not None:
                # A failed open cannot authorize deleting an unpinned directory.
                clean = False
        except Exception:
            clean = False
        finally:
            for attribute in ("_root_fd", "_parent_fd"):
                fd = getattr(self, attribute)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        clean = False
                    setattr(self, attribute, None)
            self._closed = clean
        return clean


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refused("invalid-request")
        value[key] = item
    return value


def _constant(value):
    raise Refused("invalid-request")


def _request(raw):
    """Parse one bounded JSON line without accepting paths or extra authority."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_LINE:
        raise Refused("invalid-request")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(value, dict):
            raise Refused("invalid-request")
        if value == {"op": "close"}:
            return "close", None, None
        if (
            set(value) != {"op", "name", "data"}
            or value["op"] != "put"
            or not isinstance(value["name"], str)
            or not NAME_PATTERN.fullmatch(value["name"])
            or not isinstance(value["data"], str)
            or not 0 < len(value["data"]) <= MAX_ENCODED
        ):
            raise Refused("invalid-request")
        content = base64.b64decode(value["data"], validate=True)
        if not 0 < len(content) <= MAX_FILE:
            raise Refused("invalid-request")
        return "put", value["name"], content
    except Exception:
        raise Refused("invalid-request") from None


def _emit(value):
    """Avoid buffered stdout retaining content-independent errors at shutdown."""
    try:
        remaining = memoryview(
            (json.dumps(value, separators=(",", ":")) + "\n").encode("ascii")
        )
        while remaining:
            written = os.write(sys.stdout.fileno(), remaining)
            if written <= 0:
                return False
            remaining = remaining[written:]
        return True
    except Exception:
        return False


def main():
    store = None
    failed = False
    try:
        if len(sys.argv) != 2 or not TOKEN_PATTERN.fullmatch(sys.argv[1]):
            raise Refused("invalid-request")
        if os.isatty(sys.stdin.fileno()) or os.isatty(sys.stdout.fileno()):
            raise Refused("invalid-request")
        store = DropStore(sys.argv[1])
        if not _emit({"ok": True, "root": store.root}):
            raise Refused("io-failure")
        while True:
            raw = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not raw:
                break
            operation, name, content = _request(raw)
            if operation == "close":
                break
            path = store.put(name, content)
            if not _emit({"ok": True, "path": path}):
                raise Refused("io-failure")
    except (Exception, KeyboardInterrupt) as error:
        failed = True
        category = (
            str(error)
            if isinstance(error, Refused) and str(error) in CATEGORIES
            else "io-failure"
        )
        _emit({"ok": False, "category": category})
    finally:
        if store is not None:
            closed = store.close()
            if not _emit({"ok": True, "closed": closed}) or not closed:
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
