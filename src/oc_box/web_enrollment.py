"""Staged, host-reviewed enrollment; no listener, store write or app grant.

The host supplies a verified bootstrap claim for the exact review and transfers
two dedicated, already-pinned workers. Browser requests select neither the
account nor the workspace scope, and carry no raw password. Keep protocol
values out of logs/URLs. Only confirmed HTTP delivery can release a result
to the host, which still enforces local confirmation, owner exclusion and CAS.
"""

import math
import secrets
from collections.abc import Callable
from contextlib import contextmanager
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
        web_pake_profile,
        web_pake_worker,
        web_registration_worker,
    )
else:
    import web_accounts
    import web_bootstrap
    import web_local_origins
    import web_pake_flow
    import web_pake_profile
    import web_pake_worker
    import web_registration_worker


_SETUP_SIZE = 128
_REQUEST_SIZE = 32
_RESPONSE_SIZE = 64
_RECORD_SIZE = 192
_SIZES = web_pake_worker.LAB_SIZES
_CLEANUP_ERRORS = (
    web_pake_flow.VerifierCleanupError,
    web_pake_worker.PakeWorkerCleanupError,
    web_registration_worker.RegistrationCleanupError,
)


def _frame(value, size):
    return type(value) is bytes and len(value) == size


def _digest(token):
    return sha256(token.encode("ascii")).digest()


def _checkpoint(check, deadline):
    """Trusted request liveness check; never retain it across browser rounds."""
    if monotonic() >= deadline:
        raise TimeoutError("Web enrollment request is no longer current.")
    if check is not None:
        try:
            result = check()
        except Exception:
            raise TimeoutError("Web enrollment request is no longer current.") from None
        if result is not None and result is not True:
            raise TimeoutError("Web enrollment request is no longer current.")
    if monotonic() >= deadline:
        raise TimeoutError("Web enrollment request is no longer current.")


class EnrollmentCleanupError(RuntimeError):
    """Fixed refusal; retain the owner fence until explicit drain succeeds."""

    def __init__(self):
        super().__init__("Web enrollment worker cleanup was not confirmed.")


@dataclass(frozen=True, slots=True, repr=False)
class RegistrationResponse:
    challenge: str
    response: bytes


@dataclass(frozen=True, slots=True, repr=False)
class EnrollmentProofChallenge:
    ke2: bytes
    context: bytes


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedEnrollment:
    review: web_accounts.RegistrationReview
    registration: bytes
    current: Callable[[], bool]


