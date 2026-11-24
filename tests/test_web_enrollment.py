import base64
import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from oc_box import (
    web_accounts,
    web_admission,
    web_bootstrap,
    web_enrollment,
    web_local_origins,
    web_pake_worker,
)
from oc_box.web_pake_profile import _identifier


def token(byte):
    return base64.urlsafe_b64encode(bytes((byte,)) * 32).decode().rstrip("=")


ORIGINS = web_local_origins.LocalOrigins()
ACCOUNT = ORIGINS.account.value
NONCE = token(8)
OTHER_BROWSER = token(9)
REQUEST = b"a" * 32
RESPONSE = b"b" * 64
RECORD = b"c" * 192
REVIEW = web_accounts.RegistrationReview(
    Path("/synthetic/accounts"),
    (1, 2),
    (3, 4),
    "synthetic-account",
    ("a" * 32,),
    b"s" * 128,
    False,
    b"d" * 32,
)


class EnrollmentApprovalTests(unittest.TestCase):
    def setUp(self):
        self.respond = Mock(return_value=RESPONSE)
        self.validate = Mock(return_value=RECORD)
        self.prove = Mock(return_value=True)

    def claim(self, workspace_id=REVIEW.workspace_ids[0], origins=ORIGINS):
        binding = web_admission.WebBinding(
            workspace_id, "synthetic-owner", origins.workspace.value
        )
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        with patch.object(
            web_bootstrap.secrets,
            "token_urlsafe",
            side_effect=[token(4), token(5), NONCE],
        ):
            ticket = bootstrap.issue(binding)
            self.assertEqual(
                bootstrap.accept(
                    binding, auth_origin=origins.account.value, ticket=ticket.value
                ),
                NONCE,
            )
        claim = bootstrap.claim_enrollment(
            binding,
            auth_origin=origins.account.value,
            account_nonce=NONCE,
        )
        self.assertIsNotNone(claim)
        return bootstrap, binding, claim

    def approval(self, **edits):
        if "claim" not in edits:
            edits["claim"] = self.claim()[2]
        return web_enrollment.EnrollmentApproval(
            **(
                {
                    "review": REVIEW,
                    "origins": ORIGINS,
                    "respond": self.respond,
                    "validate": self.validate,
                    "prove": self.prove,
                }
                | edits
            )
        )

    def start(self, approval, **edits):
        return approval.start(
            **(
                {"account_origin": ACCOUNT, "account_nonce": NONCE, "request": REQUEST}
                | edits
            )
        )

    def finish(self, approval, reply, **edits):
        return approval.finish(
            **(
                {
                    "account_origin": ACCOUNT,
                    "account_nonce": NONCE,
                    "challenge": reply.challenge,
                    "registration": RECORD,
                }
                | edits
            )
        )

    def test_exact_profile_and_binding_produce_host_only_single_use_result(self):
        approval = self.approval()
        self.assertIsNone(self.start(approval, account_origin=ORIGINS.workspace.value))
        self.assertIsNone(self.start(approval, account_nonce=OTHER_BROWSER))
        self.respond.assert_not_called()
        self.assertIsNone(approval.take_for_host())

        reply = self.start(approval)
        self.assertEqual(reply.response, RESPONSE)
        self.assertNotIn(reply.challenge, repr(reply))
        identifiers = {
            "credential_identifier": _identifier(
                b"credential", REVIEW.username.encode()
            ),
            "client_identifier": _identifier(b"client", REVIEW.username.encode()),
            "verifier_identifier": _identifier(b"verifier", ACCOUNT.encode("ascii")),
        }
        kwargs = dict(self.respond.call_args.kwargs)
        deadline = kwargs.pop("deadline")
        self.assertEqual(
            kwargs, {"setup": REVIEW.setup, "request": REQUEST, **identifiers}
        )
        self.assertFalse(self.finish(approval, reply, account_nonce=OTHER_BROWSER))
        self.assertFalse(self.finish(approval, reply, challenge=token(1)))
        self.validate.assert_not_called()
        self.prove.assert_not_called()
        self.assertIsNone(self.start(approval))
        self.assertTrue(self.finish(approval, reply))
        self.validate.assert_called_once_with(
            registration=RECORD, deadline=deadline, **identifiers
        )
        self.prove.assert_called_once_with(
            setup=REVIEW.setup,
            registration=RECORD,
            context=_identifier(
                b"enrollment-proof", f"{ACCOUNT}\n{reply.challenge}".encode("ascii")
            ),
            deadline=deadline,
            **identifiers,
        )
        self.assertFalse(self.finish(approval, reply))
        accepted = approval.take_for_host()
        self.assertIs(accepted.review, REVIEW)
        self.assertEqual(accepted.registration, RECORD)
        self.assertNotIn(REVIEW.username, repr(accepted))
        self.assertTrue(accepted.current())
        self.assertIsNone(approval.take_for_host())
        approval.close()
        self.assertFalse(accepted.current())

    def test_structural_upload_without_possession_is_not_a_verified_enrollment(self):
        self.prove.return_value = False
        approval = self.approval()
        reply = self.start(approval)
        self.assertFalse(self.finish(approval, reply))
        self.validate.assert_called_once()
        self.prove.assert_called_once()
        self.assertIsNone(approval.take_for_host())

    def test_account_bootstrap_cannot_start_login_or_be_reused_for_another_approval(
        self,
    ):
        bootstrap, binding, claim = self.claim()
        self.assertIsNone(
            bootstrap.app_nonce(binding, auth_origin=ACCOUNT, account_nonce=NONCE)
        )
        self.assertIsNone(
            bootstrap.claim_enrollment(
                binding, auth_origin=ACCOUNT, account_nonce=NONCE
            )
        )
        approval = self.approval(claim=claim)
        with self.assertRaises(ValueError):
            self.approval(claim=claim)
        reply = self.start(approval)
        self.assertTrue(self.finish(approval, reply))
        self.assertTrue(
            bootstrap.retire(binding, auth_origin=ACCOUNT, account_nonce=NONCE)
        )
        self.assertIsNone(approval.take_for_host())

    def test_scope_and_exact_account_port_must_match_the_reviewed_profile(self):
        with self.assertRaises(ValueError):
            self.approval(claim=self.claim(workspace_id="b" * 32)[2])
        with self.assertRaises(ValueError):
            self.approval(
                claim=self.claim(
                    origins=web_local_origins.LocalOrigins(account_port=9445)
                )[2],
            )
        self.respond.assert_not_called()

    def test_wrong_protocol_frames_and_unconfirmed_validation_close_the_attempt(self):
        for invalid in (b"", b"x" * 33, bytearray(REQUEST)):
            with self.subTest(length=len(invalid)):
                approval = self.approval()
                self.assertIsNone(self.start(approval, request=invalid))
                self.assertIsNone(self.start(approval))
        self.respond.assert_not_called()

        approval = self.approval()
        reply = self.start(approval)
        self.assertFalse(self.finish(approval, reply, registration=b"short"))
        self.validate.assert_not_called()
        self.assertFalse(self.finish(approval, reply))
        self.assertIsNone(approval.take_for_host())

        approval = self.approval()
        reply = self.start(approval)
        self.validate.return_value = b"d" * 192
        self.assertFalse(self.finish(approval, reply))
        self.assertIsNone(approval.take_for_host())

    def test_expiry_applies_during_backend_work_and_after_upload_before_host_take(self):
        with patch.object(web_enrollment, "monotonic", return_value=10) as clock:
            approval = self.approval(ttl=1)
            self.respond.side_effect = lambda **_: (
                setattr(clock, "return_value", 11) or RESPONSE
            )
            self.assertIsNone(self.start(approval))
            self.assertIsNone(approval.take_for_host())
            self.respond.side_effect = None

            pending = self.approval(ttl=1)
            reply = self.start(pending)
            self.assertTrue(self.finish(pending, reply))
            clock.return_value = 12
            pending.expire()
            self.assertIsNone(pending.take_for_host())

    def test_close_cancels_publication_of_a_response_already_in_backend(self):
        enter, allow = Event(), Event()

        def delayed(**_):
            enter.set()
            if not allow.wait(5):
                raise TimeoutError
            return RESPONSE

        approval = self.approval(respond=delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.start, approval)
            try:
                self.assertTrue(enter.wait(5))
                self.assertFalse(approval.drained())
                self.assertIsNone(self.start(approval))
                approval.close()
                self.assertFalse(approval.drained())
            finally:
                allow.set()
            self.assertIsNone(result.result(5))
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.drained())

    def test_uncertain_worker_cleanup_is_not_treated_as_an_ordinary_refusal(self):
        self.respond.side_effect = web_pake_worker.PakeWorkerCleanupError
        approval = self.approval()
        with self.assertRaises(web_pake_worker.PakeWorkerCleanupError):
            self.start(approval)
        self.assertIsNone(self.start(approval))
        self.assertIsNone(approval.take_for_host())
        self.assertFalse(approval.drained())

    def test_late_validator_cleanup_failure_remains_fenced_after_bootstrap_retirement(
        self,
    ):
        bootstrap, binding, claim = self.claim()
        enter, allow = Event(), Event()

        def delayed(**_):
            enter.set()
            if not allow.wait(5):
                raise TimeoutError
            raise web_pake_worker.PakeWorkerCleanupError

        approval = self.approval(claim=claim, validate=delayed)
        reply = self.start(approval)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.finish, approval, reply)
            try:
                self.assertTrue(enter.wait(5))
                self.assertTrue(
                    bootstrap.retire(binding, auth_origin=ACCOUNT, account_nonce=NONCE)
                )
                self.assertIsNone(approval.take_for_host())
                self.assertFalse(approval.drained())
            finally:
                allow.set()
            with self.assertRaises(web_pake_worker.PakeWorkerCleanupError):
                result.result(5)
        self.prove.assert_not_called()
        self.assertFalse(approval.drained())
        self.assertIsNone(approval.take_for_host())

    def test_rejects_untrusted_configuration_and_unbounded_lifetimes(self):
        for options in (
            {"review": None},
            {"origins": ACCOUNT},
            {"claim": "bad"},
            {"ttl": 0},
            {"ttl": 121},
            {"ttl": math.nan},
            {"ttl": True},
            {"validate": None},
            {"respond": None},
            {"prove": None},
        ):
            with self.subTest(fields=tuple(options)), self.assertRaises(ValueError):
                self.approval(**options)
