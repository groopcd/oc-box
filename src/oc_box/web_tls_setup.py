"""Explicit host-only loopback TLS setup; never a browser trust check."""

import ctypes
import fcntl
import hashlib
import os
import pwd
import re
import ssl
import stat
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable

if __package__:
    from .web_tls import _snapshot, find_helper, validate
else:
    from web_tls import _snapshot, find_helper, validate

CERT_NAME = "server.pem"
KEY_NAME = "server-key.pem"
CA_DIR_NAME = "ca"
_CA_CERT = "rootCA.pem"
_CA_KEY = "rootCA-key.pem"
_RECORD = ".generated-v1"
_MATERIAL = (CERT_NAME, KEY_NAME, CA_DIR_NAME)
_CA_FILES = frozenset((_CA_CERT, _CA_KEY))
_NAMES = ("localhost", "127.0.0.1")
_PATH = "/usr/local/bin:/usr/bin:/bin"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_NSS_FILES = frozenset(("cert9.db", "key4.db", "pkcs11.txt"))
_NSS_OPTIONAL = frozenset(
    name + suffix
    for name in ("cert9.db", "key4.db")
    for suffix in ("-journal", "-wal", "-shm")
)
_NSS_INTERNAL = re.compile(
    rb"NSS=Flags=internal,critical trustOrder=75 cipherOrder=100 "
    rb"slotParams=\(1=\{slotFlags=\[[A-Za-z0-9_,]+\] askpw=any timeout=30\}\)"
)
_MESSAGES = {
    "host_only": "Local web TLS setup must run as the normal host user.",
    "unsafe_state": "Use the private local web TLS directory without links or unsafe permissions.",
    "existing_state": "The local TLS directory has unrecognized or incomplete state. No existing files were replaced; use the supplied-certificate options for other certificates.",
    "busy": "Another local web TLS setup is using this directory.",
    "mkcert_missing": "The optional mkcert host tool is unavailable in the system path.",
    "mkcert_unsafe": "The mkcert host tool is not a trusted system executable.",
    "certutil_missing": "The optional NSS certutil host tool is unavailable in the system path.",
    "certutil_unsafe": "The NSS certutil host tool is not a trusted system executable.",
    "generation_failed": "mkcert did not complete local TLS certificate generation.",
    "invalid_bundle": "The managed local TLS material failed validation. No existing certificate or key was replaced.",
    "approval_required": "NSS trust changes require explicit typed confirmation on the host terminal.",
    "trust_refused": "NSS trust approval was not given. Nothing was installed.",
    "nss_database": "A supported, private Chromium-style NSS database was not found or could not be safely validated.",
    "trust_failed": "The NSS trust change did not complete successfully and may be partial. Inspect the intended browser settings before retrying.",
}


