import base64
import math
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from oc_box import (
    web_accounts,
    web_admission,
    web_bootstrap,
    web_enrollment,
    web_local_origins,
    web_pake_profile,
    web_pake_worker,
    web_registration_worker,
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
KE1, KE2, KE3 = b"1" * 96, b"2" * 320, b"3" * 64
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
BROWSER = {"account_origin": ACCOUNT, "account_nonce": NONCE}


class EnrollmentFixture(unittest.TestCase):
    def setUp(self):
        self.respond = Mock(return_value=RESPONSE)
        self.validate = Mock(return_value=RECORD)
        self.registrar_close = Mock(return_value=True)
        self.exchange = Mock()
        self.exchange.finish.return_value = True
        self.verifier = Mock()
        self.verifier.start.return_value = (KE2, self.exchange)
        self.verifier.close.return_value = True
        for attr, replacement in (
            ("respond", self.respond),
            ("validate_upload", self.validate),
            ("close", self.registrar_close),
        ):
            patcher = patch.object(
                web_registration_worker.RegistrationWorker, attr, replacement
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def profile(self, origins=ORIGINS):
        with patch.object(web_pake_worker, "PakeWorker", return_value=self.verifier):
            return web_pake_profile.LocalPakeProfile(
                8, expected_sha256=b"d" * 32, origins=origins
            )

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
            binding, auth_origin=origins.account.value, account_nonce=NONCE
        )
        self.assertIsNotNone(claim)
        return bootstrap, binding, claim

    def approval(self, **edits):
        if "claim" not in edits:
            edits["claim"] = self.claim()[2]
        result = web_enrollment.EnrollmentApproval(
            **(
                {
                    "review": REVIEW,
                    "origins": ORIGINS,
                    "registration_worker": object.__new__(
                        web_registration_worker.RegistrationWorker
                    ),
                    "proof_profile": self.profile(),
                }
                | edits
            )
        )
        self.addCleanup(result.close)
        return result

    def start(self, approval, **edits):
        return approval.start(**(BROWSER | {"request": REQUEST} | edits))

    def prepare(self, approval, reply, **edits):
        return approval.start_proof(
            **(
                BROWSER
                | {"challenge": reply.challenge, "registration": RECORD, "ke1": KE1}
                | edits
            )
        )

    def finish(self, approval, reply, **edits):
        return approval.finish_proof(
            **(BROWSER | {"challenge": reply.challenge, "ke3": KE3} | edits)
        )

    def retire(self, bootstrap, binding):
        self.assertTrue(
            bootstrap.retire(binding, auth_origin=ACCOUNT, account_nonce=NONCE)
        )


class EnrollmentApprovalTests(EnrollmentFixture):
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
            kwargs,
            {
                "setup": REVIEW.setup,
                "registration_request": REQUEST,
                "credential_identifier": identifiers["credential_identifier"],
            },
        )
        self.assertFalse(self.finish(approval, reply))  # Out of order; no proof yet.
        self.assertIsNone(self.prepare(approval, reply, account_nonce=OTHER_BROWSER))
        self.assertIsNone(self.prepare(approval, reply, challenge=token(1)))
        self.validate.assert_not_called()
        self.verifier.start.assert_not_called()
        self.assertIsNone(self.start(approval))
        with patch.object(web_accounts, "commit_registration") as commit:
            proof = self.prepare(approval, reply)
            self.assertIsNone(approval.take_for_host())
            self.assertFalse(approval.drained())  # Live attempt between browser rounds.
            self.assertIsNone(self.prepare(approval, reply))
            self.assertFalse(self.finish(approval, reply, account_nonce=OTHER_BROWSER))
            self.assertFalse(self.finish(approval, reply, challenge=token(3)))
            self.assertFalse(
                self.finish(
                    approval, reply, account_origin="https://accounts.ocbox.test:9445"
                )
            )
            self.exchange.finish.assert_not_called()
            self.assertTrue(self.finish(approval, reply))
            commit.assert_not_called()
        self.validate.assert_called_once_with(RECORD, deadline=deadline)
        context = _identifier(
            b"enrollment-proof", f"{ACCOUNT}\n{reply.challenge}".encode("ascii")
        )
        self.assertEqual((proof.ke2, proof.context), (KE2, context))
        self.assertNotIn(repr(context), repr(proof))
        self.verifier.start.assert_called_once_with(
            setup=REVIEW.setup,
            registration=RECORD,
            context=context,
            ke1=KE1,
            deadline=deadline,
            **identifiers,
        )
        self.exchange.finish.assert_called_once_with(KE3)
        self.registrar_close.assert_called_once()
        self.verifier.close.assert_called_once()
        self.assertTrue(approval.drained())
        self.assertFalse(self.finish(approval, reply))

        accepted = approval.take_for_host()
        self.assertIs(accepted.review, REVIEW)
        self.assertEqual(accepted.registration, RECORD)
        self.assertNotIn(REVIEW.username, repr(accepted))
        self.assertTrue(accepted.current())
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.close())
        self.assertFalse(accepted.current())

    def test_structural_upload_without_possession_is_not_a_verified_enrollment(self):
        self.exchange.finish.return_value = False
        approval = self.approval()
        reply = self.start(approval)
        self.assertIsNotNone(self.prepare(approval, reply))
        self.assertFalse(self.finish(approval, reply))
        self.assertFalse(self.finish(approval, reply))
        self.exchange.finish.assert_called_once()
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.drained())
        self.assertFalse(approval.cleanup_failed)

    @unittest.skipUnless(
        find_spec("cryptography"), "optional cryptography not installed"
    )
    def test_verified_result_preserves_the_original_store_cas(self):
        phrase = "synthetic enrollment store passphrase"
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            TemporaryDirectory() as directory,
        ):
            path = Path(directory) / "accounts"
            web_accounts.initialize(path, phrase, setup=REVIEW.setup)
            review = web_accounts.review_registration(
                path,
                phrase,
                REVIEW.username,
                workspace_ids=REVIEW.workspace_ids,
            )
            previous = path.read_bytes()
            approval = self.approval(review=review)
            reply = self.start(approval)
            self.assertIsNotNone(self.prepare(approval, reply))
            self.assertIsNone(approval.take_for_host())
            self.assertEqual(path.read_bytes(), previous)
            self.assertTrue(self.finish(approval, reply))
            self.assertEqual(path.read_bytes(), previous)
            result = approval.take_for_host()
            self.assertIs(result.review, review)

            web_accounts.add_account(
                path,
                phrase,
                "other-synthetic",
                workspace_ids=REVIEW.workspace_ids,
                registration=b"synthetic other record",
            )
            replaced = path.read_bytes()
            self.assertTrue(
                result.current()
            )  # Bootstrap is live; store review is stale.
            with self.assertRaisesRegex(
                web_accounts.WebAccountsError, "no longer current"
            ):
                web_accounts.commit_registration(
                    path,
                    phrase,
                    result.review,
                    registration=result.registration,
                    current=result.current,
                )
            self.assertEqual(path.read_bytes(), replaced)

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
        self.assertIsNone(bootstrap.claim_app(binding, token(5)))
        approval = self.approval(claim=claim)
        with self.assertRaises(ValueError):
            self.approval(claim=claim)
        reply = self.start(approval)
        self.assertIsNotNone(self.prepare(approval, reply))
        self.assertTrue(self.finish(approval, reply))
        result = approval.take_for_host()
        self.assertTrue(result.current())
        self.retire(bootstrap, binding)
        self.assertFalse(result.current())

    def test_scope_and_exact_account_app_pair_must_match_the_reviewed_profile(self):
        with self.assertRaises(ValueError):
            self.approval(claim=self.claim(workspace_id="b" * 32)[2])
        different_account = web_local_origins.LocalOrigins(account_port=9445)
        different_app = web_local_origins.LocalOrigins(workspace_port=9445)
        for origins in (different_account, different_app):
            with self.assertRaises(ValueError):
                self.approval(proof_profile=self.profile(origins))
        with self.assertRaises(ValueError):
            self.approval(claim=self.claim(origins=different_account)[2])
        with self.assertRaises(ValueError):
            self.approval(
                origins=different_app, proof_profile=self.profile(different_app)
            )
        self.respond.assert_not_called()

    def test_wrong_protocol_frames_and_unconfirmed_validation_close_the_attempt(self):
        for invalid in (b"", b"x" * 33, bytearray(REQUEST)):
            with self.subTest(length=len(invalid)):
                approval = self.approval()
                self.assertIsNone(self.start(approval, request=invalid))
                self.assertIsNone(self.start(approval))
                self.assertTrue(approval.drained())
        self.respond.assert_not_called()

        for edit in (
            {"registration": b"short"},
            {"ke1": b"s" * 97},
            {"ke1": bytearray(KE1)},
        ):
            approval = self.approval()
            reply = self.start(approval)
            self.assertIsNone(self.prepare(approval, reply, **edit))
            self.assertIsNone(self.prepare(approval, reply))
            self.assertTrue(approval.drained())
        self.validate.assert_not_called()

        approval = self.approval()
        reply = self.start(approval)
        self.validate.return_value = b"d" * 192
        self.assertIsNone(self.prepare(approval, reply))
        self.verifier.start.assert_not_called()
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.drained())

    def test_rightful_malformed_ke3_drains_pending_worker_without_a_proof(self):
        approval = self.approval()
        reply = self.start(approval)
        self.assertIsNotNone(self.prepare(approval, reply))
        self.assertFalse(self.finish(approval, reply, ke3=bytearray(KE3)))
        self.assertFalse(self.finish(approval, reply))
        self.exchange.finish.assert_not_called()
        self.verifier.close.assert_called_once()
        self.assertTrue(approval.drained())
        self.assertIsNone(approval.take_for_host())

    def test_expiry_applies_during_backend_work_and_after_proof_before_host_take(self):
        with patch.object(web_enrollment, "monotonic", return_value=10) as clock:
            approval = self.approval(ttl=1)
            self.respond.side_effect = lambda **_: (
                setattr(clock, "return_value", 11) or RESPONSE
            )
            self.assertIsNone(self.start(approval))
            self.assertIsNone(approval.take_for_host())
            self.assertTrue(approval.drained())
            self.respond.side_effect = None

            pending = self.approval(ttl=1)
            reply = self.start(pending)
            self.assertIsNotNone(self.prepare(pending, reply))
            self.assertTrue(self.finish(pending, reply))
            clock.return_value = 12
            pending.expire()
            self.assertIsNone(pending.take_for_host())
            self.assertTrue(pending.drained())

    def test_expiry_or_bootstrap_retirement_drains_an_idle_pending_proof(self):
        for condition in ("expiry", "retirement"):
            with (
                self.subTest(condition=condition),
                patch.object(web_enrollment, "monotonic", return_value=10) as clock,
            ):
                bootstrap, binding, claim = self.claim()
                approval = self.approval(claim=claim, ttl=1)
                reply = self.start(approval)
                self.assertIsNotNone(self.prepare(approval, reply))
                self.assertFalse(approval.drained())
                if condition == "expiry":
                    clock.return_value = 11
                else:
                    self.retire(bootstrap, binding)
                approval.expire()
                self.assertTrue(approval.drained())
                self.assertFalse(self.finish(approval, reply))
                self.assertIsNone(approval.take_for_host())
        self.exchange.finish.assert_not_called()
        self.assertEqual(self.verifier.close.call_count, 2)

    def test_close_cancels_publication_of_a_response_already_in_backend(self):
        enter, allow = Event(), Event()

        def delayed(**_):
            enter.set()
            if not allow.wait(5):
                raise TimeoutError
            return RESPONSE

        self.respond.side_effect = delayed
        approval = self.approval()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.start, approval)
            try:
                self.assertTrue(enter.wait(5))
                self.assertFalse(approval.drained())
                self.assertIsNone(self.start(approval))
                self.assertFalse(approval.close())
                self.assertFalse(approval.drained())
            finally:
                allow.set()
            self.assertIsNone(result.result(5))
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.drained())

    def test_retirement_prevents_late_ke2_and_late_verified_ke3_publication(self):
        for phase in ("begin-proof", "finish-proof"):
            with self.subTest(phase=phase):
                enter, allow = Event(), Event()
                target = (
                    self.verifier.start
                    if phase == "begin-proof"
                    else self.exchange.finish
                )
                value = target.return_value

                def delayed(*_, **__):
                    enter.set()
                    if not allow.wait(5):
                        raise TimeoutError
                    return value

                bootstrap, binding, claim = self.claim()
                approval = self.approval(claim=claim)
                reply = self.start(approval)
                if phase == "finish-proof":
                    self.assertIsNotNone(self.prepare(approval, reply))
                target.side_effect = delayed
                operation = self.prepare if phase == "begin-proof" else self.finish
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(operation, approval, reply)
                    try:
                        self.assertTrue(enter.wait(5))
                        self.retire(bootstrap, binding)
                        approval.expire()
                        self.assertFalse(approval.drained())
                        self.assertIsNone(approval.take_for_host())
                    finally:
                        allow.set()
                    self.assertIs(
                        result.result(5), None if phase == "begin-proof" else False
                    )
                target.side_effect = None
                self.assertIsNone(approval.take_for_host())
                self.assertTrue(approval.drained())

    def test_uncertain_worker_cleanup_is_latched_and_only_same_workers_can_drain(self):
        self.respond.side_effect = web_registration_worker.RegistrationCleanupError
        self.registrar_close.side_effect = (False, True)
        approval = self.approval()
        with self.assertRaisesRegex(
            web_enrollment.EnrollmentCleanupError,
            r"^Web enrollment worker cleanup was not confirmed\.$",
        ):
            self.start(approval)
        self.assertIsNone(self.start(approval))
        self.assertIsNone(approval.take_for_host())
        self.assertTrue(approval.cleanup_failed)
        self.assertFalse(approval.drained())
        self.registrar_close.assert_called_once()  # No automatic retry.
        self.assertTrue(approval.close())
        self.assertTrue(approval.drained())
        self.assertTrue(approval.cleanup_failed)
        self.assertIsNone(approval.take_for_host())

    def test_late_validator_cleanup_failure_remains_fenced_after_bootstrap_retirement(
        self,
    ):
        bootstrap, binding, claim = self.claim()
        enter, allow = Event(), Event()

        def delayed(*_, **__):
            enter.set()
            if not allow.wait(5):
                raise TimeoutError
            raise web_registration_worker.RegistrationCleanupError

        self.validate.side_effect = delayed
        self.registrar_close.return_value = False
        approval = self.approval(claim=claim)
        reply = self.start(approval)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.prepare, approval, reply)
            try:
                self.assertTrue(enter.wait(5))
                self.retire(bootstrap, binding)
                self.assertIsNone(approval.take_for_host())
                self.assertFalse(approval.drained())
            finally:
                allow.set()
            with self.assertRaises(web_enrollment.EnrollmentCleanupError):
                result.result(5)
        self.verifier.start.assert_not_called()
        self.assertTrue(approval.cleanup_failed)
        self.assertFalse(approval.drained())
        self.assertIsNone(approval.take_for_host())

    def test_failed_proof_worker_close_blocks_an_apparently_valid_proof(self):
        self.verifier.close.side_effect = (False, True)
        approval = self.approval()
        reply = self.start(approval)
        self.assertIsNotNone(self.prepare(approval, reply))
        self.assertFalse(self.finish(approval, reply))
        self.assertTrue(approval.cleanup_failed)
        self.assertFalse(approval.drained())
        self.assertTrue(approval.close())
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
            {"registration_worker": None},
            {"proof_profile": None},
        ):
            with self.subTest(fields=tuple(options)), self.assertRaises(ValueError):
                self.approval(**options)
