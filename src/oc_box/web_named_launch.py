"""Host preflight and retained resources for an explicitly prepared named launch."""

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import (
        secret_prompt,
        web_accounts,
        web_named_account_assets,
        web_named_foreground,
        web_named_listeners,
    )
else:
    import secret_prompt
    import web_accounts
    import web_named_account_assets
    import web_named_foreground
    import web_named_listeners


class NamedLaunchError(ValueError):
    """Fixed refusal; don't expose the operator's paths, secrets or worker errors."""


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
        if not any(
            workspace in account.workspace_ids
            for account in self._account.store.accounts
        ):
            raise NamedLaunchError(
                "No named web account is enrolled for this workspace."
            )
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
