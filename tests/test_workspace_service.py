import contextlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from urllib.request import urlopen

from oc_box import (
    activity_log,
    lifecycle_output,
    native_tabs,
    oc_box as box,
    web_sessions,
    workspace_service as service,
    vault,
)


class HistoryWaitTests(unittest.TestCase):
    def setUp(self):
        self.data = {"id": "synthetic-workspace"}
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-private-password",
        }
        self.idle = {"data": {}}
        self.busy = {"data": {"ses_child": {"type": "running"}}}
        self.no_jobs = {"data": []}
        self.job = {
            "id": "job_example",
            "sessionID": "ses_child",
            "kind": "shell",
            "status": "completed",
            "time": {"started": 1},
            "shellID": "sh_example",
            "command": "private-sentinel",
        }
        self.now = 100.0
        for name, options in (
            ("monotonic", {"side_effect": lambda: self.now}),
            ("sleep", {"side_effect": self.advance}),
        ):
            clock = patch.object(service.time, name, **options)
            setattr(self, name, clock.start())
            self.addCleanup(clock.stop)
        request = patch.object(service, "api")
        self.api = request.start()
        self.addCleanup(request.stop)

    def advance(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def assert_wait_error(self, category, *, contract_category=None, **options):
        with self.assertRaises(box.BoxError) as raised:
            service.wait_history(box, self.data, self.connection, **options)
        self.assertEqual(
            str(raised.exception),
            f"OpenCode 2.x history wait failed ({category}).",
        )
        self.assertTrue(raised.exception.__suppress_context__)
        if category in ("invalid-active-response", "invalid-jobs-response"):
            contract_category = category
        if contract_category is not None:
            self.assertEqual(
                raised.exception.history_compatibility_category, contract_category
            )
        else:
            self.assertFalse(
                hasattr(raised.exception, "history_compatibility_category")
            )
        if category == "activity-timeout":
            self.assertIsInstance(raised.exception, box.HistoryWaitTimeout)
            self.assertNotIn("private-sentinel", raised.exception.diagnostic)
            timing = raised.exception.timing_diagnostic
            self.assertTrue(timing.startswith("History wait timing: "))
            self.assertLess(len(timing), 600)
            for private in (
                "private-sentinel",
                self.connection["password"],
                self.connection["url"],
                self.job["id"],
                self.job["sessionID"],
            ):
                self.assertNotIn(private, timing)
        else:
            self.assertFalse(hasattr(raised.exception, "diagnostic"))
        return raised.exception

    def test_running_child_then_two_quiet_pairs_uses_only_same_private_inventories(
        self,
    ):
        self.api.side_effect = [
            self.busy,
            self.no_jobs,
            self.idle,
            self.no_jobs,
            self.idle,
            self.no_jobs,
        ]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            service.wait_history(box, self.data, self.connection)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.api.call_count, 6)
        self.assertEqual(self.sleep.call_count, 2)
        for index, call in enumerate(self.api.call_args_list):
            self.assertIs(call.args[0], box)
            self.assertIs(call.args[1], self.data)
            self.assertIs(call.args[2], self.connection)
            self.assertEqual(
                call.args[3:],
                (
                    "GET",
                    "/api/session/active"
                    if index % 2 == 0
                    else "/api/experimental/job",
                ),
            )
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 30)

    def test_all_known_job_statuses_and_kinds_remain_pending_until_consumed(self):
        for kind in ("shell", "subagent", "image"):
            for status in ("running", "completed", "error", "cancelled"):
                with self.subTest(kind=kind, status=status):
                    self.api.reset_mock()
                    self.api.side_effect = [
                        self.idle,
                        {"data": [{**self.job, "kind": kind, "status": status}]},
                        self.idle,
                        self.no_jobs,
                        self.idle,
                        self.no_jobs,
                    ]
                    service.wait_history(box, self.data, self.connection)
                    self.assertEqual(self.api.call_count, 6)

    def test_completed_job_can_wake_foreground_and_later_activity_resets_quiet_pairs(
        self,
    ):
        self.api.side_effect = [
            self.idle,
            {"data": [self.job]},
            self.busy,
            self.no_jobs,
            self.idle,
            self.no_jobs,
            self.idle,
            {"data": [{**self.job, "status": "error"}]},
            self.idle,
            self.no_jobs,
            self.idle,
            self.no_jobs,
        ]
        service.wait_history(box, self.data, self.connection)
        self.assertEqual(self.api.call_count, 12)
        self.assertEqual(self.sleep.call_count, 5)

    def test_invalid_inventory_matrix_fails_without_exposing_private_payloads(self):
        active_cases = (
            None,
            {},
            {"data": []},
            {"data": {"private-sentinel": {"type": "running"}}},
            {"data": {"ses_child": "private-sentinel"}},
            {"data": {"ses_child": {"type": "idle", "detail": "private-sentinel"}}},
        )
        job_cases = (
            None,
            {},
            {"data": {}},
            {"data": ["private-sentinel"]},
            {"data": [{}]},
            {"data": [{**self.job, "id": ""}]},
            {"data": [{**self.job, "sessionID": "private-sentinel"}]},
            {"data": [{**self.job, "kind": "private-sentinel"}]},
            {"data": [{**self.job, "status": "private-sentinel"}]},
            {"data": [{**self.job, "status": ["running"]}]},
        )
        for category, cases in (
            ("invalid-active-response", active_cases),
            ("invalid-jobs-response", job_cases),
        ):
            for index, response in enumerate(cases):
                with self.subTest(category=category, case=index):
                    self.api.reset_mock()
                    self.api.side_effect = (
                        [response]
                        if category == "invalid-active-response"
                        else [self.idle, response]
                    )
                    self.assert_wait_error(category)
                    self.assertEqual(
                        self.api.call_count,
                        1 if category == "invalid-active-response" else 2,
                    )
        self.sleep.assert_not_called()

    def test_api_failure_matrix_reports_fixed_category_for_either_inventory(self):
        for target in ("active", "jobs"):
            for error in (
                box.BoxError("private-sentinel"),
                OSError("private-sentinel"),
                ValueError("private-sentinel"),
                subprocess.TimeoutExpired("private-sentinel", 0.5),
            ):
                with self.subTest(target=target, error=type(error).__name__):
                    self.api.reset_mock()
                    self.api.side_effect = (
                        [error] if target == "active" else [self.idle, error]
                    )
                    self.assert_wait_error(
                        "activity-api-failed",
                        contract_category=(
                            "invalid-active-response"
                            if target == "active"
                            else "invalid-jobs-response"
                        )
                        if isinstance(error, ValueError)
                        else None,
                    )
                    self.assertEqual(
                        self.api.call_count, 1 if target == "active" else 2
                    )
        self.sleep.assert_not_called()

    def test_busy_deadline_caps_final_sleep_and_never_starts_an_expired_request(self):
        self.api.side_effect = lambda *args, **kwargs: (
            self.busy if args[4] == "/api/session/active" else self.no_jobs
        )
        self.assert_wait_error("activity-timeout", timeout=0.5)
        self.assertAlmostEqual(self.now, 100.5)
        self.assertEqual(self.api.call_count, 6)
        self.assertEqual(self.sleep.call_count, 3)
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.1)

    def test_each_sequential_request_gets_only_remaining_budget(self):
        costs = iter((3, 1, 3))

        def request(*args, **kwargs):
            cost = next(costs)
            self.assertLessEqual(cost, kwargs["timeout"])
            self.advance(cost)
            return self.idle if args[4] == "/api/session/active" else self.no_jobs

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=8, interval=1)
        self.assertEqual(
            [call.kwargs["timeout"] for call in self.api.call_args_list], [8, 5, 3]
        )
        self.assertEqual(self.now, 108)
        self.assertEqual(self.api.call_count, 3)
        self.sleep.assert_called_once_with(1)

    def test_last_quiet_response_must_arrive_before_deadline(self):
        def request(*args, **kwargs):
            if self.api.call_count == 4:
                self.advance(kwargs["timeout"])
            return self.idle if args[4] == "/api/session/active" else self.no_jobs

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual(self.now, 101)

    def test_deadline_expiring_in_active_request_skips_jobs_request(self):
        def request(*args, **kwargs):
            self.advance(kwargs["timeout"])
            return self.idle

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[4], "/api/session/active")
        self.sleep.assert_not_called()

    def test_long_overall_budget_keeps_each_native_request_at_most_sixty_seconds(self):
        self.api.side_effect = [self.idle, self.no_jobs, self.idle, self.no_jobs]
        service.wait_history(box, self.data, self.connection, timeout=120)
        self.assertEqual(
            [call.kwargs["timeout"] for call in self.api.call_args_list], [60] * 4
        )

    def test_request_failure_at_deadline_is_a_timeout_not_private_api_error(self):
        def request(*args, **kwargs):
            self.advance(kwargs["timeout"])
            raise box.BoxError("private-sentinel")

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.api.assert_called_once()
        self.sleep.assert_not_called()

    def test_retained_job_matrix_times_out_with_only_validated_counts(self):
        for kind in ("shell", "subagent", "image"):
            for status in ("running", "completed", "error", "cancelled"):
                with self.subTest(kind=kind, status=status):
                    self.api.reset_mock()
                    job = {**self.job, "kind": kind, "status": status}
                    self.api.side_effect = lambda *args, job=job, **kwargs: (
                        self.idle
                        if args[4] == "/api/session/active"
                        else {"data": [job]}
                    )
                    error = self.assert_wait_error("activity-timeout", timeout=0.5)
                    counts = ", ".join(
                        f"jobs-{name}={int(name == status)}"
                        for name in ("running", "completed", "error", "cancelled")
                    )
                    self.assertEqual(
                        error.diagnostic,
                        "History wait timeout: phase=poll-delay; "
                        "last complete job kinds: "
                        + ", ".join(
                            f"{name}={int(name == kind)}"
                            for name in ("shell", "subagent", "image")
                        )
                        + "; "
                        f"last complete observation: foreground=0, {counts}.",
                    )
                    self.assertNotIn(job["id"], error.diagnostic)
                    self.assertNotIn(job["sessionID"], error.diagnostic)
                    self.assertIn(f"{kind}=1", error.diagnostic)
                    self.assertEqual(
                        error.recovery_eligible,
                        kind == "shell",
                    )

    def test_only_consecutive_shell_pairs_can_request_post_stop_recovery(
        self,
    ):
        cancelled = {"data": [{**self.job, "status": "cancelled"}]}
        running = {"data": [{**self.job, "status": "running"}]}
        mixed = {
            "data": [
                {**self.job, "status": "cancelled"},
                {**self.job, "kind": "subagent", "status": "cancelled"},
            ]
        }
        cases = (
            (
                "two eligible",
                [(self.idle, cancelled), (self.idle, cancelled)],
                True,
                None,
            ),
            (
                "only last eligible",
                [(self.busy, running), (self.idle, cancelled)],
                False,
                "insufficient-shell-observations",
            ),
            (
                "foreground live",
                [(self.idle, cancelled), (self.busy, cancelled)],
                False,
                "foreground-active",
            ),
            (
                "mixed kind",
                [(self.idle, cancelled), (self.idle, mixed)],
                False,
                "non-shell-jobs",
            ),
            (
                "rehydrated running job",
                [(self.idle, cancelled), (self.idle, running)],
                True,
                None,
            ),
            (
                "empty inventory resets",
                [(self.idle, cancelled), (self.idle, self.no_jobs)],
                False,
                "jobs-empty",
            ),
        )
        for name, pairs, eligible, reason in cases:
            with self.subTest(case=name):
                self.api.reset_mock()
                self.api.side_effect = [response for pair in pairs for response in pair]
                error = self.assert_wait_error("activity-timeout", timeout=0.4)
                self.assertEqual(error.recovery_eligible, eligible)
                if reason is not None:
                    self.assertEqual(error.recovery_reason, reason)
                self.assertEqual(self.api.call_count, 4)

    def test_stable_23x_profile_allows_only_shell_subagent_hints_never_live_success(
        self,
    ):
        versions = (
            (None, {"shell"}),
            ("2.2.6", {"shell"}),
            ("2.2.7", {"shell"}),
            ("2.3.0", {"shell", "subagent"}),
            ("2.3.1", {"shell", "subagent"}),
            ("2.3.2", {"shell", "subagent"}),
            ("2.3.47", {"shell", "subagent"}),
            ("2.3.9999", {"shell", "subagent"}),
            ("2.2.8", {"shell"}),
            ("2.4.0", {"shell"}),
            ("2.3.01", {"shell"}),
            ("2.3.٢", {"shell"}),
            ("2.3.0+unknown", {"shell"}),
            ("2.3.2-dev", {"shell"}),
        )
        for version, allowed_kinds in versions:
            for kinds in (
                ("shell",),
                ("subagent",),
                ("shell", "subagent"),
                ("image",),
                ("shell", "image"),
            ):
                for status in ("running", "completed", "error", "cancelled"):
                    with self.subTest(version=version, kinds=kinds, status=status):
                        self.api.reset_mock()
                        data = {**self.data, "opencode_version": version}
                        jobs = [
                            {**self.job, "kind": kind, "status": status}
                            for kind in kinds
                        ]
                        self.api.side_effect = lambda *args, jobs=jobs, **kwargs: (
                            self.idle
                            if args[4] == "/api/session/active"
                            else {"data": jobs}
                        )
                        with self.assertRaises(box.HistoryWaitTimeout) as raised:
                            service.wait_history(
                                box, data, self.connection, timeout=0.5, interval=0.25
                            )
                        eligible = set(kinds) <= allowed_kinds
                        self.assertEqual(raised.exception.recovery_eligible, eligible)
                        self.assertEqual(self.api.call_count, 4)
                        self.assertNotIn(
                            "private-sentinel", raised.exception.diagnostic
                        )
                        for kind in ("shell", "subagent", "image"):
                            self.assertIn(
                                f"{kind}={int(kind in kinds)}",
                                raised.exception.diagnostic,
                            )

    def test_230_late_contradictions_and_errors_invalidate_subagent_hint(self):
        cases = (
            (5, self.busy, "foreground-active"),
            (5, {"data": []}, "invalid-active-response"),
            (6, self.no_jobs, "jobs-empty"),
            (6, {"data": {}}, "invalid-jobs-response"),
            (6, {"data": [{**self.job, "kind": "image"}]}, "non-shell-jobs"),
            (6, {"data": [{**self.job, "status": "unknown"}]}, "invalid-jobs-response"),
            (5, box.BoxError("private-sentinel"), "activity-api-failed"),
            (6, ValueError("private-sentinel"), "activity-api-failed"),
            (5, OSError("private-sentinel"), "activity-api-failed"),
        )
        for expired_call, response, reason in cases:
            with self.subTest(expired_call=expired_call, reason=reason):
                self.api.reset_mock()

                def request(*args, **kwargs):
                    if self.api.call_count == expired_call:
                        self.advance(kwargs["timeout"])
                        if isinstance(response, BaseException):
                            raise response
                        return response
                    return (
                        self.idle
                        if args[4] == "/api/session/active"
                        else {"data": [{**self.job, "kind": "subagent"}]}
                    )

                self.api.side_effect = request
                with self.assertRaises(box.HistoryWaitTimeout) as raised:
                    service.wait_history(
                        box,
                        {**self.data, "opencode_version": "2.3.0"},
                        self.connection,
                        timeout=1,
                    )
                self.assertFalse(raised.exception.recovery_eligible)
                self.assertEqual(raised.exception.recovery_reason, reason)

    def test_230_real_query_timeouts_preserve_only_prior_complete_eligible_pairs(self):
        for expired_call in (1, 3, 5, 6):
            for wrapped in (False, True):
                with self.subTest(expired_call=expired_call, wrapped=wrapped):
                    self.api.reset_mock()

                    def request(*args, **kwargs):
                        if self.api.call_count == expired_call:
                            self.advance(kwargs["timeout"])
                            error = subprocess.TimeoutExpired(
                                "private-sentinel", kwargs["timeout"]
                            )
                            if wrapped:
                                outer = box.BoxError("private-sentinel")
                                outer.__cause__ = error
                                raise outer
                            raise error
                        return (
                            self.idle
                            if args[4] == "/api/session/active"
                            else {"data": [{**self.job, "kind": "subagent"}]}
                        )

                    self.api.side_effect = request
                    with self.assertRaises(box.HistoryWaitTimeout) as raised:
                        service.wait_history(
                            box,
                            {**self.data, "opencode_version": "2.3.0"},
                            self.connection,
                            timeout=1,
                        )
                    self.assertEqual(
                        raised.exception.recovery_eligible, expired_call >= 5
                    )
                    self.assertEqual(
                        raised.exception.recovery_reason,
                        "insufficient-job-observations",
                    )

    def test_deadline_routing_needs_prior_pairs_and_distinguishes_real_timeout_causes(
        self,
    ):
        for expired_call in (1, 3, 5, 6):
            for outcome in (
                "valid",
                "timeout",
                "wrapped-timeout",
                "command-error",
                "json-error",
                "os-error",
            ):
                with self.subTest(expired_call=expired_call, outcome=outcome):
                    self.api.reset_mock()

                    def request(
                        *args, expired_call=expired_call, outcome=outcome, **kwargs
                    ):
                        if self.api.call_count == expired_call:
                            self.advance(kwargs["timeout"])
                            if outcome == "timeout":
                                raise subprocess.TimeoutExpired(
                                    "private-sentinel", kwargs["timeout"]
                                )
                            if outcome == "wrapped-timeout":
                                error = box.BoxError("private-sentinel")
                                error.__cause__ = subprocess.TimeoutExpired(
                                    "private-sentinel", kwargs["timeout"]
                                )
                                raise error
                            if outcome == "command-error":
                                raise box.BoxError("private-sentinel")
                            if outcome == "json-error":
                                raise ValueError("private-sentinel")
                            if outcome == "os-error":
                                raise OSError("private-sentinel")
                        return (
                            self.idle
                            if args[4] == "/api/session/active"
                            else {"data": [{**self.job, "status": "cancelled"}]}
                        )

                    self.api.side_effect = request
                    contract = (
                        (
                            "invalid-active-response"
                            if expired_call % 2
                            else "invalid-jobs-response"
                        )
                        if outcome == "json-error"
                        else None
                    )
                    error = self.assert_wait_error(
                        "activity-timeout", timeout=1, contract_category=contract
                    )
                    self.assertEqual(
                        error.recovery_eligible,
                        expired_call >= 5
                        and outcome in ("valid", "timeout", "wrapped-timeout"),
                    )
                    if outcome in ("command-error", "json-error", "os-error"):
                        self.assertEqual(error.recovery_reason, "activity-api-failed")

    def test_late_contradictory_or_malformed_responses_invalidate_prior_shell_pairs(
        self,
    ):
        cases = (
            (5, self.busy, "foreground-active"),
            (5, {"data": []}, "invalid-active-response"),
            (6, self.no_jobs, "jobs-empty"),
            (6, {"data": {}}, "invalid-jobs-response"),
            (6, {"data": [{**self.job, "kind": "subagent"}]}, "non-shell-jobs"),
            (
                6,
                {"data": [{**self.job, "status": "private-sentinel"}]},
                "invalid-jobs-response",
            ),
        )
        for expired_call, response, reason in cases:
            with self.subTest(expired_call=expired_call, response=response):
                self.api.reset_mock()

                def request(
                    *args, expired_call=expired_call, response=response, **kwargs
                ):
                    if self.api.call_count == expired_call:
                        self.advance(kwargs["timeout"])
                        return response
                    return (
                        self.idle
                        if args[4] == "/api/session/active"
                        else {"data": [{**self.job, "status": "cancelled"}]}
                    )

                self.api.side_effect = request
                error = self.assert_wait_error("activity-timeout", timeout=1)
                self.assertFalse(error.recovery_eligible)
                self.assertEqual(error.recovery_reason, reason)

    def test_query_timeout_diagnostics_keep_partial_observations_unknown(self):
        for target in ("/api/session/active", "/api/experimental/job"):
            for fail in (False, True):
                with self.subTest(target=target, request_failure=fail):
                    self.api.reset_mock()

                    def request(*args, target=target, fail=fail, **kwargs):
                        if args[4] == target:
                            self.advance(kwargs["timeout"])
                            if fail:
                                raise box.BoxError("private-sentinel")
                        return (
                            self.idle
                            if args[4] == "/api/session/active"
                            else self.no_jobs
                        )

                    self.api.side_effect = request
                    error = self.assert_wait_error("activity-timeout", timeout=1)
                    phase = (
                        "foreground-query" if "active" in target else "background-query"
                    )
                    self.assertEqual(
                        error.diagnostic,
                        f"History wait timeout: phase={phase}; "
                        "last complete job kinds: unavailable; "
                        "last complete observation: unavailable.",
                    )

    def test_slow_query_reports_last_complete_pair_not_partial_new_activity(self):
        for expired_call, phase in ((3, "foreground-query"), (4, "background-query")):
            for fail in (False, True):
                with self.subTest(phase=phase, request_failure=fail):
                    self.api.reset_mock()

                    def request(*args, expired_call=expired_call, fail=fail, **kwargs):
                        if self.api.call_count == expired_call:
                            self.advance(kwargs["timeout"])
                            if fail:
                                raise box.BoxError("private-sentinel")
                        if args[4] == "/api/session/active":
                            return self.idle if self.api.call_count == 1 else self.busy
                        return self.no_jobs

                    self.api.side_effect = request
                    error = self.assert_wait_error("activity-timeout", timeout=1)
                    self.assertEqual(self.api.call_count, expired_call)
                    self.assertEqual(
                        error.diagnostic,
                        f"History wait timeout: phase={phase}; "
                        "last complete job kinds: shell=0, subagent=0, image=0; "
                        "last complete observation: foreground=0, jobs-running=0, "
                        "jobs-completed=0, jobs-error=0, jobs-cancelled=0.",
                    )

    def test_observation_counts_are_capped_and_contain_no_native_fields(self):
        active = {f"ses_private_{index}": {"type": "running"} for index in range(10000)}
        self.api.side_effect = lambda *args, **kwargs: (
            {"data": active}
            if args[4] == "/api/session/active"
            else {"data": [self.job] * 10000}
        )
        error = self.assert_wait_error("activity-timeout", timeout=0.1)
        self.assertEqual(
            error.diagnostic,
            "History wait timeout: phase=poll-delay; "
            "last complete job kinds: shell=9999+, subagent=0, image=0; "
            "last complete observation: foreground=9999+, jobs-running=0, "
            "jobs-completed=9999+, jobs-error=0, jobs-cancelled=0.",
        )

    def test_verbose_timeout_reports_safe_timing_at_the_reporting_boundary(self):
        def request(*args, **kwargs):
            self.advance(kwargs["timeout"])
            raise box.BoxError("private-sentinel")

        self.api.side_effect = request
        with (
            contextlib.redirect_stderr(io.StringIO()) as output,
            patch.object(activity_log, "record_event", return_value=False),
            lifecycle_output.invocation("sync", console_level="all"),
        ):
            self.assert_wait_error("activity-timeout", timeout=1)
            self.assertEqual(output.getvalue(), "")
            lifecycle_output.flush()
            self.assertIn(
                "History wait timeout: phase=foreground-query", output.getvalue()
            )
            self.assertIn("History wait timing:", output.getvalue())
            self.assertNotIn("private-sentinel", output.getvalue())

    def test_query_timings_separate_completed_polls_from_poll_delay(self):
        def request(*args, **kwargs):
            self.advance(0.125)
            return (
                self.idle if args[4] == "/api/session/active" else {"data": [self.job]}
            )

        self.api.side_effect = request
        error = self.assert_wait_error("activity-timeout", timeout=1, interval=0.25)
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.now, 101)
        timing = error.timing_diagnostic
        self.assertIn("complete-pairs=2;", timing)
        for name in ("foreground", "background"):
            self.assertIn(f"{name}-queries=2, total=0.250s, max=0.125s;", timing)
        self.assertIn(
            "last-query=background-query, remaining-at-start=0.375s, outcome=returned;",
            timing,
        )
        self.assertIn("last-complete-age=0.250s.", timing)
        self.assertTrue(error.recovery_eligible)

    def test_final_query_timing_outcomes_do_not_change_deadline_or_recovery_routing(
        self,
    ):
        for expired_call in (5, 6):
            for outcome in (
                "returned",
                "subprocess-timeout",
                "wrapped-timeout",
                "failed",
            ):
                with self.subTest(expired_call=expired_call, outcome=outcome):
                    self.now = 100.0
                    self.api.reset_mock()
                    self.sleep.reset_mock()

                    def request(*args, **kwargs):
                        if self.api.call_count == expired_call:
                            self.advance(kwargs["timeout"])
                            if outcome in ("subprocess-timeout", "wrapped-timeout"):
                                error = subprocess.TimeoutExpired(
                                    "private-sentinel", kwargs["timeout"]
                                )
                                if outcome == "wrapped-timeout":
                                    raise box.BoxError("private-sentinel") from error
                                raise error
                            if outcome == "failed":
                                raise ValueError("private-sentinel")
                        else:
                            self.advance(0.125)
                        return (
                            self.idle
                            if args[4] == "/api/session/active"
                            else {"data": [self.job]}
                        )

                    self.api.side_effect = request
                    contract = (
                        (
                            "invalid-active-response"
                            if expired_call == 5
                            else "invalid-jobs-response"
                        )
                        if outcome == "failed"
                        else None
                    )
                    error = self.assert_wait_error(
                        "activity-timeout",
                        timeout=1,
                        interval=0.125,
                        contract_category=contract,
                    )
                    self.assertEqual(self.api.call_count, expired_call)
                    self.assertEqual(self.now, 101)
                    self.assertEqual(error.recovery_eligible, outcome != "failed")
                    timing = error.timing_diagnostic
                    self.assertIn("complete-pairs=2;", timing)
                    self.assertIn("last-complete-age=0.375s.", timing)
                    expected_outcome = (
                        "subprocess-timeout"
                        if outcome == "wrapped-timeout"
                        else outcome
                    )
                    if expired_call == 5:
                        self.assertIn(
                            "foreground-queries=3, total=0.500s, max=0.250s;", timing
                        )
                        self.assertIn(
                            "background-queries=2, total=0.250s, max=0.125s;", timing
                        )
                        self.assertIn(
                            "last-query=foreground-query, remaining-at-start=0.250s, "
                            f"outcome={expected_outcome};",
                            timing,
                        )
                    else:
                        for name in ("foreground", "background"):
                            self.assertIn(
                                f"{name}-queries=3, total=0.375s, max=0.125s;", timing
                            )
                        self.assertIn(
                            "last-query=background-query, remaining-at-start=0.125s, "
                            f"outcome={expected_outcome};",
                            timing,
                        )

    def test_initial_query_and_zero_budget_timings_keep_complete_pairs_unavailable(
        self,
    ):
        for target in ("/api/session/active", "/api/experimental/job", None):
            with self.subTest(target=target):
                self.now = 100.0
                self.api.reset_mock()

                def request(*args, **kwargs):
                    if args[4] == target:
                        self.advance(kwargs["timeout"])
                    return (
                        self.idle if args[4] == "/api/session/active" else self.no_jobs
                    )

                self.api.side_effect = request
                error = self.assert_wait_error(
                    "activity-timeout", timeout=0 if target is None else 1
                )
                timing = error.timing_diagnostic
                self.assertIn("complete-pairs=0;", timing)
                self.assertIn("last-complete-age=unavailable.", timing)
                self.assertFalse(error.recovery_eligible)
                if target is None:
                    self.api.assert_not_called()
                    self.assertIn(
                        "last-query=unavailable, remaining-at-start=unavailable, "
                        "outcome=unavailable;",
                        timing,
                    )
                else:
                    self.assertIn(
                        f"last-query={'foreground' if 'active' in target else 'background'}"
                        "-query, remaining-at-start=1.000s, outcome=returned;",
                        timing,
                    )

    def test_busy_pairs_only_authorize_post_stop_23_archival_never_live_success(self):
        active = {
            "ses_foreground_one": {"type": "running"},
            "ses_foreground_two": {"type": "running"},
        }
        jobs = [
            {**self.job, "kind": kind, "status": "running"}
            for kind in ("shell",) * 3 + ("subagent",) * 11
        ]
        self.api.side_effect = lambda *args, **kwargs: (
            {"data": active} if args[4] == "/api/session/active" else {"data": jobs}
        )
        with self.assertRaises(box.HistoryWaitTimeout) as raised:
            service.wait_history(
                box,
                {**self.data, "opencode_version": "2.3.2"},
                self.connection,
                timeout=1,
                interval=0.25,
            )
        error = raised.exception
        self.assertIn("foreground=2, jobs-running=14", error.diagnostic)
        self.assertIn("shell=3, subagent=11, image=0", error.diagnostic)
        self.assertIn("complete-pairs=4;", error.timing_diagnostic)
        self.assertIn(
            "foreground-queries=4, total=0.000s, max=0.000s;", error.timing_diagnostic
        )
        self.assertIn(
            "background-queries=4, total=0.000s, max=0.000s;", error.timing_diagnostic
        )
        self.assertTrue(error.recovery_eligible)
        self.assertTrue(error.interrupted_foreground)
        self.assertEqual(error.recovery_reason, "foreground-active")
        self.assertEqual(self.api.call_count, 8)
        self.assertEqual(self.now, 101)

    def test_foreground_snapshot_hint_needs_consecutive_validated_23_pairs(self):
        jobs = {"data": [{**self.job, "kind": "subagent", "status": "running"}]}
        cases = (
            ("2.3.2", [self.busy, jobs] * 2, True),
            ("2.3.0", [self.busy, self.no_jobs] * 2, True),
            ("2.2.7", [self.busy, {"data": [self.job]}] * 2, False),
            ("2.4.0", [self.busy, jobs] * 2, False),
            ("2.3.2", [self.idle, jobs, self.busy, jobs], False),
            ("2.3.2", [self.busy, jobs, self.idle, jobs], False),
            (
                "2.3.2",
                [self.busy, jobs, self.busy, {"data": [{**self.job, "kind": "image"}]}],
                False,
            ),
        )
        for release, replies, eligible in cases:
            with self.subTest(release=release, replies=replies):
                self.now = 100.0
                self.api.reset_mock()
                self.api.side_effect = replies
                with self.assertRaises(box.HistoryWaitTimeout) as raised:
                    service.wait_history(
                        box,
                        {**self.data, "opencode_version": release},
                        self.connection,
                        timeout=0.5,
                        interval=0.25,
                    )
                self.assertEqual(raised.exception.recovery_eligible, eligible)
                self.assertEqual(
                    raised.exception.interrupted_foreground,
                    bool(replies[-2]["data"]),
                )
                self.assertEqual(self.api.call_count, 4)

    def test_late_foreground_hint_replies_preserve_only_known_complete_pairs(self):
        for call, reply, eligible in (
            (5, self.idle, False),
            (5, {"data": []}, False),
            (6, {"data": {}}, False),
            (6, {"data": [{**self.job, "kind": "image"}]}, False),
            (5, subprocess.TimeoutExpired("private-sentinel", 1), True),
            (6, subprocess.TimeoutExpired("private-sentinel", 1), True),
            (5, box.BoxError("private-sentinel"), False),
        ):
            with self.subTest(call=call, reply=reply):
                self.now = 100.0
                self.api.reset_mock()

                def request(*args, **kwargs):
                    if self.api.call_count == call:
                        self.advance(kwargs["timeout"])
                        if isinstance(reply, BaseException):
                            raise reply
                        return reply
                    return (
                        self.busy if args[4] == "/api/session/active" else self.no_jobs
                    )

                self.api.side_effect = request
                with self.assertRaises(box.HistoryWaitTimeout) as raised:
                    service.wait_history(
                        box,
                        {**self.data, "opencode_version": "2.3.2"},
                        self.connection,
                        timeout=1,
                        interval=0.25,
                    )
                self.assertEqual(raised.exception.recovery_eligible, eligible)
                self.assertEqual(
                    raised.exception.interrupted_foreground,
                    not (call == 5 and reply is self.idle),
                )
                self.assertEqual(self.api.call_count, call)

    def test_timing_counts_and_durations_are_bounded(self):
        self.api.side_effect = lambda *args, **kwargs: (
            self.idle if args[4] == "/api/session/active" else {"data": [self.job]}
        )
        error = self.assert_wait_error("activity-timeout", timeout=10001, interval=1)
        self.assertIn("complete-pairs=9999+;", error.timing_diagnostic)
        for name in ("foreground", "background"):
            self.assertIn(f"{name}-queries=9999+,", error.timing_diagnostic)
        self.assertEqual(self.api.call_count, 20002)

        self.now = 100.0
        self.api.reset_mock()

        def request(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 60)
            # Include an overdue helper's cleanup/clock jump, not just its cap.
            self.advance(20001)
            return self.idle

        self.api.side_effect = request
        error = self.assert_wait_error("activity-timeout", timeout=20001)
        self.assertIn(
            "foreground-queries=1, total=9999+s, max=9999+s;", error.timing_diagnostic
        )
        self.assertIn(
            "remaining-at-start=9999+s, outcome=returned;", error.timing_diagnostic
        )
        self.api.assert_called_once()

    def test_zero_budget_and_invalid_wait_options_never_call_native_api(self):
        self.assert_wait_error("activity-timeout", timeout=0)
        for options in (
            {"timeout": -1},
            {"timeout": float("inf")},
            {"timeout": float("nan")},
            {"interval": 0},
            {"interval": -1},
            {"interval": float("inf")},
            {"interval": "private-sentinel"},
        ):
            with self.subTest(options=options):
                self.assert_wait_error("invalid-wait-options", **options)
        self.api.assert_not_called()
        self.sleep.assert_not_called()


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-live-service-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.root),
                "OC_BOX_CONTAINER": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        project = self.root / "project"
        project.mkdir()
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "workspace": self.data["id"],
            "session": "ses_shared",
            "version": "2.2.6",
        }
        self.info = {
            "State": {"Running": True},
            "Mounts": [{"Source": str(project), "Destination": str(project)}],
        }
        tabs = patch.object(
            box.native_tabs,
            "inspect",
            return_value=box.native_tabs.Snapshot("missing", reason="absent"),
        )
        tabs.start()
        self.addCleanup(tabs.stop)

    def test_manual_sync_history_scope_closes_before_success_or_container_stop(self):
        for outcome in (
            "success",
            "legacy-fallback",
            "sync-error",
            "cleanup-error",
            "final-stop-error",
        ):
            with self.subTest(outcome=outcome):
                args = box.parser().parse_args(["sync"])
                args._opencode_release = self.data["opencode_version"]
                events = []
                error = box.BoxError("synthetic-fixed-refusal")
                box.atomic_json(
                    box.session_file(self.path),
                    {"sessions": ["ses_shared"], "last": "ses_shared"},
                )

                @contextlib.contextmanager
                def history_scope(_box, data):
                    self.assertIs(data, self.data)
                    events.append("source-open")
                    token = service.CONNECTION.set(
                        None if outcome == "legacy-fallback" else self.connection
                    )
                    try:
                        yield
                    finally:
                        service.CONNECTION.reset(token)
                        events.append("source-closed")
                        if outcome == "cleanup-error":
                            raise error

                def synchronize(path, data, *, announce):
                    self.assertFalse(announce)
                    self.assertEqual(path, self.path)
                    self.assertIs(data, self.data)
                    self.assertEqual(
                        service.live_connection(),
                        None if outcome == "legacy-fallback" else self.connection,
                    )
                    events.append("sync")
                    if outcome == "sync-error":
                        raise error

                def stop(_data):
                    events.append("container-stop")
                    if outcome == "final-stop-error":
                        raise error

                class Output(io.StringIO):
                    def write(self, text):
                        if text.startswith("Session history synced"):
                            events.append("success")
                        return super().write(text)

                output, errors = Output(), io.StringIO()
                with (
                    patch.object(box, "check_private_output"),
                    patch.object(box, "start_container", return_value={}),
                    patch.object(box, "prepare_runtime") as prepare,
                    patch.object(
                        service,
                        "history_source",
                        side_effect=history_scope,
                    ) as source,
                    patch.object(service, "Owner") as owner,
                    patch.object(box, "sync_sessions", side_effect=synchronize),
                    patch.object(box, "stop_container", side_effect=stop),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    if outcome.endswith("-error"):
                        with self.assertRaises(box.BoxError) as raised:
                            box.launch_environment(self.path, self.data, args)
                        self.assertIs(raised.exception, error)
                    else:
                        self.assertEqual(
                            box.launch_environment(self.path, self.data, args), 0
                        )
                prepare.assert_called_once_with(
                    self.path, self.data, args._opencode_release, history_only=True
                )
                source.assert_called_once_with(box, self.data)
                owner.assert_not_called()
                self.assertEqual(
                    events,
                    ["source-open", "sync", "source-closed", "container-stop"]
                    + ([] if outcome.endswith("-error") else ["success"]),
                )
                self.assertIsNone(service.live_connection())
                if outcome.endswith("-error"):
                    self.assertIn("oc-box sync", errors.getvalue())

    def test_endpoint_validation_refuses_remote_hosts_embedded_credentials_and_bad_passwords(
        self,
    ):
        self.assertEqual(service.validate_endpoint(self.connection), self.connection)
        for url in (
            "http://localhost:1",
            "http://example.test:1",
            "http://0.0.0.0:1",
            "http://user:password@127.0.0.1:1",
            "http://127.0.0.1:1/path",
            "http://127.0.0.1:1?password=bad",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                service.validate_endpoint({**self.connection, "url": url})
        with self.assertRaises(ValueError):
            service.validate_endpoint({**self.connection, "password": "short"})

    def test_ready_owner_can_represent_native_home_without_creating_a_session(self):
        owner = service.Owner(box, self.path, self.data)
        with patch.object(service, "api") as native:
            owner.publish(None)
            self.assertTrue(owner.ready)
            self.assertIsNone(owner.session)
            with self.assertRaisesRegex(box.BoxError, "Invalid live workspace"):
                owner.publish("not-a-session")
            native.assert_not_called()
        self.assertIsNone(owner.child)

    def test_unbound_terminal_refuses_before_native_work(self):
        connection = {**self.connection, "session": None}
        with (
            patch.object(service, "Gateway") as gateway,
            patch.object(service, "api") as native,
            patch.object(service, "client_command") as client,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "check_backend"),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(connection)
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(box.BoxError, "No open native session"):
                box.main(["terminal", "--workspace", self.data["id"]])
        for operation in (gateway, native, client, prepare, credentials):
            operation.assert_not_called()

    def test_secondary_tui_preserves_unbound_home_instead_of_forcing_history_root(self):
        connection = {**self.connection, "session": None}
        command = box.ContainerCommand(["synthetic-native-tui"], {})
        with (
            patch.object(box, "check_backend"),
            patch.object(service, "controller_running", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(connection)
            ),
            patch.object(service, "client_command", return_value=command) as client,
            patch.object(box, "run_tui", return_value=0),
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "launch_credentials") as credentials,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        self.assertNotIn("--session", client.call_args.args[3])
        self.assertIn(connection["url"], client.call_args.args[3])
        prepare.assert_not_called()
        credentials.assert_not_called()

    def test_client_environment_and_argv_do_not_inherit_provider_or_vault_values(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "synthetic-key",
                "CUSTOM_SECRET": "synthetic-custom",
                "BASH_ENV": "host-startup",
                "TERM": "xterm\nbad",
            },
        ):
            command = service.client_command(
                box,
                self.data,
                self.connection,
                [
                    "opencode",
                    "mini",
                    "--server",
                    self.connection["url"],
                    "--session",
                    "ses_shared",
                ],
            )
        self.assertNotIn("synthetic-key", " ".join(command))
        self.assertNotIn(self.connection["password"], " ".join(command))
        self.assertNotIn("OPENAI_API_KEY", command.environment)
        self.assertNotIn("CUSTOM_SECRET", command.environment)
        self.assertNotIn("BASH_ENV", command.environment)
        self.assertEqual(
            command.environment["OPENCODE_PASSWORD"], self.connection["password"]
        )
        self.assertIn("TERM", " ".join(command))
        self.assertNotIn("xterm\\nbad", " ".join(command))

    def test_client_bootstrap_cannot_load_project_python_modules(self):
        project = Path(self.data["project"])
        marker = project / "unexpected-python-import"
        shadow = (
            "from pathlib import Path\n"
            + f"Path({str(marker)!r}).write_text('unexpected')\n"
            + "raise RuntimeError('project module must not run')\n"
        )
        (project / "json.py").write_text(shadow)
        (project / "sitecustomize.py").write_text(shadow)
        command = service.client_command(
            box,
            self.data,
            self.connection,
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os; assert len(os.environ['OPENCODE_PASSWORD']) == 43; "
                "assert 'PYTHONPATH' not in os.environ; print('isolated-client')",
            ],
        )
        # Substitute only Podman exec; keep the actual production bootstrap.
        arguments = command[command.index(self.data["container"]) + 1 :]
        result = subprocess.run(
            arguments,
            env={**command.environment, "PYTHONPATH": str(project)},
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertEqual(result.stdout, "isolated-client\n")
        self.assertFalse(marker.exists())

    def test_api_response_matrix_preserves_empty_success_json_and_failures(self):
        command = box.ContainerCommand(["synthetic-native-api"], {})
        for output, expected, error in (
            ("", None, None),
            ("\n", None, None),
            (" \t\n", None, None),
            (
                '{"data":{"id":"ses_synthetic"}}\n',
                {"data": {"id": "ses_synthetic"}},
                None,
            ),
            ("null\n", None, None),
            ("false\n", False, None),
            ("[]\n", [], None),
            ("not JSON", None, json.JSONDecodeError),
            (None, None, box.BoxError),
        ):
            with (
                self.subTest(output=output),
                patch.object(service, "client_command", return_value=command),
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess(command, 0, output, ""),
                    side_effect=box.BoxError("synthetic failed API command")
                    if output is None
                    else None,
                ) as run,
            ):

                def invoke():
                    return service.api(
                        box, self.data, self.connection, "POST", "/api/location/reload"
                    )

                if error is None:
                    self.assertEqual(invoke(), expected)
                else:
                    with self.assertRaises(error):
                        invoke()
                self.assertTrue(run.call_args.kwargs["capture"])
                self.assertTrue(run.call_args.kwargs["private"])
                self.assertNotIn("check", run.call_args.kwargs)
                self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_api_passes_explicit_request_timeout_without_changing_private_transport(
        self,
    ):
        command = box.ContainerCommand(["synthetic-native-api"], {})
        with (
            patch.object(service, "client_command", return_value=command),
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess(command, 0, '{"data":{}}', ""),
            ) as run,
        ):
            self.assertEqual(
                service.api(
                    box,
                    self.data,
                    self.connection,
                    "GET",
                    "/api/session/active",
                    timeout=0.75,
                ),
                {"data": {}},
            )
        run.assert_called_once_with(command, capture=True, private=True, timeout=0.75)

    def test_live_terminal_joins_without_lifecycle_lock_unlock_refresh_or_stop(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch("oc_box.terminal_ui.run_tui", return_value=7) as relay,
            patch.object(vault, "hidden") as prompt,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "stop_container") as stop,
        ):
            self.assertEqual(box.main(["terminal", "--workspace", self.data["id"]]), 7)
        command = relay.call_args.args[0]
        self.assertNotIn("mini", command)
        self.assertNotIn("--server", command)
        self.assertIn("persistent-pty", " ".join(command))
        self.assertIn("ses_shared", command)
        self.assertNotIn("--standalone", command)
        self.assertFalse(relay.call_args.kwargs["show_loading"])
        for operation in (prompt, prepare, stop):
            operation.assert_not_called()

    def test_explicit_maintenance_still_requires_the_exclusive_lifecycle_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            self.assertRaisesRegex(box.BoxError, "active oc-box process"),
        ):
            box.main(["terminal", "--maintenance", "--workspace", self.data["id"]])

    def test_web_and_full_tui_attach_to_current_session_while_owner_holds_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            patch.object(service, "controller_running", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch.object(service, "web_session", return_value=0) as web,
            patch.object(box, "run_tui", return_value=0) as tui,
            patch.object(box, "prepare_runtime") as prepare,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            for flags in ([], ["--workspace-layout"]):
                with self.subTest(flags=flags):
                    self.assertEqual(
                        box.main(
                            [
                                "web",
                                "--workspace",
                                self.data["id"],
                                "--no-open",
                                "--attachments",
                                "--session",
                                "ses_explicit",
                                *flags,
                            ]
                        ),
                        0,
                    )
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        self.assertEqual(web.call_args.args[2]["session"], "ses_shared")
        self.assertTrue(web.call_args.args[3].attachments)
        self.assertEqual(web.call_args.args[3].web_session, "ses_explicit")
        self.assertIn("ses_shared", tui.call_args.args[0])
        prepare.assert_not_called()

    def test_additional_native_clients_choose_host_open_independently_of_owner(self):
        self.data["opencode_version"] = "2.3.2"
        self.connection["version"] = "2.3.2"
        box.atomic_json(self.path, self.data)
        before = self.path.read_bytes()
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(service, "controller_running", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch.object(service, "client_command", return_value=[]) as command,
            patch.object(box, "run_native_tui", return_value=0),
            patch.object(box, "detect_opencode_release") as native,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(vault, "hidden") as prompt,
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch("oc_box.host_drop.preflight", return_value="konsole"),
            patch("oc_box.host_file_open_policy.enabled", return_value=True),
        ):
            for options, wanted in (
                (["--no-host-file-open"], False),
                (["--host-file-open"], True),
                ([], True),
            ):
                with self.subTest(options=options):
                    self.assertEqual(
                        box.main(["--workspace", self.data["id"], *options]), 0
                    )
                    self.assertIs(command.call_args.kwargs["file_preview"], wanted)
        native.assert_not_called()
        prepare.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_web_options_or_v1_fail_before_startup_and_credentials(self):
        with (
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "check_backend") as backend,
            patch.object(vault, "hidden") as prompt,
        ):
            with self.assertRaisesRegex(box.BoxError, "requires OpenCode 2.x"):
                box.main(["web", "--project", self.data["project"]])
        backend.assert_not_called()
        prompt.assert_not_called()
        with (
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            self.assertRaisesRegex(box.BoxError, "--port"),
        ):
            box.main(["web", "--port", "65536"])
        with self.assertRaisesRegex(box.BoxError, "--maintenance"):
            box.main(["start", "--maintenance"])

    def test_busy_web_port_fails_before_workspace_lookup_or_vault_prompt(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with (
                patch.object(box, "detect_opencode_release", return_value="2.2.6"),
                patch.object(box, "find_workspace") as lookup,
                patch.object(vault, "hidden") as prompt,
                self.assertRaises(OSError),
            ):
                box.main(["web", "--port", str(listener.getsockname()[1])])
            lookup.assert_not_called()
            prompt.assert_not_called()

    def test_browser_launcher_does_not_receive_provider_credentials_or_pairing_link_in_argv(
        self,
    ):
        gateway = SimpleNamespace(url="http://127.0.0.1:5555")
        args = SimpleNamespace(port=0, no_open=False)
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "synthetic-provider",
                    "CUSTOM_SECRET": "synthetic-secret",
                },
            ),
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ),
            patch.object(service, "api", return_value={"code": "synthetic_pair_code"}),
            patch.object(service.subprocess, "run") as browser,
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                service.web_session(box, self.data, self.connection, args), 0
            )
        call = browser.call_args
        self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
        self.assertNotIn("CUSTOM_SECRET", call.kwargs["env"])
        self.assertNotIn("synthetic_pair_code", " ".join(call.args[0]))
        self.assertIn("synthetic_pair_code", call.kwargs["input"])

    def test_v2_history_cli_uses_active_service_without_persisting_control_password(
        self,
    ):
        connection = {**self.connection}
        token = service.CONNECTION.set(connection)
        try:
            command = box.exec_command(
                self.data, [box.CONTAINER_OPENCODE, "export", "ses_shared"]
            )
            self.assertIn("--server", command)
            self.assertNotIn("--standalone", command)
            self.assertNotIn(connection["password"], " ".join(command))
            self.assertEqual(
                command.environment["OPENCODE_PASSWORD"], connection["password"]
            )
            self.assertNotIn(connection["password"], self.path.read_text())
        finally:
            service.CONNECTION.reset(token)

    def test_web_owner_can_start_foreground_and_client_exit_waits_before_sync(self):
        calls = []
        stages, active = [], []

        @contextlib.contextmanager
        def stage(label, **_options):
            stages.append(label)
            active.append(label)
            try:
                yield
            finally:
                active.pop()

        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: calls.append(("publish", value)),
            wait_clients=lambda: calls.append(("wait", None)),
        )

        def ready(*_args):
            self.assertEqual(active, ["Waiting for OpenCode activity to settle"])
            self.assertEqual(owner._client_exit_code, 0)
            calls.append(("ready", None))

        def synchronize(*_args):
            self.assertEqual(active, ["Syncing session history"])
            calls.append(("sync", None))

        args = box.parser().parse_args(["start"])
        args.command, args.port, args.no_open = "web", 0, True
        with (
            patch.object(box, "loading_stage", side_effect=stage),
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(
                service,
                "web_session",
                side_effect=lambda *_: calls.append(("web", None)) or 0,
            ),
            patch.object(
                service,
                "wait_history",
                side_effect=ready,
            ) as readiness,
            patch.object(
                box,
                "sync_sessions",
                side_effect=synchronize,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.launch_client(self.path, self.data, args, "2.2.6", None, owner), 0
            )
        self.assertEqual(
            calls,
            [
                ("publish", "ses_shared"),
                ("web", None),
                ("wait", None),
                ("ready", None),
                ("sync", None),
            ],
        )
        self.assertEqual(
            stages,
            [
                "Finding workspace session",
                "Waiting for OpenCode activity to settle",
                "Syncing session history",
            ],
        )
        self.assertEqual(owner._client_exit_code, 0)
        readiness.assert_called_once_with(box, self.data, owner.connection)

    def test_readiness_failure_keeps_sync_pending_without_export_or_success_message(
        self,
    ):
        args = box.parser().parse_args(["start"])
        diagnostic = (
            "History wait timeout: phase=foreground-query; "
            "last complete observation: foreground=0, jobs-running=0, "
            "jobs-completed=0, jobs-error=0, jobs-cancelled=1."
        )
        for code, error in (
            (code, error)
            for code in (0, 7, 130)
            for error in (
                box.BoxError("OpenCode 2.x history wait failed (activity-timeout)."),
                box.HistoryWaitTimeout(diagnostic),
                box.HistoryWaitTimeout(
                    diagnostic,
                    timing_diagnostic="History wait timing: complete-pairs=0.",
                ),
                box.BoxError("OpenCode 2.x history wait failed (activity-api-failed)."),
            )
        ):
            owner = SimpleNamespace(
                connection=self.connection,
                publish=Mock(),
                wait_clients=Mock(),
            )
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(
                    client_code=code, error=type(error).__name__, reason=str(error)
                ),
                patch.object(
                    box, "refresh_sessions", return_value={"last": "ses_shared"}
                ),
                patch.object(box, "run_tui", return_value=code),
                patch.object(
                    service,
                    "wait_history",
                    side_effect=error,
                ) as readiness,
                patch.object(box, "sync_sessions") as sync,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(
                    box.launch_client(self.path, self.data, args, "2.2.6", None, owner),
                    code or 1,
                )
            owner.wait_clients.assert_called_once()
            self.assertEqual(owner._client_exit_code, code)
            readiness.assert_called_once_with(box, self.data, owner.connection)
            sync.assert_not_called()
            self.assertIn("history sync pending", errors.getvalue())
            self.assertIn(str(error), errors.getvalue())
            self.assertIn("Container data retained", errors.getvalue())
            self.assertIn("oc-box sync", errors.getvalue())
            self.assertNotIn("Session history synced", output.getvalue())
            self.assertEqual(
                errors.getvalue().count("oc-box: " + diagnostic),
                int(isinstance(error, box.HistoryWaitTimeout)),
            )
            timing = getattr(error, "timing_diagnostic", None)
            self.assertEqual(
                errors.getvalue().count("oc-box: History wait timing:"),
                int(timing is not None),
            )
            if timing is not None:
                self.assertLess(
                    errors.getvalue().index(timing),
                    errors.getvalue().index("history sync pending"),
                )

    def test_failed_active_client_still_waits_for_attached_clients(self):
        args = box.parser().parse_args(["start"])
        for error in (OSError("terminal failed"), KeyboardInterrupt()):
            owner = SimpleNamespace(
                connection=self.connection,
                publish=lambda value: None,
                wait_clients=Mock(),
            )
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    box, "refresh_sessions", return_value={"last": "ses_shared"}
                ),
                patch.object(box, "run_tui", side_effect=error),
                patch.object(service, "wait_history") as readiness,
                patch.object(box, "sync_sessions") as sync,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(type(error)) as raised,
            ):
                box.launch_client(self.path, self.data, args, "2.2.6", None, owner)
            self.assertIs(raised.exception, error)
            owner.wait_clients.assert_called_once()
            readiness.assert_not_called()
            sync.assert_not_called()
            self.assertFalse(hasattr(owner, "_client_exit_code"))

    def test_23_expected_live_failures_defer_one_retry_only_for_safe_captured_source(
        self,
    ):
        args = box.parser().parse_args(["start"])
        retryable = box.BoxError("fixed-live-error")
        retryable.history_retryable = True
        cases = [
            (retryable, True),
            (box.BoxError("unclassified-error"), False),
            (box.HistoryPending("fixed-pending-error"), True),
            (box.HistoryWaitTimeout("count-only"), False),
            (ValueError("fixed-invalid-response"), False),
            (KeyError("fixed-invalid-field"), False),
        ]
        for attribute in (
            "history_compatibility_category",
            "history_reader_category",
            "history_contract_refusal",
            "history_source_cleanup_failed",
        ):
            error = box.BoxError("fixed-refusal")
            error.history_retryable = True
            setattr(error, attribute, True)
            cases.append((error, False))
        for release in ("2.2.7", "2.3.2", "2.4.0"):
            for available in (False, True):
                for stage in ("wait", "sync"):
                    for error, eligible in cases:
                        with self.subTest(
                            release=release, source=available, stage=stage, error=error
                        ):
                            owner = SimpleNamespace(
                                connection=self.connection,
                                publish=Mock(),
                                wait_clients=Mock(),
                                _history_source=SimpleNamespace()
                                if available
                                else None,
                            )
                            output = io.StringIO()
                            with (
                                patch.object(
                                    box,
                                    "refresh_sessions",
                                    return_value={"last": "ses_shared"},
                                ),
                                patch.object(box, "run_tui", return_value=7),
                                patch.object(
                                    service,
                                    "wait_history",
                                    side_effect=error if stage == "wait" else None,
                                ),
                                patch.object(
                                    box,
                                    "sync_sessions",
                                    side_effect=error if stage == "sync" else None,
                                ) as sync,
                                contextlib.redirect_stderr(output),
                                contextlib.redirect_stdout(io.StringIO()),
                            ):
                                result = box.launch_client(
                                    self.path,
                                    {**self.data, "opencode_version": release},
                                    args,
                                    release,
                                    None,
                                    owner,
                                )
                            self.assertEqual(result, 7)
                            deferred = eligible and available and release == "2.3.2"
                            self.assertEqual(
                                getattr(owner, "_history_failure", None) is error,
                                deferred,
                            )
                            self.assertEqual(
                                "oc-box sync" in output.getvalue(), not deferred
                            )
                            self.assertEqual(sync.call_count, int(stage == "sync"))
                            owner.wait_clients.assert_called_once()

    def test_malformed_api_json_retains_contract_refusal_through_wait_normalization(
        self,
    ):
        data = {**self.data, "opencode_version": "2.3.2"}
        for target in ("active", "jobs"):
            responses = [subprocess.CompletedProcess([], 0, "private-invalid-json", "")]
            if target == "jobs":
                responses.insert(
                    0, subprocess.CompletedProcess([], 0, '{"data":{}}', "")
                )
            with (
                self.subTest(target=target),
                patch.object(box, "run", side_effect=responses),
            ):
                with self.assertRaises(box.BoxError) as raised:
                    service.wait_history(box, data, self.connection)
            error = raised.exception
            self.assertNotIsInstance(error, box.HistoryWaitTimeout)
            self.assertEqual(
                error.history_compatibility_category,
                "invalid-active-response"
                if target == "active"
                else "invalid-jobs-response",
            )
            self.assertFalse(box.history_recovery.exit_retry_eligible(box, data, error))
            self.assertNotIn("private-invalid-json", str(error))

    def test_cancelled_job_timeout_is_deferred_only_for_a_captured_source(self):
        args = box.parser().parse_args(["start"])
        for available in (False, True):
            with self.subTest(source_available=available):
                owner = SimpleNamespace(
                    connection=self.connection,
                    publish=Mock(),
                    wait_clients=Mock(),
                    _history_source=SimpleNamespace() if available else None,
                )
                timing = "History wait timing: complete-pairs=2."
                error = box.HistoryWaitTimeout(
                    "count-only", recovery_eligible=True, timing_diagnostic=timing
                )
                output = io.StringIO()
                with (
                    patch.object(
                        box, "refresh_sessions", return_value={"last": "ses_shared"}
                    ),
                    patch.object(box, "run_tui", return_value=7),
                    patch.object(service, "wait_history", side_effect=error),
                    patch.object(box, "sync_sessions") as sync,
                    contextlib.redirect_stderr(output),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        box.launch_client(
                            self.path, self.data, args, "2.2.7", None, owner
                        ),
                        7,
                    )
                sync.assert_not_called()
                owner.wait_clients.assert_called_once()
                self.assertEqual(owner._client_exit_code, 7)
                self.assertEqual(output.getvalue().count("oc-box: " + timing), 1)
                if not available:
                    self.assertIn("oc-box sync", output.getvalue())
                self.assertEqual(
                    "history sync pending" in output.getvalue(), not available
                )
                self.assertEqual(
                    getattr(owner, "_history_timeout", None) is error, available
                )

    def test_local_controller_leases_delay_owner_shutdown_and_preserve_lock(self):
        owner = service.Owner(box, self.path, self.data)
        code = (
            "import json,sys; print(json.dumps("
            + repr(
                {"url": self.connection["url"], "password": self.connection["password"]}
            )
            + "),flush=True); sys.stdin.read()"
        )

        def local_exec(_data, _arguments):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", code], dict(os.environ)
            )

        with (
            box.workspace_lock(self.path),
            patch.object(box, "exec_command", side_effect=local_exec),
            patch.object(box, "inspect_owned", return_value=self.info),
            patch.object(
                service,
                "tab_inventory",
                return_value=[
                    {
                        "id": "ses_shared",
                        "location": {"directory": self.data["project"]},
                        "time": {"updated": 1},
                    }
                ],
            ),
            owner,
        ):
            owner.publish("ses_shared")
            self.assertTrue(service.controller_running(box, self.data))
            with service.attach(box, self.path, self.data) as attached:
                self.assertEqual(attached["session"], "ses_shared")
                self.assertEqual(owner.clients, 1)
                exited = threading.Event()
                thread = threading.Thread(
                    target=lambda: (owner.wait_clients(), exited.set())
                )
                thread.start()
                time.sleep(0.05)
                self.assertFalse(exited.is_set())
                with self.assertRaises(box.BoxError):
                    with box.workspace_lock(self.path):
                        pass
            thread.join(timeout=2)
            self.assertTrue(exited.is_set())
        self.assertFalse(owner.address.exists())
        self.assertIsNone(service.live_connection())

    def test_info_refresh_holds_only_a_temporary_metadata_lease(self):
        owner = service.Owner(box, self.path, self.data)
        program = (
            "import json,os,sys;"
            "print(json.dumps({'url':'http://127.0.0.1:12345',"
            "'password':os.environ['OPENCODE_PASSWORD']}),flush=True);"
            "sys.stdin.read()"
        )

        def local_exec(_data, _arguments):
            return box.ContainerCommand(
                [sys.executable, "-I", "-S", "-c", program],
                {
                    "PATH": os.environ["PATH"],
                    "OPENCODE_PASSWORD": self.connection["password"],
                },
            )

        def inspect(*_args, **_kwargs):
            self.assertEqual(owner.clients, 1)
            return box.native_tabs.Snapshot("valid")

        with (
            box.workspace_lock(self.path),
            patch.object(box, "exec_command", side_effect=local_exec),
            patch.object(box.native_tabs, "inspect", side_effect=inspect),
            owner,
        ):
            owner.publish(None)
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(3)
                stream.connect(str(owner.address))
                stream.sendall(
                    (
                        json.dumps({"action": "info", "workspace": self.data["id"]})
                        + "\n"
                    ).encode()
                )
                response = service.read_control(stream)
                self.assertIsNone(response["session"])
                with owner.condition:
                    self.assertTrue(
                        owner.condition.wait_for(lambda: owner.clients == 0, timeout=1)
                    )
                # The info socket remains open; it must not become an outer lease.
                self.assertEqual(owner.clients, 0)
        self.assertIsNone(service.live_connection())

    def test_runtime_directory_must_be_private_and_generated_socket_never_overwrites_files(
        self,
    ):
        address = service.socket_path(box, self.data)
        address.write_text("unrelated")
        with self.assertRaisesRegex(box.BoxError, "Unexpected file"):
            service.Owner(box, self.path, self.data).__enter__()
        self.assertEqual(address.read_text(), "unrelated")
        address.unlink()
        address.parent.chmod(0o777)
        with self.assertRaisesRegex(box.BoxError, "unsafe"):
            service.socket_path(box, self.data)

    def test_browser_gateway_streams_http_pairing_redirect_and_binary_upgrades(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/auth/connect/"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", "native-cookie=fixture")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/upgrade":
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.end_headers()
                    self.wfile.write(b"binary-frame\x00\xff")
                    self.wfile.flush()
                    self.connection.recv(1)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"hello")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = {
            **self.connection,
            "url": "http://127.0.0.1:" + str(server.server_port),
        }

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        with (
            patch.object(service, "relay_command", side_effect=local_relay),
            service.Gateway(box, self.data, connection) as gateway,
        ):
            with urlopen(gateway.url + "/", timeout=5) as response:
                self.assertEqual(response.read(), b"hello")
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /auth/connect/fixture HTTP/1.1\r\nHost: 127.0.0.1:1\r\nAccept: text/html\r\n\r\n"
                )
                data = stream.recv(8192)
                self.assertIn(gateway.session_route().encode(), data)
                self.assertIn(b"native-cookie=fixture", data)
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /upgrade HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                )
                data = bytearray()
                while b"binary-frame\x00\xff" not in data:
                    data.extend(stream.recv(8192))
                self.assertIn(b"101", data)
                stream.sendall(b"x")


