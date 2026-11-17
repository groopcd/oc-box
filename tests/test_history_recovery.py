import contextlib
import copy
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import history_recovery as recovery, oc_box as box, workspace_control


class HistoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-exit-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "state")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data.update(ready=True, opencode_version="2.2.7")
        box.atomic_json(self.path, self.data)
        self.token = workspace_control.capture(self.path)
        self.ident = "a" * 64
        self.running, self.calls = True, []
        self.info = {
            "Id": self.ident,
            "Config": {
                "Labels": {box.LABEL: self.data["id"]},
                "Entrypoint": ["/bin/sh"],
                "Cmd": ["-c", "exec sleep infinity"],
                "Env": [
                    "PATH=" + recovery.history_reader.PATH,
                    "HOME=" + self.data["home"],
                ],
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}, "PidMode": "private"},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.project),
                    "Destination": str(self.project),
                },
                {
                    "Type": "volume",
                    "Name": self.data["home_volume"],
                    "Source": str(self.root / "private-home"),
                    "Destination": self.data["home"],
                },
            ],
        }
        self.source = recovery.capture(box, self.data, self.info, self.token)
        self.reader = Mock(spec=recovery.history_reader.Reader)
        self.reader.__enter__ = Mock(
            side_effect=lambda: self.record("reader") or self.reader
        )
        self.reader.__exit__ = Mock(return_value=False)
        self.reader.assert_idle.side_effect = lambda: self.record("idle")
        self.output = io.StringIO()
        self.inject = lambda _event: None

    def record(self, event):
        self.calls.append(event)
        self.inject(event)

    def run_command(self, command, **options):
        self.assertEqual(command[0], "podman")
        self.assertTrue(options["capture"])
        self.assertTrue(options["private"])
        self.assertEqual(options["env"], box.maintenance_host_env())
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 20)
        args = command[1:]
        if args == ["container", "inspect", self.ident]:
            self.record("inspect-running" if self.running else "inspect-stopped")
            info = copy.deepcopy(self.info)
            info["State"] = {
                "Running": self.running,
                "Paused": False,
                "Restarting": False,
                "Status": "running" if self.running else "exited",
                "Pid": 123 if self.running else 0,
                "ConmonPid": 123 if self.running else 0,
            }
            value = [info]
        elif args == ["volume", "inspect", self.data["home_volume"]]:
            self.record("volume")
            value = [
                {
                    "Labels": {box.LABEL: self.data["id"]},
                    "Mountpoint": self.source.mount_signature[2],
                }
            ]
        elif args == ["stop", "--time", "10", self.ident]:
            self.running = False
            self.record("stop")
            value = None
        elif args == ["start", self.ident]:
            self.running = True
            self.record("start")
            value = None
        else:
            self.fail("Unexpected recovery resource operation.")
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def recover(self, *, sync=None):
        with (
            patch.object(box, "run", side_effect=self.run_command),
            patch.object(
                recovery.history_reader, "Reader", return_value=self.reader
            ) as lease,
            patch.object(
                box,
                "sync_sessions",
                side_effect=sync or (lambda *_a, **_k: self.record("sync")),
            ) as synchronize,
            contextlib.redirect_stderr(self.output),
        ):
            result = recovery.recover(box, self.path, self.data, self.source)
        return result, lease, synchronize

    def test_one_existing_id_cycle_and_one_reader_after_verified_stop(self):
        result, lease, sync = self.recover()
        self.assertTrue(result)
        self.assertFalse(self.running)
        self.assertEqual(
            self.calls,
            [
                "inspect-running",
                "volume",
                "stop",
                "inspect-stopped",
                "start",
                "inspect-running",
                "reader",
                "idle",
                "sync",
                "idle",
                "stop",
                "inspect-stopped",
            ],
        )
        lease.assert_called_once()
        pinned = lease.call_args.args[1]
        self.assertEqual(pinned["container"], self.ident)
        self.assertNotEqual(self.data["container"], self.ident)
        sync.assert_called_once_with(self.path, pinned, announce=False)
        self.assertIn("Execution was interrupted", self.output.getvalue())
        self.assertIn(
            "original history-wait failure remains nonzero", self.output.getvalue()
        )
        self.assertNotIn("normal host OpenCode database", self.output.getvalue())
        workspace_control.require_certain(self.path)

    def test_initially_stopped_or_replaced_container_never_restarts(self):
        for condition in (
            "stopped",
            "replaced",
            "healthcheck",
            "mount",
            "restart-policy",
            "startup-hook",
            "shadow-path",
            "shared-pids",
        ):
            with self.subTest(condition=condition):
                self.running = True
                self.info["Id"] = self.ident
                original = copy.deepcopy(self.info)
                if condition == "stopped":
                    self.running = False
                elif condition == "replaced":
                    self.info["Id"] = "b" * 64
                elif condition == "healthcheck":
                    self.info["Config"]["Healthcheck"] = {
                        "Test": ["CMD", "private-sentinel"]
                    }
                elif condition == "mount":
                    self.info["Mounts"][0]["Source"] = "/foreign"
                elif condition == "restart-policy":
                    self.info["HostConfig"]["RestartPolicy"]["Name"] = "always"
                elif condition == "startup-hook":
                    self.info["Config"]["Env"].append("BASH_ENV=private-sentinel")
                elif condition == "shadow-path":
                    self.info["Config"]["Env"] = ["PATH=/unverified"]
                else:
                    self.info["HostConfig"]["PidMode"] = "host"
                self.calls.clear()
                result, lease, sync = self.recover()
                self.assertFalse(result)
                lease.assert_not_called()
                sync.assert_not_called()
                self.assertNotIn("start", self.calls)
                self.assertNotIn("stop", self.calls)
                self.info = original
        self.assertNotIn("private-sentinel", self.output.getvalue())

    def test_stop_request_suppresses_retry_at_each_owned_phase(self):
        for phase in ("before", "stop", "start", "reader", "sync"):
            with self.subTest(phase=phase):
                self.token = workspace_control.capture(self.path)
                self.source = recovery.capture(box, self.data, self.info, self.token)
                self.running = True
                self.calls.clear()

                def request(event, phase=phase):
                    if event == phase:
                        workspace_control.request_stop(self.path)

                self.inject = request
                if phase == "before":
                    workspace_control.request_stop(self.path)
                result, lease, sync = self.recover()
                self.assertFalse(result)
                self.assertLessEqual(self.calls.count("start"), 1)
                if phase in ("before", "stop"):
                    self.assertNotIn("start", self.calls)
                if phase in ("before", "stop", "start"):
                    lease.assert_not_called()
                    sync.assert_not_called()
                if "start" in self.calls:
                    self.assertFalse(self.running)
                workspace_control.require_certain(self.path)

    def test_ambiguous_start_retains_uncertain_fence_and_stops_before_return(self):
        def fail(event):
            if event == "start":
                raise box.BoxError("private-sentinel")

        self.inject = fail
        result, lease, sync = self.recover()
        self.assertFalse(result)
        self.assertFalse(self.running)
        self.assertEqual(self.calls.count("start"), 1)
        self.assertEqual(self.calls[-2:], ["stop", "inspect-stopped"])
        lease.assert_not_called()
        sync.assert_not_called()
        with self.assertRaises(workspace_control.ControlError):
            workspace_control.require_certain(self.path)
        self.assertNotIn("private-sentinel", self.output.getvalue())

    def test_missing_transition_fence_prevents_start(self):
        with patch.object(
            workspace_control,
            "mark_uncertain",
            side_effect=workspace_control.ControlError("control-state"),
        ):
            result, lease, sync = self.recover()
        self.assertFalse(result)
        self.assertFalse(self.running)
        self.assertNotIn("start", self.calls)
        lease.assert_not_called()
        sync.assert_not_called()

    def test_pending_export_or_activity_failure_is_not_reported_as_saved(self):
        for phase in ("sync", "idle"):
            with self.subTest(phase=phase):
                self.running = True
                self.calls.clear()

                def fail(event, phase=phase):
                    if event == phase:
                        raise box.BoxError("private-sentinel")

                self.inject = fail
                result, _lease, _sync = self.recover()
                self.assertFalse(result)
                self.assertFalse(self.running)
                self.assertEqual(self.calls.count("start"), 1)
        self.assertNotIn("snapshots were synchronized", self.output.getvalue())
        self.assertNotIn("private-sentinel", self.output.getvalue())

    def test_reader_exit_failure_cannot_be_reported_as_saved(self):
        self.reader.__exit__.side_effect = box.BoxError("private-sentinel")
        result, _lease, sync = self.recover()
        self.assertFalse(result)
        sync.assert_called_once()
        self.assertFalse(self.running)
        self.assertNotIn("snapshots were synchronized", self.output.getvalue())
        self.assertNotIn("private-sentinel", self.output.getvalue())

    def test_stop_intent_between_process_boundaries_suppresses_the_next_process(self):
        environment = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
        with recovery.budget(5), recovery.commands(self.path, self.source):
            box.run([sys.executable, "-I", "-S", "-c", "pass"], env=environment)
            workspace_control.request_stop(self.path)
            with patch.object(box.subprocess, "Popen") as start:
                with self.assertRaisesRegex(recovery.RecoveryError, "stop-requested"):
                    box.run([sys.executable, "-I", "-S", "-c", "pass"], env=environment)
                start.assert_not_called()
        self.assertIsNone(recovery.FENCE.get())
        self.assertFalse(recovery.ADMITTED.get())

    def test_nested_forwarding_observer_admission_uses_the_same_held_lock(self):
        admission = workspace_control.admission
        with recovery.budget(5), recovery.commands(self.path, self.source):
            with patch.object(
                workspace_control, "admission", wraps=admission
            ) as acquire:
                with recovery.admit():
                    with recovery.admit():
                        self.assertTrue(recovery.ADMITTED.get())
                self.assertEqual(acquire.call_count, 1)
        self.assertFalse(recovery.ADMITTED.get())

    def test_snapshot_timeout_has_separate_cleanup_budget(self):
        def expire(*_args, **_kwargs):
            recovery.DEADLINE.set(0)
            recovery.remaining()

        result, _lease, _sync = self.recover(sync=expire)
        self.assertFalse(result)
        self.assertFalse(self.running)
        self.assertEqual(self.calls[-2:], ["stop", "inspect-stopped"])
        self.assertIsNone(recovery.remaining())

    def test_cleanup_refusal_cannot_be_reported_as_saved(self):
        stops = 0

        def fail(event):
            nonlocal stops
            if event == "stop":
                stops += 1
                if stops == 2:
                    raise box.BoxError("private-sentinel")

        self.inject = fail
        result, _lease, _sync = self.recover()
        self.assertFalse(result)
        self.assertIn("cleanup-unverified", self.output.getvalue())
        self.assertNotIn("snapshots were synchronized", self.output.getvalue())

    def test_capture_is_optional_and_never_uses_new_resource_operations(self):
        cases = (
            ({"ready": False}, "workspace-not-ready"),
            ({"opencode_version": "2.2.8"}, "unsupported-version"),
            ({"podman_args": ["--group-add=keep-groups"]}, "custom-podman-options"),
            ({"restore_incomplete": True}, "incomplete-restore"),
        )
        with patch.object(box, "run") as native:
            for changes, reason in cases:
                refusals = []
                with self.subTest(changes=changes):
                    self.assertIsNone(
                        recovery.capture(
                            box,
                            {**self.data, **changes},
                            self.info,
                            self.token,
                            refusal=refusals.append,
                        )
                    )
                    self.assertEqual(refusals, [reason])
            for bad in (
                None,
                {},
                {**self.info, "Id": "not-an-id"},
                {**self.info, "HostConfig": []},
            ):
                refusals = []
                self.assertIsNone(
                    recovery.capture(
                        box, self.data, bad, self.token, refusal=refusals.append
                    )
                )
                self.assertEqual(refusals, ["invalid-container"])
            native.assert_not_called()

    def test_refusal_diagnostics_never_expose_unknown_categories_or_probe_resources(
        self,
    ):
        refusals = []
        with patch.object(box, "run") as native:
            self.assertIsNone(
                recovery.capture(
                    box, self.data, self.info, None, refusal=refusals.append
                )
            )
            self.assertEqual(refusals, ["control-unavailable"])
            refusals.clear()
            self.assertEqual(
                recovery.capture(
                    box, self.data, self.info, self.token, refusal=refusals.append
                ),
                self.source,
            )
            self.assertEqual(refusals, [])
            with patch.object(
                recovery,
                "_signature",
                side_effect=recovery.RecoveryError("private-sentinel"),
            ):
                self.assertIsNone(
                    recovery.capture(
                        box, self.data, self.info, self.token, refusal=refusals.append
                    )
                )
            self.assertEqual(refusals, ["invalid-container"])
            native.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            recovery.report_skipped(
                ["non-shell-jobs", "private-sentinel", {"private-sentinel": 1}, None]
            )
            recovery.report_skipped([])
        self.assertIn("non-shell-jobs, unavailable", output.getvalue())
        self.assertNotIn("private-sentinel", output.getvalue())

    def test_capture_uses_reader_exact_version_policy_not_a_second_allowlist(self):
        for release in ("2.2.6", "2.2.7", "2.3.0"):
            with self.subTest(release=release):
                self.assertEqual(
                    recovery.capture(
                        box,
                        {**self.data, "opencode_version": release},
                        self.info,
                        self.token,
                    ),
                    self.source,
                )
        for release in ("2.2.8", "2.3.1", "2.4.0", "2.3.0+unknown", None):
            with self.subTest(release=release):
                self.assertIsNone(
                    recovery.capture(
                        box,
                        {**self.data, "opencode_version": release},
                        self.info,
                        self.token,
                    )
                )
        with patch.object(recovery.history_reader, "SUPPORTED", frozenset()):
            self.assertIsNone(recovery.capture(box, self.data, self.info, self.token))

    def test_230_recovery_preserves_same_resource_sequence_and_failure_warning(self):
        self.data["opencode_version"] = "2.3.0"
        self.source = recovery.capture(box, self.data, self.info, self.token)
        result, lease, sync = self.recover()
        self.assertTrue(result)
        self.assertEqual(self.calls.count("start"), 1)
        self.assertEqual(self.calls.count("stop"), 2)
        self.assertLess(self.calls.index("inspect-stopped"), self.calls.index("reader"))
        self.assertFalse(self.running)
        self.assertEqual(lease.call_args.args[1]["opencode_version"], "2.3.0")
        sync.assert_called_once()
        self.assertIn(
            "original history-wait failure remains nonzero", self.output.getvalue()
        )

    def test_real_exit_routing_distinguishes_identical_timeout_counts(self):
        cases = {
            "eligible-shell": {},
            "eligible-subagent": {"kind": "subagent"},
            "eligible-native-home-ten-subagents": {
                "kind": "subagent",
                "count": 10,
                "home": True,
                "foreground_timeout": True,
            },
            "unsafe-baseline": {
                "custom_options": True,
                "refusal": "custom-podman-options",
            },
            "unsupported-image": {"kind": "image", "refusal": "non-shell-jobs"},
            "one-pair": {
                "slow_query": True,
                "refusal": "insufficient-job-observations",
            },
            "uncertain-control": {
                "control_failure": "uncertain-control",
                "refusal": "control-uncertain",
            },
            "unsafe-control": {
                "control_failure": "unsafe-control",
                "refusal": "control-state",
            },
            "attached-client": {"clients": 1, "refusal": "clients-not-detached"},
            "owner-close-failed": {
                "wrapper_exit": 7,
                "refusal": "owner-close-unverified",
                "client_code": 7,
            },
            "owner-close-unknown": {
                "wrapper_exit": None,
                "refusal": "owner-close-unverified",
            },
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                data = {
                    **self.data,
                    "opencode_version": "2.3.0",
                    "podman_args": ["--group-add=keep-groups"]
                    if case.get("custom_options")
                    else [],
                }
                args = box.parser().parse_args(["start"])
                args._opencode_release = "2.3.0"
                events, now, requests = [], [0.0], []
                owner = SimpleNamespace(
                    connection={"url": "http://127.0.0.1:1", "password": "synthetic"},
                    publish=Mock(),
                    wait_clients=Mock(),
                    clients=case.get("clients", 0),
                    child=SimpleNamespace(returncode=case.get("wrapper_exit", 0)),
                )

                @contextlib.contextmanager
                def service(*_args):
                    events.append("owner-open")
                    try:
                        yield owner
                    finally:
                        events.append("owner-closed")

                def api(_box, _data, _connection, method, target, *, timeout):
                    self.assertEqual(method, "GET")
                    requests.append(target)
                    now[0] += (
                        timeout
                        if (
                            case.get("slow_query")
                            and len(requests) == 4
                            or case.get("foreground_timeout")
                            and len(requests) == 5
                        )
                        else min(5, timeout)
                    )
                    return {
                        "data": {}
                        if target == "/api/session/active"
                        else [
                            {
                                "id": "job_synthetic_" + str(index),
                                "sessionID": "ses_synthetic",
                                "kind": case.get("kind", "shell"),
                                "status": "running",
                            }
                            for index in range(case.get("count", 1))
                        ]
                    }

                def retry(*_args):
                    self.assertEqual(events[-1], "owner-closed")
                    events.append("recover")
                    return True

                output, terminal_output = io.StringIO(), io.StringIO()
                with (
                    contextlib.ExitStack() as boundaries,
                    patch.object(box, "check_private_output"),
                    patch.object(box, "start_container", return_value=self.info),
                    patch.object(box, "prepare_runtime"),
                    patch.object(
                        box.native_tabs,
                        "inspect",
                        return_value=box.native_tabs.Snapshot(
                            "valid" if case.get("home") else "missing",
                            reason=None if case.get("home") else "absent",
                        ),
                    ),
                    patch.object(box.workspace_service, "Owner", side_effect=service),
                    patch.object(
                        box,
                        "refresh_sessions",
                        return_value={
                            "last": None if case.get("home") else "ses_synthetic"
                        },
                    ),
                    patch.object(
                        box, "run_tui", return_value=case.get("client_code", 0)
                    ),
                    patch.object(box.workspace_service, "api", side_effect=api),
                    patch.object(
                        recovery.time, "monotonic", side_effect=lambda: now[0]
                    ),
                    patch.object(
                        recovery.time,
                        "sleep",
                        side_effect=lambda seconds: now.__setitem__(
                            0, now[0] + seconds
                        ),
                    ),
                    patch.object(
                        box,
                        "loading_stage",
                        side_effect=lambda *_a: contextlib.nullcontext(),
                    ),
                    patch.object(recovery, "recover", side_effect=retry) as recover,
                    patch.object(box, "sync_sessions") as live_sync,
                    patch.object(
                        box,
                        "stop_container",
                        side_effect=lambda *_a: events.append("final-stop"),
                    ),
                    contextlib.redirect_stderr(output),
                    contextlib.redirect_stdout(terminal_output),
                ):
                    if case.get("control_failure"):
                        boundaries.enter_context(
                            patch.object(
                                workspace_control,
                                "capture",
                                side_effect=workspace_control.ControlError(
                                    case["control_failure"]
                                ),
                            )
                        )
                    self.assertEqual(
                        box.launch_environment(self.path, data, args),
                        case.get("client_code", 0) or 1,
                    )
                self.assertIn(
                    "last complete observation: foreground=0, jobs-running="
                    + str(case.get("count", 1))
                    + ", "
                    "jobs-completed=0, jobs-error=0, jobs-cancelled=0.",
                    output.getvalue(),
                )
                live_sync.assert_not_called()
                owner.wait_clients.assert_called_once()
                if case.get("home"):
                    owner.publish.assert_called_once_with(None)
                    self.assertIn(
                        "native home (no open session selected)",
                        terminal_output.getvalue(),
                    )
                    self.assertIn("phase=foreground-query", output.getvalue())
                    self.assertIn("shell=0, subagent=10, image=0", output.getvalue())
                if "refusal" not in case:
                    recover.assert_called_once()
                    self.assertEqual(
                        events, ["owner-open", "owner-closed", "recover", "final-stop"]
                    )
                    self.assertNotIn("not attempted", output.getvalue())
                else:
                    recover.assert_not_called()
                    self.assertEqual(
                        events, ["owner-open", "owner-closed", "final-stop"]
                    )
                    self.assertIn(
                        "Automatic persisted-history snapshot not attempted",
                        output.getvalue(),
                    )
                    self.assertIn(case["refusal"], output.getvalue())
                    self.assertEqual(
                        output.getvalue().count("history sync pending:"), 1
                    )

    def test_explicit_stop_publishes_intent_before_admission_without_launch_work(self):
        admission = workspace_control.admission

        @contextlib.contextmanager
        def ordered(path, *, timeout):
            self.assertFalse(workspace_control.unchanged(path, self.token))
            with admission(path, timeout=timeout):
                yield

        def stop(_data):
            with self.assertRaises(workspace_control.ControlError):
                with admission(self.path, timeout=0):
                    self.fail("Explicit stop did not hold recovery admission.")

        output = io.StringIO()
        with (
            patch.object(box, "check_backend"),
            patch.object(workspace_control, "admission", side_effect=ordered),
            patch.object(box, "stop_container", side_effect=stop) as stopped,
            patch.object(box, "start_container") as start,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "launch_credentials") as credentials,
            patch.object(recovery.history_reader, "Reader") as reader,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["stop", "--workspace", self.data["id"]]), 0)
        stopped.assert_called_once()
        for operation in (start, prepare, credentials, reader):
            operation.assert_not_called()
        self.assertIn("Container stopped", output.getvalue())

    def test_explicit_stop_cannot_claim_success_after_an_indeterminate_start(self):
        workspace_control.mark_uncertain(self.path)
        output = io.StringIO()
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "stop_container") as stopped,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(box.BoxError, "uncertain-control"),
        ):
            box.main(["stop", "--workspace", self.data["id"]])
        self.assertGreaterEqual(stopped.call_count, 1)
        self.assertNotIn("Container stopped", output.getvalue())
        with self.assertRaises(workspace_control.ControlError):
            workspace_control.require_certain(self.path)

    def test_v1_explicit_stop_retains_legacy_behavior_without_recovery_control(self):
        self.data["opencode_version"] = "1.19.5"
        box.atomic_json(self.path, self.data)
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "stop_container") as stop,
            patch.object(workspace_control, "request_stop") as intent,
            patch.object(workspace_control, "admission") as admission,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["stop", "--workspace", self.data["id"]]), 0)
        stop.assert_called_once()
        intent.assert_not_called()
        admission.assert_not_called()


