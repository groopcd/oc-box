"""One container-only text preview over private non-PTY stdin/stdout pipes.

Run this trusted file with ``python3 -I -S -B``, close stdin after one UTF-8
JSON request, and use stdout only after exit 0. The production caller supplies
the two exact, externally verified Podman mount destinations and directory
identities. An empty roots array permits no caller-authorized mount crossings.
"""

from contextlib import ExitStack
import ctypes
import json
import os
import re
import signal
import stat
import sys
import unicodedata


MAX_REQUEST = 17 * 1024
MAX_CONTENT = 256 * 1024
MAX_PNG_INPUT = 4 * 1024 * 1024
MAX_MOUNTINFO = 4 * 1024 * 1024
READ_DEADLINE = 3
REFUSAL = 1
BLOCKED = (b"/proc", b"/sys", b"/dev", b"/run")
ARCHITECTURES = {
    "x86_64",
    "aarch64",
    "riscv64",
    "i386",
    "i686",
    "armv7l",
    "ppc64le",
    "s390x",
}
# openat2: RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS.
CONFINED = 0x08 | 0x04 | 0x02
NO_XDEV = 0x01
PROC_SUPER_MAGIC = 0x9FA0
MOUNT_ESCAPES = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}


class Refused(ValueError):
    """Fixed refusal, never include request data or OS diagnostics in a reply."""


def _within(path, root):
    return root == b"/" or path == root or path.startswith(root + b"/")


def _control(char):
    return unicodedata.category(char) in {"Cc", "Cf", "Cs"}


def _path(value):
    if not isinstance(value, str) or any(map(_control, value)):
        raise Refused()
    path = value.encode("utf-8")
    if (
        len(path) >= 4096
        or not path.startswith(b"/")
        or any(part in (b"", b".", b"..") for part in path[1:].split(b"/"))
        or any(_within(path, blocked) for blocked in BLOCKED)
    ):
        raise Refused()
    return path


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Refused()
    return result


