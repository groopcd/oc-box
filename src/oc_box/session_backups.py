"""Encrypted, bounded native V2 session-tree history; never workspace backups.

The caller supplies a SessionCatalog bound to an already verified connection and
foreground TTY for age prompts. Nothing here discovers/starts services, reads a
database, accesses credentials, runs restored work, or deletes sessions. This is
not a transaction: process-local activity checks cannot exclude other servers.

Format 1 is an uncompressed strict tar of key.age, manifest.age, sessions.age.
Only ciphertext is staged on disk. The encrypted manifest authenticates component
sizes/checksums; age authenticates the manifest itself. Plain history (64 MiB
encoded maximum, 50 sessions) and identities remain in memory. Python/age memory
is not guaranteed to be securely erased. Restores require a single-use in-memory
review plan in the same process, and create fresh unprivileged native IDs only.
"""

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import quote, urlsplit

if __package__:
    from . import opencode_v2_io as bridge
    from . import tui_sessions as sessions
else:
    import opencode_v2_io as bridge
    import tui_sessions as sessions


FORMAT = "oc-box-session-backup"
VERSION = 1
MAX_HISTORY = 64 * 1024 * 1024
MAX_SESSIONS = 50
MAX_MESSAGES = 100000
MAX_METADATA = 128 * 1024
MAX_KEY = 64 * 1024
MAX_CIPHERTEXT = MAX_HISTORY + 2 * 1024 * 1024
MAX_ENVELOPE = MAX_CIPHERTEXT + MAX_METADATA + MAX_KEY + 10240
ENTRIES = ("key.age", "manifest.age", "sessions.age")
MESSAGE_TYPES = frozenset(
    (
        "user",
        "synthetic",
        "system",
        "skill",
        "shell",
        "assistant",
        "compaction",
        "idle",
        "agent-switched",
        "model-switched",
        "location-switched",
    )
)


class SessionBackupError(RuntimeError):
    """Fixed category and fresh-ID recovery metadata, never native error text.

    created_ids are observed new IDs, not a claim that their history verified.
    attempted_ids also includes imports whose outcome could not be established.
    No automatic rollback is attempted; existing/source history is never deleted.
    """

    def __init__(self, reason, *, created_ids=(), attempted_ids=()):
        self.reason = reason
        self.created_ids = tuple(created_ids)
        self.attempted_ids = tuple(attempted_ids)
        super().__init__(f"Session backup operation refused ({reason}).")


def _fail(reason):
    raise SessionBackupError(reason) from None


def _gate(*, read_only=False, demo=False, inspect_only=False):
    if demo:
        _fail("demo-disabled")
    if read_only and not inspect_only:
        _fail("read-only")


def _json(value, limit=MAX_HISTORY):
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("invalid-history")
    if len(encoded) > limit:
        _fail("history-limit")
    return encoded


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _fail("duplicate-json-field")
        result[key] = value
    return result


def _decode(value):
    try:
        return json.loads(
            value,
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: _fail("invalid-history"),
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid-history")


def _catalog(catalog):
    if not isinstance(catalog, sessions.SessionCatalog):
        _fail("invalid-catalog")
    try:
        catalog._validate_binding()
    except sessions.CatalogError:
        _fail("origin-changed")


def _domain(function, *args):
    try:
        return function(*args)
    except sessions.CatalogError as error:
        raise SessionBackupError(error.reason) from None
    except SessionBackupError:
        raise
    except Exception:
        _fail("native-operation-failed")


def _inventory(catalog):
    _catalog(catalog)
    return _domain(sessions.list_sessions, catalog.request)


def _quiet(catalog, identities):
    _catalog(catalog)
    return _domain(catalog._idle, identities)


def _unchanged(catalog, selected):
    for item in selected:
        _domain(catalog._unchanged, item)


def _export(catalog, ident):
    # checked_export intentionally preserves native's count-only interrupted
    # history annotation. Bound its transport and refuse incomplete pagination,
    # including empty pages with a non-null cursor (which older bridges stop at).
    pages, projected_bytes, projected_count = 0, 0, 0

    def request(method, path, body=None):
        nonlocal pages, projected_bytes, projected_count
        value = _domain(sessions._call, catalog.request, method, path, body)
        if urlsplit(path).path.endswith("/message"):
            pages += 1
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("data"), list)
                or not isinstance(value.get("cursor"), dict)
                or "next" not in value["cursor"]
                or (not value["data"] and value["cursor"]["next"] is not None)
            ):
                _fail("incomplete-history")
            projected_bytes += len(_json(value["data"]))
            projected_count += len(value["data"])
            if (
                pages > sessions.MAX_PAGES
                or projected_bytes > MAX_HISTORY
                or projected_count > MAX_MESSAGES
            ):
                _fail("history-limit")
        elif path.endswith("/export"):
            _json(value)
        return value

    try:
        value = bridge.checked_export(request, ident)
    except bridge.FixedError as error:
        _fail(
            "unsettled-history"
            if str(error) in bridge.PENDING_MESSAGES
            else "invalid-native-history"
        )
    except SessionBackupError:
        raise
    except Exception:
        _fail("invalid-native-history")
    _json(value)
    return value


