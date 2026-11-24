"""Assemble the internal account and app services of one named owner.

The caller supplies a preflighted socket reservation, a verified held owner
lease, and a locally authenticated account snapshot. This module does not
unlock any vault, create a workspace, start a listener, or expose a route.
"""

import socket
from dataclasses import dataclass, replace

if __package__:
    from . import (
        web_account_ingress,
        web_accounts,
        web_bootstrap,
        web_local_origins,
        web_named_bootstrap,
        web_named_listeners,
        web_named_owner,
        web_pake_flow,
        web_pake_profile,
        web_pake_worker,
    )
else:
    import web_account_ingress
    import web_accounts
    import web_bootstrap
    import web_local_origins
    import web_named_bootstrap
    import web_named_listeners
    import web_named_owner
    import web_pake_flow
    import web_pake_profile
    import web_pake_worker


class ServicePreparationError(ValueError):
    def __init__(self):
        super().__init__("Named workspace services could not be prepared.")


class ServiceCleanupError(ServicePreparationError):
    def __init__(self):
        ValueError.__init__(
            self, "Named workspace service cleanup could not be verified."
        )


def _unserved(profile):
    return (
        type(profile) is web_named_listeners.ReservedProfile
        and type(profile.origins) is web_local_origins.LocalOrigins
        and profile.origins == web_local_origins.LocalOrigins()
        and type(profile.tls) is web_local_origins.LocalTLS
        and all(
            peer.getsockname() == address
            and peer.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0
            for peer, address in (
                (profile.account, profile.origins.account_bind),
                (profile.workspace, profile.origins.workspace_bind),
            )
        )
    )


def _fixed_profile(store):
    sizes = web_pake_worker.LAB_SIZES
    return (
        type(store) is web_accounts.AccountStore
        and type(store.setup) is bytes
        and len(store.setup) == sizes.setup
        and type(store.accounts) is tuple
        and len(store.accounts) <= web_accounts.MAX_ACCOUNTS
        and all(
            type(item) is web_accounts.Account
            and type(item.registration) is bytes
            and len(item.registration) == sizes.registration
            for item in store.accounts
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class NamedServices:
    entry: web_named_bootstrap.NamedBootstrapIngress
    account: web_account_ingress.AccountIngress
    application: web_named_owner.BoundNamedApp
    _owner: web_named_owner.NamedOwnerBinding
    _bootstrap: web_bootstrap.BrowserBootstrap
    _flow: web_pake_flow.PakeFlow
    _backend: web_pake_profile.LocalPakeProfile

    def close(self, *, timeout=5.0):
        """Fence both sites, then verify internal drain; never release the attach.

        A listener must independently stop and join its own workers before the
        original socket reservation and owner lease can be released.
        """
        cleaned = True
        for cleanup in (self._bootstrap.close, self._owner.close):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - attempt both and retain only a fixed verdict.
                cleaned = False
        try:
            worker = self._backend.close() is True
            relay = self.application.relay.wait_drained(timeout) is True
            return (
                cleaned
                and worker
                and relay
                and not self.application.sign_in.admission.cancellation_failed
                and self._flow.active_verifiers == 0
                and not self._flow.cleanup_failed
            )
        except Exception:  # noqa: BLE001 - an uncertain close is never a release.
            return False


def prepare_services(
    box, *, profile, lease, store, account_current, verifier_fd, expected_sha256
):
    """Bind all admission paths to the same owner and account-store lifetime.

    ``account_current`` is a trusted host check of the reviewed store's
    generation and approval. It is not a request value and must do no secret
    prompts or refresh on traffic. A refusal permanently fences this owner
    binding even if that check would pass again later.
    """
    owner = bootstrap = backend = flow = None
    try:
        if (
            not _unserved(profile)
            or type(lease) is not web_named_owner.VerifiedOwnerLease
            or not _fixed_profile(store)
            or not callable(account_current)
            or account_current() is not True
        ):
            raise ServicePreparationError()

        def observe():
            if account_current() is not True:
                return None
            identity = lease.observe()
            return identity if account_current() is True else None

        origins = profile.origins
        owner = web_named_owner.NamedOwnerBinding(
            box,
            origins=origins,
            lease=replace(lease, observe=observe),
        )
        binding, auth_origin = owner.binding, origins.account.value
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        backend = web_pake_profile.LocalPakeProfile(
            verifier_fd,
            expected_sha256=expected_sha256,
            origins=origins,
        )
        flow = web_pake_flow.PakeFlow(
            binding,
            store=store,
            backend=backend,
            sizes=backend.sizes,
            auth_origin=auth_origin,
            max_in_flight=1,
        )
        app = owner.compose(
            flow=flow,
            claim_bootstrap=lambda nonce: bootstrap.claim_app(binding, nonce),
        )
        account = web_account_ingress.AccountIngress(
            origins=origins,
            sign_in=app.sign_in,
            translate_nonce=lambda nonce: bootstrap.app_nonce(
                binding,
                auth_origin=auth_origin,
                account_nonce=nonce,
            ),
        )
        entry = web_named_bootstrap.NamedBootstrapIngress(
            origins=origins,
            binding=binding,
            bootstrap=bootstrap,
            owner_current=owner._owner_current,
        )
        if owner._owner_current(binding) is not True:
            raise ServicePreparationError()
        return NamedServices(entry, account, app, owner, bootstrap, flow, backend)
    except BaseException as error:
        confirmed = True
        for partial in (bootstrap, owner, flow, backend):
            if partial is None:
                continue
            try:
                outcome = partial.close()
                if partial is backend and outcome is not True:
                    confirmed = False
            except Exception:  # noqa: BLE001 - no raw startup or cleanup errors.
                confirmed = False
        if not confirmed:
            raise ServiceCleanupError() from None
        if not isinstance(error, Exception):
            raise
        raise ServicePreparationError() from None
