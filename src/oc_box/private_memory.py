"""Linux anonymous-memory files and immutable descriptor seals.

Never substitute a disk-backed temporary file. The libc compatibility path is
only for Python builds without the native binding, not failed native calls.
"""

import errno
import os
import stat
import sys


def create(name: str) -> int:
    """Return a new anonymous regular-file descriptor with atomic CLOEXEC."""
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "Linux anonymous-memory files are unavailable.")
    flags = getattr(os, "MFD_CLOEXEC", 1)
    native = getattr(os, "memfd_create", None)
    if native is not None:
        return native(name, flags)

    encoded_name = os.fsencode(name)
    if b"\0" in encoded_name:
        raise OSError(errno.EINVAL, "Invalid anonymous-memory file name.")
    try:
        # Some standalone Python builds omit os.memfd_create even though their
        # host libc/kernel support it. Do not load ctypes on the native path.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        memfd_create = libc.memfd_create
        memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        memfd_create.restype = ctypes.c_int
        ctypes.set_errno(0)
        fd = memfd_create(encoded_name, flags)
        failure_errno = ctypes.get_errno() if fd < 0 else 0
    except Exception:
        # Loader/symbol/FFI failures are capability failures, not diagnostics
        # containing loader paths or implementation-specific exception text.
        raise OSError(
            errno.ENOSYS, "Linux anonymous-memory files are unavailable."
        ) from None
    if fd < 0:
        code = failure_errno or errno.EIO
        raise OSError(code, os.strerror(code))
    return fd


def _file_seals():
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "Linux file sealing is unavailable.")
    import fcntl

    # Some Python builds omit these names. The Linux UAPI (linux/fcntl.h)
    # fixes ADD/GET at F_LINUX_SPECIFIC_BASE (1024) + 9/+10 and the bits below.
    required = (
        getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
        | getattr(fcntl, "F_SEAL_GROW", 0x0004)
        | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
    )
    return fcntl, required


def seal(fd: int) -> None:
    """Prevent writes, resizing and further seal changes; propagate kernel errors."""
    fcntl, required = _file_seals()
    fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), required)


def is_sealed(fd: int) -> bool:
    """Check all four required seals; propagate kernel errors."""
    fcntl, required = _file_seals()
    return fcntl.fcntl(fd, getattr(fcntl, "F_GET_SEALS", 1034)) & required == required


def check() -> None:
    """Probe creation and required descriptor properties without retaining it."""
    fd = create("oc-box-private-output-probe")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.get_inheritable(fd):
            raise OSError(
                errno.ENOSYS,
                "Linux anonymous-memory files lack required descriptor support.",
            )
    finally:
        os.close(fd)


def error_message() -> str:
    """Explain a private-memory capability failure without exposing raw errors."""
    return (
        "Linux memfd anonymous-memory file support is unavailable with the current "
        f"Python {sys.executable!r}. It is required for private OpenCode "
        "credential capture. "
        "--no-vault still copies native saved/OAuth auth. "
        "Run 'oc-box doctor --feature private-output' and use a compatible host "
        "Python, for example reinstall with "
        "'uv tool install --force --python /usr/bin/python3 oc-box'."
    )
