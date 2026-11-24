"""Host preflight and retained resources for an explicitly prepared named launch."""

from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from time import monotonic

if __package__:
    from . import (
        secret_prompt,
        web_accounts,
        web_named_account_assets,
        web_named_foreground,
        web_named_listeners,
        web_named_site_lifecycle,
        web_tls,
    )
else:
    import secret_prompt
    import web_accounts
    import web_named_account_assets
    import web_named_foreground
    import web_named_listeners
    import web_named_site_lifecycle
    import web_tls


class NamedLaunchError(ValueError):
    """Fixed refusal; don't expose the operator's paths, secrets or worker errors."""


class NamedClientUndrained(NamedLaunchError):
    def __init__(self):
        super().__init__(
            "Named Studio shutdown was not verified. Do not release its owner or "
            "treat this as a normal history-sync or stop result."
        )


@dataclass(frozen=True, slots=True, repr=False)
class NamedLaunchInputs:
    """Exact trusted host choices, never fields from an HTTP request."""

    account_cert: Path
    account_key: Path
    workspace_cert: Path
    workspace_key: Path
    wasm_file: Path
    wasm_pin: web_named_account_assets.ReviewedArtifactPin
    verifier_file: Path
    verifier_pin: web_named_account_assets.ReviewedArtifactPin
    account_store: Path

    def __post_init__(self):
        for path in (
            self.account_cert,
            self.account_key,
            self.workspace_cert,
            self.workspace_key,
            self.wasm_file,
            self.verifier_file,
            self.account_store,
        ):
            if (
                not isinstance(path, Path)
                or path.anchor != "/"
                or len(path.parts) < 2
                or ".." in path.parts
                or len(str(path)) > 4096
                or "\x00" in str(path)
            ):
                raise NamedLaunchError(
                    "Named HTTPS requires explicit local artifact and account paths."
                )
        if (
            type(self.wasm_pin) is not web_named_account_assets.ReviewedArtifactPin
            or type(self.verifier_pin)
            is not web_named_account_assets.ReviewedArtifactPin
        ):
            raise NamedLaunchError(
                "Named HTTPS requires independently reviewed artifact pins."
            )


_PROFILE_LIMIT = 16384
_CERT_KEYS = ("account_cert", "account_key", "workspace_cert", "workspace_key")


