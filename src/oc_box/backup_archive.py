"""Bounded archive helper, run on the host or in podman unshare, never container.

No project files are executed. Plain tar bytes go through stdin/stdout only.
Extraction refuses traversal, special files, symlink ancestors, hardlink escapes,
setuid/setgid bits, and declared/expanded size mismatches. Xattrs/ACLs are not
copied; SELinux is relabeled by Podman when a restored project is mounted.
"""

import argparse
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile


class ArchiveDurabilityError(Exception):
    pass


def sync_descriptor(descriptor):
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ArchiveDurabilityError("Archive durability barrier failed.") from exc


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size > 1024 * 1024:
            raise ValueError("Oversized archive metadata")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 1024 * 1024:
            raise ValueError("Oversized archive name/link metadata")
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive):
        raise ValueError("Sparse archive extensions are not accepted")


def create(root):
    root = Path(root).absolute()
    # Refuse even same-filesystem bind mounts nested under the selected root;
    # stat.st_dev alone does not distinguish them. Linux mountinfo paths use
    # octal escaping. No filesystem-name-specific logic is used.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mount = fields[4]
        for encoded, decoded in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            mount = mount.replace(encoded, decoded)
        location = Path(mount)
        if location != root and location.is_relative_to(root):
            raise ValueError("Nested mounts are not captured implicitly")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        root_device = os.fstat(root_fd).st_dev
        with tarfile.open(
            fileobj=sys.stdout.buffer, mode="w|", dereference=False
        ) as archive:
            inodes = {}
            root_metadata = os.fstat(root_fd)
            root_entry = tarfile.TarInfo(".")
            root_entry.type = tarfile.DIRTYPE
            root_entry.mode = stat.S_IMODE(root_metadata.st_mode)
            root_entry.uid, root_entry.gid, root_entry.mtime = (
                root_metadata.st_uid,
                root_metadata.st_gid,
                root_metadata.st_mtime,
            )
            archive.addfile(root_entry)

            def add(directory_fd, prefix):
                for name in sorted(os.listdir(directory_fd)):
                    if "\\" in name:
                        raise ValueError(
                            "Backslash filenames are unsupported by backup format v1"
                        )
                    # Keep ancestor resolution anchored to already-opened real
                    # directories. A concurrent rename/symlink swap cannot make
                    # us follow a project path into host credentials.
                    path = Path(f"/proc/self/fd/{directory_fd}") / name
                    relative = prefix / name
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    add_entry(directory_fd, name, path, relative, metadata)

            def add_entry(directory_fd, name, path, relative, metadata):
                if metadata.st_dev != root_device:
                    raise ValueError("Nested filesystems are not captured implicitly")
                if not (
                    stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                ):
                    raise ValueError(
                        "Special files/sockets are not supported in workspace archives"
                    )
                # Construct metadata from lstat rather than tarfile's pathname
                # lookup so an entry swap cannot change the declared type/size.
                info = tarfile.TarInfo(str(relative))
                info.mode = stat.S_IMODE(metadata.st_mode)
                info.uid, info.gid, info.mtime = (
                    metadata.st_uid,
                    metadata.st_gid,
                    metadata.st_mtime,
                )
                info.size = metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
                if stat.S_ISDIR(metadata.st_mode):
                    info.type = tarfile.DIRTYPE
                elif stat.S_ISLNK(metadata.st_mode):
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(name, dir_fd=directory_fd)
                else:
                    identity = (metadata.st_dev, metadata.st_ino)
                    if metadata.st_nlink > 1 and identity in inodes:
                        info.type = tarfile.LNKTYPE
                        info.linkname = inodes[identity]
                        info.size = 0
                    elif metadata.st_nlink > 1:
                        inodes[identity] = str(relative)
                if info.isreg():
                    fd = os.open(
                        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    )
                    with os.fdopen(fd, "rb") as stream:
                        actual = os.fstat(stream.fileno())
                        if not stat.S_ISREG(actual.st_mode) or (
                            actual.st_dev,
                            actual.st_ino,
                            actual.st_size,
                        ) != (metadata.st_dev, metadata.st_ino, metadata.st_size):
                            raise ValueError("File changed while backing up")
                        archive.addfile(info, stream)
                        if (
                            os.fstat(stream.fileno()).st_size,
                            os.fstat(stream.fileno()).st_mtime_ns,
                        ) != (metadata.st_size, metadata.st_mtime_ns):
                            raise ValueError("File changed while backing up")
                else:
                    archive.addfile(info)
                    if info.islnk():
                        current = path.lstat()
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_size,
                            current.st_mtime_ns,
                        ) != (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                        ):
                            raise ValueError("File changed while backing up")
                if info.isdir():
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                    try:
                        current = os.fstat(child)
                        if (current.st_dev, current.st_ino) != (
                            metadata.st_dev,
                            metadata.st_ino,
                        ):
                            raise ValueError("Directory changed while backing up")
                        add(child, relative)
                        current = os.fstat(child)
                        if current.st_mtime_ns != metadata.st_mtime_ns:
                            raise ValueError("Directory changed while backing up")
                    finally:
                        os.close(child)

            add(root_fd, PurePosixPath())
            if os.fstat(root_fd).st_mtime_ns != root_metadata.st_mtime_ns:
                raise ValueError("Root directory changed while backing up")
    finally:
        os.close(root_fd)


