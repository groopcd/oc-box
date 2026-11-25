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

from oc_box import (
    history_recovery as recovery,
    lifecycle_output,
    oc_box as box,
    workspace_control,
)


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
                "Cmd": ["-c", "exec /bin/sleep infinity"],
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
                    "Name": self.data["home_volume"],
                    "Driver": "local",
                    "Options": {},
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

    def run_stopped_preflight_command(self, command, **options):
        if command[1:] == ["container", "inspect", self.data["container"]]:
            command = [*command[:-1], self.ident]
        return self.run_command(command, **options)

    def test_explicit_persisted_sync_uses_only_the_stopped_reader_path(self):
        self.data["opencode_version"] = "2.4.1"
        box.atomic_json(self.path, self.data)
        args = SimpleNamespace(
            command="sync", persisted=True, _opencode_release="2.4.1"
        )
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.running = False
                self.calls.clear()
                before = self.path.read_bytes()

                def sync(path, data, *, announce):
                    self.record("sync")
                    self.assertEqual(path, self.path)
                    self.assertEqual(data["container"], self.ident)
                    self.assertFalse(announce)
                    recovery.REPORT.get().populated = True
                    if pending:
                        raise box.HistoryPending("synthetic pending")

                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    lifecycle_output.invocation("sync", self.root / "state"),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch.object(
                        box, "run", side_effect=self.run_stopped_preflight_command
                    ),
                    patch.object(
                        recovery.history_reader, "Reader", return_value=self.reader
                    ) as lease,
                    patch.object(box, "sync_sessions", side_effect=sync) as archives,
                    patch.object(box, "start_container") as start,
                    patch.object(box, "prepare_runtime") as prepare,
                    patch.object(box, "stop_container") as ordinary_stop,
                ):
                    self.assertEqual(
                        box.launch_environment(self.path, self.data, args), int(pending)
                    )
                self.assertEqual(self.calls[:3], ["inspect-stopped"] * 3)
                self.assertEqual(self.calls.count("start"), 1)
                self.assertEqual(self.calls.count("stop"), 1)
                self.assertLess(self.calls.index("start"), self.calls.index("sync"))
                self.assertLess(self.calls.index("sync"), self.calls.index("stop"))
                self.assertFalse(self.running)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(lease.call_count, 1)
                self.assertEqual(archives.call_count, 1)
                for ordinary in (start, prepare, ordinary_stop):
                    ordinary.assert_not_called()
                if pending:
                    self.assertIn("still unsettled", stderr.getvalue())
                    self.assertNotIn("retry with 'oc-box sync'", stderr.getvalue())
                    self.assertNotIn("Persisted history synced", stdout.getvalue())
                else:
                    self.assertIn("Persisted history synced", stdout.getvalue())

    def test_explicit_persisted_sync_refuses_before_start_on_unsafe_source(self):
        for running, command, reason in (
            (True, "exec /bin/sleep infinity", "unexpected-container-state"),
            (False, "exec sleep infinity", "startup-path-unverified"),
        ):
            with self.subTest(reason=reason):
                self.running = running
                self.info["Config"]["Cmd"] = ["-c", command]
                self.calls.clear()
                with (
                    patch.object(
                        box, "run", side_effect=self.run_stopped_preflight_command
                    ),
                    patch.object(recovery.history_reader, "Reader") as lease,
                    self.assertRaisesRegex(box.BoxError, reason),
                ):
                    recovery.stopped_source(box, self.path, self.data, "2.2.7")
                lease.assert_not_called()
                self.assertNotIn("start", self.calls)
                self.assertNotIn("stop", self.calls)

    def test_persisted_cli_does_not_refresh_or_write_registration_on_refusal(self):
        before = self.path.read_bytes(), self.path.stat().st_ino
        self.running = True
        with (
            patch.dict(
                os.environ,
                {"OC_BOX_CONTAINER": "0", "CONTAINER_HOST": "synthetic-foreign-engine"},
            ),
            patch.object(box, "check_backend") as backend,
            patch.object(
                box, "detect_opencode_release", return_value="2.2.7"
            ) as version,
            patch.object(box, "check_private_output"),
            patch.object(box, "refresh_workspace_identity") as refresh,
            patch.object(box, "configure_podman_options") as configure,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "run", side_effect=self.run_stopped_preflight_command),
            self.assertRaisesRegex(box.BoxError, "unexpected-container-state"),
        ):
            box.main(["sync", "--persisted", "--workspace", self.data["id"]])
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), before)
        self.assertNotIn("CONTAINER_HOST", backend.call_args.kwargs["env"])
        version.assert_called_once_with(isolated=True)
        for operation in (refresh, configure, prepare):
            operation.assert_not_called()
        self.assertNotIn("start", self.calls)
        self.assertNotIn("stop", self.calls)

    def test_persisted_cli_cannot_replace_an_active_workspace_owner(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(box, "check_backend"),
            patch.object(box, "_launch_persisted_sync") as snapshot,
            patch.object(box, "detect_opencode_release") as version,
            box.workspace_lock(self.path),
            self.assertRaisesRegex(
                box.BoxError, "already has an active oc-box process"
            ),
        ):
            box.main(["sync", "--persisted", "--workspace", self.data["id"]])
        snapshot.assert_not_called()
        version.assert_not_called()

    def test_persisted_stop_intent_after_capture_prevents_any_start(self):
        self.running = False
        with (
            patch.object(box, "run", side_effect=self.run_stopped_preflight_command),
            patch.object(recovery.history_reader, "Reader") as lease,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            source = recovery.stopped_source(box, self.path, self.data, "2.2.7")
            workspace_control.request_stop(self.path)
            self.assertFalse(
                recovery.recover(
                    box, self.path, self.data, source, already_stopped=True
                )
            )
        lease.assert_not_called()
        self.assertNotIn("start", self.calls)
        self.assertNotIn("stop", self.calls)

    def test_explicit_persisted_rejects_extra_mounts_and_nonlocal_home(self):
        self.running = False
        initial = copy.deepcopy(self.info)
        self.info["Mounts"].append({"Destination": "/bin/sleep"})
        with (
            patch.object(box, "run", side_effect=self.run_stopped_preflight_command),
            self.assertRaisesRegex(box.BoxError, r"refused \(mounts\)"),
        ):
            recovery.stopped_source(box, self.path, self.data, "2.2.7")
        self.assertNotIn("start", self.calls)
        self.info = initial
        self.calls.clear()

        def nonlocal_volume(command, **options):
            result = self.run_stopped_preflight_command(command, **options)
            if command[1:3] == ["volume", "inspect"]:
                (record,) = json.loads(result.stdout)
                result.stdout = json.dumps([{**record, "Driver": "foreign"}])
            return result

        with (
            patch.object(box, "run", side_effect=nonlocal_volume),
            patch.object(recovery.history_reader, "Reader") as lease,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            source = recovery.stopped_source(box, self.path, self.data, "2.2.7")
            self.assertFalse(
                recovery.recover(
                    box, self.path, self.data, source, already_stopped=True
                )
            )
        lease.assert_not_called()
        self.assertIn("home-ownership", errors.getvalue())
        self.assertNotIn("start", self.calls)

    def test_persisted_cleanup_uncertainty_wins_over_pending_or_ambiguous_start(self):
        for fault in ("reader-close", "start-timeout", "interrupted-stop"):
            with self.subTest(fault=fault):
                self.running = False
                self.calls.clear()
                args = SimpleNamespace(
                    command="sync", persisted=True, _opencode_release="2.2.7"
                )

                def inject(event):
                    if fault == "start-timeout" and event == "start":
                        raise box.BoxError("synthetic ambiguous start")
                    if fault == "interrupted-stop" and event == "stop":
                        raise KeyboardInterrupt

                def reader_close(_kind, error, _traceback):
                    if fault == "reader-close":
                        error.history_source_cleanup_failed = True

                self.inject = inject
                self.reader.__exit__.side_effect = reader_close
                with (
                    lifecycle_output.invocation(
                        "sync", self.root / "state", verbose=fault == "interrupted-stop"
                    ),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()) as errors,
                    patch.object(box, "check_private_output"),
                    patch.object(
                        box, "run", side_effect=self.run_stopped_preflight_command
                    ),
                    patch.object(
                        recovery.history_reader, "Reader", return_value=self.reader
                    ),
                    patch.object(
                        box,
                        "sync_sessions",
                        side_effect=box.HistoryPending("synthetic"),
                    ),
                ):
                    if fault == "interrupted-stop":
                        with self.assertRaises(KeyboardInterrupt):
                            box.launch_environment(self.path, self.data, args)
                    else:
                        self.assertEqual(
                            box.launch_environment(self.path, self.data, args), 1
                        )
                self.assertIn("cleanup could not be verified", errors.getvalue())
                self.assertNotIn("Persisted history synced", output.getvalue())
                if fault == "start-timeout":
                    with self.assertRaises(workspace_control.ControlError):
                        workspace_control.capture(self.path)
                    # Reset the known synthetic transition for the next matrix
                    # entry; production does not clear a fence based on stop.
                    with workspace_control.admission(self.path):
                        workspace_control.finish_transition(self.path)
        self.inject = lambda _event: None

    def test_relative_sleep_never_admits_guarded_or_post_stop_startup(self):
        self.info["Config"]["Cmd"] = ["-c", "exec sleep infinity"]
        refusals = []
        self.assertIsNone(
            recovery.capture(
                box, self.data, self.info, self.token, refusal=refusals.append
            )
        )
        self.assertEqual(refusals, ["startup-path-unverified"])
        # Covers a stopped inspection at guarded admission independently
        # of Reader capture; no engine probe, preparation or start follows.
        stopped = {**self.info, "State": {"Running": False, "Status": "exited"}}
        with (
            patch.object(box, "inspect_owned", return_value=stopped),
            patch.object(box, "podman") as podman,
            self.assertRaisesRegex(box.BoxError, "PATH-dependent entrypoint"),
        ):
            box.runtime_safeguard._inspect(box, self.path, self.data, running=False)
        podman.assert_not_called()
        # If an earlier captured inspection was absolute, the live recheck
        # must still refuse a different entrypoint before any transition.
        result, lease, sync = self.recover()
        self.assertFalse(result)
        self.assertEqual(self.calls, ["inspect-running"])
        self.assertTrue(self.running)
        lease.assert_not_called()
        sync.assert_not_called()
        self.assertIn("startup-path-unverified", self.output.getvalue())

    def test_running_legacy_owner_can_bind_another_client_without_start_authority(self):
        self.info["Config"]["Cmd"] = ["-c", "exec sleep infinity"]
        volume = self.data["home_volume"]
        running = {**self.info, "State": {"Running": True, "Status": "running"}}
        record = SimpleNamespace(
            ready=True,
            version=self.data["opencode_version"],
            volume=volume,
            container=self.ident,
            digest="d" * 64,
        )
        inventory = [
            {
                "Name": volume,
                "Driver": "local",
                "Mountpoint": self.source.mount_signature[2],
                "Labels": {box.LABEL: self.data["id"]},
            }
        ]
        connection = {"version": record.version}
        with (
            patch.object(box.runtime_safeguard, "load", return_value=record),
            patch.object(box, "inspect_owned", return_value=running),
            patch.object(
                box,
                "podman",
                return_value=SimpleNamespace(stdout=json.dumps(inventory)),
            ) as podman,
        ):
            box.runtime_safeguard.bind_client(box, self.path, self.data, connection)
        podman.assert_called_once_with(
            "volume", "inspect", volume, capture=True, timeout=15
        )
        self.assertEqual(connection["_runtime_container"], record.container)
        self.assertEqual(connection["_runtime_digest"], record.digest)
        with self.assertRaisesRegex(recovery.RecoveryError, "startup-path-unverified"):
            recovery.passive_workspace_mounts(box, self.data, running)
        stopped = {**running, "State": {"Running": False, "Status": "exited"}}
        with self.assertRaisesRegex(recovery.RecoveryError, "startup-path-unverified"):
            recovery.live_attachment_mounts(box, self.data, stopped)

    def test_guarded_24_snapshot_retains_the_exact_captured_digest_on_refusal(self):
        digest = "d" * 64
        self.data["opencode_version"] = "2.4.1"
        refusals = []
        self.assertIsNone(
            recovery.capture(
                box,
                self.data,
                self.info,
                self.token,
                refusal=refusals.append,
                runtime_digest="not-a-proof",
            )
        )
        self.assertEqual(refusals, ["runtime-executable-unverified"])
        self.source = recovery.capture(
            box, self.data, self.info, self.token, runtime_digest=digest
        )
        error = box.BoxError("synthetic refused native executable")
        error.history_reader_category = "runtime-executable-unverified"
        self.reader.__enter__.side_effect = error

        result, lease, sync = self.recover()

        self.assertIs(result, False)
        self.assertEqual(lease.call_args.kwargs["runtime_digest"], digest)
        self.assertEqual(lease.call_args.args[1]["container"], self.ident)
        self.assertFalse(self.running)
        self.assertEqual(self.calls.count("start"), 1)
        self.assertEqual(self.calls.count("stop"), 2)
        sync.assert_not_called()
        self.assertIn("runtime-executable-unverified", self.output.getvalue())

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
            "completion notices or final results may be missing", self.output.getvalue()
        )
        self.assertIn(
            "Background work is not declared complete", self.output.getvalue()
        )
        self.assertNotIn("normal host OpenCode database", self.output.getvalue())
        workspace_control.require_certain(self.path)

    def test_history_wait_reports_only_after_recovery_and_final_stop(self):
        cases = {
            "saved": (0, "Available history synced.", None),
            "nonzero-client": (7, "Available history synced.", None),
            "unknown-client": (1, "Available history synced.", None),
            "foreground": (1, "Available history synced.", None),
            "stop-requested": (
                1,
                "History not synced: a workspace stop was requested.",
                "stop-requested",
            ),
            "recovery-cleanup": (
                1,
                "History sync cleanup could not be verified.",
                "cleanup-unverified",
            ),
            "final-cleanup": (
                1,
                "History sync cleanup could not be verified.",
                "cleanup-unverified",
            ),
            "late-active": (
                1,
                "History not synced: the OpenCode compatibility contract was not recognized.",
                "schema",
            ),
            "late-jobs": (
                1,
                "History not synced: the OpenCode compatibility contract was not recognized.",
                "schema",
            ),
            "late-decode": (
                1,
                "History not synced: the OpenCode compatibility contract was not recognized.",
                "schema",
            ),
            "late-transport": (
                1,
                "History not synced: OpenCode activity did not settle.",
                "timeout",
            ),
        }
        workspace_service = box.workspace_service
        wait_history = workspace_service.wait_history
        matrix = [(case, False) for case in cases] + [
            (case, True)
            for case in ("saved", "final-cleanup", "late-active", "late-decode")
        ]
        recovery_success = (
            "oc-box: Checked persisted history snapshots were synchronized after stopping the workspace. "
            "Execution was interrupted; completion notices or final results may be missing. "
            "Background work is not declared complete."
        )
        fallback_success = "Session history synced to your normal host OpenCode database using the post-stop fallback."
        for case, verbose in matrix:
            code, message, reason = cases[case]
            late = case.startswith("late-")
            with self.subTest(case=case, diagnostic=verbose):
                self.running = True
                self.calls.clear()
                release = "2.3.2" if case == "foreground" else "2.2.7"
                self.data["opencode_version"] = release
                box.atomic_json(self.path, self.data)
                args = SimpleNamespace(
                    command="start",
                    _opencode_release=release,
                    import_session=None,
                    resume=True,
                    new=False,
                    model=None,
                )
                owner = SimpleNamespace(
                    clients=0,
                    child=SimpleNamespace(returncode=0),
                    publish=Mock(),
                    connection={"url": "http://127.0.0.1:7777"},
                    wait_clients=Mock(),
                )
                clock, requests = [0.0], []
                info = {
                    "id": f"ses_history_{case.replace('-', '_')}_{int(verbose)}",
                    "location": {"directory": str(self.project)},
                    "time": {"created": 1, "updated": 1},
                }
                host = {}

                @contextlib.contextmanager
                def service(*_args, **_kwargs):
                    token = workspace_service.CONNECTION.set(owner.connection)
                    try:
                        yield owner
                    finally:
                        workspace_service.CONNECTION.reset(token)
                        self.record("owner closed")
                        if case == "stop-requested":
                            workspace_control.request_stop(self.path)

                def api(_box, _data, _connection, method, target, *, timeout):
                    self.assertEqual(method, "GET")
                    requests.append(target)
                    if late and len(requests) == (6 if case == "late-jobs" else 5):
                        clock[0] += timeout
                        if case == "late-decode":
                            raise ValueError("private-native-response")
                        if case == "late-transport":
                            raise box.BoxError("private-transport-error")
                        return {"data": "private-native-response"}
                    if target == "/api/session/active":
                        return {
                            "data": {info["id"]: {"type": "running"}}
                            if case == "foreground"
                            else {}
                        }
                    self.assertEqual(target, "/api/experimental/job")
                    return {
                        "data": [
                            {
                                "id": "private-job-id",
                                "sessionID": info["id"],
                                "status": "running",
                                "kind": "shell",
                            }
                        ]
                    }

                def run(argv, **kwargs):
                    if argv[0] == "podman":
                        return self.run_command(argv, **kwargs)
                    self.assertFalse(workspace_service.live_connection())
                    self.assertIn("reader", self.calls)
                    if "import" in argv:
                        archived = box.read_json(Path(argv[-2]))
                        archived["info"]["location"] = {
                            "directory": str(self.path.parent / "controller")
                        }
                        host[archived["info"]["id"]] = archived
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    self.assertIn("export", argv)
                    self.record("host export checked")
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps(host[argv[-2]]), ""
                    )

                def inject(event):
                    if (
                        case == "recovery-cleanup"
                        and event == "stop"
                        and "reader" in self.calls
                    ):
                        raise box.BoxError("private-recovery-cleanup")

                def stop(data):
                    self.assertEqual(
                        data["container"],
                        self.data["container"] if late else self.ident,
                    )
                    self.assertNotIn("history synced", stdout.getvalue())
                    self.assertNotIn(recovery_success, stderr.getvalue())
                    if not verbose:
                        self.assertEqual(stderr.getvalue(), "")
                    self.record("final stop")
                    if case == "final-cleanup":
                        raise box.BoxError("private-final-cleanup")
                    self.running = False

                def record(event, **details):
                    if event == "history.result":
                        self.assertEqual(self.calls[-1], "final stop")
                        self.record("history result")

                self.inject = inject
                stdout, stderr = io.StringIO(), io.StringIO()
                boundaries = (
                    lifecycle_output.invocation(
                        "start", self.root / "state", verbose=verbose
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch.object(
                        box, "loading_stage", return_value=contextlib.nullcontext()
                    ),
                    patch.object(box, "start_container", return_value=self.info),
                    patch.object(box, "prepare_runtime"),
                    patch.object(box, "check_private_output"),
                    patch.object(
                        box,
                        "run_native_tui",
                        return_value={"nonzero-client": 7, "unknown-client": None}.get(
                            case, 0
                        ),
                    ),
                    patch.object(workspace_service, "Owner", side_effect=service),
                    patch.object(workspace_service, "client_command", return_value=[]),
                    patch.object(workspace_service, "api", side_effect=api),
                    patch.object(
                        workspace_service,
                        "wait_history",
                        side_effect=lambda *a: wait_history(
                            *a, timeout=1, interval=0.25
                        ),
                    ),
                    patch.object(
                        recovery.time, "monotonic", side_effect=lambda: clock[0]
                    ),
                    patch.object(
                        recovery.time,
                        "sleep",
                        side_effect=lambda delay: clock.__setitem__(
                            0, clock[0] + delay
                        ),
                    ),
                    patch.object(
                        recovery.history_reader, "Reader", return_value=self.reader
                    ),
                    patch.object(box, "list_container_sessions", return_value=[info]),
                    patch.object(
                        box,
                        "export_container_session",
                        return_value={"info": info, "messages": []},
                    ),
                    patch.object(box, "run", side_effect=run),
                    patch.object(box, "stop_container", side_effect=stop),
                )
                with contextlib.ExitStack() as stack:
                    for boundary in boundaries:
                        stack.enter_context(boundary)
                    journal = stack.enter_context(
                        patch.object(lifecycle_output, "record", side_effect=record)
                    )
                    self.assertEqual(
                        box.launch_environment(self.path, self.data, args), code
                    )
                result = [
                    call.kwargs
                    for call in journal.call_args_list
                    if call.args == ("history.result",)
                ]
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["reason"], reason or "interrupted")
                wait = next(
                    call.kwargs
                    for call in journal.call_args_list
                    if call.args == ("history.wait",)
                )
                self.assertEqual(wait["reason"], "timeout")
                self.assertGreaterEqual(wait["metrics"]["complete_pairs"], 2)
                self.assertEqual(wait["metrics"]["shell"], 1)
                self.assertNotIn("private-", repr(journal.call_args_list))
                self.assertEqual(
                    self.calls.count("start"),
                    int(case != "stop-requested" and not late),
                )
                if late:
                    self.assertNotIn("reader", self.calls)
                    self.assertNotIn("host export checked", self.calls)
                    self.assertFalse(hasattr(owner, "_history_failure"))
                    self.assertEqual(
                        "Check for an oc-box update first" in stderr.getvalue(),
                        reason == "schema",
                    )
                    self.assertNotIn("private-", stderr.getvalue())
                self.assertEqual(self.calls[-2:], ["final stop", "history result"])
                if verbose:
                    self.assertEqual(
                        stderr.getvalue().splitlines().count(recovery_success),
                        int(reason is None),
                    )
                    self.assertEqual(
                        stdout.getvalue().splitlines().count(fallback_success),
                        int(reason is None),
                    )
                    if late:
                        self.assertIn("History wait timeout:", stderr.getvalue())
                        self.assertIn("History wait timing:", stderr.getvalue())
                        self.assertIn(
                            "contract was not recognized (invalid-active-response)",
                            stderr.getvalue(),
                        )
                elif reason is None:
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(
                        stdout.getvalue().splitlines()[-1],
                        "Available history synced. Interrupted work may be incomplete.",
                    )
                    self.assertLess(
                        self.calls.index("host export checked"),
                        self.calls.index("final stop"),
                    )
                else:
                    self.assertNotIn("history synced.", stdout.getvalue())
                    self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                    self.assertTrue(stderr.getvalue().startswith(message))
                    self.assertNotIn("private-", stderr.getvalue())

    def test_helper_cleanup_uncertainty_survives_recovery_and_outer_stop(self):
        def fixed_cleanup():
            return box.BoxError("private-helper-cleanup")

        cases = (
            (recovery.RecoveryError("deadline"), fixed_cleanup(), None),
            (workspace_control.ControlError("unsafe-control"), fixed_cleanup(), None),
            (box.HistoryPending("private-pending"), fixed_cleanup(), None),
            (RuntimeError("private-generic"), fixed_cleanup(), None),
            (box.HistoryPending("private-pending"), KeyboardInterrupt(), None),
            (box.HistoryPending("private-pending"), SystemExit(0), None),
            (
                recovery.RecoveryError("deadline"),
                fixed_cleanup(),
                RuntimeError("private-reader-exit"),
            ),
        )
        args = SimpleNamespace(
            command="start", _opencode_release="2.2.7", import_session=None
        )
        workspace_service = box.workspace_service
        history_reader = recovery.history_reader
        for primary, cleanup, reader_error in cases:
            self.running = True
            self.calls.clear()
            owner = SimpleNamespace(clients=0, child=SimpleNamespace(returncode=0))
            lease = SimpleNamespace(
                data=self.data,
                connection={"url": "http://127.0.0.1:7777"},
                assert_idle=lambda: self.record("idle"),
            )
            stream = SimpleNamespace(close=Mock(side_effect=cleanup))

            @contextlib.contextmanager
            def open_reader(*_args, **_kwargs):
                self.record("reader")
                token = history_reader.CURRENT.set(lease)
                try:
                    yield lease
                finally:
                    history_reader.CURRENT.reset(token)
                    if reader_error is not None:
                        raise reader_error

            def client(*_args):
                owner._history_timeout = box.HistoryWaitTimeout(
                    "fixed-timeout", recovery_eligible=True
                )
                owner._client_exit_code = 7
                return 7

            def stop(data):
                self.assertEqual(data["container"], self.ident)
                self.assertFalse(self.running)
                self.assertEqual(errors.getvalue(), "")
                self.record("final stop")

            errors, stdout = io.StringIO(), io.StringIO()
            with (
                self.subTest(
                    primary=type(primary),
                    cleanup=type(cleanup),
                    reader_error=bool(reader_error),
                ),
                lifecycle_output.invocation("start", self.root / "state"),
                contextlib.redirect_stderr(errors),
                contextlib.redirect_stdout(stdout),
                patch.object(box, "check_private_output"),
                patch.object(box, "start_container", return_value=self.info),
                patch.object(box, "prepare_runtime"),
                patch.object(box, "launch_client", side_effect=client),
                patch.object(
                    workspace_service,
                    "Owner",
                    return_value=contextlib.nullcontext(owner),
                ),
                patch.object(history_reader, "Reader", side_effect=open_reader),
                patch.object(history_reader, "_Exports", return_value=stream),
                patch.object(box, "refresh_sessions", side_effect=primary),
                patch.object(box, "run", side_effect=self.run_command),
                patch.object(box, "stop_container", side_effect=stop),
                patch.object(lifecycle_output, "record") as journal,
            ):
                winner = reader_error or (
                    cleanup
                    if isinstance(cleanup, (KeyboardInterrupt, SystemExit))
                    else primary
                )
                if isinstance(winner, (RuntimeError, KeyboardInterrupt, SystemExit)):
                    with self.assertRaises(type(winner)) as raised:
                        box.launch_environment(self.path, self.data, args)
                    self.assertIs(raised.exception, winner)
                    self.assertTrue(winner.history_outcome_reported)
                    if isinstance(winner, SystemExit):
                        self.assertEqual(winner.code, 1)
                else:
                    self.assertEqual(
                        box.launch_environment(self.path, self.data, args), 7
                    )
                stream.close.assert_called_once_with()
                self.assertEqual(self.calls.count("start"), 1)
                self.assertEqual(self.calls[-2:], ["inspect-stopped", "final stop"])
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn(
                    "History sync cleanup could not be verified.", errors.getvalue()
                )
                reasons = [
                    call.kwargs["reason"]
                    for call in journal.call_args_list
                    if call.args in (("history.pass",), ("history.result",))
                ]
                self.assertEqual(reasons, ["cleanup-unverified"] * 2)

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
        for phase, pending in (("sync", False), ("idle", False), ("sync", True)):
            with self.subTest(phase=phase, pending=pending):
                self.running = True
                self.calls.clear()
                self.output.seek(0)
                self.output.truncate(0)

                def fail(event, phase=phase):
                    if event == phase:
                        kind = box.HistoryPending if pending else box.BoxError
                        raise kind("private-sentinel")

                self.inject = fail
                result, _lease, _sync = self.recover()
                self.assertFalse(result)
                self.assertFalse(self.running)
                self.assertEqual(self.calls.count("start"), 1)
                self.assertEqual(
                    "snapshot-incomplete" in self.output.getvalue(), pending
                )
                self.assertEqual("prior archives" in self.output.getvalue(), pending)
                self.assertNotIn("snapshots were synchronized", self.output.getvalue())
                self.assertIn("oc-box sync", self.output.getvalue())
                self.assertNotIn("private-sentinel", self.output.getvalue())
                self.assertNotIn("Check for an oc-box update", self.output.getvalue())
                self.assertNotIn("upgrade OpenCode", self.output.getvalue())

    def test_reader_exit_failure_cannot_be_reported_as_saved(self):
        self.reader.__exit__.side_effect = box.BoxError("private-sentinel")
        result, _lease, sync = self.recover()
        self.assertFalse(result)
        sync.assert_called_once()
        self.assertFalse(self.running)
        self.assertNotIn("snapshots were synchronized", self.output.getvalue())
        self.assertIn("oc-box sync", self.output.getvalue())
        self.assertNotIn("private-sentinel", self.output.getvalue())

    def test_reader_compatibility_refusals_keep_cleanup_and_only_fixed_upgrade_guidance(
        self,
    ):
        self.data["opencode_version"] = "2.3.9999"
        self.source = recovery.capture(box, self.data, self.info, self.token)
        cases = (
            ("unsupported-version", False, True),
            ("schedule-schema", False, True),
            ("job-schema", False, True),
            ("job-metadata", False, True),
            ("job-kind", False, True),
            ("legacy-jobs", False, True),
            ("machine-schema", False, True),
            ("active-response", False, True),
            ("job-schema", True, True),
            ("database-unsafe", False, False),
            ("video-state", False, False),
            ("machine-state", False, False),
            ("blob-state", False, False),
            ("host-service-state", False, False),
            ("private-sentinel", False, False),
            ({"private-sentinel": 1}, False, False),
        )
        for category, cleanup_failure, guidance in cases:
            with self.subTest(category=category, cleanup_failure=cleanup_failure):
                self.running = True
                self.calls.clear()
                self.output.seek(0)
                self.output.truncate(0)
                error = box.BoxError("private-sentinel")
                error.history_reader_category = category

                def fail(event):
                    if event == "reader":
                        raise error
                    if (
                        cleanup_failure
                        and event == "stop"
                        and self.calls.count("stop") == 2
                    ):
                        raise box.BoxError("private-sentinel")

                self.inject = fail
                result, lease, sync = self.recover()
                self.assertIs(result, False)
                self.assertFalse(self.running)
                lease.assert_called_once()
                sync.assert_not_called()
                self.assertEqual(self.calls.count("start"), 1)
                self.assertEqual(self.calls.count("stop"), 2)
                output = self.output.getvalue()
                self.assertIn("oc-box sync", output)
                self.assertNotIn("snapshots were synchronized", output)
                self.assertNotIn("private-sentinel", output)
                self.assertEqual("Check for an oc-box update first" in output, guidance)
                self.assertEqual("upgrade OpenCode" in output, guidance)
                if guidance:
                    self.assertIn(f"contract was not recognized ({category})", output)
                    self.assertIn("if it is older than a supported release", output)
                    self.assertIn("do not reset or downgrade state", output)
                if cleanup_failure:
                    self.assertIn("cleanup-unverified", output)
                else:
                    self.assertEqual(self.calls[-2:], ["stop", "inspect-stopped"])

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
        self.assertNotIn("Check for an oc-box update", self.output.getvalue())
        self.assertNotIn("upgrade OpenCode", self.output.getvalue())

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
        self.assertIn("oc-box sync", self.output.getvalue())
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
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            for reason in (
                "private-sentinel",
                {"private-sentinel": 1},
                None,
                "activity-api-failed",
                "deadline",
                "system-config",
            ):
                recovery.report_compatibility_refusal(reason)
        self.assertEqual(output.getvalue(), "")

    def test_capture_uses_reader_series_policy_not_a_second_allowlist(self):
        for release in (
            "2.2.6",
            "2.2.7",
            "2.3.0",
            "2.3.1",
            "2.3.2",
            "2.3.47",
            "2.3.9999",
            "2.4.0",
            "2.4.1",
            "2.4.2",
        ):
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
        for release in (
            "2.2.8",
            "2.5.0",
            "2.3.01",
            "2.3.٢",
            "2.3.0+unknown",
            "2.3.2-dev",
            None,
            False,
            ["2.3.2"],
        ):
            with self.subTest(release=release):
                self.assertIsNone(
                    recovery.capture(
                        box,
                        {**self.data, "opencode_version": release},
                        self.info,
                        self.token,
                    )
                )
        with patch.object(
            recovery.history_reader, "snapshot_profile", return_value=None
        ) as profile:
            self.assertIsNone(recovery.capture(box, self.data, self.info, self.token))
        profile.assert_called_once_with(self.data["opencode_version"])

    def test_23x_recovery_preserves_same_resource_sequence_and_failure_warning(self):
        for release in ("2.3.0", "2.3.2", "2.3.47"):
            with self.subTest(release=release):
                self.data["opencode_version"] = release
                self.source = recovery.capture(box, self.data, self.info, self.token)
                self.running = True
                self.calls.clear()
                self.output.seek(0)
                self.output.truncate(0)
                result, lease, sync = self.recover()
                self.assertTrue(result)
                self.assertEqual(self.calls.count("start"), 1)
                self.assertEqual(self.calls.count("stop"), 2)
                self.assertLess(
                    self.calls.index("inspect-stopped"), self.calls.index("reader")
                )
                self.assertFalse(self.running)
                self.assertEqual(lease.call_args.args[1]["opencode_version"], release)
                sync.assert_called_once()
                self.assertIn("Execution was interrupted", self.output.getvalue())
                self.assertIn(
                    "Background work is not declared complete", self.output.getvalue()
                )

    def test_real_exit_routing_distinguishes_identical_timeout_counts(self):
        cases = {
            "eligible-shell": {},
            "eligible-shell-232": {"release": "2.3.2"},
            "eligible-subagent-232": {"release": "2.3.2", "kind": "subagent"},
            "retained-mixed-241": {
                "release": "2.4.1",
                "count": 20,
                "mixed": True,
                "shells": 6,
            },
            "interrupted-foreground-241": {
                "release": "2.4.1",
                "foreground": 1,
                "count": 20,
                "mixed": True,
                "shells": 6,
                "expected": 1,
            },
            "interrupted-foreground-232": {
                "release": "2.3.2",
                "foreground": 2,
                "count": 14,
                "mixed": True,
                "expected": 1,
            },
            "interrupted-foreground-only-232": {
                "release": "2.3.2",
                "foreground": 2,
                "count": 0,
                "expected": 1,
            },
            "interrupted-foreground-api-failed-232": {
                "release": "2.3.2",
                "foreground": 2,
                "api_failure": 5,
                "expected": 1,
            },
            "foreground-malformed-query-232": {
                "release": "2.3.2",
                "foreground": 2,
                "api_failure": 5,
                "json_failure": True,
                "refusal": "invalid-active-response",
            },
            "legacy-foreground-refused": {
                "release": "2.2.7",
                "foreground": 2,
                "refusal": "foreground-active",
            },
            "foreground-one-pair-refused": {
                "foreground": 2,
                "slow_query": True,
                "refusal": "foreground-active",
            },
            "eligible-future-subagent": {
                "release": "2.3.9999",
                "kind": "subagent",
            },
            "eligible-client-failed": {"client_code": 7},
            "eligible-client-interrupted": {"client_code": 130},
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
            "unsupported-version": {
                "release": "2.5.0",
                "refusal": "unsupported-version",
            },
            "next-24-patch-shares-strict-profile": {
                "release": "2.4.2",
                "count": 20,
                "mixed": True,
                "shells": 6,
            },
            "future-invalid-active-response": {
                "release": "2.3.9999",
                "api_refusal": "/api/session/active",
                "refusal": "invalid-active-response",
            },
            "232-invalid-jobs-response": {
                "release": "2.3.2",
                "api_refusal": "/api/experimental/job",
                "refusal": "invalid-jobs-response",
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
                release = case.get("release", "2.3.0")
                data = {
                    **self.data,
                    "opencode_version": release,
                    "podman_args": ["--group-add=keep-groups"]
                    if case.get("custom_options")
                    else [],
                }
                args = box.parser().parse_args(["start"])
                args._opencode_release = release
                events, now, requests = [], [0.0], []
                owner = SimpleNamespace(
                    connection={"url": "http://127.0.0.1:1", "password": "synthetic"},
                    publish=Mock(),
                    wait_clients=Mock(),
                    clients=case.get("clients", 0),
                    child=SimpleNamespace(returncode=case.get("wrapper_exit", 0)),
                )

                @contextlib.contextmanager
                def service(*_args, ui_bridge=False):
                    self.assertTrue(ui_bridge)  # All matrix releases are stable V2.
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
                    if target == case.get("api_refusal"):
                        return {"data": "private-sentinel"}
                    if len(requests) == case.get("api_failure"):
                        error = ValueError if case.get("json_failure") else box.BoxError
                        raise error("private-sentinel")
                    return {
                        "data": {
                            "ses_foreground_" + str(index): {"type": "running"}
                            for index in range(case.get("foreground", 0))
                        }
                        if target == "/api/session/active"
                        else [
                            {
                                "id": "job_synthetic_" + str(index),
                                "sessionID": "ses_synthetic",
                                "kind": "subagent"
                                if case.get("mixed") and index >= case.get("shells", 3)
                                else case.get("kind", "shell"),
                                "status": "running",
                            }
                            for index in range(case.get("count", 1))
                        ]
                    }

                def retry(*_args):
                    self.assertEqual(events[-1], "owner-closed")
                    events.append("recover")
                    return True

                early_warning = (
                    "Automatic post-stop history recovery is unavailable: the "
                    "OpenCode compatibility contract was not recognized "
                    "(unsupported-version)."
                )
                unsupported = case.get("refusal") == "unsupported-version"
                launch_client = box.launch_client

                def client(*args, **kwargs):
                    self.assertEqual(
                        output.getvalue().count(early_warning), int(unsupported)
                    )
                    self.assertEqual(
                        "Check for an oc-box update first" in output.getvalue(),
                        unsupported,
                    )
                    self.assertEqual(
                        owner._history_source_refusal == "unsupported-version",
                        unsupported,
                    )
                    events.append("client-open")
                    return launch_client(*args, **kwargs)

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
                    patch.object(box, "launch_client", side_effect=client),
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
                        side_effect=lambda *_a, **_kw: contextlib.nullcontext(),
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
                    result = box.launch_environment(self.path, data, args)
                    self.assertIs(type(result), int)
                    self.assertEqual(
                        result,
                        (case.get("client_code", 0) or 1)
                        if "refusal" in case
                        else case.get("expected", case.get("client_code", 0)),
                    )
                if case.get("api_refusal") or case.get("api_failure"):
                    self.assertNotIn("last complete observation", output.getvalue())
                else:
                    self.assertIn(
                        "last complete observation: foreground="
                        + str(case.get("foreground", 0))
                        + ", jobs-running="
                        + str(case.get("count", 1))
                        + ", "
                        "jobs-completed=0, jobs-error=0, jobs-cancelled=0.",
                        output.getvalue(),
                    )
                live_sync.assert_not_called()
                owner.wait_clients.assert_called_once()
                self.assertEqual(owner._client_exit_code, case.get("client_code", 0))
                self.assertEqual(
                    output.getvalue().count(early_warning), int(unsupported)
                )
                guidance = unsupported or bool(
                    case.get("api_refusal") or case.get("json_failure")
                )
                self.assertEqual(
                    "Check for an oc-box update first" in output.getvalue(), guidance
                )
                self.assertEqual("upgrade OpenCode" in output.getvalue(), guidance)
                self.assertNotIn("private-sentinel", output.getvalue())
                if guidance:
                    self.assertIn(
                        f"contract was not recognized ({case['refusal']})",
                        output.getvalue(),
                    )
                    self.assertIn("do not reset or downgrade state", output.getvalue())
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
                    if case.get("foreground"):
                        self.assertTrue(owner._history_failure.interrupted_foreground)
                        self.assertIn(
                            "will not complete interrupted work", output.getvalue()
                        )
                    self.assertEqual(
                        events,
                        [
                            "owner-open",
                            "client-open",
                            "owner-closed",
                            "recover",
                            "final-stop",
                        ],
                    )
                    self.assertNotIn("not attempted", output.getvalue())
                else:
                    recover.assert_not_called()
                    self.assertEqual(
                        events,
                        ["owner-open", "client-open", "owner-closed", "final-stop"],
                    )
                    self.assertEqual(
                        "Automatic persisted-history snapshot not attempted"
                        in output.getvalue(),
                        not bool(case.get("api_refusal") or case.get("api_failure")),
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
            def service(*_args, ui_bridge=False, events=events, owner=owner):
                self.assertIs(ui_bridge, True)
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
                owner._client_exit_code = client_code
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
                    side_effect=lambda *_, events=events: (
                        events.append("recover") or True
                    ),
                ) as retry,
                patch.object(
                    box,
                    "stop_container",
                    side_effect=lambda *_, events=events: events.append("final-stop"),
                ) as stop,
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    box.launch_environment(self.path, self.data, self.args),
                    client_code if should_retry else client_code or 1,
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

    def test_recovered_outcome_requires_known_client_status_and_final_cleanup(self):
        cases = {
            "clean-recovered": {},
            "interrupted-foreground": {"foreground": True, "expected": 1},
            "failed-foreground-client": {
                "foreground": True,
                "client_code": 7,
                "expected": 7,
            },
            "ordinary-sync-failure": {"live_failure": True},
            "ordinary-sync-failed-client": {
                "live_failure": True,
                "client_code": 7,
                "expected": 7,
            },
            "failed-client": {"client_code": 7, "expected": 7},
            "recovery-pending": {"recovered": False, "expected": 1},
            "recovery-unconfirmed": {"recovered": None, "expected": 1},
            "recovery-not-boolean": {"recovered": 1, "expected": 1},
            "missing-client-status": {"missing_status": True, "expected": 1},
            "boolean-client-status": {"client_code": False, "expected": 1},
            "unknown-client-status": {"client_code": None, "expected": 1},
            "late-cleanup-failure": {"cleanup_failure": True, "expected": 1},
            "unexpected-cleanup-error": {
                "cleanup_error": RuntimeError("private-sentinel")
            },
            "cleanup-cancel": {"cleanup_error": KeyboardInterrupt()},
            "unexpected-recovery-error": {
                "recovery_error": RuntimeError("private-sentinel")
            },
            "recovery-cancel": {"recovery_error": KeyboardInterrupt()},
        }
        source = recovery.Source("a" * 64, "synthetic-stop-token", ())
        for name, case in cases.items():
            with self.subTest(case=name):
                events = []
                release = (
                    "2.3.2"
                    if case.get("live_failure") or case.get("foreground")
                    else "2.2.7"
                )
                self.data["opencode_version"] = release
                self.args._opencode_release = release
                owner = SimpleNamespace(clients=0, child=SimpleNamespace(returncode=0))

                @contextlib.contextmanager
                def service(*_args, ui_bridge=False):
                    self.assertTrue(ui_bridge)  # All matrix releases are stable V2.
                    events.append("owner-open")
                    try:
                        yield owner
                    finally:
                        events.append("owner-closed")

                def client(*_args):
                    if case.get("live_failure"):
                        owner._history_failure = box.BoxError("fixed-live-error")
                        owner._history_failure.history_retryable = True
                    else:
                        owner._history_timeout = box.HistoryWaitTimeout(
                            "count-only",
                            recovery_eligible=True,
                            interrupted_foreground=case.get("foreground", False),
                        )
                    if not case.get("missing_status"):
                        owner._client_exit_code = case.get("client_code", 0)
                    events.append("client-closed")
                    return case.get("client_code") or 1

                def retry(*_args):
                    self.assertEqual(events[-1], "owner-closed")
                    events.append("recover")
                    if "recovery_error" in case:
                        raise case["recovery_error"]
                    return case.get("recovered", True)

                def stop(data):
                    self.assertEqual(data["container"], source.container_id)
                    events.append("final-stop")
                    if case.get("cleanup_failure"):
                        raise box.BoxError("private-sentinel")
                    if "cleanup_error" in case:
                        raise case["cleanup_error"]

                class Output(io.StringIO):
                    def write(self, text):
                        if text.startswith("Session history synced"):
                            events.append("success")
                        return super().write(text)

                output, errors = Output(), io.StringIO()
                with (
                    patch.object(box, "check_private_output"),
                    patch.object(
                        workspace_control,
                        "capture",
                        return_value="synthetic-stop-token",
                    ),
                    patch.object(box, "start_container", return_value={}),
                    patch.object(box, "prepare_runtime"),
                    patch.object(box.workspace_service, "Owner", side_effect=service),
                    patch.object(recovery, "capture", return_value=source),
                    patch.object(box, "launch_client", side_effect=client),
                    patch.object(recovery, "recover", side_effect=retry) as recover,
                    patch.object(box, "stop_container", side_effect=stop) as stopped,
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    error = case.get("recovery_error", case.get("cleanup_error"))
                    if error is not None:
                        with self.assertRaises(type(error)) as raised:
                            box.launch_environment(self.path, self.data, self.args)
                        self.assertIs(raised.exception, error)
                    else:
                        result = box.launch_environment(self.path, self.data, self.args)
                        self.assertIs(type(result), int)
                        self.assertEqual(result, case.get("expected", 0))
                recover.assert_called_once()
                stopped.assert_called_once()
                saved = (
                    case.get("recovered", True) is True
                    and not case.get("cleanup_failure")
                    and "cleanup_error" not in case
                    and "recovery_error" not in case
                )
                self.assertEqual(
                    events,
                    [
                        "owner-open",
                        "client-closed",
                        "owner-closed",
                        "recover",
                        "final-stop",
                    ]
                    + (["success"] if saved else []),
                )
                self.assertEqual("post-stop fallback" in output.getvalue(), saved)
                if not saved:
                    self.assertNotIn("Session history synced", output.getvalue())
                if (
                    case.get("cleanup_failure")
                    or "cleanup_error" in case
                    or "recovery_error" in case
                ):
                    self.assertIn("oc-box sync", errors.getvalue())
                self.assertNotIn("private-sentinel", errors.getvalue())

    def test_outer_cleanup_has_its_own_budget_even_after_failed_recovery(self):
        owner = SimpleNamespace(clients=0, child=SimpleNamespace(returncode=0))
        source = recovery.Source("a" * 64, "synthetic-stop-token", ())

        @contextlib.contextmanager
        def service(*_args, ui_bridge=False):
            self.assertIs(ui_bridge, True)
            yield owner

        def client(*_args):
            owner._history_timeout = box.HistoryWaitTimeout(
                "count-only", recovery_eligible=True
            )
            owner._client_exit_code = 7
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
        self.assertIn("oc-box sync", output.getvalue())
        self.assertIsNone(recovery.remaining())
