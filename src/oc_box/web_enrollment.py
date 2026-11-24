"""One browser attempt for a reviewed host enrollment; no listener or store I/O.

The host must approve the exact account/scope and supply a server-verified
account bootstrap nonce, not a value accepted from a request body. Only the
trusted host may take a verified upload. Persistence still requires the
masked host confirmation, an exclusive owner/admission fence, and the account
store's compare-and-swap. Neither a request nor a proof grants workspace
access. Keep all protocol values out of logs and URLs.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic

if __package__:
    from . import (
        web_accounts,
        web_bootstrap,
        web_local_origins,
        web_pake_flow,
        web_pake_worker,
    )
    from .web_pake_profile import _identifier
else:
    import web_accounts
    import web_bootstrap
    import web_local_origins
    import web_pake_flow
    import web_pake_worker
    from web_pake_profile import _identifier


_NONCE = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_SETUP_SIZE = 128
_REQUEST_SIZE = 32
_RESPONSE_SIZE = 64
_RECORD_SIZE = 192


def _token(value):
    return (
        type(value) is str and len(value) == 43 and _NONCE.fullmatch(value) is not None
    )


def _frame(value, size):
    return type(value) is bytes and len(value) == size


def _digest(token):
    return sha256(token.encode("ascii")).digest()


@dataclass(frozen=True, slots=True, repr=False)
class RegistrationResponse:
    challenge: str
    response: bytes


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedEnrollment:
    review: web_accounts.RegistrationReview
    registration: bytes
    current: Callable[[], bool]


class EnrollmentApproval:
    """Monotonic one-shot protocol; backend callbacks are trusted host code.

    The backend is responsible for a pinned implementation, deadlines and
    verified cleanup. A slow callback cannot be interrupted by this object;
    its result is discarded after expiry or close. External owner exclusion
    is not established by this protocol or by a matching browser nonce.
    """

    def __init__(
        self,
        *,
        review,
        origins,
        claim,
        respond,
        validate,
        prove,
        ttl=90,
    ):
        if (
            type(review) is not web_accounts.RegistrationReview
            or type(origins) is not web_local_origins.LocalOrigins
            or type(claim) is not web_bootstrap.EnrollmentClaim
            or claim.origin != origins.account.value
            or type(review.workspace_ids) is not tuple
            or claim.workspace_id not in review.workspace_ids
            or not _frame(review.setup, _SETUP_SIZE)
            or not callable(respond)
            or not callable(validate)
            or not callable(prove)
            or type(ttl) not in (int, float)
            or not 0 < ttl <= 120
        ):
            raise ValueError("Invalid host web enrollment approval.")
        name = web_accounts._username(review.username).encode("ascii")
        if not claim.bind():
            raise ValueError("Invalid host web enrollment approval.")
        self._review = review
        self._origin = origins.account.value
        self._claim = claim
        self._bindings = {
            "credential_identifier": _identifier(b"credential", name),
            "client_identifier": _identifier(b"client", name),
            "verifier_identifier": _identifier(
                b"verifier", self._origin.encode("ascii")
            ),
        }
        self._respond = respond
        self._validate = validate
        self._prove = prove
        self._deadline = monotonic() + ttl
        self._phase = "approved"
        self._challenge = None
        self._record = None
        self._callbacks = 0
        self._cleanup_uncertain = False
        self._lock = Lock()

    def _matching(self, origin, nonce):
        return self._claim.matches(origin, nonce)

    def _close_locked(self):
        self._phase = "closed"
        self._challenge = self._record = None
        self._review = None

    def _current_locked(self, phase):
        if monotonic() >= self._deadline or not self._claim.current():
            self._close_locked()
        return self._phase == phase

    def drained(self):
        """Check callback completion and any latched cleanup uncertainty."""
        with self._lock:
            return self._callbacks == 0 and not self._cleanup_uncertain

    def _after_callback(self, success):
        with self._lock:
            self._callbacks -= 1
            if not success:
                self._close_locked()

    def _fence_cleanup(self):
        with self._lock:
            self._cleanup_uncertain = True
            self._close_locked()

    def start(self, *, account_origin, account_nonce, request):
        """Return at most one response for this browser and reviewed account."""
        if not self._matching(account_origin, account_nonce):
            return None
        with self._lock:
            if not self._current_locked("approved"):
                return None
            if not _frame(request, _REQUEST_SIZE):
                self._close_locked()
                return None
            setup = self._review.setup
            self._phase = "responding"
            self._callbacks += 1
        finished = False
        try:
            response = self._respond(
                setup=setup, request=request, deadline=self._deadline, **self._bindings
            )
            if not _frame(response, _RESPONSE_SIZE):
                return None
            challenge = secrets.token_urlsafe(32)
            if not _token(challenge):
                return None
            challenge_hash = _digest(challenge)
            if self._claim.matches(account_origin, challenge):
                return None
            with self._lock:
                if not self._current_locked("responding"):
                    return None
                self._challenge = challenge_hash
                self._phase = "upload"
                finished = True
            return RegistrationResponse(challenge, response)
        except (
            web_pake_flow.VerifierCleanupError,
            web_pake_worker.PakeWorkerCleanupError,
        ):
            self._fence_cleanup()
            raise
        except (OSError, ValueError, web_pake_worker.PakeWorkerError):
            return None
        except BaseException:
            self._fence_cleanup()
            raise
        finally:
            self._after_callback(finished)

    def finish(self, *, account_origin, account_nonce, challenge, registration):
        """Validate one upload, then demand proof of possession before host take.

        ``prove`` is a trusted coordinator for a fresh OCOP login using exactly
        this tentative setup and record. It must not issue a sign-in handoff.
        The caller must allow the proof's separate browser round trips and
        confirmed cleanup; this is not a synchronous HTTP handler.
        """
        if not self._matching(account_origin, account_nonce) or not _token(challenge):
            return False
        with self._lock:
            if not self._current_locked("upload") or not secrets.compare_digest(
                _digest(challenge), self._challenge
            ):
                return False
            if not _frame(registration, _RECORD_SIZE):
                self._close_locked()
                return False
            self._challenge = None
            self._phase = "validating"
            self._callbacks += 1
            setup = self._review.setup
        finished = False
        try:
            checked = self._validate(
                registration=registration, deadline=self._deadline, **self._bindings
            )
            if not _frame(checked, _RECORD_SIZE) or not secrets.compare_digest(
                checked, registration
            ):
                return False
            with self._lock:
                if not self._current_locked("validating"):
                    return False
                self._phase = "proving"
            # Separate the enrollment proof from normal login. The public
            # context never contains the account cookie or workspace IDs.
            context = _identifier(
                b"enrollment-proof", f"{self._origin}\n{challenge}".encode("ascii")
            )
            if (
                self._prove(
                    setup=setup,
                    registration=checked,
                    context=context,
                    deadline=self._deadline,
                    **self._bindings,
                )
                is not True
            ):
                return False
            with self._lock:
                if not self._current_locked("proving"):
                    return False
                self._record = checked
                self._phase = "ready"
                finished = True
            return True
        except (
            web_pake_flow.VerifierCleanupError,
            web_pake_worker.PakeWorkerCleanupError,
        ):
            self._fence_cleanup()
            raise
        except (OSError, ValueError, web_pake_worker.PakeWorkerError):
            return False
        except BaseException:
            self._fence_cleanup()
            raise
        finally:
            self._after_callback(finished)

    def take_for_host(self):
        """Take once after local confirmation and exclusion; never expose to HTTP."""
        with self._lock:
            if (
                not self._current_locked("ready")
                or self._callbacks
                or self._cleanup_uncertain
            ):
                return None
            result = VerifiedEnrollment(
                self._review, self._record, self._taken_is_current
            )
            self._phase = "taken"
            self._review = self._record = None
            return result

    def _taken_is_current(self):
        with self._lock:
            return self._current_locked("taken")

    def expire(self):
        with self._lock:
            if monotonic() >= self._deadline or not self._claim.current():
                self._close_locked()

    def close(self):
        with self._lock:
            self._close_locked()
