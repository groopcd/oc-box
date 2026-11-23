"""Read-only host validation of operator-supplied web TLS files."""

import base64
import os
import re
import shutil
import ssl
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

if __package__:
    from . import private_memory
else:
    import private_memory

_CERT_LIMIT = 96 * 1024
_KEY_LIMIT = 32 * 1024
_CERT_COUNT = 8
_HELPER_PATH = "/usr/local/bin:/usr/bin:/bin"
_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_ORIGIN = re.compile(
    r"https://([a-z0-9.-]{1,253})(?::([1-9][0-9]{0,4}))?", re.IGNORECASE | re.ASCII
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)")
_CERT_PEM = re.compile(
    rb"-----BEGIN CERTIFICATE-----\r?\n([A-Za-z0-9+/=\r\n]+)"
    rb"\r?\n-----END CERTIFICATE-----"
)
_PEM_SPACE = b" \t\r\n"
_MESSAGES = {
    "origin": "Use an HTTPS origin with localhost, a concrete IPv4 address, or an ASCII DNS name.",
    "certificate_missing": "The TLS certificate file or its directory is missing.",
    "certificate_file": "Use a readable PEM certificate file owned by this user or root, without links or shared write access.",
    "certificate_invalid": "Use a valid PEM certificate chain with the server certificate first.",
    "key_missing": "The TLS private key file or its directory is missing.",
    "key_file": "Use a readable private key owned by this user, mode 0600 or 0400, without links.",
    "key_encrypted": "Encrypted TLS private keys are not supported by this check.",
    "key_mismatch": "The TLS private key does not match the server certificate.",
    "key_load": "The TLS certificate and private key could not be loaded; check the PEM files.",
    "certificate_name": "The TLS certificate's subject alternative names do not cover the HTTPS origin.",
    "certificate_time": "A TLS certificate in the verified path is expired or not yet valid.",
    "certificate_chain": "The TLS certificate could not be verified against the supplied chain for server use.",
    "unavailable": "Safe local TLS validation is unavailable on this system.",
}


class WebTLSError(ValueError):
    """A fixed user-facing TLS diagnostic without paths or raw SSL errors."""

    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class HTTPSOrigin:
    hostname: str
    port: int

    @property
    def value(self) -> str:
        port_suffix = "" if self.port == 443 else f":{self.port}"
        return f"https://{self.hostname}{port_suffix}"


def parse_https_origin(origin: str) -> HTTPSOrigin:
    """Parse an advertised origin, not a bind address or full request URL."""
    match = (
        _ORIGIN.fullmatch(origin)
        if isinstance(origin, str) and len(origin) <= 267
        else None
    )
    if match is None:
        raise WebTLSError("origin")
    hostname = match[1].lower()
    port = int(match[2]) if match[2] is not None else 443
    if port > 65535:
        raise WebTLSError("origin")
    try:
        address = IPv4Address(hostname)
    except ValueError:
        labels = hostname.split(".")
        # Browsers treat numeric-ending domains as potential IPv4 literals.
        # Do not let abbreviated, hexadecimal or octal forms masquerade as DNS.
        if _NUMERIC_LABEL.fullmatch(labels[-1]) or not all(
            map(_DNS_LABEL.fullmatch, labels)
        ):
            raise WebTLSError("origin") from None
    else:
        if address.is_unspecified:
            raise WebTLSError("origin")
    return HTTPSOrigin(hostname, port)


def find_helper(name: str) -> str | None:
    """Check the system path only; a discovered path is not permission to execute."""
    if name not in ("mkcert", "certutil"):
        return None
    return shutil.which(name, path=_HELPER_PATH)


