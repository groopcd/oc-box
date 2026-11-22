"""Host-only workspace UI preferences, independent of lifecycle metadata.

The preference is a request for future TUI clients, not native-version/terminal
compatibility or live activation. Absent preferences mean host_drop=True,
host_clipboard=False and host_clipboard_symlinks=True.
Reads never create directories, files or locks. Only an intentional setter
writes, using a distinct per-workspace lock and an atomic private-file replace.

No workspace.json, project marker, native configuration, service, credential or
lifecycle operation is changed here. The host-only file must not be transported
as archive authority to a fresh workspace ID. Namespace checks detect observed
substitution; they are not atomic protection against a malicious same-user
process racing host-state rename/unlink operations.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time


DEFAULT_HOST_DROP = True
DEFAULT_HOST_CLIPBOARD = False
DEFAULT_HOST_CLIPBOARD_SYMLINKS = True
FILE_NAME = "ui-preferences.json"
LOCK_NAME = ".ui-preferences.lock"
MAX_BYTES = 16 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_DEPTH = 16
MAX_NODES = 1024
MAX_SNAPSHOT_ATTEMPTS = 16
LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.01
_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
INVALID_WORKSPACE = (
    "Workspace preferences require an existing valid workspace registration."
)
INVALID_DATA = "Workspace preferences are invalid or unsupported."
INVALID_VALUE = "Host drop preference must be a boolean."
INVALID_CLIPBOARD_VALUE = "Host clipboard preferences must be booleans."
UNSAFE_STORAGE = "Workspace preference storage is unsafe or unavailable."
BUSY = "Workspace preferences are busy; retry the command."
SAVE_UNVERIFIED = "Workspace preference saving could not be verified; read preferences before retrying."
HOST_ONLY = "Run 'oc-box preferences' on the host, not inside a development container."


class PreferenceError(ValueError):
    """Fixed explanations only; never include private state or native errors."""


class _SnapshotReplaced(PreferenceError):
    """Discard a safely replaced snapshot; never weaken validation of its successor."""


def _close(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _workspace_path(workspace_path, workspace_id):
    if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id):
        raise PreferenceError(INVALID_WORKSPACE)
    try:
        path = Path(workspace_path)
        if (
            path.anchor != "/"
            or path.name != "workspace.json"
            or path.parent.name != workspace_id
            or ".." in path.parts
            or "\0" in str(path)
            or len(os.fsencode(path)) > 4096
        ):
            raise PreferenceError(INVALID_WORKSPACE)
        return path
    except (TypeError, ValueError, UnicodeError):
        raise PreferenceError(INVALID_WORKSPACE) from None


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_info(info):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise PreferenceError(UNSAFE_STORAGE)


def _file_info(info, *, private, maximum, allow_retired=False):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink not in ((0, 1) if allow_retired else (1,))
        or info.st_size > maximum
        or (
            stat.S_IMODE(info.st_mode) != 0o600
            if private
            else bool(info.st_mode & 0o022)
        )
    ):
        raise PreferenceError(UNSAFE_STORAGE)


def _open_directory(path):
    """Pin each absolute component without following a symlink."""
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            _close(fd)
            fd = child
        _directory_info(os.fstat(fd))
        return fd
    except BaseException:
        _close(fd)
        raise


class _Directory:
    def __init__(self, path, workspace_id, fd):
        self.path = path
        self.workspace_id = workspace_id
        self.fd = fd
        self.identity = _identity(os.fstat(fd))

    def check(self):
        _directory_info(os.fstat(self.fd))
        current = _open_directory(self.path)
        try:
            if _identity(os.fstat(current)) != self.identity:
                raise PreferenceError(UNSAFE_STORAGE)
        finally:
            _close(current)

    def registration(self):
        raw, _ = _read_file(
            self, "workspace.json", private=False, maximum=MAX_REGISTRY_BYTES
        )
        if raw is None:
            raise PreferenceError(INVALID_WORKSPACE)
        value = _decode(raw)
        if not isinstance(value, dict) or value.get("id") != self.workspace_id:
            raise PreferenceError(INVALID_WORKSPACE)


@contextmanager
def _directory(workspace_path, workspace_id):
    path = _workspace_path(workspace_path, workspace_id)
    fd = None
    try:
        fd = _open_directory(path.parent)
        directory = _Directory(path.parent, workspace_id, fd)
        directory.check()
        directory.registration()
        yield directory
    except PreferenceError:
        raise
    except (OSError, ValueError, TypeError):
        raise PreferenceError(UNSAFE_STORAGE) from None
    finally:
        _close(fd)


def _read_file_once(directory, name, *, private, maximum):
    """One stable snapshot, distinguishing safe replacement from unsafe state."""
    try:
        observed = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    _file_info(observed, private=private, maximum=maximum, allow_retired=True)
    if observed.st_nlink == 0:
        entry = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        _file_info(entry, private=private, maximum=maximum)
        if _identity(entry) != _identity(observed):
            raise _SnapshotReplaced(UNSAFE_STORAGE)
        raise PreferenceError(UNSAFE_STORAGE)
    fd = os.open(name, _READ_FLAGS, dir_fd=directory.fd)
    try:
        before = os.fstat(fd)
        # A cooperating atomic replace can retire an already opened inode.
        # Zero-link descriptors are accepted only for disposal/retry, never read
        # or returned as a usable snapshot. The canonical successor stays strict.
        _file_info(before, private=private, maximum=maximum, allow_retired=True)
        if before.st_nlink == 0:
            entry = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            _file_info(entry, private=private, maximum=maximum)
            if _identity(entry) != _identity(before):
                raise _SnapshotReplaced(UNSAFE_STORAGE)
            raise PreferenceError(UNSAFE_STORAGE)
        # A safe replacement before open is a valid new snapshot, not an unsafe
        # inode or permission to trust the earlier stat. Pin the validated FD as
        # the baseline; still require its canonical entry to match after reading.
        chunks, size = [], 0
        while size <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        entry = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        # Pathname stat may resolve the old inode just before replacement and
        # itself return zero-link metadata. Discard these bytes into the bounded
        # retry; only a fresh, fully validated canonical successor may be used.
        _file_info(entry, private=private, maximum=maximum, allow_retired=True)
        if entry.st_nlink == 0 or _identity(entry) != _identity(before):
            _file_info(after, private=private, maximum=maximum, allow_retired=True)
            raise _SnapshotReplaced(UNSAFE_STORAGE)
        _file_info(after, private=private, maximum=maximum)
        if (
            size > maximum
            or size != before.st_size
            or _stamp(after) != _stamp(before)
            or _stamp(entry) != _stamp(before)
        ):
            raise PreferenceError(UNSAFE_STORAGE)
        return b"".join(chunks), before
    finally:
        _close(fd)


def _read_file(directory, name, *, private, maximum):
    """Retry bounded, safe same-owner replacements, including owner registry writes."""
    replaced = False
    for _ in range(MAX_SNAPSHOT_ATTEMPTS):
        try:
            raw, info = _read_file_once(
                directory, name, private=private, maximum=maximum
            )
        except _SnapshotReplaced:
            replaced = True
            directory.check()
            continue
        if replaced and raw is None:
            # Atomic replacement never leaves an absent canonical entry. Losing
            # observed state is not permission to fall back to default-on.
            raise PreferenceError(UNSAFE_STORAGE)
        return raw, info
    raise PreferenceError(BUSY) from None


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PreferenceError(INVALID_DATA)
        value[key] = item
    return value


def _constant(value):
    raise PreferenceError(INVALID_DATA)


def _decode(raw):
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise PreferenceError(INVALID_DATA) from None


def _bounded_tree(value):
    pending = [(value, 0)]
    count = 0
    try:
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise PreferenceError(INVALID_DATA)
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise PreferenceError(INVALID_DATA)
    except UnicodeError:
        raise PreferenceError(INVALID_DATA) from None


def _preferences(directory):
    raw, info = _read_file(directory, FILE_NAME, private=True, maximum=MAX_BYTES)
    if raw is None:
        return {"host_drop": DEFAULT_HOST_DROP}, None
    value = _decode(raw)
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("workspace") != directory.workspace_id
        or type(value.get("host_drop")) is not bool
        or any(
            name in value and type(value[name]) is not bool
            for name in ("host_clipboard", "host_clipboard_symlinks")
        )
    ):
        raise PreferenceError(INVALID_DATA)
    _bounded_tree(value)
    return value, info


def read(workspace_path, workspace_id):
    """Return saved fields, or exactly {'host_drop': True} when the file is absent.

    Saved mappings include validated version/workspace fields and bounded unknown
    future fields. No file, lock, directory or runtime operation is created.
    """
    with _directory(workspace_path, workspace_id) as directory:
        value, _ = _preferences(directory)
        directory.check()
        return value


def enabled(workspace_path, workspace_id):
    """Return the requested boolean, without testing compatibility or activating it."""
    return read(workspace_path, workspace_id)["host_drop"]


def _check_lock(directory, fd):
    info = os.fstat(fd)
    _file_info(info, private=True, maximum=0)
    entry = os.stat(LOCK_NAME, dir_fd=directory.fd, follow_symlinks=False)
    if _stamp(entry) != _stamp(info):
        raise PreferenceError(UNSAFE_STORAGE)


@contextmanager
def _locked(directory):
    fd = None
    try:
        directory.check()
        try:
            existing = os.stat(LOCK_NAME, dir_fd=directory.fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _file_info(existing, private=True, maximum=0)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if existing is not None:
            fd = os.open(
                LOCK_NAME,
                flags,
                dir_fd=directory.fd,
            )
            if _identity(os.fstat(fd)) != _identity(existing):
                raise PreferenceError(UNSAFE_STORAGE)
        else:
            try:
                fd = os.open(
                    LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory.fd,
                )
                os.fchmod(fd, 0o600)
            except FileExistsError:
                fd = os.open(LOCK_NAME, flags, dir_fd=directory.fd)
        _check_lock(directory, fd)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PreferenceError(BUSY) from None
                time.sleep(LOCK_POLL)
        directory.check()
        _check_lock(directory, fd)
        yield fd
    finally:
        _close(fd)


def _encode(value):
    try:
        raw = (
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise PreferenceError(INVALID_DATA)
        return raw
    except (ValueError, UnicodeError, RecursionError):
        raise PreferenceError(INVALID_DATA) from None


def _unchanged(directory, expected):
    try:
        current = os.stat(FILE_NAME, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if (expected is None) != (current is None):
        raise PreferenceError(UNSAFE_STORAGE)
    if current is not None:
        _file_info(current, private=True, maximum=MAX_BYTES)
        if _stamp(current) != _stamp(expected):
            raise PreferenceError(UNSAFE_STORAGE)


def _write(directory, lock_fd, value, previous):
    raw = _encode(value)
    temporary = ".ui-preferences-" + secrets.token_hex(16) + ".tmp"
    fd = None
    identity = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory.fd,
        )
        identity = _identity(os.fstat(fd))
        os.fchmod(fd, 0o600)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(fd)
        info = os.fstat(fd)
        _file_info(info, private=True, maximum=MAX_BYTES)
        if info.st_size != len(raw):
            raise PreferenceError(SAVE_UNVERIFIED)
        directory.check()
        directory.registration()
        _check_lock(directory, lock_fd)
        _unchanged(directory, previous)
        entry = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
        if _stamp(entry) != _stamp(info):
            raise PreferenceError(UNSAFE_STORAGE)
        os.replace(
            temporary, FILE_NAME, src_dir_fd=directory.fd, dst_dir_fd=directory.fd
        )
        os.fsync(directory.fd)
        directory.check()
        saved = os.stat(FILE_NAME, dir_fd=directory.fd, follow_symlinks=False)
        _file_info(saved, private=True, maximum=MAX_BYTES)
        if _stamp(saved) != _stamp(os.fstat(fd)):
            raise PreferenceError(SAVE_UNVERIFIED)
    except OSError:
        raise PreferenceError(SAVE_UNVERIFIED) from None
    finally:
        _close(fd)
        if identity is not None:
            try:
                directory.check()
                current = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
                _file_info(current, private=True, maximum=MAX_BYTES)
                if _identity(current) == identity:
                    os.unlink(temporary, dir_fd=directory.fd)
            except (OSError, PreferenceError):
                pass


def set_host_drop(workspace_path, workspace_id, enabled):
    if type(enabled) is not bool:
        raise PreferenceError(INVALID_VALUE)
    return set_preferences(workspace_path, workspace_id, host_drop=enabled)


def set_preferences(
    workspace_path,
    workspace_id,
    *,
    host_drop=None,
    host_clipboard=None,
    host_clipboard_symlinks=None,
):
    """Atomically save explicit choices, preserving other fields under the lock."""
    updates = {}
    for name, value in (
        ("host_drop", host_drop),
        ("host_clipboard", host_clipboard),
        ("host_clipboard_symlinks", host_clipboard_symlinks),
    ):
        if value is not None:
            if type(value) is not bool:
                raise PreferenceError(
                    INVALID_VALUE if name == "host_drop" else INVALID_CLIPBOARD_VALUE
                )
            updates[name] = value
    with _directory(workspace_path, workspace_id) as directory:
        # Refuse pre-existing unsafe/corrupt state before creating even the lock.
        initial, _ = _preferences(directory)
        _encode({**initial, "version": 1, "workspace": workspace_id, **updates})
        with _locked(directory) as lock_fd:
            directory.registration()
            current, previous = _preferences(directory)
            value = {
                **current,
                "version": 1,
                "workspace": workspace_id,
                **updates,
            }
            _write(directory, lock_fd, value, previous)
            return value


def parser():
    result = argparse.ArgumentParser(
        prog="oc-box preferences",
        description="Read or save host-only preferences for future workspace TUI clients.",
        epilog="Output flags change only the display; explicit settings are still saved.",
    )
    selectors = result.add_mutually_exclusive_group()
    selectors.add_argument(
        "--project",
        type=Path,
        metavar="PATH",
        help="select the registered workspace for a project (default: current directory)",
    )
    selectors.add_argument(
        "--workspace", metavar="ID", help="select an existing workspace by its ID"
    )
    result.add_argument(
        "--host-drop",
        choices=("on", "off"),
        metavar="on|off",
        help="save the future-client host-drop preference (default: on)",
    )
    result.add_argument(
        "--host-clipboard",
        choices=("on", "off"),
        metavar="on|off",
        help="save the future-client Ctrl+Y host clipboard preference (default: off)",
    )
    result.add_argument(
        "--host-clipboard-symlinks",
        choices=("on", "off"),
        metavar="on|off",
        help="save whether future clipboard file imports follow symlinks (default: on)",
    )
    output = result.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="emit machine-readable preference status"
    )
    output.add_argument(
        "--table",
        action="store_true",
        help="show preference values and sources in a table",
    )
    return result


def _table(value, source, changed):
    if __package__:
        from . import cli_table
    else:
        import cli_table

    clipboard = value.get("host_clipboard", DEFAULT_HOST_CLIPBOARD)
    symlinks = value.get("host_clipboard_symlinks", DEFAULT_HOST_CLIPBOARD_SYMLINKS)
    status = cli_table.render(
        ("PREFERENCE", "VALUE", "SOURCE"),
        (
            ("Host drop", "enabled" if value["host_drop"] else "disabled", source),
            (
                "Host clipboard",
                "enabled" if clipboard else "disabled",
                "saved" if "host_clipboard" in value else "default",
            ),
            (
                "Host clipboard symlinks",
                "enabled" if symlinks else "disabled",
                "saved" if "host_clipboard_symlinks" in value else "default",
            ),
        ),
    )
    scope = {
        "Scope": "Future workspace TUI clients (host only)",
        "Current clients": "unchanged",
    }
    if changed:
        scope["Result"] = "Explicit preferences saved"
    return status + "\n\n" + cli_table.fields(scope)


def cli(box, args):
    args = parser().parse_args(args)
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise box.BoxError(HOST_ONLY)
    try:
        if args.workspace is not None and not _ID.fullmatch(args.workspace):
            raise PreferenceError(INVALID_WORKSPACE)
        try:
            path, data = box.find_workspace(args.project, args.workspace)
        except Exception:
            raise PreferenceError(INVALID_WORKSPACE) from None
        if (
            path is None
            or not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not _ID.fullmatch(data["id"])
            or (args.workspace is not None and data["id"] != args.workspace)
        ):
            raise PreferenceError(INVALID_WORKSPACE)
        updates = {
            name: getattr(args, name) == "on"
            for name in ("host_drop", "host_clipboard", "host_clipboard_symlinks")
            if getattr(args, name) is not None
        }
        changed = bool(updates)
        value = (
            set_preferences(path, data["id"], **updates)
            if changed
            else read(path, data["id"])
        )
    except PreferenceError as error:
        raise box.BoxError(str(error)) from None
    source = "saved" if "version" in value else "default"
    if args.json:
        projected = {
            "host_drop": value["host_drop"],
            "host_clipboard": value.get("host_clipboard", DEFAULT_HOST_CLIPBOARD),
            "host_clipboard_symlinks": value.get(
                "host_clipboard_symlinks", DEFAULT_HOST_CLIPBOARD_SYMLINKS
            ),
            "source": source,
        }
        if changed:
            projected.update(
                applies_to_future_clients=True, current_clients_unchanged=True
            )
        print(json.dumps(projected, separators=(",", ":")))
    elif args.table:
        print(_table(value, source, changed))
    elif changed:
        for name, label in (
            ("host_drop", "host drop"),
            ("host_clipboard", "host clipboard"),
            ("host_clipboard_symlinks", "host clipboard symlink"),
        ):
            if name in updates:
                print(
                    "Saved "
                    + label
                    + " preference: "
                    + ("enabled." if updates[name] else "disabled.")
                )
        print("Applies to future TUI clients; current clients keep their setting.")
    else:
        print(
            "Host drop preference: "
            + ("enabled" if value["host_drop"] else "disabled")
            + " ("
            + source
            + ")."
        )
        for name, label, default in (
            ("host_clipboard", "Host clipboard", DEFAULT_HOST_CLIPBOARD),
            (
                "host_clipboard_symlinks",
                "Host clipboard symlink",
                DEFAULT_HOST_CLIPBOARD_SYMLINKS,
            ),
        ):
            print(
                label
                + " preference: "
                + ("enabled" if value.get(name, default) else "disabled")
                + " ("
                + ("saved" if name in value else "default")
                + ")."
            )
    return 0