def _directory(value):
    return (
        sessions._directory(value)
        and ".." not in value.split("/")
        and "." not in value.split("/")
    )


def _validate_tree(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"source", "sessions"}
        or not isinstance(payload["source"], dict)
        or set(payload["source"]) != {"origin", "store", "id"}
        or not isinstance(payload["sessions"], list)
        or not 1 <= len(payload["sessions"]) <= MAX_SESSIONS
    ):
        _fail("invalid-session-tree")
    source = payload["source"]
    _domain(sessions.session_identity, source["origin"], source["store"], source["id"])
    infos, ids, message_ids, total_messages = [], set(), set(), 0
    for exported in payload["sessions"]:
        if (
            not isinstance(exported, dict)
            or set(exported) - {"info", "messages", bridge.INTERRUPTED}
            or not isinstance(exported.get("messages"), list)
        ):
            _fail("invalid-history")
        info = _domain(sessions.normalize_session, exported.get("info"))
        if info["id"] in ids:
            _fail("duplicate-session")
        ids.add(info["id"])
        infos.append(info)
        if bridge.INTERRUPTED in exported:
            count = exported[bridge.INTERRUPTED]
            if (
                not isinstance(count, dict)
                or set(count) != {"assistant"}
                or type(count["assistant"]) is not int
                or count["assistant"] < 1
                or count["assistant"] > MAX_MESSAGES
            ):
                _fail("invalid-interrupted-count")
        for message in exported["messages"]:
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("id"), str)
                or not re.fullmatch(r"msg_[A-Za-z0-9_-]+", message["id"])
                or message["id"] in message_ids
                or not isinstance(message.get("type"), str)
                or message.get("type") not in MESSAGE_TYPES
                or not isinstance(message.get("time"), dict)
                or type(message["time"].get("created")) not in (int, float)
            ):
                _fail("invalid-message")
            content = message.get("content", [])
            if message["type"] == "assistant" and (
                not isinstance(content, list)
                or any(not isinstance(part, dict) for part in content)
                or any(
                    part.get("type") == "tool"
                    and not isinstance(part.get("state"), dict)
                    for part in content
                )
            ):
                _fail("invalid-message")
            if (
                (
                    message["type"] in ("shell", "compaction")
                    and message.get("status") == "running"
                )
                or (
                    message["type"] == "assistant"
                    and message["time"].get("completed") is None
                )
                or (
                    message["type"] == "assistant"
                    and any(
                        part.get("type") == "tool"
                        and part.get("state", {}).get("status")
                        in ("running", "streaming")
                        for part in content
                        if isinstance(part, dict)
                    )
                )
            ):
                _fail("unsettled-history")
            message_ids.add(message["id"])
            total_messages += 1
            if total_messages > MAX_MESSAGES:
                _fail("history-limit")
    ordered = _domain(sessions._descendants, infos, source["id"])
    if ordered != infos:
        _fail("invalid-session-tree")
    return infos


def _review(payload):
    infos = _validate_tree(payload)
    return {
        "format": FORMAT,
        "format_version": VERSION,
        "source": copy.deepcopy(payload["source"]),
        "sessions": copy.deepcopy(infos),
        "session_count": len(infos),
        "message_count": sum(len(item["messages"]) for item in payload["sessions"]),
        "interrupted_assistant_count": sum(
            item.get(bridge.INTERRUPTED, {}).get("assistant", 0)
            for item in payload["sessions"]
        ),
    }


