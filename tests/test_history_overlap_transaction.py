"""Only a durable and separately verified creation can finish the guarded phase."""

from dataclasses import fields
import json
import subprocess
import unittest
from unittest.mock import Mock

from oc_box.history_overlap_archive import sealed_candidate
from oc_box.history_overlap_order import OverlapRefusal, Route
from oc_box.history_overlap_transaction import (
    AtomicFirstImport,
    FirstImport,
    create_candidate,
    create_candidate_once,
    native_missing_target,
)


ROUTE = Route("ses_source", "ses_archive_source")
ORDER = tuple(field.name for field in fields(FirstImport))


class FirstImportTests(unittest.TestCase):
    def fixture(self):
        self.observed = []
        self.steps = {}
        self.intent, self.checkpoint = object(), object()
        returns = {
            "absent": True,
            "reserve": self.intent,
            "readback": True,
            "publish_checkpoint": self.checkpoint,
            "retire": True,
        }

        def action(name):
            def invoke(*args):
                self.observed.append((name, args))
                return returns.get(name)

            return Mock(side_effect=invoke)

        for name in ORDER:
            self.steps[name] = action(name)
        return FirstImport(**self.steps)

    def test_only_ordered_independent_verification_completes_the_attempt(self):
        operations = self.fixture()
        with sealed_candidate([b"candidate"]) as candidate:
            self.assertEqual(
                create_candidate(ROUTE, candidate, operations), candidate.digest
            )
            self.steps["native_import"].assert_called_once_with(candidate.native_path())
            self.steps["reserve"].assert_called_once_with(ROUTE, candidate.digest)
            self.steps["readback"].assert_called_once_with(ROUTE, candidate.digest)
            self.steps["publish_archive"].assert_called_once_with(candidate)
            self.steps["publish_checkpoint"].assert_called_once_with(
                ROUTE, candidate.digest
            )
        self.steps["retire"].assert_called_once_with(self.intent, self.checkpoint)
        self.assertEqual(tuple(name for name, _ in self.observed), ORDER)

    def test_no_further_action_or_record_retirement_after_a_failure(self):
        for index, failed in enumerate(ORDER):
            failure = OSError("synthetic phase error")
            operations = self.fixture()
            self.steps[failed].side_effect = failure
            with (
                self.subTest(failed=failed),
                sealed_candidate([b"candidate"]) as candidate,
            ):
                with self.assertRaises(OSError) as raised:
                    create_candidate(ROUTE, candidate, operations)
                self.assertIs(raised.exception, failure)
            self.steps[failed].assert_called_once()
            for name in ORDER[index + 1 :]:
                self.steps[name].assert_not_called()

    def test_no_mutation_without_positive_absence_even_if_a_result_is_truthy(self):
        for response in (False, None, 1, "not-found"):
            operations = self.fixture()
            self.steps["absent"].side_effect = lambda _route: response
            with (
                self.subTest(response=response),
                sealed_candidate([b"candidate"]) as candidate,
            ):
                with self.assertRaisesRegex(OverlapRefusal, "destination-not-absent"):
                    create_candidate(ROUTE, candidate, operations)
            for name in ORDER[1:]:
                self.steps[name].assert_not_called()

    def test_silent_import_noop_does_not_publish_or_retire(self):
        operations = self.fixture()
        self.steps["readback"].side_effect = lambda *_: False
        with sealed_candidate([b"candidate"]) as candidate:
            with self.assertRaisesRegex(OverlapRefusal, "destination-readback"):
                create_candidate(ROUTE, candidate, operations)
        self.steps["reserve"].assert_called_once()
        self.steps["native_import"].assert_called_once()
        for name in ("publish_archive", "publish_checkpoint", "retire"):
            self.steps[name].assert_not_called()

    def test_only_exact_identity_bearing_native_404_proves_absence(self):
        target = ROUTE.archive
        body = json.dumps(
            {
                "_tag": "SessionNotFoundError",
                "message": "Session not found: " + target,
                "sessionID": target,
            }
        )
        stderr = "HTTP 404 Not Found\n"
        self.assertTrue(
            native_missing_target(
                subprocess.CompletedProcess([], 1, body, stderr), target
            )
        )
        for code, output, errors in (
            (0, body, stderr),
            (1, body, ""),
            (1, "", stderr),
            (1, body.replace(target, "ses_other"), stderr),
            (1, body[:-1] + ',"sessionID":' + json.dumps(target) + "}", stderr),
            (1, '{"data":[]}', stderr),
        ):
            with self.subTest(code=code, output=output, errors=errors):
                self.assertFalse(
                    native_missing_target(
                        subprocess.CompletedProcess([], code, output, errors), target
                    )
                )

    def test_native_create_contract_needs_no_lookup_but_requires_exact_readback(self):
        serial = self.fixture()
        write = Mock(return_value=ROUTE.archive)
        atomic = AtomicFirstImport(
            serial.reserve,
            serial.before_write,
            write,
            serial.readback,
            serial.publish_archive,
            serial.publish_checkpoint,
            serial.retire,
        )
        with sealed_candidate([b"candidate"]) as candidate:
            self.assertEqual(
                create_candidate_once(ROUTE, candidate, atomic), candidate.digest
            )
            write.assert_called_once_with(candidate, ROUTE)
            serial.readback.assert_called_once_with(ROUTE, candidate.digest)
        serial.absent.assert_not_called()
        serial.native_import.assert_not_called()

    def test_unverified_native_create_leaves_intent_and_no_new_checkpoint(self):
        serial = self.fixture()
        write = Mock(return_value="ses_other_archive")
        atomic = AtomicFirstImport(
            serial.reserve,
            serial.before_write,
            write,
            serial.readback,
            serial.publish_archive,
            serial.publish_checkpoint,
            serial.retire,
        )
        with sealed_candidate([b"candidate"]) as candidate:
            with self.assertRaisesRegex(OverlapRefusal, "destination-acknowledgement"):
                create_candidate_once(ROUTE, candidate, atomic)
        serial.reserve.assert_called_once()
        write.assert_called_once()
        for action in (
            serial.readback,
            serial.publish_archive,
            serial.publish_checkpoint,
            serial.retire,
        ):
            action.assert_not_called()
