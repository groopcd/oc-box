"""Host-only enrollment writes with synthetic scopes and PAKE workers."""

import errno
import os
import pty
import select
import signal
import socket
import stat
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_web_enrollment import RECORD, REVIEW
from test_web_enrollment_ingress import FINISH, EnrollmentIngressFixture, b64

from oc_box import (
    secret_prompt,
    web_accounts,
    web_enrollment,
    web_named_headers,
)
from oc_box import (
    web_enrollment_commit as host,
)
from oc_box import (
    web_enrollment_ingress as ingress,
)

STORE_PHRASE = "synthetic named account store passphrase"
FULL_SCOPE = (*REVIEW.workspace_ids, "b" * 32)


class HostEnrollmentFixture(EnrollmentIngressFixture):
    def setUp(self):
        super().setUp()
        allowed = patch.object(
            host.web_named_listeners, "_host_allowed", return_value=True
        )
        allowed.start()
        self.addCleanup(allowed.stop)

    @contextmanager
    def final(self):
        challenge = self.prepared()
        writer, reader = socket.socketpair()
        with writer, reader:
            response = self.send(FINISH, self.proof_finish(challenge), stream=writer)
            self.assertEqual(response.status, 200)
            yield writer, response

    def write_final(self, writer, response):
        return ingress.write_enrollment_reply(
            writer, response, monotonic() + 2, check=lambda: True
        )

    def delivered(self):
        with self.final() as (writer, response):
            self.assertEqual(self.write_final(writer, response), 200)

    def scope(self, *, review=REVIEW, current=lambda: True):
        # There is no workspace or owner in this fixture.
        return host.HeldEnrollmentScope(self.reviewed, review, current)

    @contextmanager
    def prompts(self, *answers, foreground=True):
        # Mock only the terminal boundary; do not mock guard or mutation.
        with TemporaryFile() as terminal:
            tty_os = SimpleNamespace(
                O_RDWR=os.O_RDWR,
                O_CLOEXEC=os.O_CLOEXEC,
                O_NOCTTY=os.O_NOCTTY,
                fsencode=os.fsencode,
                close=os.close,
                open=lambda *_: os.dup(terminal.fileno()),
                isatty=lambda _: True,
                tcgetpgrp=lambda _: 10 if foreground else 20,
                getpgrp=lambda: 10,
            )
            with (
                patch.object(host, "os", tty_os),
                patch.object(secret_prompt, "read_secret", side_effect=answers) as read,
            ):
                yield read


