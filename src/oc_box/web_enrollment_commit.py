"""Unserved host-only commit of one checked browser enrollment result."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import current_thread, main_thread

if __package__:
    from . import secret_prompt, web_accounts, web_enrollment, web_named_listeners
else:
    import secret_prompt
    import web_accounts
    import web_enrollment
    import web_named_listeners


class EnrollmentCommitError(ValueError):
    """Fixed host refusal, without account, path, protocol or secret data."""


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class HeldEnrollmentScope:
    """Explicit trusted-host contract, not proof by construction.

    Bind the original approval and review, not another browser proof for
    the same account. The producer must independently verify the full review
    scope and retain exclusion until commit returns or raises. For every
    affected owner, require verified stopped/absent state and exclusion of
    restart, or a separately proved exclusive scope with conflicting
    admission fenced and affected browser grants revoked and drained.

    ``current()`` is only a bounded, read-only recheck of that same held
    scope. It must not acquire locks, stop owners, access credentials, or
    reopen transports. It is called even with the account store lock held.
    No production scope producer is selected or implemented here.
    """

    approval: web_enrollment.EnrollmentApproval
    review: web_accounts.RegistrationReview
    current: Callable[[], bool]


def _review_text(review):
    username = web_accounts._username(review.username)
    scope = web_accounts._scope(review.workspace_ids)
    path = review._path
    if (
        type(review.workspace_ids) is not tuple
        or scope != review.workspace_ids
        or type(review.replacing) is not bool
        or not isinstance(path, Path)
        or path.anchor != "/"
        or path.parent == Path("/")
        or ".." in path.parts
        or "\0" in str(path)
        or len(os.fsencode(path)) > 4096
    ):
        raise EnrollmentCommitError("Invalid host web enrollment review.")
    action = (
        "Reset named web account" if review.replacing else "Enroll named web account"
    )
    return (
        f"\n{action}: {username}\n"
        f"Host web account store: {str(path)!a}\n"
        "Exact workspace scope:\n" + "".join(f"  {item}\n" for item in scope)
    )


def _require_foreground(fd):
    if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
        raise OSError


def _confirm_and_unlock(review, check):
    text = _review_text(review)
    phrase = "RESET" if review.replacing else "ENROLL"
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
        try:
            _require_foreground(fd)
            secret_prompt.write(fd, text)
            if (
                secret_prompt.read_secret(
                    f"Confirm ({phrase}): ", allow_reveal=False, maximum=32
                )
                != phrase
            ):
                raise EnrollmentCommitError("Host web enrollment was not confirmed.")
            check()
            _require_foreground(fd)
            return secret_prompt.read_secret(
                "Host web account store passphrase: ", allow_reveal=False
            )
        finally:
            os.close(fd)
    except (OSError, EOFError):
        raise EnrollmentCommitError(
            "Confirm host web enrollment on the controlling terminal."
        ) from None


def commit_enrollment(approval, *, scope):
    """Make one exact, locally confirmed commit attempt; never retry.

    Call only on the ordinary host foreground after checked reply delivery.
    No browser data, secret supplier, default store or default owner lookup is
    accepted. The caller retains the external owner/cleanup fence, even if an
    early attempt retires an approval that has not confirmed drain.
    """
    if (
        not web_named_listeners._host_allowed()
        or current_thread() is not main_thread()
        or secret_prompt.operation_active()
    ):
        raise EnrollmentCommitError(
            "Web enrollment commit requires the host foreground."
        )
    if (
        type(approval) is not web_enrollment.EnrollmentApproval
        or type(scope) is not HeldEnrollmentScope
        or scope.approval is not approval
        or type(scope.review) is not web_accounts.RegistrationReview
        or not callable(scope.current)
    ):
        raise EnrollmentCommitError(
            "An exact, independently held enrollment scope is required."
        )
    result = approval.take_for_host(expected_review=scope.review)
    if result is None:
        raise EnrollmentCommitError(
            "No current delivered web enrollment for this review."
        )

    def current():
        try:
            return (
                result.current() is True
                and scope.current() is True
                and result.current() is True
            )
        except Exception:  # noqa: BLE001 - uncertain host scope fails closed.
            return False

    def check():
        if not current():
            raise EnrollmentCommitError("Host web enrollment is no longer current.")

    try:
        check()
        passphrase = _confirm_and_unlock(result.review, check)
        try:
            check()
            web_accounts.commit_registration(
                result.review._path,
                passphrase,
                result.review,
                registration=result.registration,
                current=current,
            )
        finally:
            del passphrase
    finally:
        # A taken result has already drained its workers/writer. Retire only
        # the approval we consumed; a mismatched review cannot cancel it.
        approval.cancel()