def extract(root, limit, owners=False, *, stream=None):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ValueError("Extraction destination must be an empty real directory")
    os.chmod(root, 0o700)
    seen = set()
    regular = set()
    directories = []
    directory_paths = {root}
    regular_metadata = []
    total = 0

    def safe_name(name):
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or "\0" in name
            or "\\" in name
        ):
            raise ValueError("Unsafe archive path")
        return path

    def destination(name):
        path = safe_name(name)
        current = root
        for part in path.parts[:-1]:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise ValueError("Archive would traverse a symlink/non-directory")
            current.mkdir(mode=0o700, exist_ok=True)
            directory_paths.add(current)
        return root / str(path)

    with tarfile.open(
        fileobj=stream or sys.stdin.buffer, mode="r|", tarinfo=BoundedTarInfo
    ) as archive:
        for member in archive:
            if member.name == ".":
                if seen or not member.isdir() or member.size != 0:
                    raise ValueError("Invalid root directory archive entry")
                seen.add(".")
                directories.append((root, member))
                continue
            name = member.name.rstrip("/") if member.isdir() else member.name
            if member.sparse is not None:
                raise ValueError("Sparse archive extensions are not accepted")
            if not member.isreg() and member.size != 0:
                raise ValueError("Nonregular archive entries cannot carry payload data")
            path = destination(name)
            if name in seen or len(seen) >= 2_000_000:
                raise ValueError("Duplicate/excessive archive entries")
            seen.add(name)
            if member.size < 0:
                raise ValueError("Invalid archive size")
            total += member.size
            if total > limit:
                raise ValueError("Archive expanded beyond its bound")
            if member.isdir():
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    raise ValueError("Directory collides with existing entry")
                path.mkdir(mode=0o700, exist_ok=True)
                directory_paths.add(path)
                directories.append((path, member))
            elif member.isreg():
                fd = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Missing archive content")
                with os.fdopen(fd, "wb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Truncated archive file")
                        output.write(chunk)
                        remaining -= len(chunk)
                regular.add(name)
                regular_metadata.append((path, member))
            elif member.issym():
                if "\0" in member.linkname:
                    raise ValueError("Invalid link target")
                os.symlink(member.linkname, path)
            elif member.islnk():
                target = str(safe_name(member.linkname))
                if target not in regular:
                    raise ValueError("Hardlink must name a prior regular archive entry")
                os.link(root / target, path, follow_symlinks=False)
                regular.add(name)
            else:
                raise ValueError("Device nodes, sockets, and FIFOs are not restored")
            if member.issym():
                apply_metadata(path, member, owners)
    # Consume padding/trailing bytes so age verifies its final auth chunk.
    source = stream or sys.stdin.buffer
    while source.read(1024 * 1024):
        pass
    # Keep ordinary files readable and directories traversable until every link
    # has been created. Open before applying restrictive modes/mapped ownership,
    # then sync final metadata through that descriptor in this same namespace.
    for path, member in regular_metadata:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        try:
            apply_metadata(path, member, owners)
            sync_descriptor(descriptor)
        finally:
            os.close(descriptor)
    metadata = dict(directories)
    for path in sorted(
        directory_paths, key=lambda value: len(value.parts), reverse=True
    ):
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            member = metadata.get(path)
            if path == root:
                # Never make a staging root public, including during extraction.
                os.chmod(root, 0o700)
                if member is not None:
                    os.utime(root, (member.mtime, member.mtime), follow_symlinks=False)
            elif member is not None:
                apply_metadata(path, member, owners)
            sync_descriptor(descriptor)
        finally:
            os.close(descriptor)


def apply_metadata(path, member, owners):
    if owners:
        os.chown(path, member.uid, member.gid, follow_symlinks=False)
    if not member.issym():
        os.chmod(path, member.mode & 0o777)  # never restore setuid/setgid/sticky
        os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)


def write_status(path, value, *, durable=True):
    if path is None:
        return
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > 16
            ):
                raise OSError("Invalid extraction status file")
            os.ftruncate(descriptor, 0)
            data = memoryview((value + "\n").encode("ascii"))
            while data:
                count = os.write(descriptor, data)
                if not count:
                    raise OSError("Cannot write extraction status")
                data = data[count:]
            if durable:
                sync_descriptor(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ArchiveDurabilityError(
            "Archive durability status could not be recorded."
        ) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "extract"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--owners", action="store_true")
    parser.add_argument("--durability-status", type=Path)
    args = parser.parse_args(argv)
    if args.action == "create":
        create(args.root)
    else:
        if args.limit < 1:
            raise ValueError("A positive extraction bound is required")
        try:
            write_status(args.durability_status, "started")
            extract(args.root, args.limit, args.owners)
            write_status(args.durability_status, "complete")
        except ArchiveDurabilityError:
            try:
                write_status(args.durability_status, "failed", durable=False)
            except ArchiveDurabilityError:
                pass
            print(
                "Archive durability barrier failed; extraction is incomplete.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