def _profile_path(value):
    if (
        type(value) is not str
        or not 1 < len(value.encode("utf-8")) <= 4096
        or not value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError
    path = Path(value)
    if path.anchor != "/" or str(path) != value or ".." in path.parts:
        raise ValueError
    return path


def _unique(values):
    result = dict(values)
    if len(result) != len(values):
        raise ValueError
    return result


def _artifact(row):
    if (
        type(row) is not dict
        or row.keys() != {"path", "size", "sha256"}
        or type(row["sha256"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) is None
    ):
        raise ValueError
    return _profile_path(row["path"]), web_named_account_assets.ReviewedArtifactPin(
        row["size"], bytes.fromhex(row["sha256"])
    )


def load_named_inputs(path):
    """Read one explicitly selected private host profile, without creating it.

    The profile selects paths and independently reviewed SHA-256 pins only.
    It is not approval to distribute artifacts and contains no password or
    credential fields. The eventual TLS/artifact/account loaders independently
    verify their corresponding input descriptors before use.
    """
    if not web_named_listeners._host_allowed():
        raise NamedLaunchError(
            "Named HTTPS preparation requires a regular Linux host user."
        )
    try:
        if not isinstance(path, Path):
            raise ValueError
        path = _profile_path(str(path))
        with ExitStack() as cleanup:
            pinned = web_tls._pin_file(path, cleanup)
            info = os.fstat(pinned)
            stamp = web_tls._stamp(info)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                or not 0 < info.st_size <= _PROFILE_LIMIT
            ):
                raise ValueError
            data_fd = os.open(
                f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
            )
            cleanup.callback(os.close, data_fd)
            if web_tls._stamp(os.fstat(data_fd)) != stamp:
                raise ValueError
            raw = bytearray()
            while len(raw) < info.st_size:
                chunk = os.pread(data_fd, info.st_size - len(raw), len(raw))
                if not chunk:
                    raise ValueError
                raw.extend(chunk)
            if (
                os.pread(data_fd, 1, len(raw))
                or web_tls._stamp(os.fstat(data_fd)) != stamp
                or web_tls._stamp(os.fstat(web_tls._pin_file(path, cleanup))) != stamp
            ):
                raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if (
            type(value) is not dict
            or value.keys() != {"version", "tls", "wasm", "verifier", "account_store"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["tls"]) is not dict
            or set(value["tls"]) != set(_CERT_KEYS)
        ):
            raise ValueError
        wasm, wasm_pin = _artifact(value["wasm"])
        verifier, verifier_pin = _artifact(value["verifier"])
        return NamedLaunchInputs(
            *(_profile_path(value["tls"][key]) for key in _CERT_KEYS),
            wasm,
            wasm_pin,
            verifier,
            verifier_pin,
            _profile_path(value["account_store"]),
        )
    except (OSError, ValueError, TypeError, RecursionError):
        raise NamedLaunchError(
            "The explicit named HTTPS profile could not be verified."
        ) from None


class PreparedNamedLaunch:
    """Retain preflight resources until the exact foreground attach has drained.

    The same host client uses this handle before workspace preparation and
    after the verified named-only owner is prepared. Keep it on startup
    failure as well. This object deliberately has no scope exit that could
    release its TLS/account resources after unconfirmed drain.
    """

    def __init__(self, resources, profile, account, assets):
        self._resources = resources
        self._profile = profile
        self._account = account
        self._assets = assets
        self._foreground = None
        self._attempted = self._retired = self._released = self._uncertain = False

    def require_workspace(self, workspace):
        """Check authenticated host scope before launch and again before attach."""
        if self._retired or self._account.current() is not True:
            raise NamedLaunchError(
                "The host web account store changed; prepare a new named launch."
            )
        if (
            type(workspace) is not str
            or re.fullmatch(r"[a-f0-9]{32}", workspace) is None
            or not any(
                workspace in account.workspace_ids
                for account in self._account.store.accounts
            )
        ):
            raise NamedLaunchError(
                "No named web account is enrolled for this workspace."
            )

    def start(self, owner, *, expires_at, studio=False):
        if self._attempted or self._retired:
            raise NamedLaunchError("Named HTTPS cannot reuse this launch preparation.")
        self._attempted = True
        try:
            workspace = owner.data["id"]
        except (AttributeError, KeyError, TypeError):
            raise NamedLaunchError(
                "Named HTTPS requires the prepared workspace owner."
            ) from None
        self.require_workspace(workspace)
        self._foreground = web_named_foreground.NamedForeground(
            owner,
            profile=self._profile,
            account=self._account,
            assets=self._assets,
        )
        return self._foreground.start(expires_at=expires_at, studio=studio)

    def wait_stopped(self, timeout=None):
        return self._foreground is not None and self._foreground.wait_stopped(timeout)

    @property
    def stop_reason(self):
        return self._foreground.stop_reason if self._foreground is not None else None

    def close(self, *, timeout=5.0):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("Invalid named HTTPS drain timeout.")
        self._retired = True
        if self._released:
            return True
        if self._uncertain:
            return False
        if (
            self._foreground is not None
            and self._foreground.close(timeout=timeout) is not True
        ):
            return False
        self._uncertain = True
        try:
            self._resources.close()
        except Exception:  # noqa: BLE001 - no raw resource teardown diagnostics.
            return False
        self._released = True
        self._uncertain = False
        return True


def prepare_named_launch(inputs, *, workspace_label="Private workspace"):
    """Preflight fixed TLS and assets, then unlock only the host account store.

    No workspace lookup/creation, provider vault, Podman, native service, or
    listener activation may run inside this preparation. No compiled default
    is chosen and no credential is obtained from the browser or environment.
    The caller owns a returned handle and must check ``close()``.
    """
    if type(inputs) is not NamedLaunchInputs or not web_named_listeners._host_allowed():
        raise NamedLaunchError(
            "Named HTTPS preparation requires a regular Linux host user."
        )
    with ExitStack() as cleanup:
        try:
            profile = cleanup.enter_context(
                web_named_listeners.reserve_profile(
                    account_cert=inputs.account_cert,
                    account_key=inputs.account_key,
                    workspace_cert=inputs.workspace_cert,
                    workspace_key=inputs.workspace_key,
                )
            )
        except web_named_listeners.LocalHTTPSUnavailable:
            raise NamedLaunchError(
                "Named HTTPS certificate or loopback reservation refused."
            ) from None
        try:
            assets = cleanup.enter_context(
                web_named_account_assets.prepare_account_assets(
                    origins=profile.origins,
                    wasm_file=inputs.wasm_file,
                    wasm_pin=inputs.wasm_pin,
                    verifier_file=inputs.verifier_file,
                    verifier_pin=inputs.verifier_pin,
                    workspace_label=workspace_label,
                )
            )
        except web_named_account_assets.AccountAssetError:
            raise NamedLaunchError(
                "Named HTTPS browser or verifier artifact refused."
            ) from None
        try:
            phrase = secret_prompt.read_secret("Host web account store passphrase: ")
        except (OSError, EOFError):
            raise NamedLaunchError(
                "Unlock the host web account store on the controlling terminal."
            ) from None
        try:
            account = cleanup.enter_context(
                web_accounts.load_snapshot(inputs.account_store, phrase)
            )
        except web_accounts.WebAccountsError:
            raise NamedLaunchError(
                "The host web account store could not be unlocked or verified."
            ) from None
        finally:
            del phrase
        return PreparedNamedLaunch(cleanup.pop_all(), profile, account, assets)


def _activate_browser(box, url):
    env = box.maintenance_host_env()
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys,webbrowser; webbrowser.open(sys.stdin.read())",
            ],
            input=url,
            text=True,
            env=env,
            check=False,
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        # The printed fixed URL remains available; no retry or guest fallback.
        pass


