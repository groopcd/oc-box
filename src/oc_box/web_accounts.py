"""Host-only storage for future named web sign-in, not an authentication API.

Callers supply an absolute file Path in a dedicated, existing 0700 directory
outside workspaces and backup inputs, and an in-memory store passphrase.
Registration and server setup are opaque protocol bytes, never passwords.
The caller must vet those records and obtain workspace IDs from verified host
registrations. This module checks ID shape only; a loaded snapshot grants
nothing and does not prove a workspace still exists.

No default path, credential forwarding, migration, or backup/restore hook.
File checks detect observed substitutions, not all same-user races. Encryption
does not protect against rollback, same-user memory access, or memory copies.
"""

import base64
import fcntl
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock, current_thread

MAX_ACCOUNTS = 128
MAX_WORKSPACES = 32
MAX_SETUP_BYTES = 16 * 1024
MAX_REGISTRATION_BYTES = 4 * 1024
MAX_FILE = 1024 * 1024
_MAX_PASSPHRASE_BYTES = 1024
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.01
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
_WORKSPACE_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_HASH_CHUNK = 64 * 1024

# V1: magic/version, 16-byte salt, 12-byte nonce, then AES-256-GCM ciphertext
# and tag. The entire prefix is AAD. V1 fixes Scrypt at N=2**17, r=8, p=1;
# nothing in an input file can request more work. Its salt domain is separate
# from the provider vault's, even if an operator uses the same passphrase.
_FORMAT = b"oc-box-web-accounts\x00\x01"
_HEADER_SIZE = len(_FORMAT) + 16 + 12
_TAG_SIZE = 16

_UNSAFE = "Web account storage is unsafe or unavailable."
_MISSING = "Web account store is not initialized."
_EXISTS = "Web account store already exists; initialization will not overwrite it."
_INVALID = "Web account store has an invalid or unsupported format."
_UNVERIFIED = "Web account store save could not be verified; reload before retrying."
_BAD_PASSPHRASE = "Invalid web account store passphrase length or encoding."
_BAD_SCOPE = "Web account scope requires distinct, explicit workspace IDs."
_STALE_REVIEW = "Web account registration review is no longer current."


class WebAccountsError(ValueError):
    """Fixed explanations only, without record data, passphrases or raw paths."""


