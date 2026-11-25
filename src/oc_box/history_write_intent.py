"""A workspace-local fence for one serial V2 destination operation.

The caller holds the workspace lifecycle lock and performs native admission,
fresh destination checks, checked readback and durable checkpoint publication.
This module does none of those operations. In particular, no record is not
proof that a legacy launcher (or an older, interrupted write) left the native
destination equal to the checkpoint. The prototype must independently inspect
the destination before trusting an unchanged checkpoint or reserving a write.

An immutable pending record is published before returning permission to start
the operation. Retirement durably records the *same* intent in a separate
receipt after the caller supplies the verified checkpoint result. The pending
bytes stay intact even if retirement directory fsync fails. Only a subsequent
reservation may remove a durably receipted pending name; the receipt survives
that removal. Abandoned staging files are never swept. Invalid or uncertain
state is not permission to replay, replace or roll back native history.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat

if __package__:
    from . import workspace_preferences as _workspace
else:
    import workspace_preferences as _workspace


PENDING_NAME = "history-write-intent.json"
RECEIPT_NAME = "history-write-verified.json"
MAX_BYTES = 2048
_DIGEST = re.compile(r"[0-9a-f]{64}")
_WORKSPACE_ID = re.compile(r"[0-9a-f]{32}")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_V2_VERSION = re.compile(r"2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_SESSION_ID = re.compile(r"ses[A-Za-z0-9_-]{1,253}")
_CATEGORIES = frozenset(
    (
        "invalid-scope",
        "unsafe-storage",
        "busy",
        "closed",
        "invalid-record",
        "profile-mismatch",
        "unresolved",
        "publication-unverified",
        "checkpoint-unverified",
        "retirement-unverified",
    )
)


class JournalError(RuntimeError):
    """Fixed diagnostics; never include private state or raw system errors."""

    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in _CATEGORIES
            else "unsafe-storage"
        )
        super().__init__(f"History write intent refused ({self.category}).")


@dataclass(frozen=True)
class WriteIntent:
    operation_id: str
    workspace_id: str
    profile: str
    source_id: str
    archive_id: str
    expected_prior_digest: str | None
    intended_digest: str
    atomic_create: bool = False


@dataclass(frozen=True)
class VerifiedCheckpoint:
    """Caller attestation, not a way to verify a native or checkpoint result.

    Construct only after independent, fresh target readback AND successful
    durable publication of the checkpoint bound to that target. Populate the
    digests from their respective results, never from the proposed payload.
    """

    operation_id: str
    workspace_id: str
    profile: str
    source_id: str
    archive_id: str
    destination_digest: str
    checkpoint_digest: str


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _valid(intent):
    if not (
        type(intent) is WriteIntent
        and _matches(_DIGEST, intent.operation_id)
        and _matches(_WORKSPACE_ID, intent.workspace_id)
        and _matches(_PROFILE, intent.profile)
        and _matches(_SESSION_ID, intent.source_id)
        and _matches(_SESSION_ID, intent.archive_id)
        and (
            intent.expected_prior_digest is None
            or _matches(_DIGEST, intent.expected_prior_digest)
        )
        and _matches(_DIGEST, intent.intended_digest)
        and intent.expected_prior_digest != intent.intended_digest
        and type(intent.atomic_create) is bool
        and (not intent.atomic_create or intent.expected_prior_digest is None)
    ):
        return False
    # Match the existing V2 archive namespace, not merely a valid session ID.
    namespace = hashlib.sha256((intent.workspace_id + "-v2").encode("ascii"))
    suffix = intent.source_id.split("_", 1)[-1]
    return intent.archive_id == f"ses_{namespace.hexdigest()[:12]}_{suffix}"


def _encode(intent, kind):
    return (
        json.dumps(
            {"version": 1, "native_major": 2, "kind": kind, **asdict(intent)},
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _decode(raw, kind):
    try:
        value = json.loads(raw.decode("ascii"))
        if type(value) is not dict:
            raise JournalError("invalid-record")
        version = value.pop("version", None)
        major = value.pop("native_major", None)
        if (type(version), version, type(major), major) != (int, 1, int, 2):
            raise JournalError("invalid-record")
        if value.pop("kind", None) != kind:
            raise JournalError("invalid-record")
        intent = WriteIntent(**value)
        # Canonical round-trip also rejects duplicate keys, alternate types,
        # unexpected fields and trailing content. Parsing is byte bounded.
        if not _valid(intent) or raw != _encode(intent, kind):
            raise JournalError("invalid-record")
        return intent
    except (ValueError, TypeError, RecursionError):
        raise JournalError("invalid-record") from None


@contextmanager
def _errors(category):
    try:
        yield
    except _workspace.PreferenceError:
        raise JournalError("unsafe-storage") from None
    except OSError:
        raise JournalError(category) from None


@dataclass(frozen=True)
class _Entry:
    intent: WriteIntent
    stamp: tuple


class Journal:
    """Pin one V2 workspace and exclusively hold its journal until context exit.

    ``profile`` is the caller's bounded, exact admission-profile identity, not
    evidence of native compatibility. An active intent from another profile
    refuses; a previous receipt alone grants no permission or shortcut.
    """

    def __init__(self, workspace_path, workspace_id, *, profile):
        if not _matches(_WORKSPACE_ID, workspace_id) or not _matches(_PROFILE, profile):
            raise JournalError("invalid-scope")
        self._path = workspace_path
        self._id = workspace_id
        self._profile = profile
        self._context = self._directory = None

    @contextmanager
    def _open(self):
        stack = ExitStack()
        try:
            with _errors("unsafe-storage"):
                directory = stack.enter_context(
                    _workspace._directory(self._path, self._id)
                )
                try:
                    # The directory itself is a stable lock inode; no separate
                    # replaceable lock file or initialization is needed.
                    fcntl.flock(directory.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise JournalError("busy") from None
                directory.check()
                raw, _ = _workspace._read_file(
                    directory,
                    "workspace.json",
                    private=False,
                    maximum=_workspace.MAX_REGISTRY_BYTES,
                )
                data = _workspace._decode(raw) if raw is not None else None
                if (
                    type(data) is not dict
                    or data.get("id") != self._id
                    or not _matches(_V2_VERSION, data.get("opencode_version"))
                ):
                    raise JournalError("invalid-scope")
            yield directory
        finally:
            # Release the pin without reclassifying caller-side exceptions
            # from native admission/mutation/verification as journal errors.
            stack.close()

    def __enter__(self):
        if self._context is not None:
            raise JournalError("busy")
        context = self._open()
        self._directory = context.__enter__()
        self._context = context
        return self

    def __exit__(self, *exc):
        context = self._context
        self._context = self._directory = None
        return context.__exit__(*exc)

    def _check(self):
        if self._directory is None:
            raise JournalError("closed")
        self._directory.check()
        return self._directory.fd

    def _read(self, name, kind):
        # Do not retry a replacing journal inode. The directory lock excludes
        # cooperating replacement, so a change here is unsafe, not a new record.
        raw, info = _workspace._read_file_once(
            self._directory,
            name,
            private=True,
            maximum=MAX_BYTES,
        )
        if raw is None:
            return None
        intent = _decode(raw, kind)
        if intent.workspace_id != self._id:
            raise JournalError("invalid-record")
        return _Entry(intent, _workspace._stamp(info))

    def _snapshot(self):
        self._check()
        pending = self._read(PENDING_NAME, "pending")
        receipt = self._read(RECEIPT_NAME, "verified")
        self._check()
        if pending and _receipted(pending, receipt):
            return pending, receipt
        if pending and pending.intent.profile != self._profile:
            raise JournalError("profile-mismatch")
        return pending, receipt

    def _sync_pair(self, pending, receipt):
        # A visible retirement receipt after an earlier fsync failure is not
        # settled until fsync actually succeeds and the validated pair survives.
        os.fsync(self._check())
        if self._snapshot() != (pending, receipt):
            raise JournalError("retirement-unverified")

    def load(self):
        """Return the unresolved intent, or None (not destination proof)."""
        with _errors("unsafe-storage"):
            pending, receipt = self._snapshot()
            if pending is None:
                return None
            if _receipted(pending, receipt):
                with _errors("retirement-unverified"):
                    self._sync_pair(pending, receipt)
                return None
            return pending.intent

    def reserve(self, source_id, archive_id, expected_prior_digest, intended_digest):
        """Durably reserve a new mutation, without replacing an unresolved one.

        ``expected_prior_digest=None`` means independently verified absence,
        never unknown state. Call only with fresh caller-side destination
        evidence, under the same lifecycle and native admission fences as the
        mutation. A raised exception never authorizes native mutation.
        """
        return self._reserve(
            source_id,
            archive_id,
            expected_prior_digest,
            intended_digest,
            atomic_create=False,
        )

    def reserve_create(self, source_id, archive_id, intended_digest):
        """Record one qualified atomic create without implying prior absence.

        The caller must enforce a native create-if-absent contract. This
        record never substitutes for admission, a native acknowledgement,
        or independent readback.
        """
        return self._reserve(
            source_id, archive_id, None, intended_digest, atomic_create=True
        )

    def _reserve(
        self,
        source_id,
        archive_id,
        expected_prior_digest,
        intended_digest,
        *,
        atomic_create,
    ):
        intent = WriteIntent(
            secrets.token_hex(32),
            self._id,
            self._profile,
            source_id,
            archive_id,
            expected_prior_digest,
            intended_digest,
            atomic_create,
        )
        if not _valid(intent):
            raise JournalError("invalid-record")
        with _errors("publication-unverified"):
            directory = self._check()
            pending, receipt = self._snapshot()
            if pending and not _receipted(pending, receipt):
                raise JournalError("unresolved")
            if pending:
                self._sync_pair(pending, receipt)
            with _stage(directory, _encode(intent, "pending")) as (name, identity):
                if self._snapshot() != (pending, receipt):
                    raise JournalError("publication-unverified")
                if pending:
                    # The verified receipt already durably preserves these
                    # exact bytes semantically and remains untouched here.
                    os.unlink(PENDING_NAME, dir_fd=directory)
                _link(directory, name, PENDING_NAME)
            os.fsync(directory)
            current, previous_receipt = self._snapshot()
            if (
                not current
                or current.intent != intent
                or current.stamp[:2] != identity
                or receipt != previous_receipt
            ):
                raise JournalError("publication-unverified")
            return intent

    def retire(self, intent, proof):
        """Retire only this exact intent with matching durable checkpoint proof."""
        if (
            not _valid(intent)
            or intent.workspace_id != self._id
            or intent.profile != self._profile
            or type(proof) is not VerifiedCheckpoint
            or any(type(value) is not str for value in vars(proof).values())
            or proof
            != VerifiedCheckpoint(
                intent.operation_id,
                intent.workspace_id,
                intent.profile,
                intent.source_id,
                intent.archive_id,
                intent.intended_digest,
                intent.intended_digest,
            )
        ):
            raise JournalError("checkpoint-unverified")
        with _errors("retirement-unverified"):
            directory = self._check()
            pending, receipt = self._snapshot()
            if not pending or pending.intent != intent:
                raise JournalError("retirement-unverified")
            if _receipted(pending, receipt):
                self._sync_pair(pending, receipt)
                return
            with _stage(directory, _encode(intent, "verified")) as (name, identity):
                if self._snapshot() != (pending, receipt):
                    raise JournalError("retirement-unverified")
                if receipt is None:
                    _link(directory, name, RECEIPT_NAME)
                else:
                    # The prior receipt is for a resolved operation. Keep the
                    # *current* pending inode unchanged across this publication,
                    # including on a failed directory fsync.
                    os.replace(
                        name, RECEIPT_NAME, src_dir_fd=directory, dst_dir_fd=directory
                    )
            os.fsync(directory)
            current, current_receipt = self._snapshot()
            if (
                current != pending
                or not _receipted(pending, current_receipt)
                or current_receipt.stamp[:2] != identity
            ):
                raise JournalError("retirement-unverified")


def _receipted(pending, receipt):
    if not receipt or pending.intent.operation_id != receipt.intent.operation_id:
        return False
    if pending.intent != receipt.intent:
        raise JournalError("invalid-record")
    return True


def _link(directory, source, target):
    # Atomic no-overwrite publication, including when a duplicate, special file
    # or dangling symlink appears after preflight.
    os.link(
        source,
        target,
        src_dir_fd=directory,
        dst_dir_fd=directory,
        follow_symlinks=False,
    )


@contextmanager
def _stage(directory, payload):
    name = ".history-write-" + secrets.token_hex(16) + ".tmp"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if not written:
                    raise OSError("short journal write")
                remaining = remaining[written:]
            os.fsync(fd)
            info = os.fstat(fd)
            observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
            _workspace._file_info(info, private=True, maximum=MAX_BYTES)
            if _workspace._stamp(info) != _workspace._stamp(observed):
                raise JournalError("publication-unverified")
            yield name, _workspace._identity(info)
        finally:
            try:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass  # The staged receipt was renamed to its verified name.
            else:
                if stat.S_ISREG(entry.st_mode) and _workspace._identity(
                    entry
                ) == _workspace._identity(os.fstat(fd)):
                    os.unlink(name, dir_fd=directory)
                else:
                    raise JournalError("publication-unverified")
    finally:
        os.close(fd)
