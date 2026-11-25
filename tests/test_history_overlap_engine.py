"""Use synthetic Podman observations to test exact-original engine binding."""

from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import history_overlap_engine as engine
from oc_box import (
    history_overlap_pass,
    history_reader,
    history_recovery,
    oc_box,
    workspace_control,
    workspace_service,
)
from oc_box.history_overlap_io import SourceAttempt
from oc_box.history_overlap_order import OverlapRefusal


IDENT = "a" * 64
DATA = {
    "id": "b" * 32,
    "container": "oc-box-synthetic",
    "home_volume": "oc-box-synthetic-home",
    "project": "/synthetic/project",
    "home": "/synthetic/home",
}


def observation(*, stamp="2026-01-02T03:04:05Z", running=False):
    return {
        "Id": IDENT,
        "Config": {
            "Labels": {oc_box.LABEL: DATA["id"]},
            "Entrypoint": [history_recovery.PASSIVE_ENTRYPOINT],
            "Cmd": ["-c", history_recovery.PASSIVE_COMMAND],
            "Env": ["PATH=" + history_reader.PATH],
        },
        "HostConfig": {"PidMode": "private", "Devices": []},
        "Mounts": [
            {"Type": "bind", "Source": DATA["project"], "Destination": DATA["project"]},
            {
                "Type": "volume",
                "Source": "/synthetic/backing",
                "Destination": DATA["home"],
                "Name": DATA["home_volume"],
            },
        ],
        "State": {
            "StartedAt": stamp,
            "Status": "running" if running else "exited",
            "Running": running,
            "Pid": 1234 if running else 0,
            "ConmonPid": 5678 if running else 0,
        },
    }