class WebTLSSetupError(ValueError):
    """A fixed diagnostic that never exposes paths or helper output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


def _absolute_directory(directory):
    raw = os.fspath(directory)
    path = Path(raw)
    if (
        path.anchor != "/"
        or not 2 <= len(path.parts) <= 64
        or ".." in path.parts
        or "\0" in raw
        or len(os.fsencode(raw)) > 4096
    ):
        raise WebTLSSetupError("unsafe_state")
    return path


def _directory_stat(info, *, root_only=False, private=False):
    uid = os.getuid()
    mode = stat.S_IMODE(info.st_mode)
    trusted_owner = info.st_uid == 0 if root_only else info.st_uid in (0, uid)
    # A root-owned sticky directory (e.g. /tmp) cannot let another user
    # replace the checked root-/user-owned child. All other parents must
    # exclude shared writers.
    sticky = not root_only and info.st_uid == 0 and mode & stat.S_ISVTX
    if (
        not stat.S_ISDIR(info.st_mode)
        or not trusted_owner
        or (mode & 0o022 and not sticky)
        or (private and (info.st_uid != uid or mode != 0o700))
    ):
        raise WebTLSSetupError("unsafe_state")
    return info


@contextmanager
def _open_directory(path, *, create=False, root_only=False):
    current = os.open("/", _DIRECTORY_FLAGS)
    try:
        _directory_stat(os.fstat(current), root_only=root_only)
        for index, part in enumerate(path.parts[1:], 2):
            # A competing entry at a previously missing setup root is not
            # authority to use or mutate it in the already-approved attempt.
            if create and index == len(path.parts):
                os.mkdir(part, 0o700, dir_fd=current)
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create or index == len(path.parts):
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
            _directory_stat(os.fstat(current), root_only=root_only)
        yield current
    finally:
        os.close(current)


def _names(fd, allowed, *, code):
    names = set()
    # Use a fresh open description: an inherited cursor can predate creation
    # of the entries, notably on overlayfs.
    cursor = os.open(".", _DIRECTORY_FLAGS, dir_fd=fd)
    try:
        with os.scandir(cursor) as entries:
            for entry in entries:
                if entry.name not in allowed:
                    raise WebTLSSetupError(code)
                names.add(entry.name)
    finally:
        os.close(cursor)
    return names


def _owner_regular(info, *, limit=None):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) in (0o400, 0o600)
        and (limit is None or 0 < info.st_size <= limit)
    )


def _read_file(directory, name, limit, *, code="existing_state"):
    with ExitStack() as stack:
        pinned = os.open(name, _FILE_FLAGS, dir_fd=directory)
        stack.callback(os.close, pinned)
        info = os.fstat(pinned)
        if not _owner_regular(info, limit=limit):
            raise WebTLSSetupError(code)
        reader = os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
        stack.callback(os.close, reader)
        data = os.pread(reader, limit + 1, 0)
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            not os.path.samestat(info, after)
            or info.st_ctime_ns != after.st_ctime_ns
            or len(data) != info.st_size
        ):
            raise WebTLSSetupError(code)
        return data


def _helper(name):
    found = find_helper(name)
    if found is None:
        raise WebTLSSetupError(f"{name}_missing")
    try:
        allowed = tuple(Path(part) for part in _PATH.split(":"))
        raw = Path(found)
        if raw.name != name or raw.parent not in allowed:
            raise ValueError
        # /bin can be the merged-/usr alias. Never resolve a discovered
        # executable itself, or bypass safety checks on its original parent.
        parent = raw.parent
        parent = parent.resolve(strict=True) if parent == Path("/bin") else parent
        if parent not in allowed:
            raise ValueError
        with _open_directory(parent, root_only=True) as directory:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                info.st_uid != 0
                or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o7022
                or not info.st_mode & stat.S_IXOTH
            ):
                raise ValueError
        return str(parent / name)
    except (OSError, ValueError, RuntimeError):
        raise WebTLSSetupError(f"{name}_unsafe") from None


def _run(
    command, directory, *, code, extra_env=None, pass_fds=(), stdin=subprocess.DEVNULL
):
    isolated = f"/proc/self/fd/{directory}"
    env = {"PATH": _PATH, "LC_ALL": "C", "HOME": isolated}
    env.update(extra_env or {})
    try:
        subprocess.run(
            command,
            cwd=isolated,
            env=env,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            umask=0o077,
            pass_fds=(directory, *pass_fds),
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise WebTLSSetupError(code) from None


def _no_password():
    raise ValueError


def _verify_bundle(path, root):
    """Validate locally with only the generated root and exact loopback SANs."""
    try:
        verifier = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        verifier.hostname_checks_common_name = False
        verifier.load_verify_locations(cadata=root.decode("ascii"))
        # mkcert emits exactly one canonical public certificate.
        [ca] = verifier.get_ca_certs(binary_form=True)
        if ssl.DER_cert_to_PEM_cert(ca).encode("ascii") != root:
            raise ValueError
        ca = path / CA_DIR_NAME
        with (
            _snapshot(ca / _CA_CERT, private=False) as cert,
            _snapshot(ca / _CA_KEY, private=True) as key,
        ):
            ca_pair = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ca_pair.load_cert_chain(
                f"/proc/self/fd/{cert}", f"/proc/self/fd/{key}", password=_no_password
            )
        server = validate(path / CERT_NAME, path / KEY_NAME, "https://localhost")
        client_in, client_out, server_in, server_out = (
            ssl.MemoryBIO() for _ in range(4)
        )
        client = verifier.wrap_bio(client_in, client_out, server_hostname=_NAMES[0])
        server = server.wrap_bio(server_in, server_out, server_side=True)
        pending = [(client, client_out, server_in), (server, server_out, client_in)]
        for _ in range(16):
            for peer, outgoing, incoming in pending[:]:
                try:
                    peer.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                else:
                    pending.remove((peer, outgoing, incoming))
                if outgoing.pending:
                    incoming.write(outgoing.read())
            if not pending:
                if sorted(client.getpeercert().get("subjectAltName", ())) == [
                    ("DNS", _NAMES[0]),
                    ("IP Address", _NAMES[1]),
                ]:
                    return
                break
    except (OSError, ValueError):
        pass
    raise WebTLSSetupError("invalid_bundle")


def _material(directory):
    ca = os.open(CA_DIR_NAME, _DIRECTORY_FLAGS, dir_fd=directory)
    try:
        _directory_stat(os.fstat(ca), private=True)
        if _names(ca, _CA_FILES, code="existing_state") != _CA_FILES:
            raise WebTLSSetupError("existing_state")
        root = _read_file(ca, _CA_CERT, 96 * 1024)
        record = b"".join(
            hashlib.sha256(data).hexdigest().encode("ascii") + b"\n"
            for data in (
                _read_file(directory, CERT_NAME, 96 * 1024),
                _read_file(directory, KEY_NAME, 32 * 1024),
                root,
                _read_file(ca, _CA_KEY, 32 * 1024),
            )
        )
        return root, record
    finally:
        os.close(ca)


def _existing(path, directory):
    _directory_stat(os.fstat(directory), private=True)
    names = _names(directory, {*_MATERIAL, _RECORD}, code="existing_state")
    if not names:
        return None
    if names != {*_MATERIAL, _RECORD}:
        raise WebTLSSetupError("existing_state")
    root, record = _material(directory)
    if _read_file(directory, _RECORD, 4 * 65) != record:
        raise WebTLSSetupError("existing_state")
    _verify_bundle(path, root)
    if _material(directory) != (root, record):
        raise WebTLSSetupError("existing_state")
    return root


def _publish(source, destination):
    # Publish only newly generated files, never replace an existing path even
    # if it appeared after preflight. The provenance record is committed last.
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        raise WebTLSSetupError("generation_failed") from None
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    for name in (*_MATERIAL, _RECORD):
        encoded = os.fsencode(name)
        if rename(source, encoded, destination, encoded, 1):  # RENAME_NOREPLACE
            raise WebTLSSetupError("generation_failed")


def _generate(path, directory, mkcert):
    with tempfile.TemporaryDirectory(
        prefix=".setup-", dir=f"/proc/self/fd/{directory}"
    ) as tmp:
        stage_path = path / Path(tmp).name
        with _open_directory(stage_path) as stage:
            _directory_stat(os.fstat(stage), private=True)
            if not os.path.samestat(
                os.fstat(stage), os.stat(tmp, follow_symlinks=False)
            ):
                raise WebTLSSetupError("unsafe_state")
            os.mkdir(CA_DIR_NAME, 0o700, dir_fd=stage)
            _run(
                [mkcert, "-cert-file", CERT_NAME, "-key-file", KEY_NAME, *_NAMES],
                stage,
                code="generation_failed",
                extra_env={
                    "CAROOT": f"/proc/self/fd/{stage}/{CA_DIR_NAME}",
                    "TRUST_STORES": "none",
                },
            )
            if _names(stage, _MATERIAL, code="invalid_bundle") != set(_MATERIAL):
                raise WebTLSSetupError("invalid_bundle")
            root, record = _material(stage)
            _verify_bundle(stage_path, root)
            if _material(stage) != (root, record):
                raise WebTLSSetupError("invalid_bundle")
            fd = os.open(
                _RECORD,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=stage,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(record)
            _publish(stage, directory)
    return root


def _nss_modules(path, directory):
    # NSS loads modules from pkcs11.txt even for certutil -A. Refuse external
    # libraries or an internal config that can redirect the database elsewhere.
    data = _read_file(directory, "pkcs11.txt", 8192, code="nss_database")
    target = os.fsencode(path)
    if any(value in target for value in (b"'", b"\\", b"\n", b"\r")):
        raise WebTLSSetupError("nss_database")
    params = {
        b"parameters=configdir='"
        + prefix
        + target
        + b"' certPrefix='' keyPrefix='' secmod='secmod.db' flags= updatedir='' "
        b"updateCertPrefix='' updateKeyPrefix='' updateid='' updateTokenDescription=''"
        for prefix in (b"", b"sql:")
    }
    lines = data.strip().splitlines()
    if (
        len(lines) != 4
        or lines[:2] != [b"library=", b"name=NSS Internal PKCS #11 Module"]
        or lines[2].rstrip(b" ") not in params
        or not _NSS_INTERNAL.fullmatch(lines[3])
    ):
        raise WebTLSSetupError("nss_database")


def _nss_sql_database(directory, name):
    pinned = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        info = os.fstat(pinned)
        if not _owner_regular(info) or info.st_size < 100:
            raise WebTLSSetupError("nss_database")
        reader = os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            if os.pread(reader, 16, 0) != b"SQLite format 3\0":
                raise WebTLSSetupError("nss_database")
        finally:
            os.close(reader)
        return info.st_dev, info.st_ino
    finally:
        os.close(pinned)


def _nss_database(stack):
    try:
        home = _absolute_directory(pwd.getpwuid(os.getuid()).pw_dir)
        with _open_directory(home) as user_home:
            if os.fstat(user_home).st_uid != os.getuid():
                raise WebTLSSetupError("nss_database")
        for path in (home / ".pki/nssdb", home / ".local/share/pki/nssdb"):
            try:
                directory = stack.enter_context(_open_directory(path))
            except FileNotFoundError:
                continue
            _directory_stat(os.fstat(directory), private=True)
            names = _names(directory, _NSS_FILES | _NSS_OPTIONAL, code="nss_database")
            if not _NSS_FILES <= names or not all(
                _owner_regular(os.stat(name, dir_fd=directory, follow_symlinks=False))
                for name in names
            ):
                raise WebTLSSetupError("nss_database")
            _nss_modules(path, directory)
            identity = tuple(
                _nss_sql_database(directory, name) for name in ("cert9.db", "key4.db")
            )
            return path, directory, identity
    except (OSError, ValueError, KeyError):
        pass
    raise WebTLSSetupError("nss_database")


def _lock(directory):
    _directory_stat(os.fstat(directory), private=True)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise WebTLSSetupError("busy") from None


def _require_host(install_trust):
    if (
        os.environ.get("OC_BOX_CONTAINER") == "1"
        or os.getuid() != os.geteuid()
        or (install_trust and os.getuid() == 0)
    ):
        raise WebTLSSetupError("host_only")


def _install_nss(certutil, directory, nss, root):
    # mkcert -install can touch system NSS and retry through sudo even with
    # TRUST_STORES=nss. Import only the verified public CA into this account's
    # one checked database, without delegation or the CA private key.
    with tempfile.TemporaryFile(dir=f"/proc/self/fd/{directory}") as public:
        public.write(root)
        public.seek(0)
        _run(
            [
                certutil,
                "-A",
                "-a",
                "-d",
                f"sql:/proc/self/fd/{nss}",
                "-h",
                "internal",
                "-t",
                "C,,",
                "-n",
                "oc-box local web TLS " + hashlib.sha256(root).hexdigest(),
            ],
            directory,
            code="trust_failed",
            pass_fds=(nss,),
            stdin=public,
        )


def setup(
    directory: str | os.PathLike[str],
    *,
    install_trust: bool = False,
    approve: Callable[[], bool] | None = None,
) -> tuple[Path, Path]:
    """Return fixed certificate and key paths; never infer any browser's trust.

    For an explicit NSS trust change, the caller must obtain a typed approval
    on the host controlling TTY. The callback is never used for generation only.
    """
    _require_host(install_trust)
    if install_trust and approve is None:
        raise WebTLSSetupError("approval_required")
    try:
        path = _absolute_directory(directory)
        with ExitStack() as stack:
            try:
                bound = stack.enter_context(_open_directory(path))
            except FileNotFoundError:
                bound = None
            root = None
            if bound is not None:
                _lock(bound)
                root = _existing(path, bound)
            mkcert = _helper("mkcert") if root is None else None
            approved_nss = None
            if install_trust:
                certutil = _helper("certutil")
                approved_nss = _nss_database(stack)
                try:
                    approved = approve()
                except (OSError, UnicodeError):
                    approved = False
                if approved is not True:
                    raise WebTLSSetupError("trust_refused")
            if bound is None:
                bound = stack.enter_context(_open_directory(path, create=True))
                _lock(bound)
                if _existing(path, bound) is not None:
                    raise WebTLSSetupError("existing_state")
            if root is None:
                root = _generate(path, bound, mkcert)
            # Do not return paths or grant trust against a replaced destination.
            current = stack.enter_context(_open_directory(path))
            if not os.path.samestat(os.fstat(current), os.fstat(bound)):
                raise WebTLSSetupError("unsafe_state")
            if _existing(path, bound) != root:
                raise WebTLSSetupError("existing_state")
            if approved_nss is not None:
                nss_path, nss, nss_identity = approved_nss
                current_path, current_nss, current_identity = _nss_database(stack)
                if (
                    current_path != nss_path
                    or current_identity != nss_identity
                    or not os.path.samestat(os.fstat(nss), os.fstat(current_nss))
                ):
                    raise WebTLSSetupError("nss_database")
                _install_nss(certutil, bound, current_nss, root)
            return path / CERT_NAME, path / KEY_NAME
    except WebTLSSetupError:
        raise
    except (OSError, ValueError, TypeError):
        raise WebTLSSetupError("unsafe_state") from None