def run_named_client(box, owner, prepared, *, no_open):
    """Run the actual scoped Studio client loop, not an ordinary owner exit.

    The calling lifecycle owns the controlling terminal and verified owner,
    and must retain them and ``prepared`` on NamedClientUndrained. Do not call
    this through an ordinary fallback cleanup/finally that releases the owner
    without confirmed drain. Nothing here opens or refreshes the provider
    vault, mutates native history, starts a workspace, or falls back to pairing.
    """
    if type(prepared) is not PreparedNamedLaunch or type(no_open) is not bool:
        raise NamedLaunchError("Invalid prepared named Studio client.")
    try:
        url = prepared.start(owner, expires_at=monotonic() + 8 * 60 * 60, studio=True)
        expected = prepared._profile.origins.workspace.value + "/auth/oc-box-entry"
        if url != expected:
            raise NamedLaunchError(
                "Named Studio did not produce the verified workspace entry URL."
            )
        print(
            "oc-box: Named workspace Studio\n"
            + url
            + "\nKeep this host command open. Browser login never asks for your host vault.",
            flush=True,
        )
        if not no_open:
            _activate_browser(box, url)
        if (
            prepared.wait_stopped() is not True
            or prepared.stop_reason is not web_named_site_lifecycle.StopReason.REQUESTED
        ):
            raise NamedLaunchError(
                "The named Studio site stopped; reopen it from the host."
            )
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            drained = prepared.close()
        except BaseException:
            # An interruption or fault *during* teardown cannot bypass the
            # retained-owner error path. Observing the accept fence, or the
            # original client exiting, does not prove that the close completed.
            raise NamedClientUndrained() from None
        if drained is not True:
            raise NamedClientUndrained() from None