class EnrollmentApproval:
    """One exact bootstrap and review, with distinct OCOR and OCOP rounds.

    Successful construction transfers ownership of both workers. Never share
    either with a sign-in flow or another approval: expiry/close retires them.
    The owner calls expire() while awaiting browser work, and verifies close()
    before dropping its teardown fence. This does not acquire owner exclusion.
    """

    def __init__(
        self,
        *,
        review,
        origins,
        claim,
        registration_worker,
        proof_profile,
        ttl=90,
    ):
        if (
            type(review) is not web_accounts.RegistrationReview
            or type(origins) is not web_local_origins.LocalOrigins
            or type(claim) is not web_bootstrap.EnrollmentClaim
            or claim.origin != origins.account.value
            or claim.workspace_origin != origins.workspace.value
            or type(review.workspace_ids) is not tuple
            or claim.workspace_id not in review.workspace_ids
            or not _frame(review.setup, _SETUP_SIZE)
            or type(registration_worker)
            is not web_registration_worker.RegistrationWorker
            or type(proof_profile) is not web_pake_profile.LocalPakeProfile
            or not proof_profile.matches(origins)
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
        self._username = name
        self._credential = web_pake_profile._identifier(b"credential", name)
        self._registrar = registration_worker
        self._profile = proof_profile
        self._deadline = monotonic() + ttl
        self._phase = "approved"
        self._challenge = self._record = self._proof = None
        self._http = self._final_reply = self._write_cutoff = self._writer = None
        self._busy = False
        self._draining = self._drain_attempted = self._workers_drained = False
        self._cleanup_uncertain = False
        self._lock = Lock()

    def _close_locked(self):
        self._phase = "closed"
        self._challenge = self._record = self._proof = self._review = None
        self._final_reply = self._write_cutoff = None
        # A cancelled writer must still leave its original write scope.

    def _current_locked(self, phase):
        if self._phase != "closed" and (
            monotonic() >= self._deadline or not self._claim.current()
        ):
            self._close_locked()
        return self._phase == phase

    def _fence_locked(self):
        self._cleanup_uncertain = True
        self._close_locked()

    def matches_origins(self, origins):
        return (
            type(origins) is web_local_origins.LocalOrigins
            and origins.account.value == self._origin
            and origins.workspace.value == self._claim.workspace_origin
        )

    def _bind_http(self):
        """Fix the mode before the first admitted round; never attach late."""
        with self._lock:
            if (
                self._http is None
                and self._current_locked("approved")
                and not self._busy
            ):
                self._http = object()
                return self._http
        self._drain_closed()
        return None

    def _reply_current(self, *, account_origin, account_nonce, http, final=None):
        """Check the original claim again before disclosing a prepared reply."""
        matching = self._claim.matches(account_origin, account_nonce)
        with self._lock:
            closed = self._current_locked("closed")
            current = (
                matching
                and http is self._http
                and not closed
                and not self._cleanup_uncertain
                and (final is None or self._final_reply is final)
            )
        if closed:
            self._drain_closed()
        return current

    def _request_deadline(self, deadline):
        if deadline is None:
            return self._deadline
        if type(deadline) not in (int, float) or not 0 < deadline < math.inf:
            raise ValueError("Invalid web enrollment request deadline.")
        return min(self._deadline, deadline)

    @property
    def cleanup_failed(self):
        """Sticky uncertainty; a later confirmed reap never restores authority."""
        with self._lock:
            return self._cleanup_uncertain

    def drained(self):
        """True only after both workers and any admitted final writer drain."""
        with self._lock:
            return self._workers_drained and not self._busy and self._writer is None

    def _drain_workers(self, *, recheck=False):
        with self._lock:
            if self._workers_drained:
                return True
            if self._draining or (self._drain_attempted and not recheck):
                return False
            self._draining = self._drain_attempted = True
        confirmed = False
        try:
            results = []
            # Never wait for a worker while holding the approval lock.
            for worker in (self._registrar, self._profile):
                try:
                    results.append(worker.close() is True)
                except Exception:  # noqa: BLE001 - no raw worker error at the host boundary.
                    results.append(False)
            confirmed = all(results)
        finally:
            with self._lock:
                self._draining = False
                self._workers_drained = confirmed
                if not confirmed:
                    self._fence_locked()
        return confirmed

    def _drain_closed(self):
        with self._lock:
            closed = self._phase == "closed"
        if closed:
            self._drain_workers()

    def _reserve(self, origin, nonce, phase, running, challenge, valid, http):
        if not self._claim.matches(origin, nonce):
            self.expire()
            return None
        with self._lock:
            if http is self._http and self._current_locked(phase) and not self._busy:
                if phase != "approved" and (
                    not web_bootstrap._valid(challenge)
                    or not secrets.compare_digest(_digest(challenge), self._challenge)
                ):
                    return None  # Another browser/challenge cannot burn the attempt.
                if valid:
                    self._busy = True
                    self._phase = running
                    proof, self._proof = self._proof, None
                    return self._review, proof
                self._close_locked()  # A rightful malformed frame consumes its round.
        self._drain_closed()
        return None

    @contextmanager
    def _step(self, origin, nonce, phase, running, *, challenge=None, valid, http):
        admitted = self._reserve(origin, nonce, phase, running, challenge, valid, http)
        try:
            yield admitted
        except _CLEANUP_ERRORS:
            with self._lock:
                self._fence_locked()
            raise EnrollmentCleanupError() from None
        except (
            OSError,
            ValueError,
            web_pake_worker.PakeWorkerError,
            web_registration_worker.RegistrationError,
        ):
            pass
        except Exception:  # noqa: BLE001 - unexpected worker failures are fixed and fenced.
            with self._lock:
                self._fence_locked()
            raise EnrollmentCleanupError() from None
        except BaseException:
            with self._lock:
                self._fence_locked()
            raise
        finally:
            if admitted is not None:
                with self._lock:
                    if self._phase == running:
                        self._close_locked()
                    self._busy = False
                self._drain_closed()

    def start(
        self,
        *,
        account_origin,
        account_nonce,
        request,
        check=None,
        deadline=None,
        _http=None,
    ):
        """One blinded registration response, with a browser-bound challenge."""
        with self._step(
            account_origin,
            account_nonce,
            "approved",
            "responding",
            valid=_frame(request, _REQUEST_SIZE),
            http=_http,
        ) as admitted:
            if admitted is None:
                return None
            review, _ = admitted
            cutoff = self._request_deadline(deadline)
            _checkpoint(check, cutoff)
            response = self._registrar.respond(
                setup=review.setup,
                credential_identifier=self._credential,
                registration_request=request,
                deadline=cutoff,
            )
            _checkpoint(check, cutoff)
            if not _frame(response, _RESPONSE_SIZE):
                return None
            challenge = secrets.token_urlsafe(32)
            if not web_bootstrap._valid(challenge) or self._claim.matches(
                account_origin, challenge
            ):
                return None
            with self._lock:
                if monotonic() >= cutoff or not self._current_locked("responding"):
                    return None
                self._challenge = _digest(challenge)
                self._phase = "upload"
            return RegistrationResponse(challenge, response)
        return None

    def start_proof(
        self,
        *,
        account_origin,
        account_nonce,
        challenge,
        registration,
        ke1,
        check=None,
        deadline=None,
        _http=None,
    ):
        """Validate one canonical upload, then return KE2 for its fresh KE1."""
        with self._step(
            account_origin,
            account_nonce,
            "upload",
            "starting-proof",
            challenge=challenge,
            valid=_frame(registration, _RECORD_SIZE) and _frame(ke1, _SIZES.ke1),
            http=_http,
        ) as admitted:
            if admitted is None:
                return None
            review, _ = admitted
            cutoff = self._request_deadline(deadline)
            _checkpoint(check, cutoff)
            record = self._registrar.validate_upload(registration, deadline=cutoff)
            _checkpoint(check, cutoff)
            if not _frame(record, _RECORD_SIZE) or not secrets.compare_digest(
                record, registration
            ):
                return None
            with self._lock:
                if not self._current_locked("starting-proof"):
                    return None
            # Domain-separate from sign-in; never hash browser cookies, private
            # workspace IDs or store/setup data into the public context.
            context = web_pake_profile._identifier(
                b"enrollment-proof", f"{self._origin}\n{challenge}".encode("ascii")
            )
            ke2, proof = self._profile.start(
                setup=review.setup,
                credential_identifier=self._username,
                registration=record,
                context=context,
                ke1=ke1,
                deadline=self._deadline,
            )
            # The proof child may outlive this request. Its short internal I/O
            # bound still applies; this request cannot publish after its cutoff.
            _checkpoint(check, cutoff)
            if not _frame(ke2, _SIZES.ke2) or not callable(
                getattr(proof, "finish", None)
            ):
                return None
            with self._lock:
                if monotonic() >= cutoff or not self._current_locked("starting-proof"):
                    return None
                self._record, self._proof = record, proof
                self._phase = "proof"
            return EnrollmentProofChallenge(ke2, context)
        return None

    def finish_proof(
        self,
        *,
        account_origin,
        account_nonce,
        challenge,
        ke3,
        check=None,
        deadline=None,
        _http=None,
        _final_reply=None,
    ):
        """Consume KE3 once; HTTP proof alone never makes a host result ready."""
        if (_http is None) != (_final_reply is None):
            return False
        with self._step(
            account_origin,
            account_nonce,
            "proof",
            "finishing",
            challenge=challenge,
            valid=_frame(ke3, _SIZES.ke3),
            http=_http,
        ) as admitted:
            if admitted is None:
                return False
            _, proof = admitted
            cutoff = self._request_deadline(deadline)
            _checkpoint(check, cutoff)
            if proof.finish(ke3) is not True:
                return False
            _checkpoint(check, cutoff)
            if not self._drain_workers():
                return False
            _checkpoint(check, cutoff)
            with self._lock:
                if (
                    monotonic() >= cutoff
                    or not self._current_locked("finishing")
                    or self._cleanup_uncertain
                ):
                    return False
                self._challenge = None
                if _http is None:
                    self._phase = "fixture-ready"
                else:
                    self._final_reply, self._write_cutoff = _final_reply, cutoff
                    self._phase = "awaiting-write"
                return True
        return False

    def _begin_final_write(self, http, reply, writer):
        with self._lock:
            if (
                http is not None
                and http is self._http
                and writer is not None
                and self._current_locked("awaiting-write")
                and reply is self._final_reply
                and not self._busy
                and self._workers_drained
                and not self._cleanup_uncertain
            ):
                self._writer = writer
                self._phase = "writing"
                return self._write_cutoff
        self._drain_closed()
        return None

    def _end_final_write(self, http, writer, *, deadline=None):
        """Release one writer; only checked delivery supplies a cutoff here."""
        with self._lock:
            if writer is None or writer is not self._writer or http is not self._http:
                return False
            self._writer = None
            if (
                deadline is not None
                and self._current_locked("writing")
                and monotonic() < min(self._write_cutoff, deadline)
            ):
                self._final_reply = self._write_cutoff = None
                self._phase = "delivered"
                return True
            self._close_locked()
        self._drain_closed()
        return False

    def _abort_final_reply(self, http, reply):
        with self._lock:
            if reply is not None and http is self._http and reply is self._final_reply:
                self._close_locked()
        self._drain_closed()

    def take_for_fixture(self):
        """Take only the synthetic direct round-trip bytes, not a host result."""
        with self._lock:
            if (
                self._http is None
                and self._current_locked("fixture-ready")
                and not self._busy
            ):
                record = self._record
                self._close_locked()
                return record
        self._drain_closed()
        return None

    def take_for_host(self, *, expected_review=None):
        """Take the original reviewed result once, only after checked delivery.

        A host commit attempt supplies its exact review. An early attempt for
        that review retires it; a different review cannot consume it.
        """
        with self._lock:
            if expected_review is not None and self._review is not expected_review:
                return None
            if (
                self._http is not None
                and self._current_locked("delivered")
                and not self._busy
                and self._workers_drained
                and not self._cleanup_uncertain
            ):
                result = VerifiedEnrollment(
                    self._review, self._record, self._taken_is_current
                )
                self._phase = "taken"
                self._review = self._record = None
                return result
            if expected_review is not None:
                self._close_locked()
        self._drain_closed()
        return None

    def _taken_is_current(self):
        with self._lock:
            return self._current_locked("taken")

    def expire(self):
        with self._lock:
            self._current_locked("closed")
        self._drain_closed()

    def cancel(self):
        """Fence on request loss and attempt the first cleanup, never retry it."""
        with self._lock:
            self._close_locked()
        self._drain_closed()

    def close(self):
        """Retire the approval; an explicit recheck can drain only these workers."""
        with self._lock:
            self._close_locked()
        self._drain_workers(recheck=True)
        return self.drained()
