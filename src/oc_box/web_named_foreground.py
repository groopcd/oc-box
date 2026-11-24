"""Compose the reviewed named HTTPS services for one existing foreground owner."""

from contextlib import ExitStack
from functools import partial
import math
from time import monotonic

if __package__:
    from . import (
        web_accounts,
        web_named_account_assets,
        web_named_bootstrap,
        web_named_http,
        web_named_listeners,
        web_named_owner,
        web_named_protocol,
        web_named_services,
        web_named_site_lifecycle,
        workspace_service,
    )
else:
    import web_accounts
    import web_named_account_assets
    import web_named_bootstrap
    import web_named_http
    import web_named_listeners
    import web_named_owner
    import web_named_protocol
    import web_named_services
    import web_named_site_lifecycle
    import workspace_service


class ForegroundError(ValueError):
    def __init__(self):
        super().__init__("Named HTTPS could not start for this foreground workspace.")


class NamedForeground:
    """One explicit, in-process named owner lease and listener lifetime.

    The caller keeps its reviewed reservation, immutable artifacts and account
    snapshot alive through startup, and keeps the reservation, snapshot and
    owner alive until ``close()`` returns True. Keep this object on startup
    failure as well. A false close is not permission to release the attach,
    synchronize/stop as though the client had exited, or restart sites. The
    methods run only in the owning foreground thread; the listener owns workers.

    This does not start a workspace, unlock or refresh credentials, select
    artifacts, open a browser, expose a CLI, or release an unrelated owner.
    """

    def __init__(self, owner, *, profile, account, assets):
        if (
            type(owner) is not workspace_service.Owner
            or type(profile) is not web_named_listeners.ReservedProfile
            or type(account) is not web_accounts.AccountSnapshot
            or type(assets) is not web_named_account_assets.PreparedAccountAssets
            or type(assets.bundle) is not web_named_account_assets.AccountAssetBundle
            or assets.bundle.origins != profile.origins
        ):
            raise ForegroundError()
        self._owner = owner
        self._profile = profile
        self._account = account
        self._assets = assets
        self._attach = ExitStack()
        self._sites = None
        self._attempted = self._retired = self._uncertain = self._released = False

    def start(self, *, expires_at, studio=False):
        """Start once and return only the fixed, token-free workspace entry URL."""
        if self._attempted or self._retired:
            raise ForegroundError()
        self._attempted = True
        if (
            type(studio) is not bool
            or type(expires_at) not in (int, float)
            or not math.isfinite(expires_at)
            or expires_at <= monotonic()
            or not web_named_listeners._host_allowed()
            or self._account.current() is not True
        ):
            raise ForegroundError()
        owner = self._owner
        try:
            lease = self._attach.enter_context(
                web_named_protocol.verified_owner_lease(
                    owner,
                    connect_native=partial(
                        web_named_owner.connect_workspace_native, owner.box
                    ),
                )
            )
        except web_named_owner.OwnerLeaseError:
            raise ForegroundError() from None

        # If preparation cannot provide a cleanup verdict, keep our attach.
        # A normal fixed refusal is known to have created no lasting services.
        self._uncertain = True
        try:
            self._sites = web_named_site_lifecycle.prepare_sites(
                owner.box,
                profile=self._profile,
                lease=lease,
                store=self._account.store,
                account_current=self._account.current,
                verifier_fd=self._assets.verifier_fd,
                expected_sha256=self._assets.verifier_sha256,
            )
        except web_named_services.ServiceCleanupError:
            raise ForegroundError() from None
        except (
            web_named_site_lifecycle.ListenerError,
            web_named_services.ServicePreparationError,
        ):
            self._uncertain = False
            raise ForegroundError() from None
        self._uncertain = False
        try:
            web_named_http.start_named_http(
                self._sites,
                self._profile,
                account_assets=self._assets,
                expires_at=expires_at,
                studio=studio,
                studio_ui=studio,
            )
        except (OSError, ValueError):
            raise ForegroundError() from None
        return (
            self._profile.origins.workspace.value + web_named_bootstrap.WORKSPACE_ENTRY
        )

    @property
    def stop_reason(self):
        return self._sites.stop_reason if self._sites is not None else None

    def wait_stopped(self, timeout=None):
        """Observe the listener fence only; this does not release the attach."""
        return self._sites is not None and self._sites.wait_stopped(timeout)

    def close(self, *, timeout=5.0):
        """Release only after verified site/worker teardown; never redial."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("Invalid named HTTPS drain timeout.")
        self._retired = True
        if self._uncertain:
            return False
        if self._released:
            return True
        try:
            if (
                self._sites is not None
                and self._sites.close(timeout=timeout) is not True
            ):
                return False
        except Exception:  # noqa: BLE001 - keep the held attach and fixed verdict.
            return False
        self._uncertain = True
        try:
            self._attach.close()
        except Exception:  # noqa: BLE001 - do not assert release on an unverified close.
            return False
        self._released = True
        self._uncertain = False
        return True