@contextlib.contextmanager
def _parent(path, *, writing=False):
    # Anchor every ancestor by directory descriptor; O_NOFOLLOW on the final
    # file alone would still follow symlinked parents. Do not create directories.
    path = Path(path).expanduser().absolute()
    if ".." in path.parts or path.name in ("", ".", ".."):
        _fail("unsafe-path")
    descriptor = None
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for component in path.parent.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if writing and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            _fail("unsafe-output-directory")
        yield descriptor, path.name
    except OSError:
        _fail("file-operation-failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    _fail("output-exists")


def _crypto(function, *args, **kwargs):
    crypto = _crypto_module()

    try:
        return function(*args, **kwargs)
    except crypto.BackupError:
        _fail("encryption-failed")
    except OSError:
        _fail("encryption-failed")


def _crypto_module():
    if __package__:
        from . import backup_crypto
    else:
        import backup_crypto
    return backup_crypto


def _decrypt(path, private, limit):
    """Bound decrypted output as it arrives, without changing old crypto limits.

    A pipe and nonblocking bounded reads avoid letting age fill an unbounded
    memfd before checking the size. Errors are discarded, never displayed.
    Key-envelope TTY prompts remain the existing load_key implementation.
    """
    crypto = _crypto_module()

    process = None
    with crypto.memory_file(private) as identity:
        try:
            process = subprocess.Popen(
                [
                    "age",
                    "--decrypt",
                    "--identity",
                    f"/proc/self/fd/{identity.fileno()}",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=crypto.environment(),
                pass_fds=(identity.fileno(),),
                start_new_session=True,
            )
            os.set_blocking(process.stdout.fileno(), False)
            result = bytearray()
            deadline = time.monotonic() + 120
            while True:
                if time.monotonic() >= deadline:
                    _fail("decryption-timeout")
                if not select.select([process.stdout], [], [], 0.1)[0]:
                    continue
                chunk = os.read(
                    process.stdout.fileno(), min(65536, limit + 1 - len(result))
                )
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > limit:
                    _fail("history-limit")
            if process.wait(timeout=3):
                _fail("decryption-failed")
            return bytes(result)
        except (OSError, subprocess.SubprocessError):
            _fail("decryption-failed")
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                process.stdout.close()


def _pack(directory, output):
    with tarfile.open(
        fileobj=output, mode="w|", format=tarfile.USTAR_FORMAT
    ) as archive:
        for name in ENTRIES:
            path = directory / name
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, path.stat().st_size
            with path.open("rb") as source:
                archive.addfile(info, source)


def _unpack(source, directory):
    # Parse just three ordinary USTAR records. tarfile's generic parser accepts
    # PAX/GNU extensions and trailing concatenated archives; this format does not.
    for expected in ENTRIES:
        header = source.read(512)
        try:
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.TarError, ValueError, UnicodeError):
            _fail("invalid-envelope")
        limit = (
            MAX_KEY
            if expected == "key.age"
            else MAX_METADATA
            if expected == "manifest.age"
            else MAX_CIPHERTEXT
        )
        if (
            member.name != expected
            or member.type != tarfile.REGTYPE
            or member.linkname
            or member.size <= 0
            or member.size > limit
            or member.mode != 0o600
            or member.uid
            or member.gid
            or header[257:263] != b"ustar\x00"
        ):
            _fail("invalid-envelope")
        with (directory / expected).open("xb") as output:
            os.chmod(output.fileno(), 0o600)
            remaining = member.size
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    _fail("truncated-envelope")
                output.write(chunk)
                remaining -= len(chunk)
        padding = source.read((-member.size) % 512)
        if any(padding) or len(padding) != (-member.size) % 512:
            _fail("invalid-envelope")
    trailing = source.read(10241)
    if len(trailing) < 1024 or len(trailing) > 10240 or any(trailing):
        _fail("invalid-envelope")


def _load(path, identities):
    crypto = _crypto_module()

    with (
        _parent(path) as (parent, name),
        tempfile.TemporaryDirectory(prefix="oc-box-encrypted-sessions-") as temporary,
    ):
        directory = Path(temporary)
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > MAX_ENVELOPE
                ):
                    _fail("unsafe-input")
                _unpack(source, directory)
        except OSError:
            _fail("file-operation-failed")
        _crypto(crypto.tools)
        private = _crypto(crypto.load_key, directory / "key.age", identities)
        manifest = _decode(_decrypt(directory / "manifest.age", private, MAX_METADATA))
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "format_version", "components", "review"}
            or manifest.get("format") != FORMAT
            or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != VERSION
            or not isinstance(manifest.get("components"), dict)
            or set(manifest["components"]) != {"key.age", "sessions.age"}
        ):
            _fail("unsupported-format")
        for component in ("key.age", "sessions.age"):
            if manifest["components"][component] != crypto.digest(
                directory / component
            ):
                _fail("ciphertext-mismatch")
        payload = _decode(_decrypt(directory / "sessions.age", private, MAX_HISTORY))
        if _review(payload) != manifest["review"]:
            _fail("manifest-mismatch")
        return payload