class HostEnrollmentCommitTests(HostEnrollmentFixture):
    def test_exact_attempt_before_wire_acknowledgement_retires_the_approval(self):
        with (
            self.final() as (writer, response),
            patch.object(web_accounts, "commit_registration") as commit,
            self.prompts() as prompt,
        ):
            with self.assertRaisesRegex(host.EnrollmentCommitError, "No current"):
                host.commit_enrollment(self.reviewed, scope=self.scope())
            with self.assertRaises(web_named_headers.NamedRequestError):
                self.write_final(writer, response)
            with self.assertRaisesRegex(host.EnrollmentCommitError, "No current"):
                host.commit_enrollment(self.reviewed, scope=self.scope())
        self.assertTrue(self.reviewed.drained())
        prompt.assert_not_called()
        commit.assert_not_called()

    def test_scope_must_bind_the_original_review_not_an_equivalent_copy(self):
        self.delivered()
        guard = Mock(return_value=True)
        with (
            self.prompts() as prompt,
            patch.object(web_accounts, "commit_registration") as commit,
        ):
            for other in (
                replace(REVIEW),
                replace(REVIEW, _path=Path("/other/accounts")),
                replace(REVIEW, username="different-account"),
                replace(REVIEW, workspace_ids=FULL_SCOPE),
                replace(REVIEW, replacing=True),
            ):
                with self.assertRaisesRegex(host.EnrollmentCommitError, "No current"):
                    host.commit_enrollment(
                        self.reviewed, scope=self.scope(review=other, current=guard)
                    )
            for invalid in (True, guard):
                with self.assertRaisesRegex(host.EnrollmentCommitError, "held"):
                    host.commit_enrollment(self.reviewed, scope=invalid)
            with self.assertRaisesRegex(host.EnrollmentCommitError, "held"):
                host.commit_enrollment(
                    web_enrollment.VerifiedEnrollment(REVIEW, RECORD, guard),
                    scope=self.scope(),
                )
        self.assertIsNotNone(self.reviewed.take_for_host())
        prompt.assert_not_called()
        guard.assert_not_called()
        commit.assert_not_called()

    def test_scope_cannot_switch_to_another_browser_approval_for_the_same_review(self):
        self.delivered()
        original, scope = self.reviewed, self.scope()
        self.bind()
        self.delivered()
        with (
            self.prompts() as prompt,
            patch.object(web_accounts, "commit_registration") as commit,
            self.assertRaisesRegex(host.EnrollmentCommitError, "held"),
        ):
            host.commit_enrollment(self.reviewed, scope=scope)
        self.assertIsNotNone(original.take_for_host())
        self.assertIsNotNone(self.reviewed.take_for_host())
        prompt.assert_not_called()
        commit.assert_not_called()

    def test_nonhost_http_thread_and_dashboard_do_not_enter_the_host_path(self):
        self.delivered()
        with (
            self.prompts() as prompt,
            patch.object(web_accounts, "commit_registration") as commit,
        ):
            for refusal in (
                patch.object(
                    host.web_named_listeners, "_host_allowed", return_value=False
                ),
                secret_prompt.operation_prompts(Mock()),
            ):
                with (
                    refusal,
                    self.assertRaisesRegex(
                        host.EnrollmentCommitError, "host foreground"
                    ),
                ):
                    host.commit_enrollment(self.reviewed, scope=self.scope())
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(
                    host.commit_enrollment, self.reviewed, scope=self.scope()
                )
                with self.assertRaisesRegex(
                    host.EnrollmentCommitError, "host foreground"
                ):
                    task.result(timeout=2)
        self.assertIsNotNone(self.reviewed.take_for_host())
        prompt.assert_not_called()
        commit.assert_not_called()

    def test_denial_terminal_loss_and_interrupt_spend_attempt_without_unlock(self):
        for answer, error in (
            ("no", "not confirmed"),
            ("RESET", "not confirmed"),
            (EOFError("synthetic private tty error"), "controlling terminal"),
            (KeyboardInterrupt(), None),
        ):
            with self.subTest(error=error):
                self.bind()
                self.delivered()
                with (
                    self.prompts(answer) as prompt,
                    patch.object(web_accounts, "commit_registration") as commit,
                ):
                    refusal = (
                        self.assertRaises(KeyboardInterrupt)
                        if error is None
                        else self.assertRaisesRegex(host.EnrollmentCommitError, error)
                    )
                    with refusal:
                        host.commit_enrollment(self.reviewed, scope=self.scope())
                    with self.assertRaisesRegex(
                        host.EnrollmentCommitError, "No current"
                    ):
                        host.commit_enrollment(self.reviewed, scope=self.scope())
                prompt.assert_called_once_with(
                    "Confirm (ENROLL): ", allow_reveal=False, maximum=32
                )
                commit.assert_not_called()

    def test_background_tty_cannot_confirm_or_unlock(self):
        self.delivered()
        with (
            self.prompts(foreground=False) as prompt,
            patch.object(web_accounts, "commit_registration") as commit,
            self.assertRaisesRegex(host.EnrollmentCommitError, "controlling terminal"),
        ):
            host.commit_enrollment(self.reviewed, scope=self.scope())
        self.assertIsNone(self.reviewed.take_for_host())
        prompt.assert_not_called()
        commit.assert_not_called()

    def test_uncertain_or_lost_exclusion_aborts_before_unlock(self):
        for status in (False, None, 1, OSError("private scope diagnostics"), True):
            with self.subTest(status=status):
                self.bind()
                self.delivered()
                # True once models loss while the explicit review is on screen.
                current = Mock(side_effect=(status, False))
                with (
                    self.prompts("ENROLL") as prompt,
                    patch.object(web_accounts, "commit_registration") as commit,
                    self.assertRaisesRegex(
                        host.EnrollmentCommitError, "no longer current"
                    ),
                ):
                    host.commit_enrollment(
                        self.reviewed, scope=self.scope(current=current)
                    )
                self.assertEqual(prompt.call_count, int(status is True))
                self.assertIsNone(self.reviewed.take_for_host())
                commit.assert_not_called()

    def test_expiry_during_scope_recheck_prevents_prompt(self):
        now = monotonic()
        with patch.object(web_enrollment, "monotonic", return_value=now) as clock:
            self.bind(ttl=45)
            self.delivered()

            def recheck():
                clock.return_value = now + 46
                return True

            with (
                self.prompts() as prompt,
                patch.object(web_accounts, "commit_registration") as commit,
                self.assertRaisesRegex(host.EnrollmentCommitError, "no longer current"),
            ):
                host.commit_enrollment(self.reviewed, scope=self.scope(current=recheck))
        prompt.assert_not_called()
        commit.assert_not_called()


