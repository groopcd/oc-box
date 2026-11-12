import contextlib
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

from oc_box import oc_box as box, workspace_service as service, vault


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

    def assert_wait_error(self, category, **options):
        with self.assertRaises(box.BoxError) as raised:
            service.wait_history(box, self.data, self.connection, **options)
        self.assertEqual(
            str(raised.exception),
            f"OpenCode 2.x history wait failed ({category}).",
        )
        self.assertTrue(raised.exception.__suppress_context__)
        if category == "activity-timeout":
            self.assertIsInstance(raised.exception, box.HistoryWaitTimeout)
            self.assertNotIn("private-sentinel", raised.exception.diagnostic)
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
        service.wait_history(box, self.data, self.connection)
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
                    self.assert_wait_error("activity-api-failed")
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
                        f"last complete observation: foreground=0, {counts}.",
                    )
                    self.assertNotIn(job["id"], error.diagnostic)
                    self.assertNotIn(job["sessionID"], error.diagnostic)
                    self.assertNotIn(kind, error.diagnostic)

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
            "last complete observation: foreground=9999+, jobs-running=0, "
            "jobs-completed=9999+, jobs-error=0, jobs-cancelled=0.",
        )

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
            self.assertEqual(
                box.main(["web", "--workspace", self.data["id"], "--no-open"]), 0
            )
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        self.assertEqual(web.call_args.args[2]["session"], "ses_shared")
        self.assertIn("ses_shared", tui.call_args.args[0])
        prepare.assert_not_called()

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
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: calls.append(("publish", value)),
            wait_clients=lambda: calls.append(("wait", None)),
        )
        args = box.parser().parse_args(["start"])
        args.command, args.port, args.no_open = "web", 0, True
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(
                service,
                "web_session",
                side_effect=lambda *_: calls.append(("web", None)) or 0,
            ),
            patch.object(
                service,
                "wait_history",
                side_effect=lambda *_: calls.append(("ready", None)),
            ) as readiness,
            patch.object(
                box,
                "sync_sessions",
                side_effect=lambda *_: calls.append(("sync", None)),
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
            for code in (0, 7)
            for error in (
                box.BoxError("OpenCode 2.x history wait failed (activity-timeout)."),
                box.HistoryWaitTimeout(diagnostic),
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
            readiness.assert_called_once_with(box, self.data, owner.connection)
            sync.assert_not_called()
            self.assertIn("history sync pending", errors.getvalue())
            self.assertIn(str(error), errors.getvalue())
            self.assertIn("Container data retained", errors.getvalue())
            self.assertNotIn("Session history synced", output.getvalue())
            self.assertEqual(
                errors.getvalue().count("oc-box: " + diagnostic),
                int(isinstance(error, box.HistoryWaitTimeout)),
            )

    def test_failed_active_client_still_waits_for_attached_clients(self):
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: None,
            wait_clients=Mock(),
        )
        args = box.parser().parse_args(["start"])
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(box, "run_tui", side_effect=OSError("terminal failed")),
            patch.object(service, "wait_history") as readiness,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(OSError),
        ):
            box.launch_client(self.path, self.data, args, "2.2.6", None, owner)
        owner.wait_clients.assert_called_once()
        readiness.assert_not_called()

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