def backup(
    catalog,
    session_id,
    output,
    *,
    recipients=(),
    minimum=None,
    read_only=False,
    demo=False,
):
    """Export one selected native tree across all project filters, always age encrypted.

    Existing output, unknown ownership, incomplete inventory, active/unsettled or
    changed history refuses. Idle persistent PTYs are intentionally not deleted
    or treated as active. No delete preview/token or managed-child fence is used.
    """
    _gate(read_only=read_only, demo=demo)
    _catalog(catalog)
    with _parent(output, writing=True) as (parent, name):
        _absent(parent, name)
        selected = _domain(sessions._descendants, _inventory(catalog), session_id)
        if len(selected) > MAX_SESSIONS:
            _fail("session-limit")
        previous = catalog._snapshots.get(session_id)
        if previous is not None and previous != selected[0]:
            _fail("snapshot-changed")
        ownership = _quiet(catalog, [item["id"] for item in selected])
        payload = {
            "source": {
                "origin": catalog.origin,
                "store": catalog.store,
                "id": session_id,
            },
            "sessions": [],
        }
        for item in selected:
            _unchanged(catalog, [item])
            exported = _export(catalog, item["id"])
            if _domain(sessions.normalize_session, exported.get("info")) != item:
                _fail("snapshot-changed")
            payload["sessions"].append(exported)
            _json(payload)
            _unchanged(catalog, [item])
            if _quiet(catalog, [entry["id"] for entry in selected]) != ownership:
                _fail("ownership-changed")
        if _domain(sessions._descendants, _inventory(catalog), session_id) != selected:
            _fail("snapshot-changed")
        for item, exported in zip(selected, payload["sessions"]):
            if _export(catalog, item["id"]) != exported:
                _fail("snapshot-changed")
        _unchanged(catalog, selected)
        if _quiet(catalog, [item["id"] for item in selected]) != ownership:
            _fail("ownership-changed")
        if _domain(sessions._descendants, _inventory(catalog), session_id) != selected:
            _fail("snapshot-changed")
        _unchanged(catalog, selected)
        review = _review(payload)
        crypto = _crypto_module()

        temporary_name = ".ocsession-" + secrets.token_hex(16)
        created = False
        try:
            with tempfile.TemporaryDirectory(
                prefix="oc-box-encrypted-sessions-"
            ) as temporary:
                directory = Path(temporary)
                private, recipient = _crypto(
                    crypto.generate_key,
                    tuple(recipients),
                    directory / "key.age",
                    minimum=minimum,
                )
                _crypto(
                    crypto.encrypt_bytes,
                    _json(payload),
                    recipient,
                    directory / "sessions.age",
                )
                manifest = {
                    "format": FORMAT,
                    "format_version": VERSION,
                    "review": review,
                    "components": {
                        entry: crypto.digest(directory / entry)
                        for entry in ("key.age", "sessions.age")
                    },
                }
                _crypto(
                    crypto.encrypt_bytes,
                    _json(manifest, MAX_METADATA // 2),
                    recipient,
                    directory / "manifest.age",
                )
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                created = True
                with os.fdopen(descriptor, "wb") as destination:
                    os.fchmod(destination.fileno(), 0o600)
                    _pack(directory, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                # link is atomic and refuses *all* existing targets, including
                # symlinks. Never use replace, rename-overwrite or --force.
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.fsync(parent)
                del private
        except FileExistsError:
            _fail("output-exists")
        except OSError:
            _fail("file-operation-failed")
        finally:
            if created:
                os.unlink(temporary_name, dir_fd=parent)
    return {
        **review,
        "warning": sessions.PROCESS_LOCAL_WARNING
        if ownership == "process-local"
        else "",
    }


def inspect(input, *, identities=(), read_only=False, demo=False):
    """Return allowlisted review metadata only, never decrypted messages."""
    _gate(read_only=read_only, demo=demo, inspect_only=True)
    return _review(_load(input, tuple(identities)))


class RestorePlan:
    """Opaque same-process review, not serializable storage or an import payload.

    Call close() to discard unconfirmed plaintext promptly. The caller must show
    review and obtain explicit confirmation before restore(catalog, plan).
    """

    __slots__ = (
        "_catalog",
        "_binding",
        "_inventory",
        "_ownership",
        "_payload",
        "_review",
        "_digest",
        "_used",
        "_pid",
    )

    def __init__(self, catalog, inventory, ownership, payload, review):
        self._catalog = catalog
        self._binding = (catalog.request, catalog.origin, catalog.store)
        self._inventory, self._ownership = inventory, ownership
        self._payload, self._review = payload, review
        self._digest = hashlib.sha256(
            _json([inventory, ownership, payload, review], MAX_HISTORY * 2)
        ).digest()
        self._used, self._pid = False, os.getpid()

    @property
    def review(self):
        return copy.deepcopy(self._review)

    def close(self):
        self._used = True
        self._payload = None


def _fresh(prefix, used):
    for _ in range(10):
        ident = prefix + "_" + secrets.token_hex(16)
        if ident not in used:
            used.add(ident)
            return ident
    _fail("identity-collision")


def _imports(payload, directory, used):
    # Fresh also means different from the source even when restoring into an
    # otherwise empty, unrelated store.
    used.update(item["info"]["id"] for item in payload["sessions"])
    used.update(
        message["id"] for item in payload["sessions"] for message in item["messages"]
    )
    session_ids = {
        item["info"]["id"]: _fresh("ses", used) for item in payload["sessions"]
    }
    message_ids = {
        message["id"]: _fresh("msg", used)
        for item in payload["sessions"]
        for message in item["messages"]
    }
    result = []
    for item in payload["sessions"]:
        old = item["info"]
        provenance = {
            "version": VERSION,
            "source": {**payload["source"], "id": old["id"]},
        }
        if bridge.INTERRUPTED in item:
            provenance["interrupted"] = copy.deepcopy(item[bridge.INTERRUPTED])
        # Deliberately construct rather than deleting a list of known privileges.
        # No source agent/model, auth/config, managed-archive marker, shell/job,
        # fork/revert, permissions, arbitrary metadata or subpath is reactivated.
        info = {
            "id": session_ids[old["id"]],
            "projectID": "global",
            "location": {"directory": directory},
            "title": old.get("title", "Session"),
            "time": {
                key: old["time"][key]
                for key in ("created", "updated", "archived")
                if key in old["time"]
            },
            "permissions": [],
            "cost": 0,
            "tokens": {
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "metadata": {"oc-box.session-backup": provenance},
        }
        parent = session_ids.get(old.get("parentID"))
        if parent:
            info["parentID"] = parent
        messages = copy.deepcopy(item["messages"])
        for message in messages:
            message["id"] = message_ids[message["id"]]
            # Only known structural references: never rewrite arbitrary user text
            # or opaque tool/provider payloads that happen to contain an old ID.
            if "sessionID" in message:
                message["sessionID"] = info["id"]
            metadata = message.get("metadata")
            if message["type"] == "compaction" and isinstance(metadata, dict):
                if "keepID" in metadata:
                    if (
                        not isinstance(metadata["keepID"], str)
                        or metadata["keepID"] not in message_ids
                    ):
                        _fail("foreign-message-reference")
                    metadata["keepID"] = message_ids[metadata["keepID"]]
        result.append(
            {"info": info, "messages": messages, "location": {"directory": directory}}
        )
    return result


def prepare_restore(
    catalog, input, directory, *, identities=(), read_only=False, demo=False
):
    """Decrypt/validate first, then bind a metadata-only review to a quiet target.

    No import occurs here. Keep this object only in the foreground child and
    pass it to restore after the user confirms its review, or close on cancel.
    """
    _gate(read_only=read_only, demo=demo)
    _catalog(catalog)
    if not _directory(directory):
        _fail("invalid-directory")
    directory = "/" + "/".join(part for part in directory.split("/") if part)
    payload = _load(input, tuple(identities))
    review = _review(payload)
    inventory = _inventory(catalog)
    ownership = _quiet(catalog, [item["id"] for item in inventory])
    _unchanged(catalog, inventory)
    imports = _imports(payload, directory, {item["id"] for item in inventory})
    review.update(
        {
            "destination": {
                "origin": catalog.origin,
                "store": catalog.store,
                "directory": directory,
            },
            "created_ids": [item["info"]["id"] for item in imports],
            "warning": sessions.PROCESS_LOCAL_WARNING
            if ownership == "process-local"
            else "",
        }
    )
    return RestorePlan(catalog, inventory, ownership, imports, review)


def _verify(catalog, expected):
    ident = expected["info"]["id"]
    info = _domain(
        sessions._call, catalog.request, "GET", "/api/session/" + quote(ident, safe="")
    )
    info = info.get("data") if isinstance(info, dict) else None
    if not isinstance(info, dict):
        _fail("import-verification-failed")
    actual = _export(catalog, ident)
    for observed in (info, actual.get("info", {})):
        if not isinstance(observed, dict):
            _fail("import-verification-failed")
        for key in ("id", "location", "title", "permissions", "metadata", "parentID"):
            if observed.get(key) != expected["info"].get(key):
                _fail("import-verification-failed")
        if any(
            key in observed for key in ("fork", "revert", "agent", "model", "subpath")
        ):
            _fail("import-verification-failed")
        # Native stamps updated on import. Creation/archive state and zeroed
        # accounting must nevertheless agree; no busy/idle state is revived.
        if (
            not isinstance(observed.get("time"), dict)
            or any(
                observed["time"].get(key) != expected["info"]["time"].get(key)
                for key in ("created", "archived", "busy", "idle")
            )
            or any(
                observed.get(key) != expected["info"][key] for key in ("cost", "tokens")
            )
        ):
            _fail("import-verification-failed")
    if actual.get("messages") != expected["messages"] or bridge.INTERRUPTED in actual:
        _fail("import-verification-failed")


def restore(catalog, plan, *, read_only=False, demo=False):
    """Consume a confirmed plan once; never overwrite/delete or claim atomicity.

    Verification includes GET, checked export and complete inventory, not merely
    native import success. Failures carry only observed fresh IDs and attempted
    IDs for explicit operator recovery; no broad rollback or retry is performed.
    """
    _gate(read_only=read_only, demo=demo)
    _catalog(catalog)
    if (
        not isinstance(plan, RestorePlan)
        or plan._used
        or plan._pid != os.getpid()
        or plan._catalog is not catalog
        or plan._binding != (catalog.request, catalog.origin, catalog.store)
    ):
        _fail("invalid-review")
    plan._used = True
    attempted, observed = [], []
    try:
        if (
            hashlib.sha256(
                _json(
                    [plan._inventory, plan._ownership, plan._payload, plan._review],
                    MAX_HISTORY * 2,
                )
            ).digest()
            != plan._digest
        ):
            _fail("invalid-review")
        before = {item["id"]: item for item in plan._inventory}
        if _inventory(catalog) != plan._inventory:
            _fail("snapshot-changed")
        if _quiet(catalog, tuple(before)) != plan._ownership:
            _fail("ownership-changed")
        _unchanged(catalog, plan._inventory)
        for item in plan._payload:
            current = _inventory(catalog)
            if {row["id"] for row in current} != set(before) | set(observed):
                _fail("snapshot-changed")
            _unchanged(catalog, plan._inventory)
            if _quiet(catalog, tuple(before) + tuple(observed)) != plan._ownership:
                _fail("ownership-changed")
            ident = item["info"]["id"]
            if ident in {row["id"] for row in current}:
                _fail("identity-collision")
            attempted.append(ident)
            _domain(
                sessions._call,
                catalog.request,
                "POST",
                "/api/experimental/session/import",
                item,
            )
            _verify(catalog, item)
            current = _inventory(catalog)
            if {row["id"] for row in current} != set(before) | set(attempted):
                _fail("import-verification-failed")
            observed.append(ident)
            _unchanged(catalog, plan._inventory)
        for item in plan._payload:
            _verify(catalog, item)
        if _quiet(catalog, tuple(before) + tuple(observed)) != plan._ownership:
            _fail("ownership-changed")
        if {row["id"] for row in _inventory(catalog)} != set(before) | set(observed):
            _fail("import-verification-failed")
        _unchanged(catalog, plan._inventory)
        return {
            "created_ids": observed,
            "session_count": len(observed),
            "warning": plan._review["warning"],
            "atomic": False,
        }
    except SessionBackupError as error:
        # A transport can fail after commit. Observe only planned/attempted fresh
        # IDs, without discovering/deleting unrelated sessions or masking failure.
        try:
            observed = [
                row["id"] for row in _inventory(catalog) if row["id"] in attempted
            ]
        except SessionBackupError:
            pass
        raise SessionBackupError(
            error.reason, created_ids=observed, attempted_ids=attempted
        ) from None
    finally:
        plan.close()
