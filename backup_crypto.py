"""Established age encryption around streaming components; no plaintext spool.

A .ocbox file is a strict, versioned tar envelope of fixed-name age ciphertext
components. A fresh per-backup age identity stays in anonymous memory. Its key
envelope is age-passphrase- or recipient-encrypted; the encrypted manifest binds
all ciphertext checksums. No custom encryption format/primitives are used.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import time

if __package__:
    from . import dependencies, secret_prompt
    from .backup_archive import BoundedTarInfo
else:
    import secret_prompt
    import dependencies
    from backup_archive import BoundedTarInfo

COMPONENTS = {
    "key.age",
    "manifest.age",
    "project.age",
    "home.age",
    "registry.age",
    "image.age",
}
MAX_SMALL = 4 * 1024 * 1024
MAX_BACKUP = 16 * 1024**4


class BackupError(Exception):
    pass


def tools():
    try:
        dependencies.require("backup-info")
    except dependencies.DependencyError as exc:
        raise BackupError(str(exc)) from exc


def environment():
    # Do not forward provider tokens, pass_env, shell hooks, proxy credentials,
    # agent sockets or any existing AGE_* passphrase/plugin settings.
    result = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "DBUS_SESSION_BUS_ADDRESS",
        )
        if key in os.environ
    }
    result["LANG"] = "C.UTF-8"
    return result


@contextlib.contextmanager
def memory_file(content=b""):
    if not hasattr(os, "memfd_create"):
        raise BackupError(
            "Backup/restore requires Linux memfd support for secret keys and private subprocess output."
        )
    fd = os.memfd_create("oc-box-backup-memory", os.MFD_CLOEXEC)
    with os.fdopen(fd, "w+b") as stream:
        stream.write(content)
        stream.seek(0)
        yield stream


def small(command, *, data=None, timeout=30, tty_errors=False, pass_fds=()):
    terminal = None
    attributes = None
    if tty_errors:
        try:
            terminal = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
            attributes = termios.tcgetattr(terminal)
        except OSError:
            if terminal is not None:
                os.close(terminal)
            terminal = None
    try:
        return small_capture(
            command,
            data=data,
            timeout=timeout,
            tty_errors=tty_errors,
            pass_fds=pass_fds,
        )
    finally:
        if terminal is not None:
            try:
                if attributes is not None:
                    termios.tcsetattr(terminal, termios.TCSAFLUSH, attributes)
            finally:
                os.close(terminal)


def small_capture(command, *, data=None, timeout=30, tty_errors=False, pass_fds=()):
    with memory_file() as output, memory_file() as errors:
        try:
            result = subprocess.run(
                command,
                input=data,
                stdout=output,
                stderr=None if tty_errors else errors,
                env=environment(),
                timeout=timeout,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError(
                "Backup tool failed or timed out; no credentials or private tool output were logged."
            ) from exc
        if result.returncode:
            raise BackupError(
                "Backup encryption/tool operation failed. Check the passphrase/identity, host permissions, and installed tools; no existing workspace was overwritten."
            )
        output.seek(0)
        value = output.read(MAX_SMALL + 1)
        if len(value) > MAX_SMALL:
            raise BackupError("Unexpected oversized backup metadata/tool response.")
        return value


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return {"sha256": checksum.hexdigest(), "bytes": Path(path).stat().st_size}


def validate_recipients(recipients):
    if len(recipients) > 16 or any(
        not re.fullmatch(r"age1[0-9a-z]{58}", value) for value in recipients
    ):
        raise BackupError(
            "Backup recipients must be at most 16 native age X25519 public keys (age1...). Plugins and SSH recipients are not accepted in format v1."
        )


def passphrase_minimum(recipients, override=None):
    if recipients:
        if override is not None:
            raise BackupError(
                "--min-passphrase-length cannot be combined with --recipient."
            )
        return None
    try:
        return secret_prompt.passphrase_minimum("backup", override)
    except ValueError as exc:
        raise BackupError(str(exc)) from exc


def encrypt_passphrase(private, output, minimum):
    try:
        phrase = secret_prompt.new_passphrase("backup", minimum)
    except (OSError, EOFError) as exc:
        raise BackupError(
            "Passphrase-encrypted backup requires a controlling host terminal; use --recipient for unattended backups."
        ) from exc
    except ValueError as exc:
        raise BackupError(str(exc)) from exc
    # age has no passphrase-fd option. Give it a private controlling PTY, with
    # echo disabled before exec, and answer its standard prompts there. Only
    # validated user input crosses this PTY; no secret enters argv/env or disk.
    master, slave = pty.openpty()
    process = None
    try:
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        child = (
            "import fcntl,os,sys,termios; "
            "fcntl.ioctl(2,termios.TIOCSCTTY,0); os.execvp(sys.argv[1],sys.argv[1:])"
        )
        with memory_file(private) as source:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    child,
                    "age",
                    "--encrypt",
                    "--passphrase",
                    "--output",
                    str(output),
                ],
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=slave,
                env=environment(),
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            prompts = (b"Enter passphrase", b"Confirm passphrase")
            pending = bytearray()
            step = 0
            deadline = time.monotonic() + 30
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise BackupError("Backup passphrase encryption timed out.")
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # Linux PTY returns EIO after its slave exits.
                if not chunk:
                    break
                pending.extend(chunk)
                if len(pending) > 65536:
                    raise BackupError("Unexpected backup passphrase tool response.")
                if step < len(prompts) and prompts[step] in pending:
                    remaining = memoryview(phrase.encode() + b"\n")
                    while remaining:
                        written = os.write(master, remaining)
                        if not written:
                            raise BackupError("Backup passphrase terminal closed.")
                        remaining = remaining[written:]
                    pending.clear()
                    step += 1
            if process.wait(timeout=3) or step != len(prompts):
                raise BackupError(
                    "Backup passphrase encryption failed; private tool output was not logged."
                )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(
            "Backup passphrase tool failed; private tool output was not logged."
        ) from exc
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def generate_key(recipients, output, *, minimum=None):
    tools()
    validate_recipients(recipients)
    minimum = passphrase_minimum(recipients, minimum)
    private = small(["age-keygen"])
    recipient = small(["age-keygen", "-y"], data=private).decode().strip()
    if not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
        raise BackupError("Unexpected age-keygen recipient output.")
    command = ["age", "--encrypt", "--output", str(output)]
    if recipients:
        for value in recipients:
            command.extend(["--recipient", value])
        small(command, data=private)
    else:
        encrypt_passphrase(private, output, minimum)
    output.chmod(0o600)
    return private, recipient


def encrypt_bytes(value, recipient, path):
    small(
        ["age", "--encrypt", "--recipient", recipient, "--output", str(path)],
        data=value,
    )
    path.chmod(0o600)


def load_key(path, identities):
    if len(identities) > 16:
        raise BackupError("At most 16 age identity files can be selected.")
    with contextlib.ExitStack() as stack:
        command = ["age", "--decrypt"]
        descriptors = []
        for identity in identities:
            location = Path(identity).expanduser().absolute()
            try:
                fd = os.open(
                    location, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                )
            except OSError as exc:
                raise BackupError(
                    "Backup identity must be a readable regular, non-symlink host file."
                ) from exc
            with os.fdopen(fd, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077
                    or metadata.st_nlink != 1
                    or metadata.st_size >= 65536
                ):
                    raise BackupError(
                        "Backup identity must be owned by your user, private (0600), bounded, and have no extra hard links."
                    )
                prefix = source.read(65536)
            # Validate the exact bytes that age will read, not a pathname that
            # could be swapped into a plugin/SSH identity after the check.
            lines = [
                line.strip()
                for line in prefix.decode("ascii", errors="replace").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            if (
                not lines
                or any(
                    not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", line)
                    for line in lines
                )
                or len(prefix) >= 65536
            ):
                raise BackupError(
                    "Use a native unencrypted age X25519 identity file (0600), not a plugin/SSH identity. Passphrase-protected backups instead prompt through age directly."
                )
            memory = stack.enter_context(memory_file(prefix))
            descriptors.append(memory.fileno())
            command.extend(["--identity", f"/proc/self/fd/{memory.fileno()}"])
        command.append(str(path))
        private = small(
            command, timeout=300, tty_errors=True, pass_fds=tuple(descriptors)
        )
    # The decrypted key envelope is untrusted archive input too. Do not feed
    # arbitrary identity/plugin lines from it to a subsequent age invocation.
    lines = [
        line.strip()
        for line in private.decode("ascii", errors="replace").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if (
        len(private) > 16384
        or len(lines) != 1
        or not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", lines[0])
    ):
        raise BackupError("Invalid backup key envelope.")
    return private


def decrypt_bytes(path, private):
    with memory_file(private) as identity:
        return small(
            [
                "age",
                "--decrypt",
                "--identity",
                f"/proc/self/fd/{identity.fileno()}",
                str(path),
            ],
            pass_fds=(identity.fileno(),),
        )


def pipeline_error(processes, errors):
    """Report only exit codes and fixed diagnostic categories, never tool text."""
    stages = []
    for index, (process, stream) in enumerate(zip(processes, errors)):
        code = process.poll()
        if code in (None, 0):
            continue
        name = "producer" if index == 0 else "consumer"
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 65536))
        text = stream.read().lower()
        category = next(
            (
                description
                for patterns, description in (
                    (
                        (b"no space left on device", b"enospc"),
                        "storage or temporary-image space exhausted; check free bytes and inodes",
                    ),
                    ((b"disk quota exceeded", b"edquot"), "storage quota exceeded"),
                    (
                        (b"read-only file system", b"erofs"),
                        "destination filesystem is read-only",
                    ),
                    (
                        (
                            b"permission denied",
                            b"operation not permitted",
                            b"eacces",
                            b"eperm",
                        ),
                        "filesystem or user-namespace permission denied; check storage ownership and rootless mappings",
                    ),
                    (
                        (
                            b"failed to decrypt",
                            b"authentication failed",
                            b"incorrect identity",
                            b"no identity matched",
                            b"failed to read header",
                        ),
                        "decryption/authentication failed; check backup integrity and identity",
                    ),
                    (
                        (
                            b"payload does not match",
                            b"unsupported image format",
                            b"invalid tar header",
                            b"invalid checksum",
                            b"unexpected eof",
                        ),
                        "archive/image loading was rejected; check integrity and Podman compatibility",
                    ),
                    (
                        (
                            b"database configuration mismatch",
                            b"database static dir",
                            b"database graph root",
                            b"database run root",
                        ),
                        "Podman storage/database configuration mismatch",
                    ),
                    (
                        (b"broken pipe", b"epipe"),
                        "output pipe closed after the other stage failed",
                    ),
                )
                if any(pattern in text for pattern in patterns)
            ),
            "tool failed; private output was not logged",
        )
        stages.append(f"{name} exit {code}: {category}")
    return BackupError(
        "Backup pipeline failed; partial data was not activated. " + "; ".join(stages)
    )


def stream_pipeline(
    first, second, *, source=None, destination=None, first_fds=(), timeout=3600.0
):
    """Pipe bytes directly; stderr is anonymous memory and never echoed."""
    processes = []
    with memory_file() as producer_errors, memory_file() as consumer_errors:
        errors = (producer_errors, consumer_errors)
        try:
            producer = subprocess.Popen(
                first,
                stdin=subprocess.DEVNULL if source is None else source,
                stdout=subprocess.PIPE,
                stderr=producer_errors,
                env=environment(),
                pass_fds=first_fds,
                start_new_session=True,
            )
            processes.append(producer)
            consumer = subprocess.Popen(
                second,
                stdin=producer.stdout,
                stdout=destination or subprocess.DEVNULL,
                stderr=consumer_errors,
                env=environment(),
                start_new_session=True,
            )
            processes.append(consumer)
            assert producer.stdout is not None
            producer.stdout.close()
            until = time.monotonic() + timeout
            while True:
                codes = [process.poll() for process in processes]
                if all(code is not None for code in codes):
                    break
                if time.monotonic() >= until:
                    raise BackupError(
                        "Backup streaming operation timed out; partial data was not activated."
                    )
                if any(code not in (None, 0) for code in codes):
                    raise pipeline_error(processes, errors)
                time.sleep(0.02)
            if any(process.returncode for process in processes):
                raise pipeline_error(processes, errors)
        except OSError as exc:
            raise BackupError(
                "Cannot run backup tool; check age, Python, and Podman on the host."
            ) from exc
        finally:
            if processes and processes[0].stdout is not None:
                processes[0].stdout.close()
            for process in processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for process in processes:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def encrypt_stream(producer, recipient, path):
    with path.open("xb") as output:
        path.chmod(0o600)
        stream_pipeline(
            producer, ["age", "--encrypt", "--recipient", recipient], destination=output
        )


def decrypt_stream(path, private, consumer):
    with memory_file(private) as identity:
        stream_pipeline(
            [
                "age",
                "--decrypt",
                "--identity",
                f"/proc/self/fd/{identity.fileno()}",
                str(path),
            ],
            consumer,
            first_fds=(identity.fileno(),),
        )


def pack(directory, output):
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for path in sorted(directory.iterdir()):
            if path.name not in COMPONENTS or not path.is_file():
                raise BackupError("Unexpected backup staging entry.")
            info = tarfile.TarInfo(path.name)
            info.mode, info.size = 0o600, path.stat().st_size
            with path.open("rb") as stream:
                archive.addfile(info, stream)


@contextlib.contextmanager
def opened(archive_path, identities):
    tools()
    archive_path = Path(archive_path).expanduser().absolute()
    with contextlib.ExitStack() as stack:
        try:
            descriptor = os.open(
                archive_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except OSError as exc:
            raise BackupError(
                "Backup input must be a readable regular, non-symlink file."
            ) from exc
        source_file = stack.enter_context(os.fdopen(descriptor, "rb"))
        if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
            raise BackupError("Backup input must be a regular, non-symlink file.")
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="oc-box-encrypted-backup-")
        )
        directory = Path(temporary)
        seen = set()
        total = 0
        try:
            with tarfile.open(
                fileobj=source_file, mode="r|", tarinfo=BoundedTarInfo
            ) as archive:
                for member in archive:
                    if (
                        not member.isfile()
                        or member.sparse is not None
                        or member.name not in COMPONENTS
                        or member.name in seen
                        or member.size < 0
                    ):
                        raise BackupError(
                            "Invalid backup envelope entry (duplicates, links, or unexpected names are not accepted)."
                        )
                    total += member.size
                    if total > MAX_BACKUP or (
                        member.name in ("key.age", "manifest.age")
                        and member.size > MAX_SMALL
                    ):
                        raise BackupError("Backup envelope exceeds its format bounds.")
                    source = archive.extractfile(member)
                    if source is None:
                        raise BackupError("Missing backup envelope data.")
                    destination = directory / member.name
                    with destination.open("xb") as output:
                        destination.chmod(0o600)
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    seen.add(member.name)
        except (tarfile.TarError, EOFError, ValueError) as exc:
            raise BackupError("Invalid or truncated backup envelope.") from exc
        if (
            not {"key.age", "manifest.age", "project.age", "home.age", "registry.age"}
            <= seen
        ):
            raise BackupError("Incomplete backup envelope.")
        private = load_key(directory / "key.age", identities)
        try:
            manifest = json.loads(decrypt_bytes(directory / "manifest.age", private))
        except (ValueError, UnicodeError) as exc:
            raise BackupError("Invalid encrypted backup manifest.") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "oc-box-backup"
            or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1
            or manifest.get("kind") not in ("full", "portable")
        ):
            raise BackupError("Unsupported backup format/version.")
        expected = {"project.age", "home.age", "registry.age"} | (
            {"image.age"} if manifest["kind"] == "full" else set()
        )
        if (
            not isinstance(manifest.get("components"), dict)
            or set(manifest["components"]) != expected
            or seen
            != expected
            | {
                "key.age",
                "manifest.age",
            }
        ):
            raise BackupError("Backup manifest/components disagree.")
        for name in expected:
            if manifest["components"][name] != digest(directory / name):
                raise BackupError(
                    "Backup ciphertext checksum/size mismatch; no restoration was performed."
                )
        yield manifest, directory, private
