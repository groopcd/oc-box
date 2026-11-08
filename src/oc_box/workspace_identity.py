"""Capability-based directory identity through generic Linux VFS interfaces.

No filesystem-name checks, storage-device lookups, mount parsing, filesystem
utilities, or on-disk format knowledge. name_to_handle_at supplies an opaque
object handle; statvfs supplies the filesystem ID. Both are read through one
directory fd, without open_by_handle_at or elevated capabilities. A stored
strong identity must remain verifiable; unsupported filesystems retain strict
device/inode checks rather than trust a copyable marker as physical identity.
"""

import ctypes
import errno
import os
import struct
import sys

STABLE_IDENTITY_KEY = "project_filesystem_identity"


def file_handle(fd):
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        lookup = libc.name_to_handle_at
    except (AttributeError, OSError):
        return None
    lookup.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    lookup.restype = ctypes.c_int
    # Opaque handles vary by driver; retry a larger one when explicitly asked,
    # with a bounded allocation/call count. No type or encoding is assumed.
    capacity = 128
    for _ in range(2):
        buffer = ctypes.create_string_buffer(8 + capacity)
        struct.pack_into("=I", buffer, 0, capacity)
        mount = ctypes.c_int()
        result = lookup(fd, b"", buffer, ctypes.byref(mount), 0x1000)  # AT_EMPTY_PATH
        length, kind = struct.unpack_from("=Ii", buffer)
        if result == 0:
            if not 0 < length <= capacity:
                return None
            return {"type": kind, "bytes": bytes(buffer[8 : 8 + length]).hex()}
        if ctypes.get_errno() != errno.EOVERFLOW or not capacity < length <= 4096:
            return None
        capacity = length
    return None


def snapshot(project):
    """Read inode, filesystem ID, and handle through the same directory fd."""
    fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        stat = os.fstat(fd)
        identity = [stat.st_dev, stat.st_ino]
        handle = file_handle(fd)
        if handle is None:
            return identity, None
        try:
            fsid = os.fstatvfs(fd).f_fsid
        except (AttributeError, OSError):
            return identity, None
        if not fsid:
            return identity, None
        return identity, {
            "method": "linux-file-handle-v1",
            "fsid": format(fsid, "x"),
            "handle": handle,
        }
    finally:
        os.close(fd)


def matches(data, identity, stable):
    recorded = data.get("project_identity")
    if (
        not isinstance(recorded, list)
        or len(recorded) != 2
        or recorded[1] != identity[1]
    ):
        return False
    if STABLE_IDENTITY_KEY in data:
        expected = data[STABLE_IDENTITY_KEY]
        # Never downgrade a recorded strong identity when the probe fails or
        # a mount now names another filesystem/snapshot with the same inode.
        return (
            isinstance(expected, dict)
            and expected.get("method") == "linux-file-handle-v1"
            and stable is not None
            and expected == stable
        )
    return recorded == identity


def same_project(data, project):
    try:
        identity, stable = snapshot(project)
        return matches(data, identity, stable)
    except OSError:
        return False


def record(data, identity, stable):
    data["project_identity"] = identity
    if stable is None:
        data.pop(STABLE_IDENTITY_KEY, None)
    else:
        data[STABLE_IDENTITY_KEY] = stable