class RecoveryBudgetTests(unittest.TestCase):
    def test_nested_budget_and_subprocess_timeout_use_only_remaining_duration(self):
        with patch.object(recovery.time, "monotonic", return_value=100):
            with recovery.budget(10):
                with recovery.budget(30):
                    self.assertEqual(recovery.remaining(), 10)
                    with patch.object(
                        box.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0),
                    ) as run:
                        box.run(["synthetic-private-command"], timeout=60)
                    self.assertEqual(run.call_args.kwargs["timeout"], 10)
                with patch.object(recovery.time, "monotonic", return_value=110):
                    with patch.object(box.subprocess, "run") as run:
                        with self.assertRaisesRegex(recovery.RecoveryError, "deadline"):
                            box.run(["synthetic-private-command"])
                    run.assert_not_called()
        self.assertIsNone(recovery.remaining())


class RecoveryExitCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-exit-coordinator-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "workspace.json"
        self.path.write_text("{}")
        self.data = {
            "id": "synthetic",
            "distro": "tumbleweed",
            "opencode_version": "2.2.7",
        }
        self.args = SimpleNamespace(
            command="start", import_session=None, _opencode_release="2.2.7"
        )

    def test_owner_closes_before_recovery_and_original_client_result_survives(self):
        cases = (
            (0, True, 0, 0, True),
            (7, True, 0, 0, True),
            (0, False, 0, 0, True),
            (0, True, 1, 0, True),
            (0, True, 0, 7, True),
            (0, True, 0, None, True),
            (0, True, 0, 0, False),
        )
        for release, (
            client_code,
            eligible,
            clients,
            wrapper_exit,
            captured,
        ) in itertools.product(("2.2.7", "2.3.0"), cases):
            self.data["opencode_version"] = release
            self.args._opencode_release = release
            events = []
            owner = SimpleNamespace(
                clients=clients, child=SimpleNamespace(returncode=wrapper_exit)
            )
            source = recovery.Source("a" * 64, "synthetic-stop-token", ())
            should_retry = eligible and clients == 0 and wrapper_exit == 0 and captured

            @contextlib.contextmanager
            def service(*_args, events=events, owner=owner):
                events.append("owner-open")
                try:
                    yield owner
                finally:
                    events.append("owner-closed")

            def client(
                *_args,
                eligible=eligible,
                client_code=client_code,
                owner=owner,
                events=events,
            ):
                owner._history_timeout = box.HistoryWaitTimeout(
                    "count-only", recovery_eligible=eligible
                )
                events.append("client-closed")
                return client_code or 1

            with (
                self.subTest(
                    release=release,
                    client_code=client_code,
                    eligible=eligible,
                    clients=clients,
                    wrapper_exit=wrapper_exit,
                    captured=captured,
                ),
                patch.object(box, "check_private_output"),
                patch.object(
                    box.workspace_control,
                    "capture",
                    return_value="synthetic-stop-token",
                ),
                patch.object(box, "start_container", return_value={}),
                patch.object(box, "prepare_runtime"),
                patch.object(box.workspace_service, "Owner", side_effect=service),
                patch.object(
                    recovery, "capture", return_value=source if captured else None
                ),
                patch.object(box, "launch_client", side_effect=client),
                patch.object(
                    recovery,
                    "recover",
                    side_effect=lambda *_, events=events: events.append("recover"),
                ) as retry,
                patch.object(
                    box,
                    "stop_container",
                    side_effect=lambda *_, events=events: events.append("final-stop"),
                ) as stop,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    box.launch_environment(self.path, self.data, self.args),
                    client_code or 1,
                )
            self.assertEqual(
                events,
                ["owner-open", "client-closed", "owner-closed"]
                + (["recover"] if should_retry else [])
                + ["final-stop"],
            )
            if should_retry:
                retry.assert_called_once()
                self.assertEqual(
                    stop.call_args.args[0]["container"], source.container_id
                )
            else:
                retry.assert_not_called()
                self.assertIs(stop.call_args.args[0], self.data)

    def test_outer_cleanup_has_its_own_budget_even_after_failed_recovery(self):
        owner = SimpleNamespace(clients=0, child=SimpleNamespace(returncode=0))
        source = recovery.Source("a" * 64, "synthetic-stop-token", ())

        @contextlib.contextmanager
        def service(*_args):
            yield owner

        def client(*_args):
            owner._history_timeout = box.HistoryWaitTimeout(
                "count-only", recovery_eligible=True
            )
            return 7

        def stop(data):
            self.assertEqual(data["container"], source.container_id)
            self.assertGreater(recovery.remaining(), 0)
            self.assertLessEqual(recovery.remaining(), recovery.CLEANUP_BUDGET)
            raise recovery.RecoveryError("deadline")

        output = io.StringIO()
        with (
            patch.object(box, "check_private_output"),
            patch.object(
                workspace_control, "capture", return_value="synthetic-stop-token"
            ),
            patch.object(box, "start_container", return_value={}),
            patch.object(box, "prepare_runtime"),
            patch.object(box.workspace_service, "Owner", side_effect=service),
            patch.object(recovery, "capture", return_value=source),
            patch.object(box, "launch_client", side_effect=client),
            patch.object(recovery, "recover", return_value=False),
            patch.object(box, "stop_container", side_effect=stop),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(box.launch_environment(self.path, self.data, self.args), 7)
        self.assertIn("cleanup could not be verified", output.getvalue())
        self.assertIsNone(recovery.remaining())