def _request(content):
    if len(content) > MAX_REQUEST:
        raise Refused()
    value = json.loads(content.decode("utf-8"), object_pairs_hook=_object)
    if not isinstance(value, dict) or not {"path", "roots"} <= value.keys() <= {
        "path",
        "roots",
        "identities",
    }:
        raise Refused()
    roots = value["roots"]
    if not isinstance(roots, list) or len(roots) not in (0, 2):
        raise Refused()
    roots = tuple(map(_path, roots))
    if roots and roots[0] == roots[1]:
        raise Refused()
    identities = value.get("identities", [])
    if not isinstance(identities, list) or len(identities) != len(roots):
        raise Refused()
    expected = {}
    for root, pair in zip(roots, identities):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(
                isinstance(x, str) and re.fullmatch("[0-9]{1,20}", x) for x in pair
            )
        ):
            raise Refused()
        expected[root] = tuple(map(int, pair))
    return _path(value["path"]), roots, expected


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _openat2(parent, path, flags, *, cross=False):
    # Same supported Linux ABIs as the live-share reader. Never fall back to
    # a pathname lookup with weaker resolution on old kernels or seccomp denial.
    how = _OpenHow(flags | os.O_CLOEXEC, 0, CONFINED | (0 if cross else NO_XDEV))
    fd = ctypes.CDLL(None, use_errno=True).syscall(
        ctypes.c_long(437),
        ctypes.c_int(parent),
        ctypes.c_char_p(path),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if fd < 0:
        raise Refused()
    return fd


def _keep(stack, fd):
    stack.callback(os.close, fd)
    return fd


def _procfs(fd):
    # f_type is the first Linux statfs ABI word. Use oversized aligned
    # storage; we don't interpret the architecture-dependent remaining fields.
    result = (ctypes.c_long * 64)()
    return (
        ctypes.CDLL(None).fstatfs(ctypes.c_int(fd), ctypes.byref(result)) == 0
        and result[0] == PROC_SUPER_MAGIC
    )


def _proc_read(pid, entry, limit):
    fd = _openat2(pid, entry, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise Refused()
    return content


def _mount_id(pid, fd):
    lines = _proc_read(pid, b"fdinfo/" + str(fd).encode("ascii"), 4096).splitlines()
    matches = [line[7:].strip() for line in lines if line.startswith(b"mnt_id:")]
    if len(matches) != 1 or not matches[0].isdigit():
        raise Refused()
    return int(matches[0])


def _mount_path(field):
    # Decode exactly once: \134040 represents a literal backslash then "040".
    return re.sub(rb"\\(040|011|012|134)", lambda m: MOUNT_ESCAPES[m[1]], field)


def _mounts(content):
    entries = {}
    seen_ids = set()
    if not content.endswith(b"\n"):
        raise Refused()
    for line in content[:-1].split(b"\n"):
        fields = line.split(b" ")
        if len(fields) < 10 or not fields[0].isdigit() or not fields[1].isdigit():
            raise Refused()
        sep = fields.index(b"-", 6)
        if len(fields) != sep + 4:
            raise Refused()
        mount_id, parent_id = int(fields[0]), int(fields[1])
        target = _mount_path(fields[4])
        if mount_id in seen_ids or not target.startswith(b"/"):
            raise Refused()
        seen_ids.add(mount_id)
        entries.setdefault(target, []).append((mount_id, parent_id, line))
    return entries


def _exact_mount(entries, location, *, parent=None):
    matches = entries.get(location, ())
    if len(matches) != 1 or (parent is not None and matches[0][1] != parent):
        # No missing roots, stacked mounts, or mounts attached beneath an
        # unexpected parent (even same-filesystem binds).
        raise Refused()
    return matches[0]


def _pin_mount(parent_fd, relative, flags):
    # Permit exactly one mount crossing at the final component. Do not let
    # a foreign mount at a prefix participate in resolving the mount point.
    prefix, _, name = relative.rpartition(b"/")
    if not prefix:
        return _openat2(parent_fd, name, flags, cross=True)
    directory = _openat2(parent_fd, prefix, flags)
    try:
        return _openat2(directory, name, flags, cross=True)
    finally:
        os.close(directory)


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _pin_roots(stack, namespace, entries, pid, roots, flags):
    # Pin the outer root first. The normal project's mount can sit inside the
    # named home (the project's absolute path is preserved inside the box).
    pinned = {b"/": namespace}
    for location in sorted(roots, key=len):
        parent = max((root for root in pinned if _within(location, root)), key=len)
        parent_fd, parent_mount = pinned[parent]
        mount = _exact_mount(entries, location, parent=parent_mount[0])
        fd = _keep(
            stack, _pin_mount(parent_fd, location[len(parent) :].lstrip(b"/"), flags)
        )
        if _mount_id(pid, fd) != mount[0]:
            raise Refused()
        pinned[location] = (fd, mount)
    return pinned


def _namespace(stack, roots):
    flags = os.O_PATH | os.O_DIRECTORY
    root = _keep(stack, os.open("/", flags | os.O_CLOEXEC))
    proc = _keep(stack, _pin_mount(root, b"proc", flags))
    process = str(os.getpid()).encode("ascii")
    if not _procfs(proc) or os.readlink(b"self", dir_fd=proc) != process:
        raise Refused()
    pid = _keep(stack, _openat2(proc, process, flags))
    entries = _mounts(_proc_read(pid, b"mountinfo", MAX_MOUNTINFO))
    root_mount = _exact_mount(entries, b"/")
    proc_mount = _exact_mount(entries, b"/proc", parent=root_mount[0])
    if _mount_id(pid, root) != root_mount[0] or _mount_id(pid, proc) != proc_mount[0]:
        raise Refused()
    pinned = _pin_roots(stack, (root, root_mount), entries, pid, roots, flags)
    stamp = {path: (mount, _identity(fd)) for path, (fd, mount) in pinned.items()}
    stamp[b"/proc"] = (proc_mount, _identity(proc))
    return pid, pinned, stamp


def _file_stamp(info, limit=MAX_CONTENT):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not 0 <= info.st_size <= limit
    ):
        raise Refused()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_file(fd, size):
    data = bytearray()
    while len(data) <= size:
        chunk = os.read(fd, min(64 * 1024, size + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) != size:
        raise Refused()
    return data


def _content(pid, fd, mount, *, limit=MAX_CONTENT, text=True):
    info = os.fstat(fd)
    before = _file_stamp(info, limit)  # O_PATH has not opened the file for data access.
    if _mount_id(pid, fd) != mount:
        raise Refused()
    with ExitStack() as stack:
        proc_fds = _keep(stack, _openat2(pid, b"fd", os.O_PATH | os.O_DIRECTORY))
        name = str(fd).encode("ascii")
        if not stat.S_ISLNK(
            os.stat(name, dir_fd=proc_fds, follow_symlinks=False).st_mode
        ):
            raise Refused()
        # Follow only the genuine procfs magic link back to the classified,
        # pinned file. Never re-open the request pathname for data.
        readable = _keep(
            stack,
            os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=proc_fds,
            ),
        )
        if (
            _file_stamp(os.fstat(readable), limit) != before
            or _mount_id(pid, readable) != mount
        ):
            raise Refused()
        data = _read_file(readable, info.st_size)
        # Compare two bounded reads as well as metadata: a filesystem with
        # coarse timestamps may not show a same-size rewrite in fstat.
        os.lseek(readable, 0, os.SEEK_SET)
        if (
            _read_file(readable, info.st_size) != data
            or _file_stamp(os.fstat(readable), limit) != before
        ):
            raise Refused()
    if text and any(
        char not in "\t\r\n" and _control(char) for char in data.decode("utf-8")
    ):
        raise Refused()
    return data, before


def _snapshot(path, roots, expected=None, *, png=False):
    system = os.uname()
    if system.sysname != "Linux" or system.machine not in ARCHITECTURES:
        raise Refused()
    with ExitStack() as stack:
        pid, pinned, stamp = _namespace(stack, roots)
        if expected is not None and any(
            stamp[root][1][:2] != identity for root, identity in expected.items()
        ):
            raise Refused()
        base = max((root for root in pinned if _within(path, root)), key=len)
        relative = path[len(base) :].lstrip(b"/") or b"."
        root, mount = pinned[base]
        target = _keep(stack, _openat2(root, relative, os.O_PATH))
        limit = MAX_PNG_INPUT if png else MAX_CONTENT
        data, before = _content(pid, target, mount[0], limit=limit, text=not png)

        _, after, current_stamp = _namespace(stack, roots)
        if current_stamp != stamp or _file_stamp(os.fstat(target), limit) != before:
            raise Refused()
        current = _keep(stack, _openat2(after[base][0], relative, os.O_PATH))
        if _file_stamp(os.fstat(current), limit) != before:
            raise Refused()
        return data


def _timeout(*_):
    raise Refused()


def main():
    try:
        png = sys.argv[1:] == ["--png"]
        if sys.argv[1:] and not png:
            raise Refused()
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(12 if png else READ_DEADLINE)
        try:
            path, roots, expected = _request(sys.stdin.buffer.read(MAX_REQUEST + 1))
            if png and not path.lower().endswith(b".png"):
                raise Refused()
            data = _snapshot(path, roots, expected, png=png)
            if png:
                # Python -I -S excludes the script directory. Add the shipped
                # helpers' directory explicitly, never the project / cwd.
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from file_preview_png import normalize

                data, _ = normalize(data)
        finally:
            signal.alarm(0)
        # Emit only after all validation. A failed/aborted pipe transfer is not
        # a reply; the caller has to buffer stdout and require successful exit.
        remaining = memoryview(data)
        while remaining:
            count = os.write(sys.stdout.fileno(), remaining)
            if count <= 0:
                raise Refused()
            remaining = remaining[count:]
    except (
        OSError,
        ValueError,
        LookupError,
        AttributeError,
        ImportError,
        OverflowError,
        RecursionError,
        KeyboardInterrupt,
    ):
        return REFUSAL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
