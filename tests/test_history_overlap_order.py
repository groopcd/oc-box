"""Pass ordering only; no native operations or live workspace access."""

import unittest
from unittest.mock import Mock

from oc_box.history_overlap_order import OrderedOverlap, OverlapRefusal, Route


ROUTES = tuple(Route("ses_" + suffix, "ses_archive_" + suffix) for suffix in "abc")


class Pending(ValueError):
    pass


class OverlapOrderTests(unittest.TestCase):
    def fixture(self, routes=ROUTES):
        self.read = Mock(side_effect=lambda route: route.source)
        self.begin = Mock(side_effect=lambda route: route.archive)
        self.receive = Mock(side_effect=lambda route, _handle: route.source)
        self.cancel = Mock(return_value=True)
        return OrderedOverlap(
            routes,
            read=self.read,
            begin=self.begin,
            receive=self.receive,
            cancel=self.cancel,
            pending_error=Pending,
        )

    def test_only_one_checked_read_ahead_and_not_needed_for_unchanged_sources(self):
        with self.fixture() as run:
            self.assertEqual(run.take(ROUTES[0]), "ses_a")
            run.before_serial_destination()
            self.assertEqual(run.take(ROUTES[1]), "ses_b")
            self.assertEqual(run.take(ROUTES[2]), "ses_c")
        self.assertEqual(
            [call.args for call in self.read.call_args_list],
            [(ROUTES[0],), (ROUTES[2],)],
        )
        self.begin.assert_called_once_with(ROUTES[1])
        self.receive.assert_called_once_with(ROUTES[1], ROUTES[1].archive)
        self.cancel.assert_not_called()

    def test_pending_speculation_preserves_independent_progress(self):
        with self.fixture() as run:
            self.receive.side_effect = Pending
            run.take(ROUTES[0])
            run.before_serial_destination()
            with self.assertRaises(Pending):
                run.take(ROUTES[1])
            self.assertEqual(run.take(ROUTES[2]), "ses_c")
        self.cancel.assert_not_called()

    def test_failed_destination_aborts_slot_without_replay(self):
        original = OSError("synthetic host failure")
        with self.assertRaises(OSError) as raised:
            with self.fixture() as run:
                run.take(ROUTES[0])
                run.before_serial_destination()
                raise original
        self.assertIs(raised.exception, original)
        self.cancel.assert_called_once_with(ROUTES[1], ROUTES[1].archive)
        self.receive.assert_not_called()

    def test_unverified_cleanup_does_not_hide_primary_failure(self):
        original = OSError("synthetic host failure")
        with self.assertRaises(OSError) as raised:
            with self.fixture() as run:
                run.take(ROUTES[0])
                run.before_serial_destination()
                self.cancel.return_value = None
                raise original
        self.assertIs(raised.exception, original)
        self.assertTrue(raised.exception.history_source_cleanup_failed)

    def test_interrupted_cleanup_keeps_nonzero_interrupt_and_uncertainty(self):
        interrupt = SystemExit(0)
        with self.assertRaises(SystemExit) as raised:
            with self.fixture() as run:
                run.take(ROUTES[0])
                run.before_serial_destination()
                self.cancel.side_effect = interrupt
        self.assertIs(raised.exception, interrupt)
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(raised.exception.history_source_cleanup_failed)

    def test_hard_speculative_read_aborts_and_does_not_start_another_worker(self):
        with self.assertRaisesRegex(ValueError, "synthetic bad projection"):
            with self.fixture() as run:
                self.receive.side_effect = ValueError("synthetic bad projection")
                run.take(ROUTES[0])
                run.before_serial_destination()
                run.take(ROUTES[1])
        self.begin.assert_called_once_with(ROUTES[1])
        self.cancel.assert_called_once_with(ROUTES[1], ROUTES[1].archive)

    def test_cannot_credit_partial_pass_or_double_admission(self):
        with self.assertRaisesRegex(OverlapRefusal, "incomplete-pass"):
            with self.fixture() as run:
                run.take(ROUTES[0])
                run.before_serial_destination()
        self.cancel.assert_called_once_with(ROUTES[1], ROUTES[1].archive)
        with self.assertRaisesRegex(OverlapRefusal, "read-ownership"):
            with self.fixture() as run:
                run.take(ROUTES[0])
                run.before_serial_destination()
                run.before_serial_destination()
        self.begin.assert_called_once_with(ROUTES[1])

    def test_duplicate_and_alias_routes_refuse_before_native_calls(self):
        for routes in (
            ROUTES + (ROUTES[0],),
            (ROUTES[0], Route("ses_b", ROUTES[0].archive)),
        ):
            with (
                self.subTest(routes=routes),
                self.assertRaisesRegex(OverlapRefusal, "inventory-binding"),
            ):
                self.fixture(routes)
            self.read.assert_not_called()
            self.begin.assert_not_called()
