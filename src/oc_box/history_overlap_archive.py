"""Bounded immutable candidate archive bytes for guarded history sync."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import stat

if __package__:
    from . import private_memory
else:
    import private_memory


MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


class ArchiveRefusal(ValueError):
    """Fixed diagnostic only; candidate contents are never an error detail."""


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
    )


@dataclass(frozen=True)
class SealedCandidate:
    _fd: int
    _process: int
    _signature: tuple
    digest: str
    size: int

    def _checked_fd(self):
        try:
            if (
                os.getpid() != self._process
                or _signature(os.fstat(self._fd)) != self._signature
                or not private_memory.is_sealed(self._fd)
            ):
                raise ArchiveRefusal("candidate-descriptor")
        except OSError:
            raise ArchiveRefusal("candidate-descriptor") from None
        return self._fd

    def native_path(self):
        """Only for a single verified host-side import while this FD is live."""
        return f"/proc/{self._process}/fd/{self._checked_fd()}"

    def write_to(self, stream):
        """Publish the same immutable bytes once native readback is verified."""
        fd = self._checked_fd()
        offset = 0
        try:
            while offset < self.size:
                data = os.pread(fd, min(65536, self.size - offset), offset)
                if not data or stream.write(data) != len(data):
                    raise ArchiveRefusal("candidate-copy")
                offset += len(data)
        except OSError:
            raise ArchiveRefusal("candidate-copy") from None


@contextmanager
def sealed_candidate(chunks, *, limit=MAX_ARCHIVE_BYTES):
    """Stage byte-exact archive data without introducing a second disk copy."""
    if type(limit) is not int or not 1 <= limit <= MAX_ARCHIVE_BYTES:
        raise ArchiveRefusal("candidate-bound")
    try:
        fd = os.memfd_create(
            "oc-box-history-candidate", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
    except (AttributeError, OSError):
        raise ArchiveRefusal("candidate-unavailable") from None
    try:
        size = 0
        digest = hashlib.sha256()
        try:
            for chunk in chunks:
                if type(chunk) is not bytes or len(chunk) > limit - size:
                    raise ArchiveRefusal("candidate-bound")
                offset = 0
                while offset < len(chunk):
                    written = os.write(fd, memoryview(chunk)[offset:])
                    if written <= 0:
                        raise ArchiveRefusal("candidate-write")
                    offset += written
                digest.update(chunk)
                size += len(chunk)
            if not size:
                raise ArchiveRefusal("candidate-bound")
            os.fchmod(fd, 0o400)
            private_memory.seal(fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 0
                or info.st_size != size
                or os.get_inheritable(fd)
                or not private_memory.is_sealed(fd)
            ):
                raise ArchiveRefusal("candidate-descriptor")
        except OSError:
            raise ArchiveRefusal("candidate-write") from None
        yield SealedCandidate(
            fd, os.getpid(), _signature(info), digest.hexdigest(), size
        )
    finally:
        os.close(fd)