@dataclass(frozen=True, slots=True)
class Account:
    username: str = field(repr=False)
    workspace_ids: tuple[str, ...] = field(repr=False)
    registration: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AccountStore:
    setup: bytes = field(repr=False)
    accounts: tuple[Account, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class AccountSnapshot:
    """Host-only loaded store and independently closable generation check."""

    store: AccountStore
    current: Callable[[], bool]
    close: Callable[[], None]

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


@dataclass(frozen=True, slots=True, eq=False)
class RegistrationReview:
    """Trusted in-process store binding, not browser or host approval."""

    _path: Path = field(repr=False)
    _directory_identity: tuple[int, int] = field(repr=False)
    _file_stamp: tuple = field(repr=False)
    username: str = field(repr=False)
    workspace_ids: tuple[str, ...] = field(repr=False)
    setup: bytes = field(repr=False)
    replacing: bool = field(repr=False)
    _ciphertext_digest: bytes = field(repr=False)


def _passphrase(value, *, new=False):
    if (
        type(value) is not str
        or not 0 < len(value) <= _MAX_PASSPHRASE_BYTES
        or "\0" in value
    ):
        raise WebAccountsError(_BAD_PASSPHRASE)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise WebAccountsError(_BAD_PASSPHRASE) from None
    if len(encoded) > _MAX_PASSPHRASE_BYTES:
        raise WebAccountsError(_BAD_PASSPHRASE)
    if new and len(value) < 8:
        raise WebAccountsError(
            "A new web account store passphrase needs at least 8 characters."
        )
    return encoded


def _username(value):
    if type(value) is not str or not _USERNAME.fullmatch(value):
        raise WebAccountsError(
            "Web usernames must be 1–32 lowercase ASCII letters, digits, "
            "underscores or hyphens and start with a letter or digit."
        )
    return value


def _scope(workspace_ids):
    if type(workspace_ids) not in (tuple, list):
        raise WebAccountsError(_BAD_SCOPE)
    # Copy only a bounded slice, then validate the snapshot, not a mutable input.
    workspace_ids = tuple(workspace_ids[: MAX_WORKSPACES + 1])
    if (
        not 0 < len(workspace_ids) <= MAX_WORKSPACES
        or any(
            type(ident) is not str or not _WORKSPACE_ID.fullmatch(ident)
            for ident in workspace_ids
        )
        or len(set(workspace_ids)) != len(workspace_ids)
    ):
        raise WebAccountsError(_BAD_SCOPE)
    return tuple(sorted(workspace_ids))


def _protocol_bytes(value, maximum):
    if type(value) is not bytes or not 0 < len(value) <= maximum:
        raise WebAccountsError(
            "Web account protocol record is not bounded, nonempty bytes."
        )
    return value


def _b64(value):
    return base64.b64encode(value).decode("ascii")


def _decode_record(encoded, maximum):
    if type(encoded) is not str or len(encoded) > ((maximum + 2) // 3) * 4:
        raise ValueError
    value = _protocol_bytes(base64.b64decode(encoded, validate=True), maximum)
    if _b64(value) != encoded:
        raise ValueError
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _reject_number(_):
    raise ValueError


def _parse(raw):
    try:
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_object,
            parse_int=_reject_number,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        if type(payload) is not dict or set(payload) != {"setup", "accounts"}:
            raise ValueError
        setup = _decode_record(payload["setup"], MAX_SETUP_BYTES)
        entries = payload["accounts"]
        if type(entries) is not list or len(entries) > MAX_ACCOUNTS:
            raise ValueError
        accounts = {}
        for item in entries:
            if type(item) is not dict or set(item) != {
                "username",
                "workspace_ids",
                "registration",
            }:
                raise ValueError
            name = _username(item["username"])
            if name in accounts:
                raise ValueError
            accounts[name] = Account(
                name,
                _scope(item["workspace_ids"]),
                _decode_record(item["registration"], MAX_REGISTRATION_BYTES),
            )
        return AccountStore(setup, tuple(accounts[name] for name in sorted(accounts)))
    except (ValueError, RecursionError):
        raise WebAccountsError(_INVALID) from None


def _cipher(passphrase, salt):
    # Keep the optional crypto dependency out of ordinary launcher imports.
    try:
        from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise WebAccountsError(
            "Web account storage needs optional cryptography."
        ) from None
    try:
        key = Scrypt(salt=_FORMAT + salt, length=32, n=2**17, r=8, p=1).derive(
            passphrase
        )
        return AESGCM(key), InvalidTag
    except (UnsupportedAlgorithm, MemoryError, ValueError):
        raise WebAccountsError(
            "Web account storage could not derive its encryption key."
        ) from None


def _open_envelope(raw, passphrase):
    if not _HEADER_SIZE + _TAG_SIZE < len(raw) <= MAX_FILE or not raw.startswith(
        _FORMAT
    ):
        raise WebAccountsError(_INVALID)
    nonce_start = len(_FORMAT) + 16
    cipher, invalid_tag = _cipher(passphrase, raw[len(_FORMAT) : nonce_start])
    try:
        plaintext = cipher.decrypt(
            raw[nonce_start:_HEADER_SIZE], raw[_HEADER_SIZE:], raw[:_HEADER_SIZE]
        )
    except invalid_tag:
        raise WebAccountsError(
            "Unable to unlock web account store: incorrect passphrase or damaged data."
        ) from None
    return _parse(plaintext)


def _seal(store, passphrase):
    plaintext = json.dumps(
        {
            "setup": _b64(store.setup),
            "accounts": [
                {
                    "username": account.username,
                    "workspace_ids": account.workspace_ids,
                    "registration": _b64(account.registration),
                }
                for account in store.accounts
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")
    if _HEADER_SIZE + len(plaintext) + _TAG_SIZE > MAX_FILE:
        raise WebAccountsError("Web account store exceeds its encrypted size limit.")
    salt, nonce = os.urandom(16), os.urandom(12)
    prefix = _FORMAT + salt + nonce
    cipher, _ = _cipher(passphrase, salt)
    return prefix + cipher.encrypt(nonce, plaintext, prefix)


def _directory_info(info):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise WebAccountsError(_UNSAFE)


def _open_directory(path):
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        _directory_info(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    if info is None:
        return None
    return (
        _identity(info),
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _check_directory(path, fd):
    current = _open_directory(path.parent)
    try:
        info = os.fstat(fd)
        _directory_info(info)
        if _identity(os.fstat(current)) != _identity(info):
            raise WebAccountsError(_UNSAFE)
    finally:
        os.close(current)


@contextmanager
def _locked(path):
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise WebAccountsError("Web account storage is host-only.")
    try:
        if (
            not isinstance(path, Path)
            or path.anchor != "/"
            or path.parent == Path("/")
            or ".." in path.parts
            or "\0" in str(path)
            or len(os.fsencode(path)) > 4096
        ):
            raise WebAccountsError(
                "Web account storage requires an absolute private file Path."
            )
        directory = _open_directory(path.parent)
        try:
            # Lock the dedicated directory, not the inode we atomically replace.
            # Exclusive even for reads to bound concurrent Scrypt work here.
            deadline = time.monotonic() + _LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, InterruptedError):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WebAccountsError(
                            "Web account storage is busy; retry later."
                        ) from None
                    time.sleep(min(_LOCK_POLL, remaining))
            _check_directory(path, directory)
            yield directory
        finally:
            os.close(directory)
    except (OSError, UnicodeError):
        raise WebAccountsError(_UNSAFE) from None


def _entry(directory, name):
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _file_info(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > MAX_FILE
    ):
        raise WebAccountsError(_UNSAFE)


def _unchanged(path, directory, expected):
    _check_directory(path, directory)
    current = _entry(directory, path.name)
    if current is not None:
        _file_info(current)
    if _stamp(current) != _stamp(expected):
        raise WebAccountsError(_UNSAFE)


@contextmanager
def _read_ciphertext(path, directory):
    previous = _entry(directory, path.name)
    if previous is None:
        raise WebAccountsError(_MISSING)
    _file_info(previous)
    fd = os.open(path.name, _READ_FLAGS, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if _stamp(os.fstat(fd)) != _stamp(previous):
            raise WebAccountsError(_UNSAFE)
        raw = stream.read(MAX_FILE + 1)
        if len(raw) != previous.st_size or _stamp(os.fstat(fd)) != _stamp(previous):
            raise WebAccountsError(_UNSAFE)
        yield raw, previous, fd


def _read(path, directory, passphrase, *, with_digest=False):
    with _read_ciphertext(path, directory) as (raw, previous, _):
        store = _open_envelope(raw, passphrase)
    _unchanged(path, directory, previous)
    if with_digest:
        return store, previous, sha256(raw).digest()
    return store, previous


def _cleanup(directory, name, fd):
    """Unlink only this writer's own staging inode, even after cancellation."""
    entry = _entry(directory, name)
    if entry is not None and _identity(entry) == _identity(os.fstat(fd)):
        os.unlink(name, dir_fd=directory)


def _publish(path, directory, raw, previous, *, current=None):
    name = ".web-accounts-" + secrets.token_hex(16) + ".tmp"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    published = False
    try:
        os.fchmod(fd, 0o600)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(fd)
        staged = os.fstat(fd)
        _file_info(staged)
        _unchanged(path, directory, previous)
        if staged.st_size != len(raw) or _stamp(_entry(directory, name)) != _stamp(
            staged
        ):
            raise WebAccountsError(_UNSAFE)
        if current is not None and current() is not True:
            raise WebAccountsError(_STALE_REVIEW)
        if previous is None:
            # No-clobber creation. A crash between link and unlink leaves an
            # extra cipher-only link; later operations refuse it, not repair it.
            try:
                os.link(
                    name,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise WebAccountsError(_EXISTS) from None
            published = True
            _cleanup(directory, name, fd)
        else:
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            published = True
        os.fsync(directory)
        saved = os.fstat(fd)
        if (saved.st_size, saved.st_mtime_ns) != (staged.st_size, staged.st_mtime_ns):
            raise WebAccountsError(_UNVERIFIED)
        _unchanged(path, directory, saved)
    except (OSError, WebAccountsError):
        if published:
            raise WebAccountsError(_UNVERIFIED) from None
        raise
    finally:
        try:
            _cleanup(directory, name, fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def initialize(path: Path, passphrase: str, *, setup: bytes) -> None:
    """Explicitly create an empty store with server setup; never overwrite."""
    secret = _passphrase(passphrase, new=True)
    store = AccountStore(_protocol_bytes(setup, MAX_SETUP_BYTES), ())
    with _locked(path) as directory:
        if _entry(directory, path.name) is not None:
            raise WebAccountsError(_EXISTS)
        _publish(path, directory, _seal(store, secret), None)


def load(path: Path, passphrase: str) -> AccountStore:
    """Read and authenticate a snapshot without creating or modifying files."""
    secret = _passphrase(passphrase)
    with _locked(path) as directory:
        store, _ = _read(path, directory, secret)
        return store


def _close_fd(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


class _AccountGeneration:
    """Pinned file/directory, expected metadata and ciphertext digest only."""

    __slots__ = ("_lock", "_path", "_dir", "_file", "_previous", "_digest", "_refused")

    def __init__(self, path, directory, file, previous, digest):
        self._lock = Lock()
        self._path, self._dir, self._file = path, directory, file
        self._previous, self._digest = previous, digest
        self._refused = False

    def _checked(self):
        # Walk only the exact, no-follow parent path. Ciphertext is read only
        # from the descriptor used to authenticate this snapshot, not by name.
        path, directory, file = self._path, self._dir, self._file
        expected = self._previous
        _unchanged(path, directory, expected)
        if _stamp(os.fstat(file)) != _stamp(expected):
            return False
        digest = sha256()
        for offset in range(0, expected.st_size, _HASH_CHUNK):
            size = min(_HASH_CHUNK, expected.st_size - offset)
            chunk = os.pread(file, size, offset)
            if len(chunk) != size:
                return False
            digest.update(chunk)
        if (
            os.pread(file, 1, expected.st_size)
            or _stamp(os.fstat(file)) != _stamp(expected)
            or not secrets.compare_digest(digest.digest(), self._digest)
        ):
            return False
        _unchanged(path, directory, expected)
        return True

    def current(self):
        """Fast sticky refusal: no flock wait, key derivation, or plaintext."""
        with self._lock:
            if self._refused:
                return False
            try:
                current = os.environ.get("OC_BOX_CONTAINER") != "1" and self._checked()
            except Exception:  # noqa: BLE001 - a check failure never exposes path errors.
                current = False
            except BaseException:
                self._refused = True
                raise
            self._refused = not current
            return current

    def close(self):
        with self._lock:
            self._refused = True
            file, self._file = self._file, None
            directory, self._dir = self._dir, None
            _close_fd(file)
            _close_fd(directory)


def load_snapshot(path: Path, passphrase: str) -> AccountSnapshot:
    """Authenticate one existing store and bind its exact ciphertext lifetime.

    Pass ``snapshot.store`` and ``snapshot.current`` to named-site preparation.
    Close the snapshot after service drain (or on startup failure). No secret
    or decrypted record is kept by the generation check. This never grants
    write authority or initializes or falls back to another store.
    """
    secret = _passphrase(passphrase)
    pinned_dir = pinned_file = generation = None
    try:
        with _locked(path) as directory:
            with _read_ciphertext(path, directory) as (raw, previous, file):
                store = _open_envelope(raw, secret)
                _unchanged(path, directory, previous)
                # A dup of the locked directory would retain its flock. Open
                # a separate description of the same pinned directory instead.
                pinned_dir = os.open(".", _DIRECTORY_FLAGS, dir_fd=directory)
                pinned_file = os.dup(file)
                generation = _AccountGeneration(
                    path, pinned_dir, pinned_file, previous, sha256(raw).digest()
                )
                pinned_dir = pinned_file = None
                if not generation.current():
                    raise WebAccountsError(_UNSAFE)
                return AccountSnapshot(store, generation.current, generation.close)
    except BaseException as error:
        if generation is not None:
            generation.close()
        if isinstance(error, Exception) and not isinstance(error, WebAccountsError):
            raise WebAccountsError(_UNSAFE) from None
        raise
    finally:
        _close_fd(pinned_file)
        _close_fd(pinned_dir)


def _check_enrollment_target(store, username, scope, replacing):
    existing = next(
        (item for item in store.accounts if item.username == username), None
    )
    if replacing:
        # A password reset must not quietly expand or narrow the account scope.
        if existing is None or existing.workspace_ids != scope:
            raise WebAccountsError(
                "Web account reset requires the existing workspace scope."
            )
    elif existing is not None:
        raise WebAccountsError("Web account already exists; no record was replaced.")
    elif len(store.accounts) >= MAX_ACCOUNTS:
        raise WebAccountsError("Web account store has reached its account limit.")


def review_registration(path, passphrase, username, *, workspace_ids, replacing=False):
    """Read an authenticated store binding before a reviewed enrollment/reset.

    Only a trusted host caller may select the username and verified workspace
    IDs. Keep this object and its setup bytes inside the host; it is not the
    one-use browser authorization or an account/session lifecycle fence.
    """
    name, scope = _review_target(username, workspace_ids, replacing)
    secret = _passphrase(passphrase)
    with _locked(path) as directory:
        return _review_locked(path, directory, secret, name, scope, replacing)


def _review_target(username, workspace_ids, replacing):
    if type(replacing) is not bool:
        raise WebAccountsError("Web account registration mode is invalid.")
    return _username(username), _scope(workspace_ids)


def _review_locked(path, directory, secret, name, scope, replacing):
    store, previous, digest = _read(path, directory, secret, with_digest=True)
    _check_enrollment_target(store, name, scope, replacing)
    return RegistrationReview(
        path,
        _identity(os.fstat(directory)),
        _stamp(previous),
        name,
        scope,
        store.setup,
        replacing,
        digest,
    )


def commit_registration(path, passphrase, review, *, registration, current=None):
    """Publish a vetted protocol upload against its exact reviewed store.

    This performs a storage compare-and-swap only. The caller must enforce
    host approval, one-use redemption, native owner exclusion/revocation,
    and proof of possession before calling it. Pass the original reviewed
    object. A trusted current guard is checked before replacement while
    holding the store lock. This does not prevent offline store rollback.
    Do not retry on a stale or unverified outcome.
    """
    username, scope, secret, record = _registration_inputs(
        path, passphrase, review, registration, current
    )
    with _locked(path) as directory:
        _commit_registration_locked(
            path, directory, review, username, scope, secret, record, current
        )


def _registration_inputs(path, passphrase, review, registration, current):
    if type(review) is not RegistrationReview or path != review._path:
        raise WebAccountsError(_STALE_REVIEW)
    scope = _scope(review.workspace_ids)
    if (
        type(review.workspace_ids) is not tuple
        or scope != review.workspace_ids
        or type(review.replacing) is not bool
        or type(review._ciphertext_digest) is not bytes
        or len(review._ciphertext_digest) != 32
        or (current is not None and not callable(current))
    ):
        raise WebAccountsError(_STALE_REVIEW)
    username = _username(review.username)
    secret = _passphrase(passphrase)
    record = _protocol_bytes(registration, MAX_REGISTRATION_BYTES)
    return username, scope, secret, record


def _commit_registration_locked(
    path, directory, review, username, scope, secret, record, current
):
    if (
        _identity(os.fstat(directory)) != review._directory_identity
        or _stamp(_entry(directory, path.name)) != review._file_stamp
    ):
        raise WebAccountsError(_STALE_REVIEW)
    store, previous, digest = _read(path, directory, secret, with_digest=True)
    if (
        _stamp(previous) != review._file_stamp
        or store.setup != review.setup
        or not secrets.compare_digest(digest, review._ciphertext_digest)
    ):
        raise WebAccountsError(_STALE_REVIEW)
    _check_enrollment_target(store, username, scope, review.replacing)
    account = Account(username, scope, record)
    others = (item for item in store.accounts if item.username != username)
    updated = AccountStore(
        store.setup,
        tuple(sorted((*others, account), key=lambda item: item.username)),
    )
    try:
        _publish(path, directory, _seal(updated, secret), previous, current=current)
    except KeyboardInterrupt:
        try:
            _unchanged(path, directory, previous)
        except (OSError, WebAccountsError, KeyboardInterrupt):
            raise WebAccountsError(_UNVERIFIED) from None
        raise


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _HeldRegistration:
    """Private storage capability, not browser proof or workspace authority."""

    review: RegistrationReview
    _save: Callable[..., None]

    def commit(self, passphrase, review, *, registration, current):
        self._save(self, passphrase, review, registration, current)


@contextmanager
def _hold_registration(path, passphrase, username, *, workspace_ids, replacing=False):
    """Hold the ordinary account-directory flock from review through one CAS.

    No production callers. The original process/thread must keep this context
    open. The caller owns ceremony deadlines, host approval, proof and any
    independent workspace fence. Its current guard must be bounded and
    read-only. Never pass this capability to the browser or a worker.
    Acquiring the lock is bounded as for every other store operation; only
    scope exit releases it, including after a refused or uncertain CAS.
    """
    name, scope = _review_target(username, workspace_ids, replacing)
    secret = _passphrase(passphrase)
    del passphrase
    with _locked(path) as directory:
        review = _review_locked(path, directory, secret, name, scope, replacing)
        del secret
        owner, pid = current_thread(), os.getpid()
        attempted = False

        def held_here(candidate):
            return (
                held is not None
                and candidate is held
                and current_thread() is owner
                and os.getpid() == pid
                and os.environ.get("OC_BOX_CONTAINER") != "1"
            )

        def save(candidate, passphrase, expected, registration, current):
            nonlocal attempted
            if not held_here(candidate) or expected is not review or attempted:
                raise WebAccountsError(_STALE_REVIEW)
            attempted = True
            if not callable(current):
                raise WebAccountsError(_STALE_REVIEW)

            def checked():
                if not held_here(candidate) or current() is not True:
                    return False
                if not held_here(candidate):
                    return False
                _check_directory(path, directory)
                return _stamp(_entry(directory, path.name)) == review._file_stamp

            user, ids, secret, record = _registration_inputs(
                path, passphrase, review, registration, current
            )
            try:
                # Refuse directory substitution before reading or deriving
                # the store key again after the caller's review interval.
                _check_directory(path, directory)
                _commit_registration_locked(
                    path, directory, review, user, ids, secret, record, checked
                )
            except (OSError, UnicodeError):
                # The caller may catch this while still holding the context.
                raise WebAccountsError(_UNSAFE) from None

        held = _HeldRegistration(review, save)
        try:
            yield held
        finally:
            held = None


def add_account(
    path: Path,
    passphrase: str,
    username: str,
    *,
    workspace_ids: tuple[str, ...] | list[str],
    registration: bytes,
) -> None:
    """Persist a pre-vetted protocol record; refuse existing names and scopes with wildcards."""
    secret = _passphrase(passphrase)
    account = Account(
        _username(username),
        _scope(workspace_ids),
        _protocol_bytes(registration, MAX_REGISTRATION_BYTES),
    )
    with _locked(path) as directory:
        store, previous = _read(path, directory, secret)
        if any(existing.username == account.username for existing in store.accounts):
            raise WebAccountsError(
                "Web account already exists; no record was replaced."
            )
        if len(store.accounts) >= MAX_ACCOUNTS:
            raise WebAccountsError("Web account store has reached its account limit.")
        updated = AccountStore(
            store.setup,
            tuple(sorted((*store.accounts, account), key=lambda entry: entry.username)),
        )
        _publish(path, directory, _seal(updated, secret), previous)


def remove_account(path: Path, passphrase: str, username: str) -> None:
    """Remove one stored record, without claiming erasure or live revocation."""
    secret = _passphrase(passphrase)
    name = _username(username)
    with _locked(path) as directory:
        store, previous = _read(path, directory, secret)
        remaining = tuple(
            account for account in store.accounts if account.username != name
        )
        if len(remaining) == len(store.accounts):
            raise WebAccountsError("Web account is not present; no record was removed.")
        _publish(
            path,
            directory,
            _seal(AccountStore(store.setup, remaining), secret),
            previous,
        )
