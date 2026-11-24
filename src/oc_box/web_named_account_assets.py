"""Unserved account document and fixed assets for the local OCOP profile.

Only a trusted host caller supplies the reviewed pins, local paths and origins.
This module does not bind, start a verifier, unlock accounts, install assets or
approve artifacts for distribution. A transport owns TLS role/SNI, request and
response bounds, owner rechecks before writing, and the admission lifetime.
"""

import hashlib
import os
import re
import stat
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hmac import compare_digest
from html import escape
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

if __package__:
    from . import (
        private_memory,
        web_account_ingress,
        web_bootstrap,
        web_local_origins,
        web_named_bootstrap,
        web_named_handoff,
        web_named_headers,
        web_pake_worker,
        web_tls,
    )
else:
    import private_memory
    import web_account_ingress
    import web_bootstrap
    import web_local_origins
    import web_named_bootstrap
    import web_named_handoff
    import web_named_headers
    import web_pake_worker
    import web_tls


_SOURCE_ROOT = Path(__file__).absolute().parent
_MAX_WASM = 1024 * 1024
_ACCOUNT_COOKIE = "__Host-ocbox-account"
_COOKIE = re.compile(
    r"__Host-ocbox-account=[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]; Secure; HttpOnly; SameSite=Strict; Path=/"
)
_ASSET_PREFIX = "/account-assets/"
_WASM_ROUTE = _ASSET_PREFIX + "client.wasm"
_JS = "text/javascript; charset=utf-8"
_CSS = "text/css; charset=utf-8"
# Source assets are versioned together. The WASM and verifier deliberately
# have no default pins: a locally approved pair is required even in a checkout.
_DOCUMENT = (
    "web_named_login.html",
    2963,
    "493352962220d6d14b7da53222d64e36f1d7cb9e2345b11615f01f2f69c76d88",
)
_SOURCES = (
    (
        "sign-in.css",
        _CSS,
        "no-cors",
        "style",
        "web_named_login.css",
        3871,
        "19e2758947ad80a6b3335b294bd97fa123b762d6c0d9b77c0b9ca462757fb69e",
    ),
    (
        "sign-in.mjs",
        _JS,
        "cors",
        "script",
        "web_named_login.mjs",
        7062,
        "fbfe848b69abe3c58acdf734cbfd61181f6050f50f4f3f1b8dcc57c96186488a",
    ),
    (
        "transport.mjs",
        _JS,
        "cors",
        "script",
        "web_pake_client.mjs",
        12182,
        "be51703c623afa49427e5810a38a56ae4242c40117b404b3d62361b18b4e4b13",
    ),
    (
        "slot.mjs",
        _JS,
        "cors",
        "script",
        "web_named_login_slot.mjs",
        6787,
        "e8272c8e4e3bc675afa5325cefef3493864681d44dcd6144959e2811a3cb0c34",
    ),
    (
        "transfer.mjs",
        _JS,
        "cors",
        "script",
        "web_named_handoff_client.mjs",
        1905,
        "89503ae3b2aa996f9ecc580bdd32338f304423b42b1a3e6dc0997b47baea5c6d",
    ),
)
ACCOUNT_ASSET_PATHS = tuple(_ASSET_PREFIX + item[0] for item in _SOURCES) + (
    _WASM_ROUTE,
)
_PRIVATE = (
    ("Cache-Control", "no-store"),
    (
        "Referrer-Policy",
        "origin",
    ),  # Chromium otherwise may serialize a null Origin for a form.
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
)
_ASSET_CSP = (
    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_FRAMING = frozenset(
    (
        "transfer-encoding",
        "content-encoding",
        "te",
        "expect",
        "trailer",
        "upgrade",
        "range",
        "if-range",
        "if-match",
        "if-none-match",
        "if-modified-since",
        "if-unmodified-since",
    )
)


class AccountAssetError(ValueError):
    """Fixed refusal without paths, local file contents, or credentials."""

    def __init__(self):
        super().__init__("Named browser assets could not be verified.")


@dataclass(frozen=True, slots=True)
class ReviewedArtifactPin:
    """Exact byte count and raw SHA-256 from the trusted artifact review."""

    size: int
    sha256: bytes

    def __post_init__(self):
        if (
            type(self.size) is not int
            or not 4 < self.size <= web_pake_worker._MAX_EXECUTABLE
            or type(self.sha256) is not bytes
            or len(self.sha256) != 32
        ):
            raise AccountAssetError()


@dataclass(frozen=True, slots=True, repr=False)
class AccountAssetReply:
    body: bytes
    headers: tuple[tuple[str, str], ...]
    status: int = 200


@dataclass(frozen=True, slots=True, repr=False)
class _Asset:
    body: bytes
    content_type: str
    mode: str
    destination: str


@dataclass(frozen=True, slots=True, repr=False)
class AccountAssetBundle:
    """Read-only response projection; not a router or an owner liveness check."""

    origins: web_local_origins.LocalOrigins
    _page: bytes
    _policy: str
    _assets: Mapping[str, _Asset]

    def document(self, entry):
        """Project the exact result of a successful account bootstrap POST."""
        if (
            type(entry) is not web_named_bootstrap.AccountEntry
            or type(entry.cookie) is not str
            or _COOKIE.fullmatch(entry.cookie) is None
        ):
            raise web_named_headers.NamedRequestError()
        return AccountAssetReply(
            self._page,
            _PRIVATE
            + (
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", self._policy),
                ("Set-Cookie", entry.cookie),
            ),
        )

    def asset(self, *, method, target, headers, actual_tls, body, translate_nonce):
        """Serve one exact same-origin subresource for a live paired bootstrap.

        ``translate_nonce`` must recheck the same named owner and current
        bootstrap. The transport must already have selected the account role,
        bounded the full raw request and must recheck liveness before sending.
        """
        if (
            type(method) is not str
            or method != "GET"
            or type(target) is not str
            or target not in self._assets
            or type(body) is not bytes
            or body
            or not callable(translate_nonce)
        ):
            raise web_named_headers.NamedRequestError()
        item = self._assets[target]
        origin = self.origins.account
        nonce = web_named_headers.admit_headers(
            origin=origin,
            headers=headers,
            method=method,
            actual_tls=actual_tls,
            cookie_name=_ACCOUNT_COOKIE,
        )
        fields = web_named_headers._fields(
            headers, origin.value.removeprefix("https://")
        )
        if (
            fields.keys() & _FRAMING
            or fields.get("content-length", "0") != "0"
            or fields.get("connection", "close").lower() not in ("close", "keep-alive")
            or fields.get("sec-fetch-site") != "same-origin"
            or fields.get("sec-fetch-mode") != item.mode
            or fields.get("sec-fetch-dest") != item.destination
        ):
            raise web_named_headers.NamedRequestError()
        try:
            peer = translate_nonce(nonce)
        except Exception:  # noqa: BLE001 - opaque bootstrap and owner diagnostics never escape.
            raise web_named_headers.NamedRequestError() from None
        if not web_bootstrap._valid(peer) or compare_digest(peer, nonce):
            raise web_named_headers.NamedRequestError()
        return AccountAssetReply(
            item.body,
            _PRIVATE
            + (
                ("Content-Type", item.content_type),
                ("Content-Security-Policy", _ASSET_CSP),
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAccountAssets:
    bundle: AccountAssetBundle
    verifier_fd: int  # Borrowed only inside prepare_account_assets().
    verifier_sha256: bytes


def _read_pinned(path, pin):
    if not isinstance(path, Path) or path.anchor != "/":
        raise AccountAssetError()
    with ExitStack() as cleanup:
        # Pin every path component without following user symlinks. Classify
        # the leaf before any real I/O (including pipes and device nodes).
        pinned = web_tls._pin_file(path, cleanup)
        info = os.fstat(pinned)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o7022
            or info.st_size != pin.size
        ):
            raise AccountAssetError()
        stamp = web_tls._stamp(info)
        fd = os.open(
            f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        )
        cleanup.callback(os.close, fd)
        if web_tls._stamp(os.fstat(fd)) != stamp:
            raise AccountAssetError()
        data = bytearray()
        while len(data) < pin.size:
            chunk = os.pread(fd, min(65536, pin.size - len(data)), len(data))
            if not chunk:
                raise AccountAssetError()
            data.extend(chunk)
        if os.pread(fd, 1, pin.size) or web_tls._stamp(os.fstat(fd)) != stamp:
            raise AccountAssetError()
    result = bytes(data)
    if not compare_digest(hashlib.sha256(result).digest(), pin.sha256):
        raise AccountAssetError()
    return result


def _source(filename, size, digest):
    return _read_pinned(
        _SOURCE_ROOT / filename, ReviewedArtifactPin(size, bytes.fromhex(digest))
    )


def _bundle(origins, label, wasm, pin):
    if (
        type(label) is not str
        or not 0 < len(label) <= 80
        or not label.strip()
        or any(unicodedata.category(char).startswith("C") for char in label)
    ):
        raise AccountAssetError()
    assets = {
        _ASSET_PREFIX + name: _Asset(
            _source(filename, size, digest), mime, mode, destination
        )
        for name, mime, mode, destination, filename, size, digest in _SOURCES
    }
    assets[_WASM_ROUTE] = _Asset(wasm, "application/wasm", "same-origin", "empty")
    auth, workspace = origins.account.value, origins.workspace.value
    interpolations = {
        "__ACCOUNT_ORIGIN__": auth,
        "__WORKSPACE_ORIGIN__": workspace,
        "__WORKSPACE_LABEL__": label,
        "__WASM_SIZE__": str(pin.size),
        "__WASM_SHA256__": pin.sha256.hex(),
    }
    template = _source(*_DOCUMENT).decode("utf-8")
    page = re.sub(
        r"__[A-Z0-9_]+__",
        lambda match: escape(interpolations[match[0]], quote=True),
        template,
    ).encode("utf-8")
    script = " ".join(
        auth + path for path, item in assets.items() if item.content_type == _JS
    )
    policy = (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        f"script-src {script} 'wasm-unsafe-eval'; style-src {auth}{_ASSET_PREFIX}sign-in.css; "
        f"connect-src {auth}{_WASM_ROUTE} {auth}{web_account_ingress.START_PATH} "
        f"{auth}{web_account_ingress.FINISH_PATH}; form-action {workspace}{web_named_handoff.HANDOFF_PATH}"
    )
    return AccountAssetBundle(origins, page, policy, MappingProxyType(assets))


@contextmanager
def prepare_account_assets(
    *,
    origins,
    wasm_file,
    wasm_pin,
    verifier_file,
    verifier_pin,
    workspace_label="Private workspace",
):
    """Prepare immutable responses and a borrowed fully sealed verifier FD.

    Take pins from a reviewed matching OCOP build, not from either file, a
    request, or an adjacent manifest. Never pass the descriptor downstream to
    browsers. Construct the service that duplicates it before leaving this
    context. Neither artifact is ever reopened by pathname after verification.
    """
    with ExitStack() as cleanup:
        try:
            if (
                type(origins) is not web_local_origins.LocalOrigins
                or type(wasm_pin) is not ReviewedArtifactPin
                or not 8 < wasm_pin.size <= _MAX_WASM
                or type(verifier_pin) is not ReviewedArtifactPin
            ):
                raise AccountAssetError()
            # Snapshot only the two validated numeric ports from trusted config.
            origins = web_local_origins.LocalOrigins(
                origins.account_port, origins.workspace_port
            )
            wasm = _read_pinned(wasm_file, wasm_pin)
            if not wasm.startswith(b"\0asm\x01\0\0\0"):
                raise AccountAssetError()
            bundle = _bundle(origins, workspace_label, wasm, wasm_pin)
            worker = _read_pinned(verifier_file, verifier_pin)
            fd = os.memfd_create(
                "oc-box-reviewed-ocop", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            cleanup.callback(os.close, fd)
            if fd < 3 or os.get_inheritable(fd):
                raise AccountAssetError()
            os.fchmod(fd, 0o500)
            with memoryview(worker) as data:
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise AccountAssetError()
                    offset += written
            del worker
            private_memory.seal(fd)
            web_pake_worker._verify_executable(fd, verifier_pin.sha256)
            prepared = PreparedAccountAssets(bundle, fd, verifier_pin.sha256)
        except (
            AttributeError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
            web_pake_worker.PakeWorkerError,
        ):
            raise AccountAssetError() from None
        yield prepared