class OriginalBindingTests(unittest.TestCase):
    def setUp(self):
        self.deadline = time.monotonic() + 4
        self.engine = Mock()
        self.engine.inspect.return_value = observation()
        self.original = engine.Original(oc_box, self.engine, DATA, self.deadline)
        self.engine.inspect.assert_called_once_with(DATA["container"], self.deadline)
        pin = patch.object(
            engine.history_overlap_domains, "observe_process_start", return_value=42
        )
        self.start_ticks = pin.start()
        self.addCleanup(pin.stop)
        self.started = observation(stamp="2026-01-03T03:04:05Z", running=True)

    def start(self):
        self.original.require_before_start(self.deadline)
        self.engine.inspect.return_value = self.started
        self.original.bind_started(self.deadline)

    def test_exact_id_and_generation_before_and_after_top(self):
        self.start()
        attempt = SourceAttempt(IDENT, self.original.generation)
        self.engine._top.return_value = b"fixture top output"
        size = 123
        result = self.original.probe(attempt, self.deadline, size)
        self.assertIs(result.attempt, attempt)
        self.assertEqual((result.init_host_pid, result.init_start_ticks), (1234, 42))
        self.assertEqual(result.output, b"fixture top output")
        self.engine._top.assert_called_once_with(IDENT, self.deadline, size)
        self.assertEqual(
            [call.args[0] for call in self.engine.inspect.call_args_list],
            [DATA["container"], IDENT, IDENT, IDENT, IDENT],
        )

    def test_replacement_during_top_cannot_issue_witness(self):
        self.start()
        replacement = deepcopy(self.started)
        replacement["State"]["StartedAt"] = "2026-01-04T03:04:05Z"
        self.engine.inspect.side_effect = [self.started, replacement]
        with self.assertRaises(OverlapRefusal):
            self.original.probe(
                SourceAttempt(IDENT, self.original.generation), self.deadline, 123
            )

    def test_same_id_and_timestamp_do_not_waive_process_identity(self):
        self.start()
        self.start_ticks.return_value = 43
        with self.assertRaises(OverlapRefusal):
            self.original.require_running(self.deadline)

    def test_cleanup_metadata_cannot_switch_original_or_generation(self):
        self.start()
        self.engine.inspect.return_value = observation()
        with self.assertRaises(OverlapRefusal):
            self.original.require_stopped(self.deadline)
        stopped = observation(stamp=self.original.generation)
        self.engine.inspect.return_value = stopped
        self.assertIs(self.original.require_stopped(self.deadline), stopped)
        stopped["Mounts"][1]["Source"] = "/synthetic/other"
        with self.assertRaises(OverlapRefusal):
            self.original.require_stopped(self.deadline)

    def test_legacy_or_failed_inspect_is_not_admission(self):
        self.engine.inspect.side_effect = OverlapRefusal("synthetic-query-failed")
        with self.assertRaisesRegex(OverlapRefusal, "synthetic-query-failed"):
            engine.Original(oc_box, self.engine, DATA, self.deadline)
        self.engine.inspect.side_effect = None
        prior = observation()
        prior["Config"]["Cmd"] = ["-c", "exec sleep infinity"]
        self.engine.inspect.return_value = prior
        with self.assertRaises(OverlapRefusal):
            engine.Original(oc_box, self.engine, DATA, self.deadline)

    def control(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        path = Path(private.name) / "workspace.json"
        path.write_text("{}\n")
        path.chmod(0o600)
        return path, workspace_control.capture(path)

    def test_stop_before_dispatch_does_not_start_or_mark_uncertain(self):
        path, token = self.control()
        workspace_control.request_stop(path)
        with ExitStack() as lifetime:
            with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
                self.original.start(
                    path, token, lifetime=lifetime, deadline=self.deadline
                )
        workspace_control.require_certain(path)
        self.assertFalse(self.original.start_requested)
        self.engine._transition.assert_not_called()

    def test_stop_winning_the_intent_writer_lock_vetoes_start_and_uncertainty(self):
        path, token = self.control()
        mark = workspace_control.mark_uncertain_if_current

        def raced(path, baseline):
            workspace_control.request_stop(path)
            return mark(path, baseline)

        with (
            ExitStack() as lifetime,
            patch.object(
                workspace_control, "mark_uncertain_if_current", side_effect=raced
            ),
        ):
            with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
                self.original.start(
                    path, token, lifetime=lifetime, deadline=self.deadline
                )
        workspace_control.require_certain(path)
        self.assertFalse(workspace_control.unchanged(path, token))
        self.engine._transition.assert_not_called()

    def test_failed_start_retains_durable_uncertainty_without_substitute_stop(self):
        path, token = self.control()
        self.engine._transition.side_effect = OverlapRefusal("synthetic start failure")
        with ExitStack() as lifetime:
            with self.assertRaisesRegex(OverlapRefusal, "synthetic start failure"):
                self.original.start(
                    path, token, lifetime=lifetime, deadline=self.deadline
                )
        self.assertTrue(self.original.start_requested)
        with self.assertRaises(workspace_control.ControlError) as failure:
            workspace_control.require_certain(path)
        self.assertEqual(failure.exception.category, "uncertain-control")
        with self.assertRaisesRegex(OverlapRefusal, "cleanup-unverified"):
            self.original.stop(path, deadline=self.deadline)
        self.engine._transition.assert_called_once_with("start", IDENT, self.deadline)

    def test_rotated_stop_intent_blocks_new_work_but_not_bound_cleanup(self):
        path, token = self.control()
        init = Mock(pidfd=11)
        init.live.return_value = ("synthetic",)

        def transition(command, ident, _deadline):
            self.assertEqual(ident, IDENT)
            if command == "start":
                with self.assertRaises(workspace_control.ControlError):
                    workspace_control.require_certain(path)
                self.engine.inspect.return_value = self.started
                workspace_control.request_stop(path)
            elif command == "stop":
                self.engine.inspect.return_value = observation(
                    stamp=self.original.generation
                )

        def pin(_lifetime):
            self.original._init = init, init.live.return_value

        self.engine._transition.side_effect = transition
        with (
            ExitStack() as lifetime,
            patch.object(self.original, "_pin_private_init", side_effect=pin),
            patch.object(
                engine.history_overlap_remote, "_poll", side_effect=[set(), {11}]
            ),
        ):
            with self.assertRaisesRegex(OverlapRefusal, "stop-intent"):
                self.original.start(
                    path, token, lifetime=lifetime, deadline=self.deadline
                )
            self.assertFalse(workspace_control.unchanged(path, token))
            self.original.stop(path, deadline=self.deadline)
        self.assertEqual(
            [call.args[0] for call in self.engine._transition.call_args_list],
            ["start", "stop"],
        )

    def test_stop_refuses_a_later_running_generation_without_dispatch(self):
        path, _token = self.control()
        self.start()
        self.original._init = Mock(pidfd=11), ("synthetic",)
        self.engine.inspect.return_value = observation(
            stamp="2026-01-04T03:04:05Z",
            running=True,
        )
        with patch.object(engine.history_overlap_remote, "_poll", return_value=set()):
            with self.assertRaisesRegex(
                OverlapRefusal, "cleanup-unverified"
            ) as failure:
                self.original.stop(path, deadline=self.deadline)
        self.assertTrue(failure.exception.history_source_cleanup_failed)
        self.engine._transition.assert_not_called()

    def test_stopped_metadata_alone_cannot_confirm_cleanup(self):
        path, _token = self.control()
        self.start()
        init = Mock(pidfd=11)
        init.live.return_value = ("synthetic",)
        self.original._init = init, init.live.return_value
        self.engine._transition.side_effect = lambda *_: setattr(
            self.engine.inspect,
            "return_value",
            observation(stamp=self.original.generation),
        )
        with patch.object(engine.history_overlap_remote, "_poll", return_value=set()):
            with self.assertRaisesRegex(OverlapRefusal, "cleanup-unverified"):
                self.original.stop(path, deadline=self.deadline)
        self.engine._transition.assert_called_once_with("stop", IDENT, self.deadline)


class EngineSelectionTests(unittest.TestCase):
    def test_unsupported_selectors_are_refused_before_lookup(self):
        for env in (
            {"HOME": "/synthetic", "CONTAINERS_STORAGE_CONF": "/fixture/config"},
            {"HOME": "/synthetic", "PATH": ".:/usr/bin"},
        ):
            with (
                self.subTest(env=env),
                patch.object(engine.workspace_podman, "_host_uid", return_value=1000),
                patch.object(engine.workspace_podman, "_pin") as pin,
                patch.object(engine.workspace_podman, "_capture_pinned") as capture,
            ):
                with self.assertRaises(OverlapRefusal):
                    with engine.pin_local_witness_engine(env):
                        self.fail("unexpected engine")
                pin.assert_not_called()
                capture.assert_not_called()

    def test_stage_rechecks_roots_and_fails_before_using_changed_engine(self):
        leaf = Mock()
        config = Mock(environment={"HOME": "/synthetic"})
        bound = engine._Engine(leaf, config)
        root = Mock()
        bound._roots = (root,)
        root.recheck.side_effect = engine.workspace_podman.InventoryError("synthetic")
        with patch.object(engine.workspace_podman, "_capture_pinned") as capture:
            with self.assertRaises(OverlapRefusal):
                bound.inspect(IDENT, time.monotonic() + 4)
        leaf.recheck.assert_called_once()
        config.recheck.assert_called_once()
        capture.assert_not_called()

    def test_effective_store_and_queries_use_the_same_config_witness(self):
        config = Mock(environment={"HOME": "/synthetic", "PATH": "/usr/bin"})
        config.require_local_store.return_value = ("/graph", "/run", "/graph/volumes")
        leaf = Mock()
        bound = engine._Engine(leaf, config)
        deadline = time.monotonic() + 4
        with patch.object(
            engine.workspace_podman,
            "_capture_pinned",
            return_value=(0, b'{"store":{}}'),
        ) as capture:
            self.assertEqual(
                bound.check_local(deadline), config.require_local_store.return_value
            )
            capture.assert_called_once_with(
                leaf,
                ("info", "--format", "json"),
                config.environment,
                deadline,
                engine._LIMIT,
            )
            config.require_local_store.assert_called_once_with({"store": {}})
            self.assertEqual(config.recheck.call_count, 2)
            config.recheck.side_effect = OverlapRefusal("synthetic drift")
            with self.assertRaisesRegex(OverlapRefusal, "synthetic drift"):
                bound.inspect(IDENT, deadline)
            capture.assert_called_once()

    def test_launcher_dispatch_uses_pinned_executable_with_normal_argv_zero(self):
        executable = "/proc/123/fd/4"
        pin = Mock()
        pin.command_path.return_value = executable
        environment = {"HOME": "/synthetic", "PATH": "/usr/bin"}
        binding = oc_box._QuietSyncBinding(IDENT, (), environment, engine=pin)
        token = oc_box._QUIET_SYNC_BINDING.set(binding)
        self.addCleanup(oc_box._QUIET_SYNC_BINDING.reset, token)
        with patch.object(oc_box.subprocess, "run") as invoke:
            invoke.return_value = subprocess.CompletedProcess([], 0)
            oc_box.podman("container", "inspect", IDENT)
            invoke.assert_called_once_with(
                ["podman", "container", "inspect", IDENT],
                text=True,
                env=environment,
                executable=executable,
                cwd="/",
            )
            pin.command_path.side_effect = OverlapRefusal("synthetic")
            with self.assertRaisesRegex(oc_box.BoxError, "engine changed"):
                oc_box.podman("stop", IDENT)
            invoke.assert_called_once()


class FirstPassLauncherTests(unittest.TestCase):
    def test_explicit_owned_first_pass_never_substitutes_an_ordinary_source(self):
        for error in (
            None,
            OverlapRefusal("synthetic"),
            KeyboardInterrupt(),
            SystemExit(0),
        ):
            with self.subTest(error=type(error).__name__), ExitStack() as lifetime:
                root = Path(lifetime.enter_context(tempfile.TemporaryDirectory()))
                path = root / "workspace.json"
                data = {
                    **DATA,
                    "opencode_version": "2.4.1",
                    "ready": True,
                    "distro": "fedora",
                }
                oc_box.atomic_json(path, data)
                args = SimpleNamespace(command="sync", import_session=None)
                lock = (path, object())
                current = workspace_service.LIFECYCLE_LOCK.set(lock)
                lifetime.callback(workspace_service.LIFECYCLE_LOCK.reset, current)
                original = Mock(_data=data)
                ownership = oc_box._HistoryOverlapOwnership(
                    path,
                    lock,
                    lifetime,
                    workspace_control.capture(path),
                    original,
                    object(),
                )
                binding = oc_box._QuietSyncBinding(
                    IDENT,
                    (),
                    {"PATH": "/usr/bin"},
                    engine=Mock(),
                    overlap=ownership,
                )
                first = object()
                calls = []

                def start(_path, manual_source):
                    self.assertIs(oc_box._QUIET_SYNC_BINDING.get(), binding)
                    self.assertEqual(manual_source["container"], IDENT)
                    binding.start_requested = binding.start_verified = True
                    calls.append("start")
                    return observation(stamp="2026-01-03T03:04:05Z", running=True)

                @contextmanager
                def source(_box, _path, manual_source, **kwargs):
                    self.assertIs(oc_box._QUIET_SYNC_BINDING.get(), binding)
                    self.assertEqual(manual_source["container"], IDENT)
                    self.assertIs(kwargs["original"], original)
                    calls.append("source")
                    if error is not None:
                        raise error
                    yield first
                    calls.append("source-retired")

                def sync(_path, manual_source, *, announce, first_overlap):
                    self.assertFalse(announce)
                    self.assertEqual(manual_source["container"], IDENT)
                    self.assertIs(first_overlap, first)
                    calls.append("sync")

                handlers = {
                    "validate_host_file_open": Mock(),
                    "configure_host_clipboard": Mock(),
                    "effective_image_preview": Mock(return_value=False),
                    "check_private_output": Mock(),
                    "detect_opencode_release": Mock(
                        side_effect=AssertionError("unpinned native")
                    ),
                    "start_container": start,
                    "prepare_runtime": Mock(
                        side_effect=AssertionError("ordinary preparation")
                    ),
                    "sync_sessions": sync,
                    "loading_stage": lambda *a, **k: nullcontext(),
                    "_report_history_result": Mock(),
                }

                def stop(*_, **_options):
                    calls.append("stop")
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise OverlapRefusal("cleanup-unverified", cleanup_failed=True)

                original.stop.side_effect = stop
                lifetime.enter_context(patch.multiple(oc_box, **handlers))
                lifetime.enter_context(patch.dict(oc_box.os.environ, {}, clear=True))
                lifetime.enter_context(
                    patch.object(history_overlap_pass, "open_first_pass", source)
                )
                ordinary = lifetime.enter_context(
                    patch.object(workspace_service, "history_source")
                )
                if error is not None:
                    with self.assertRaises(type(error)) as failure:
                        oc_box.launch_environment(
                            path, data, args, _first_history=binding
                        )
                    self.assertIs(failure.exception, error)
                    self.assertEqual(calls, ["start", "source", "stop"])
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        self.assertTrue(error.history_source_cleanup_failed)
                        report = handlers["_report_history_result"].call_args.args[0]
                        self.assertEqual(report.reason, "cleanup-unverified")
                    if isinstance(error, SystemExit):
                        self.assertEqual(error.code, 1)
                else:
                    self.assertEqual(
                        oc_box.launch_environment(
                            path, data, args, _first_history=binding
                        ),
                        0,
                    )
                    self.assertEqual(
                        calls,
                        [
                            "start",
                            "source",
                            "sync",
                            "source-retired",
                            "stop",
                        ],
                    )
                ordinary.assert_not_called()
                handlers["prepare_runtime"].assert_not_called()
                handlers["detect_opencode_release"].assert_not_called()
                self.assertIsNone(oc_box._QUIET_SYNC_BINDING.get())