def _pin_file(path, stack):
    raw = os.fspath(path)
    if (
        not isinstance(raw, str)
        or len(os.fsencode(raw)) > 4096
        or raw.rsplit("/", 1)[-1] in ("", ".", "..")
    ):
        raise ValueError
    parent = Path(raw)
    parts = parent.parts[1:] if parent.is_absolute() else parent.parts
    if not 1 <= len(parts) <= 64 or ".." in parts:
        raise ValueError
    directory = os.open(parent.anchor or ".", _DIRECTORY_FLAGS)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        # Classify before opening for actual I/O (no FIFO/device side effects).
        pinned = os.open(
            parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
        )
        stack.callback(os.close, pinned)
        return pinned
    finally:
        os.close(directory)


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _snapshot(path, *, private):
    kind = "key" if private else "certificate"
    with ExitStack() as stack:
        try:
            pinned = _pin_file(path, stack)
            info = os.fstat(pinned)
        except FileNotFoundError:
            raise WebTLSError(f"{kind}_missing") from None
        except (OSError, TypeError, ValueError):
            raise WebTLSError(f"{kind}_file") from None
        mode = stat.S_IMODE(info.st_mode)
        trusted_mode = mode in (0o400, 0o600) if private else not mode & 0o7022
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= (_KEY_LIMIT if private else _CERT_LIMIT)
            or info.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
            or not trusted_mode
        ):
            raise WebTLSError(f"{kind}_file")
        stamp = _stamp(info)
        try:
            source = os.open(
                f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
            )
            stack.callback(os.close, source)
            if _stamp(os.fstat(source)) != stamp:
                raise WebTLSError(f"{kind}_file")
        except OSError:
            raise WebTLSError(f"{kind}_file") from None
        try:
            # load_cert_chain takes paths. Use sealed, private anonymous files so
            # OpenSSL consumes exactly the checked bytes, never the source path.
            # sendfile keeps the key out of Python strings and on-disk staging.
            snapshot = os.memfd_create(
                "oc-box-web-tls", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            stack.callback(os.close, snapshot)
            os.fchmod(snapshot, 0o600)
        except (AttributeError, OSError):
            raise WebTLSError("unavailable") from None
        try:
            offset = 0
            while offset < info.st_size:
                copied = os.sendfile(snapshot, source, offset, info.st_size - offset)
                if not copied:
                    raise WebTLSError(f"{kind}_file")
                offset += copied
            if _stamp(os.fstat(source)) != stamp:
                raise WebTLSError(f"{kind}_file")
        except OSError:
            raise WebTLSError(f"{kind}_file") from None
        try:
            private_memory.seal(snapshot)
        except (AttributeError, OSError):
            raise WebTLSError("unavailable") from None
        yield snapshot


def _parse_chain(fd):
    try:
        remainder = os.pread(fd, _CERT_LIMIT, 0).strip(_PEM_SPACE)
    except OSError:
        raise WebTLSError("unavailable") from None
    certificates = []
    seen = set()
    while remainder:
        match = _CERT_PEM.match(remainder)
        if not match or len(certificates) >= _CERT_COUNT:
            raise WebTLSError("certificate_invalid")
        try:
            der = base64.b64decode(b"".join(match[1].splitlines()), validate=True)
        except ValueError:
            raise WebTLSError("certificate_invalid") from None
        if der in seen:
            raise WebTLSError("certificate_invalid")
        seen.add(der)
        certificates.append(ssl.DER_cert_to_PEM_cert(der))
        remainder = remainder[match.end() :].lstrip(_PEM_SPACE)
    if not certificates:
        raise WebTLSError("certificate_invalid")
    return certificates


def _reject_encrypted_key():
    # Never let OpenSSL prompt on a host TTY or try an empty password.
    raise WebTLSError("key_encrypted")


def _verify_handshake(server, verifier, hostname):
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_peer = verifier.wrap_bio(client_in, client_out, server_hostname=hostname)
    server_peer = server.wrap_bio(server_in, server_out, server_side=True)
    pending = [
        (client_peer, client_out, server_in),
        (server_peer, server_out, client_in),
    ]
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
            return
    raise WebTLSError("certificate_chain")


def validate(
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    origin: str,
) -> ssl.SSLContext:
    """Load PEM files and check local server usability before opening any socket.

    The final supplied certificate is the temporary verification anchor. A
    valid leaf alone (including self-signed) or a rootless chain can pass.
    This checks the built path's time, SAN and server purpose, not whether a
    browser trusts the issuer, or whether all intermediates a browser needs
    were supplied. It does not read or alter host trust stores.
    """
    hostname = parse_https_origin(origin).hostname
    if not ssl.HAS_NEVER_CHECK_COMMON_NAME:
        raise WebTLSError("unavailable")
    with _snapshot(cert_path, private=False) as cert:
        certificates = _parse_chain(cert)
        try:
            # Parse all supplied PEMs, without adding intermediates to the
            # verification store (which could mask an unusable served path).
            parser = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            for pem in certificates:
                parser.load_verify_locations(cadata=pem)
            verifier = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            verifier.load_verify_locations(cadata=certificates[-1])
        except (OSError, ValueError):
            raise WebTLSError("certificate_invalid") from None
        verifier.hostname_checks_common_name = False
        verifier.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        with _snapshot(key_path, private=True) as key:
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                server.load_cert_chain(
                    f"/proc/self/fd/{cert}",
                    f"/proc/self/fd/{key}",
                    password=_reject_encrypted_key,
                )
            except OSError as exc:
                code = (
                    "key_mismatch"
                    if getattr(exc, "reason", None) == "KEY_VALUES_MISMATCH"
                    else "key_load"
                )
                raise WebTLSError(code) from None
    try:
        _verify_handshake(server, verifier, hostname)
    except ssl.SSLCertVerificationError as exc:
        if exc.verify_code in (9, 10):  # X509 not yet valid / expired
            code = "certificate_time"
        elif exc.verify_code in (62, 64):  # X509 DNS / IP SAN mismatch
            code = "certificate_name"
        else:
            code = "certificate_chain"
        raise WebTLSError(code) from None
    except ssl.SSLError:
        raise WebTLSError("certificate_chain") from None
    return server