@unittest.skipUnless(find_spec("cryptography"), "optional cryptography not installed")
class EncryptedHostEnrollmentCommitTests(HostEnrollmentFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        env.start()
        self.addCleanup(env.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts"
        web_accounts.initialize(self.path, STORE_PHRASE, setup=REVIEW.setup)

    def review(self, *, replacing=False):
        review = web_accounts.review_registration(
            self.path,
            STORE_PHRASE,
            REVIEW.username,
            workspace_ids=FULL_SCOPE,
            replacing=replacing,
        )
        self.bind(review=review)
        self.delivered()
        return review

    def proof_start(self, challenge):
        return super().proof_start(challenge) | {
            "registration": b64(self.validate.return_value)
        }

    def run_controlling_terminal(self, scope, command):
        # Synthetic PTY: no CLI, browser, TLS listener or native owner.
        pid, terminal = pty.fork()
        if pid == 0:
            try:
                host.commit_enrollment(self.reviewed, scope=scope)
                try:
                    host.commit_enrollment(self.reviewed, scope=scope)
                except host.EnrollmentCommitError:
                    os._exit(0)
            except BaseException:  # noqa: BLE001 - never unwind the forked test runner.
                os._exit(1)
            os._exit(2)
        output = bytearray()
        answered = 0
        reaped = False
        deadline = monotonic() + 10
        responses = (
            (b"Confirm (", command.encode("ascii") + b"\n"),
            (
                b"Host web account store passphrase",
                b"\x12" + STORE_PHRASE.encode() + b"\n",
            ),
        )
        try:
            while monotonic() < deadline and len(output) < 16 * 1024:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        data = os.read(terminal, 2048)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        data = b""
                    if not data:
                        _, status = os.waitpid(pid, 0)
                        reaped = True
                        self.assertEqual(status, 0)
                        break
                    output.extend(data)
                    if answered < len(responses) and responses[answered][0] in output:
                        os.write(terminal, responses[answered][1])
                        answered += 1
            self.assertTrue(reaped, "enrollment prompt did not finish")
            self.assertEqual(answered, len(responses))
            self.assertNotIn(STORE_PHRASE.encode(), output)
            self.assertNotIn(self.validate.return_value, output)
            self.assertNotIn(REVIEW.setup, output)
            return bytes(output)
        finally:
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(terminal)

    def test_enrollment_and_same_scope_reset_require_masked_foreground_confirmation(
        self,
    ):
        for replacing, action, command in (
            (False, b"Enroll named web account", "ENROLL"),
            (True, b"Reset named web account", "RESET"),
        ):
            with self.subTest(replacing=replacing):
                record = b"r" * len(RECORD) if replacing else RECORD
                self.validate.return_value = record
                review = self.review(replacing=replacing)
                before = self.path.read_bytes()
                output = self.run_controlling_terminal(
                    self.scope(review=review), command
                )
                self.assertIn(action + b": " + REVIEW.username.encode(), output)
                self.assertIn(os.fsencode(self.path), output)
                for ident in FULL_SCOPE:
                    self.assertIn(ident.encode(), output)
                saved = self.path.read_bytes()
                self.assertNotEqual(saved, before)
                self.assertNotIn(STORE_PHRASE.encode(), saved)
                self.assertNotIn(record, saved)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                self.assertEqual(
                    web_accounts.load(self.path, STORE_PHRASE).accounts,
                    (web_accounts.Account(REVIEW.username, FULL_SCOPE, record),),
                )

    def test_stale_review_and_wrong_phrase_do_not_retry_or_change_ciphertext(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                review = self.review()
                if stale:
                    web_accounts.add_account(
                        self.path,
                        STORE_PHRASE,
                        "another-account",
                        workspace_ids=FULL_SCOPE,
                        registration=b"another synthetic record",
                    )
                before = self.path.read_bytes()
                scope = self.scope(review=review)
                with self.prompts(
                    "ENROLL", STORE_PHRASE if stale else "wrong store passphrase"
                ) as prompt:
                    with self.assertRaises(web_accounts.WebAccountsError):
                        host.commit_enrollment(self.reviewed, scope=scope)
                    with self.assertRaisesRegex(
                        host.EnrollmentCommitError, "No current"
                    ):
                        host.commit_enrollment(self.reviewed, scope=scope)
                self.assertEqual(prompt.call_count, 2)
                self.assertEqual(self.path.read_bytes(), before)

    def reject_during_publish(self, loss):
        review = self.review()
        before = self.path.read_bytes()
        current = Mock(return_value=True)
        publish = web_accounts._publish

        def lose_at_publish(*args, **kwargs):
            if loss == "scope":
                current.return_value = False
            else:
                self.reviewed.cancel()
            return publish(*args, **kwargs)

        scope = self.scope(review=review, current=current)
        with (
            self.prompts("ENROLL", STORE_PHRASE),
            patch.object(web_accounts, "_publish", side_effect=lose_at_publish),
            self.assertRaisesRegex(web_accounts.WebAccountsError, "no longer current"),
        ):
            host.commit_enrollment(self.reviewed, scope=scope)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with self.assertRaisesRegex(host.EnrollmentCommitError, "No current"):
            host.commit_enrollment(self.reviewed, scope=scope)

    def test_final_cas_guard_refuses_lost_scope_or_cancelled_approval(self):
        for loss in ("scope", "approval"):
            with self.subTest(loss=loss):
                self.reject_during_publish(loss)

    def test_unverified_store_save_is_not_reported_as_success_or_retried(self):
        review = self.review()
        scope = self.scope(review=review)
        sync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic private write diagnostics")
            return sync(fd)

        before = self.path.read_bytes()
        with (
            self.prompts("ENROLL", STORE_PHRASE) as prompt,
            patch.object(web_accounts.os, "fsync", side_effect=fail_directory_sync),
        ):
            with self.assertRaisesRegex(
                web_accounts.WebAccountsError, "could not be verified"
            ):
                host.commit_enrollment(self.reviewed, scope=scope)
            changed = self.path.read_bytes()
            self.assertNotEqual(changed, before)
            with self.assertRaisesRegex(host.EnrollmentCommitError, "No current"):
                host.commit_enrollment(self.reviewed, scope=scope)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.path.read_bytes(), changed)