class GatewayHTTPTests(unittest.TestCase):
    def test_disconnect_with_buffered_input_releases_gateway_slot(self):
        gateway = service.Gateway(box, {}, {"url": "http://127.0.0.1:12345"})
        child = Mock()
        child.poll.return_value = 0
        child.stdin.close.side_effect = BrokenPipeError
        stream, peer = socket.socketpair()
        self.addCleanup(stream.close)
        with (
            peer,
            patch.object(
                service,
                "relay_command",
                return_value=box.ContainerCommand(["native"], {}),
            ),
            patch.object(service.subprocess, "Popen", return_value=child),
        ):
            peer.shutdown(socket.SHUT_WR)
            self.assertTrue(gateway.capacity.acquire(blocking=False))
            gateway.forward(stream)
        child.stdout.close.assert_called_once()
        self.assertFalse(gateway.children)
        self.assertTrue(
            all(gateway.capacity.acquire(blocking=False) for _ in range(32))
        )
        self.assertFalse(gateway.capacity.acquire(blocking=False))

    @contextlib.contextmanager
    def relay(self, handler):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(
            target=lambda: upstream.serve_forever(poll_interval=0.02), daemon=True
        )
        thread.start()

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        connection = {
            "workspace": "a" * 32,
            "url": "http://127.0.0.1:" + str(upstream.server_port),
            "password": "a" * 43,
            "session": None,
        }
        try:
            with (
                patch.object(service, "relay_command", side_effect=local_relay),
                patch.object(
                    native_tabs,
                    "inspect",
                    return_value=native_tabs.Snapshot("missing", reason="absent"),
                ),
                service.Gateway(
                    box, {"id": connection["workspace"]}, connection
                ) as gateway,
            ):
                yield gateway
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    @staticmethod
    def read_head(reader):
        result = bytearray()
        while not result.endswith(b"\r\n\r\n"):
            line = reader.readline(65537 - len(result))
            if not line or len(result) + len(line) > 65536:
                raise AssertionError("Fixture response headers missing or oversized.")
            result.extend(line)
        return bytes(result)

    def test_fragmented_interims_pairing_headers_and_body_bytes(self):
        interim = (
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
        )
        body = b"\x00\xffLocation: /\r\nConnection: keep-alive\r\n\r\nbody"
        fields = (
            b"HTTP/1.1 302 Found\r\nlOcAtIoN: /\r\nSet-Cookie: native=fixture\r\n"
            b"Content-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: keep-alive\r\nkEeP-aLiVe: timeout=60\r\n\r\n"
        )
        wire = interim + fields + body
        expected = (
            interim + b"HTTP/1.1 302 Found\r\nLocation: /auth/oc-box-sessions\r\n"
            b"Set-Cookie: native=fixture\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                route = Mock(return_value=web_sessions.INDEX)
                response = service.NativeResponseHeaders(route)
                self.assertEqual(
                    response.feed(wire[:split]) + response.feed(wire[split:]), expected
                )
                self.assertTrue(response.complete)
                self.assertEqual(response.pending, b"")
                route.assert_called_once_with()
                opaque = b"binary body" * 10000
                self.assertIs(response.feed(opaque), opaque)

    def test_header_bounds_exclude_body_and_upgrade_is_byte_exact(self):
        response = service.NativeResponseHeaders()
        response.LIMIT = 80
        body = b"\x00\xff" * 10000
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 20000\r\n\r\n"
        self.assertEqual(
            response.feed(head + body),
            head[:-2] + b"Connection: close\r\n\r\n" + body,
        )
        for data in (
            b"not HTTP\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nX: " + b"x" * 65536,
            (b"HTTP/1.1 103 Early Hints\r\n\r\n" * 3000),
        ):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                service.NativeResponseHeaders().feed(data)
        upgrade = (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nKeep-Alive: untouched\r\n\r\n\x82\x02\x00\xff"
        )
        response = service.NativeResponseHeaders(
            Mock(side_effect=AssertionError("pair"))
        )
        self.assertEqual(response.feed(upgrade), upgrade)
        self.assertIs(response.feed(body), body)

    def test_keepalive_pair_cookie_and_return_navigation_reenter_host_routes(self):
        observed = []
        accepted = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(handler):
                observed.append((handler.path, handler.headers.get("Cookie")))
                if handler.path == "/auth/connect/fixture":
                    handler.send_response(302)
                    handler.send_header("Location", "/")
                    handler.send_header(
                        "Set-Cookie", "native=fixture; Path=/; HttpOnly"
                    )
                    body = b""
                elif handler.path == "/api/info":
                    handler.send_response(
                        200
                        if handler.headers.get("Cookie") == "native=fixture"
                        else 401
                    )
                    body = b"{}"
                elif handler.path == "/native-page":
                    handler.send_response(200)
                    body = b"native page"
                else:
                    handler.send_response(404)
                    body = b"native has no host route"
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Connection", "keep-alive")
                handler.send_header("Keep-Alive", "timeout=60")
                handler.end_headers()
                handler.wfile.write(body)

        original = service.Gateway.forward

        def forward(gateway, stream):
            accepted.append(stream.getpeername())
            return original(gateway, stream)

        with (
            patch.object(service.Gateway, "forward", forward),
            self.relay(Handler) as gateway,
        ):
            client = HTTPConnection("127.0.0.1", urlparse(gateway.url).port, timeout=5)
            self.addCleanup(client.close)
            headers = {"Connection": "keep-alive", "Accept": "text/html"}
            client.request("GET", "/auth/connect/fixture", headers=headers)
            paired = client.getresponse()
            self.assertEqual(paired.status, 302)
            location = paired.getheader("Location")
            cookie = paired.getheader("Set-Cookie").split(";", 1)[0]
            close = [paired.will_close]
            self.assertEqual(location, web_sessions.INDEX)
            self.assertEqual(cookie, "native=fixture")
            paired.read()
            for path in (location, "/native-page", location, "/native-page", location):
                client.request("GET", path, headers={**headers, "Cookie": cookie})
                response = client.getresponse()
                body = response.read()
                # Old Gateway tunnels the redirect follow-up to native, giving 404.
                self.assertEqual(response.status, 200)
                close.append(response.will_close)
                self.assertEqual(response.getheader("Connection"), "close")
                self.assertIsNone(response.getheader("Keep-Alive"))
                if path == location:
                    self.assertIn(b'href="/"', body)
                else:
                    self.assertEqual(body, b"native page")
            self.assertTrue(all(close))
            self.assertEqual(len(accepted), 6)
            self.assertFalse(any(path == location for path, _cookie in observed))
            self.assertEqual(
                [cookie for path, cookie in observed if path == "/api/info"],
                ["native=fixture"] * 3,
            )
            client.close()

    def test_head_204_download_and_expect_continue_upload_are_not_truncated(self):
        payload = (bytes(range(256)) + b"\r\nLocation: /\r\n\r\n") * 4096
        interims = (
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
        )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(handler, code, body=b"", length=None):
                handler.send_response(code)
                if length is not None:
                    handler.send_header("Content-Length", str(length))
                handler.send_header("Connection", "keep-alive")
                handler.send_header("Keep-Alive", "timeout=60")
                handler.end_headers()
                for start in range(0, len(body), 8192):
                    handler.wfile.write(body[start : start + 8192])

            def do_HEAD(handler):
                handler.reply(200, length=len(payload))

            def do_GET(handler):
                if handler.path == "/empty":
                    handler.reply(204)
                else:
                    handler.reply(200, payload, len(payload))

            def handle_expect_100(handler):
                handler.wfile.write(interims)
                handler.wfile.flush()
                return True

            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers["Content-Length"]))
                handler.reply(200, body, len(body))

        with self.relay(Handler) as gateway:
            client = HTTPConnection("127.0.0.1", urlparse(gateway.url).port, timeout=5)
            try:
                for method, path, expected in (
                    ("HEAD", "/download", b""),
                    ("GET", "/empty", b""),
                    ("GET", "/download", payload),
                ):
                    client.request(method, path, headers={"Connection": "keep-alive"})
                    response = client.getresponse()
                    self.assertTrue(response.will_close)
                    self.assertEqual(response.read(), expected)
                    if method == "HEAD":
                        self.assertEqual(
                            response.getheader("Content-Length"), str(len(payload))
                        )
            finally:
                client.close()
            with (
                socket.create_connection(
                    ("127.0.0.1", urlparse(gateway.url).port), timeout=5
                ) as stream,
                stream.makefile("rb") as reader,
            ):
                stream.sendall(
                    (
                        "POST /upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        "Connection: keep-alive\r\nExpect: 100-continue\r\nContent-Length: "
                        + str(len(payload))
                        + "\r\n\r\n"
                    ).encode()
                )
                self.assertEqual(
                    self.read_head(reader) + self.read_head(reader), interims
                )
                for start in range(0, len(payload), 8192):
                    stream.sendall(payload[start : start + 8192])
                head = self.read_head(reader)
                self.assertIn(b"Connection: close\r\n", head)
                self.assertEqual(reader.read(len(payload)), payload)

    def test_chunked_sse_streams_before_completion_and_101_stays_bidirectional(self):
        release = threading.Event()
        first = b"data: first\x00\xff\r\nLocation: /\r\n\r\n"
        second = b"data: second\r\n\r\n"
        chunk = lambda value: (
            format(len(value), "x").encode() + b"\r\n" + value + b"\r\n"
        )
        upgrade = (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nKeep-Alive: untouched\r\n\r\n"
        )
        frame = b"\x82\x04\x00\xffAB"
        masked = b"\x82\x82\x01\x02\x03\x04\x40\x40"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(handler):
                if handler.path == "/upgrade":
                    handler.wfile.write(upgrade + frame)
                    handler.wfile.flush()
                    handler.wfile.write(handler.rfile.read(len(masked)))
                    handler.wfile.flush()
                    handler.close_connection = True
                    return
                handler.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n"
                    b"Keep-Alive: timeout=60\r\n\r\n" + chunk(first)
                )
                handler.wfile.flush()
                if release.wait(5):
                    handler.wfile.write(
                        chunk(second) + b"0\r\nX-Trailer: fixed\r\n\r\n"
                    )
                    handler.wfile.flush()

        try:
            with self.relay(Handler) as gateway:
                with (
                    socket.create_connection(
                        ("127.0.0.1", urlparse(gateway.url).port), timeout=5
                    ) as stream,
                    stream.makefile("rb") as reader,
                ):
                    stream.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    head = self.read_head(reader)
                    self.assertIn(b"Connection: close\r\n", head)
                    self.assertIn(b"Transfer-Encoding: chunked\r\n", head)
                    self.assertEqual(reader.read(len(chunk(first))), chunk(first))
                    release.set()
                    tail = chunk(second) + b"0\r\nX-Trailer: fixed\r\n\r\n"
                    self.assertEqual(reader.read(len(tail)), tail)
                with (
                    socket.create_connection(
                        ("127.0.0.1", urlparse(gateway.url).port), timeout=5
                    ) as stream,
                    stream.makefile("rb") as reader,
                ):
                    stream.sendall(
                        b"GET /upgrade HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                    )
                    self.assertEqual(self.read_head(reader), upgrade)
                    self.assertEqual(reader.read(len(frame)), frame)
                    stream.sendall(masked)
                    self.assertEqual(reader.read(len(masked)), masked)
        finally:
            release.set()
