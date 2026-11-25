"""Exercise the private owning history handoff without running Podman or OpenCode."""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from functools import partial
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    history_overlap_binding,
    history_overlap_domains,
    history_overlap_engine,
    history_overlap_io,
    history_overlap_remote,
    history_overlap_writer,
    history_reader,
    history_recovery,
    lifecycle_output,
    native_runtime_guard,
    oc_box,
    opencode_v2_io,
    runtime_safeguard,
    workspace_control,
    workspace_service,
)


_RELEASE = "2.4.1"
_ORIGINAL = "a" * 64
_PREVIOUS = "2026-01-02T03:04:05Z"
_STARTED = "2026-01-03T03:04:05Z"
_INIT_PID = 1_234_567_890
_INIT_PIDFD = 42
_TICKS = 4321
_ENDPOINT = {
    "url": "http://127.0.0.1:12345",
    "password": "synthetic-endpoint-marker-not-a-credential",
}


class FirstHistoryLauncherTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        home, self.project = root / "home", root / "project"
        home.mkdir()
        self.project.mkdir()
        self.engine_env = {"HOME": str(home), "PATH": "/usr/bin"}
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    **self.engine_env,
                    "OC_BOX_STATE_HOME": str(root / "state"),
                    "SYNTHETIC_API_KEY": "synthetic-forwarding-marker",
                    "OPENCODE_AUTH_CONTENT": "synthetic-auth-marker",
                },
                clear=True,
            )
        )
        self.path, self.data = oc_box.create_workspace(self.project, "fedora")
        self.data.update(ready=True, opencode_version=_RELEASE)
        oc_box.atomic_json(self.path, self.data)
        self.registry = self.path.read_bytes()
        self.args = SimpleNamespace(command="sync", import_session=None)
        self.host = runtime_safeguard.HostBinary(
            -1, (), history_overlap_writer._STOCK[_RELEASE]
        )
        # A harmless descriptor stands in for the already-qualified engine pin.
        self.fd = self.stack.enter_context(open(os.devnull, "rb")).fileno()
        self.engine = history_overlap_engine._Engine(
            SimpleNamespace(path="/usr/bin/podman", fd=self.fd, recheck=lambda: None),
            SimpleNamespace(
                environment=self.engine_env,
                require_local_store=lambda info: tuple(info["roots"]),
                recheck=lambda: None,
            ),
        )
        self.running = False
        self.generation = _PREVIOUS
        self.binding = None
        self.events = []
        self.lookups = []
        self.stop_in_prelease = False
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))
        self.stack.enter_context(lifecycle_output.invocation("sync", root / "journal"))
        self.stub(runtime_safeguard, "pinned_host", return_value=self.host_pin())
        self.stub(history_overlap_engine, "pin_local_witness_engine", self.engine_pin)
        self.stub(history_overlap_domains, "observe_process_start", self.start_ticks)
        self.stub(history_overlap_remote, "_open_proc", return_value=object())
        self.stub(history_overlap_remote, "_pin", self.pin_task)
        self.stub(
            history_overlap_remote, "_pin_namespaces", lambda _, pin: pin.live()[2]
        )
        self.stub(history_overlap_remote, "_poll", self.poll_init)
        self.stub(subprocess, "Popen", self.popen)
        self.stub(
            oc_box,
            "check_resource_controllers",
            partial(oc_box.check_resource_controllers, cgroup_root=root / "cgroups"),
        )
        self.stub(
            workspace_service.HistorySource,
            "_write",
            autospec=True,
            side_effect=self.lease_payload,
        )
        self.stub(opencode_v2_io, "_wait_exact_child", self.wait_lease)
        self.stub(history_overlap_binding, "pin_quiet_volume", self.pin_volume)
        self.stub(history_overlap_binding, "pin_ready_source", self.pin_source)
        self.stub(history_overlap_domains, "pin_writable_domains", self.pin_domains)
        self.stub(history_overlap_domains, "verify_prelease_separation", self.prelease)
        for check in ("_check_24_host_service", "_check_24_blobs", "_check_database"):
            self.stub(history_reader, check)
        self.stub(history_overlap_io.SourceRead, "start", self.inventory_spawn)
        self.stub(
            history_overlap_remote.RemoteWitness, "acquire", self.inventory_witness
        )

        # Refuse the fallback at its entry points, not only if it gets as far
        # as launching an unexpected command. Real history_source is audited by
        # the lease Popen and its private scratch payload.
        for name in (
            "detect_opencode_release",
            "prepare_runtime",
            "container_env",
            "_capture_quiet_sync_binding",
            "_stop_quiet_sync_source",
            "stop_container",
            "list_container_sessions",
        ):
            self.stub(oc_box, name, side_effect=AssertionError(f"unexpected {name}"))

    def stub(self, obj, name, callback=None, **options):
        if callback is not None:
            options["side_effect"] = callback
        return self.stack.enter_context(patch.object(obj, name, **options))

    @contextmanager
    def host_pin(self):
        self.events.append("host-pin")
        try:
            yield self.host
        finally:
            self.events.append("host-release")

    @contextmanager
    def engine_pin(self, environment):
        self.assertEqual(environment, self.engine_env)
        self.events.append("engine-pin")
        try:
            yield self.engine
        finally:
            self.events.append("engine-release")

    def require_admission(self):
        with self.assertRaises(workspace_control.ControlError) as refused:
            with workspace_control.admission(self.path, timeout=0):
                self.fail("dispatch was not protected by an admission lock")
        self.assertEqual(refused.exception.category, "control-timeout")

    def require_owner(self):
        self.assertIs(oc_box._QUIET_SYNC_BINDING.get(), self.binding)
        self.assertIs(workspace_service.LIFECYCLE_LOCK.get(), self.binding.overlap.lock)

    def observation(self):
        return {
            "Id": _ORIGINAL,
            "Config": {
                "Labels": {oc_box.LABEL: self.data["id"]},
                "Entrypoint": [history_recovery.PASSIVE_ENTRYPOINT],
                "Cmd": ["-c", history_recovery.PASSIVE_COMMAND],
                "Env": ["PATH=" + history_reader.PATH],
            },
            "HostConfig": {"PidMode": "private", "Devices": []},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.project),
                    "Destination": str(self.project),
                },
                {
                    "Type": "volume",
                    "Source": "/synthetic/backing",
                    "Destination": self.data["home"],
                    "Name": self.data["home_volume"],
                },
            ],
            "State": {
                "StartedAt": self.generation,
                "Status": "running" if self.running else "exited",
                "Running": self.running,
                "Pid": _INIT_PID if self.running else 0,
                "ConmonPid": 2345 if self.running else 0,
            },
        }

    def start_ticks(self, pid):
        self.assertEqual(pid, _INIT_PID)
        self.assertTrue(self.running)
        return _TICKS

    def pin_task(self, _stack, _proc, pid):
        if pid == os.getpid():
            return SimpleNamespace(live=lambda: ((0, 1), (pid,), (10, 20)))
        self.start_ticks(pid)
        return SimpleNamespace(
            pidfd=_INIT_PIDFD, live=lambda: ((2345, _TICKS), (_INIT_PID, 1), (11, 21))
        )

    def poll_init(self, pidfds, milliseconds=0):
        self.assertEqual(tuple(pidfds), (_INIT_PIDFD,))
        self.assertLessEqual(milliseconds, 2000)
        if not self.running:
            self.events.append("init-exited")
            return {_INIT_PIDFD}
        return set()

    def captured_query(self, query):
        if query[:3] == ("container", "inspect", "--"):
            selector = query[3]
            self.assertEqual(
                selector, self.data["container"] if not self.lookups else _ORIGINAL
            )
            self.lookups.append(selector)
            return json.dumps([self.observation()]).encode()
        if query == ("info", "--format", "json"):
            return (
                b'{"roots":["/synthetic/graph","/synthetic/run","/synthetic/volume"]}'
            )
        if query == ("volume", "inspect", "--", self.data["home_volume"]):
            return json.dumps([{"Name": self.data["home_volume"]}]).encode()
        if query == ("top", _ORIGINAL, "pid", "hpid"):
            self.assertTrue(self.running)
            return f"PID HPID\n1 {_INIT_PID}\n".encode()
        if query == ("start", "--", _ORIGINAL):
            self.require_owner()
            self.require_admission()
            with self.assertRaises(workspace_control.ControlError) as uncertain:
                workspace_control.require_certain(self.path)
            self.assertEqual(uncertain.exception.category, "uncertain-control")
            self.assertFalse(self.running)
            self.running, self.generation = True, _STARTED
            self.events.append("start")
            return b""
        if query == ("stop", "--time", "5", "--", _ORIGINAL):
            self.require_owner()
            self.require_admission()
            self.assertTrue(self.running)
            self.assertFalse(list(self.path.parent.glob(".oc-box-guarded-host-*")))
            self.assertIsNone(workspace_service.live_connection())
            self.running = False
            self.events.append("stop")
            return b""
        self.fail(f"unexpected synthetic engine query: {query[:3]!r}")

    def popen(self, argv, **options):
        argv = tuple(argv)
        self.assertEqual(options["env"], self.engine_env)
        self.assertEqual(options["cwd"], "/")
        if argv[0] == "/usr/bin/podman":
            # The real bounded engine capture uses the inherited descriptor.
            self.assertEqual(options["executable"], f"/proc/self/fd/{self.fd}")
            self.assertEqual(options["pass_fds"], (self.fd,))
            result = self.captured_query(argv[1:])
            rd, wr = os.pipe()
            with os.fdopen(wr, "wb") as stream:
                stream.write(result)
            return SimpleNamespace(
                stdout=os.fdopen(rd, "rb"), poll=lambda: 0, wait=lambda *, timeout: 0
            )
        self.assertEqual(argv[0], "podman")
        self.assertEqual(options["executable"], f"/proc/{os.getpid()}/fd/{self.fd}")
        self.require_owner()
        if argv[1:] == ("info", "--format", "{{json .Host.CgroupControllers}}"):
            options["stdout"].write(b'["pids","cpu","memory"]')
        elif argv[1:4] == ("exec", _ORIGINAL, "/bin/sh"):
            self.require_admission()
            self.assertTrue(self.running)
            self.assertEqual(argv[-2:], ("oc-box", str(self.project)))
            self.assertTrue(options["start_new_session"])
            self.events.append("access")
        else:
            self.assertEqual(
                argv[1:9],
                (
                    "exec",
                    "-i",
                    "--workdir",
                    "/",
                    _ORIGINAL,
                    "/usr/bin/env",
                    "-i",
                    "PATH=" + history_reader.PATH,
                ),
            )
            self.assertEqual(argv[-2:], ("history-source", oc_box.CONTAINER_OPENCODE))
            self.require_admission()
            self.assertTrue(self.running)
            rd, wr = os.pipe()
            child = SimpleNamespace(
                stdin=self.stack.enter_context(os.fdopen(wr, "wb")),
                stdout=self.stack.enter_context(os.fdopen(rd, "rb")),
                returncode=None,
            )
            child.poll = lambda: child.returncode
            self.events.append("lease-spawn")
            return child

        # oc_box.run captures real binary streams around the simulated command.
        def communicate(input=None, *, timeout=None):
            self.assertIsNone(input)
            if argv[1] == "exec":
                self.assertGreater(timeout, 0)
            return None, None

        child = Mock(returncode=0, communicate=communicate, poll=lambda: 0)
        child.__enter__ = Mock(return_value=child)
        child.__exit__ = Mock(return_value=False)
        return child

    def lease_payload(self, lease, payload, deadline):
        self.assertGreater(deadline, time.monotonic())
        self.assertEqual(lease.data["container"], _ORIGINAL)
        settings = json.loads(payload)
        self.assertEqual(settings["version"], _RELEASE)
        self.assertEqual(
            settings["scratch_binding"],
            {
                "home": self.data["home"],
                "version": _RELEASE,
                "stock_digest": self.host.digest,
            },
        )
        self.source_scope = {
            "pid": 2,
            "cwd": self.data["home"] + "/.oc-box-quiet-history-" + "c" * 32 + "/work",
        }
        lease.child.stdin.write(
            json.dumps({"endpoint": _ENDPOINT, "source": self.source_scope}).encode()
            + b"\n"
        )
        lease.child.stdin.flush()

    def wait_lease(self, child, timeout):
        if timeout == 0 and not child.stdin.closed:
            # Scoped readiness now uses nonblocking exact waits rather than
            # Popen.poll(), which can synthesize success after a lost wait.
            raise subprocess.TimeoutExpired(["podman"], timeout)
        self.assertGreater(timeout, 0)
        self.assertTrue(child.stdin.closed)
        child.returncode = 0
        self.events.append("lease-retired")

    @contextmanager
    def pin_volume(self, _box, data, binding, *, check, inspect, inspect_volume):
        self.assertIs(binding, self.binding)
        self.assertEqual(data["container"], _ORIGINAL)
        check()
        self.assertEqual(inspect()["Id"], _ORIGINAL)
        self.assertEqual(inspect_volume()["Name"], self.data["home_volume"])
        yield SimpleNamespace(
            scratch_binding=lambda version: {"home": data["home"], "version": version}
        )

    def prelease(self, _volume, _layout):
        self.require_admission()
        (scratch,) = self.path.parent.glob(".oc-box-guarded-host-*")
        self.assertTrue((scratch / "home").is_dir())
        if self.stop_in_prelease:
            workspace_control.request_stop(self.path)
            self.events.append("stop-intent")

    @contextmanager
    def pin_source(self, volume, data, scope, *, top, check):
        check()
        self.assertEqual(scope, self.source_scope)
        self.assertIn(f"1 {_INIT_PID}".encode(), top())
        yield volume, history_overlap_binding.source_layout(data, scope)

    @contextmanager
    def pin_domains(self, **proof):
        self.assertEqual(proof["host_cwd"], str(self.path.parent / "controller"))
        domains = history_overlap_domains._Domains(None, (), (), (), None)
        domains.require_destination = lambda layout: self.assertEqual(
            layout, proof["destination"]
        )
        yield domains

    def inventory_spawn(self, argv, *, attempt, env, payload, admission, **bounds):
        self.assertEqual(
            tuple(argv[:8]),
            (
                "podman",
                "exec",
                "-i",
                "--workdir",
                "/",
                _ORIGINAL,
                "/usr/bin/env",
                "-i",
            ),
        )
        self.assertEqual(env, self.engine_env)
        self.assertEqual(bounds["executable"], f"/proc/{os.getpid()}/fd/{self.fd}")
        self.assertTrue(bounds["two_phase"])
        self.assertNotIn(_ENDPOINT["password"], repr((argv, env)))
        self.assertEqual(
            json.loads(payload), {"directory": str(self.project), "endpoint": _ENDPOINT}
        )
        self.assertEqual(
            (attempt.container_id, attempt.generation), (_ORIGINAL, _STARTED)
        )
        with admission(time.monotonic() + 1):
            self.require_owner()
            self.require_admission()
            self.events.append("inventory-spawn")

        def go(witness, *, admission):
            with admission(time.monotonic() + 1):
                self.require_admission()
                self.assertIs(witness._attempt, attempt)

        return SimpleNamespace(
            wait_ready=lambda: b"synthetic-ready",
            go=go,
            finish=lambda: history_overlap_io.SourceOutcome(
                "local-complete",
                True,
                0,
                True,
                True,
                True,
                time.monotonic_ns(),
                attempt,
            ),
            response=lambda _proof: b"I[]",
        )

    def inventory_witness(self, attempt, ready, *, probe, deadline):
        self.assertEqual(ready, b"synthetic-ready")
        snapshot = probe(attempt, deadline, history_overlap_remote.MAX_TOP_BYTES)
        self.assertIs(snapshot.attempt, attempt)
        self.assertEqual(
            (snapshot.init_host_pid, snapshot.init_start_ticks), (_INIT_PID, _TICKS)
        )
        return SimpleNamespace(
            _attempt=attempt,
            observe=lambda **_: history_overlap_io.HelperAbsent(
                attempt, time.monotonic_ns()
            ),
            close=lambda: self.events.append("inventory-retired"),
        )

    def launch_owned(self):
        with oc_box.workspace_lock(self.path):
            with self.assertRaisesRegex(oc_box.BoxError, "active oc-box process"):
                with oc_box.workspace_lock(self.path):
                    self.fail("a second workspace lock was granted")
            with oc_box._bind_first_history_sync(self.path, self.data) as binding:
                self.binding = binding
                try:
                    return oc_box.launch_environment(
                        self.path, self.data, self.args, _first_history=binding
                    )
                finally:
                    self.assertIsNone(oc_box._QUIET_SYNC_BINDING.get())
                    self.assertIsNone(workspace_service.quiet_history_source())
                    self.assertIsNone(workspace_service.live_connection())

    def assert_clean(self):
        self.assertTrue(self.binding.overlap.closed)
        self.assertIsNone(workspace_service.LIFECYCLE_LOCK.get())
        self.assertFalse(self.running)
        self.assertEqual(self.generation, _STARTED)
        workspace_control.require_certain(self.path)
        self.assertEqual(self.path.read_bytes(), self.registry)
        self.assertEqual(self.lookups[0], self.data["container"])
        self.assertTrue(all(selector == _ORIGINAL for selector in self.lookups[1:]))
        self.assertFalse(list(self.path.parent.glob(".oc-box-guarded-host-*")))

    def test_registered_transitions_and_device_bindings_refuse_before_engine_pin(self):
        migration = runtime_safeguard.entrypoint_migration
        cases = {
            "pending-conversion": {migration.FIELD: {}},
            "pending-retirement": {migration.RETIRE: {}},
            "requires-enrollment": {migration.RECEIPT: {}},
            "saved-device": {"devices": ["/dev/dri/renderD128"]},
        }
        with oc_box.workspace_lock(self.path):
            for name, additions in cases.items():
                with self.subTest(name=name):
                    registered = {**self.data, **additions}
                    oc_box.atomic_json(self.path, registered)
                    for supplied in (registered, self.data):
                        with self.assertRaises(oc_box.BoxError):
                            with oc_box._bind_first_history_sync(self.path, supplied):
                                self.fail(
                                    "guarded state entered the ordinary overlap lane"
                                )
            oc_box.atomic_json(self.path, self.data)
            with workspace_service._device_engine_scope(
                self.data, _ORIGINAL, self.engine_env
            ):
                with self.assertRaises(oc_box.BoxError):
                    with oc_box._bind_first_history_sync(self.path, self.data):
                        self.fail(
                            "borrowed device engine entered the ordinary overlap lane"
                        )
            native_runtime_guard.publish(
                self.path,
                None,
                native_runtime_guard.Record(
                    _ORIGINAL,
                    self.data["home_volume"],
                    _RELEASE,
                    self.host.digest,
                    False,
                ),
            )
            with self.assertRaisesRegex(oc_box.BoxError, "guarded runtime enrollment"):
                with oc_box._bind_first_history_sync(self.path, self.data):
                    self.fail("registered runtime guard was bypassed")
        self.assertEqual(self.events, [])
        self.assertEqual(self.lookups, [])

    def test_new_retirement_intent_after_binding_vetoes_launch_without_stop(self):
        migration = runtime_safeguard.entrypoint_migration
        with oc_box.workspace_lock(self.path):
            with oc_box._bind_first_history_sync(self.path, self.data) as binding:
                oc_box.atomic_json(self.path, {**self.data, migration.RETIRE: {}})
                with self.assertRaisesRegex(oc_box.BoxError, "registration changed"):
                    oc_box.launch_environment(
                        self.path, self.data, self.args, _first_history=binding
                    )
                self.assertFalse(binding.start_requested)
                workspace_control.require_certain(self.path)
        self.assertEqual(
            self.events, ["host-pin", "engine-pin", "engine-release", "host-release"]
        )
        self.assertEqual(self.lookups, [self.data["container"]])
        self.assertFalse(self.running)

    def test_exact_owner_launches_only_the_access_probe_and_isolated_source(self):
        self.assertEqual(self.launch_owned(), 0)
        self.assert_clean()
        self.assertEqual(
            self.events,
            [
                "host-pin",
                "engine-pin",
                "start",
                "access",
                "lease-spawn",
                "inventory-spawn",
                "inventory-retired",
                "lease-retired",
                "stop",
                "init-exited",
                "engine-release",
                "host-release",
            ],
        )
        self.assertEqual(oc_box.session_state(self.path)["sessions"], [])
        self.assertIn("No history to sync.", self.stdout.getvalue())

    def test_stop_during_prelease_proof_vetoes_lease_but_stops_the_original(self):
        self.stop_in_prelease = True
        with self.assertRaisesRegex(
            oc_box.BoxError, "Private history source lease failed"
        ):
            self.launch_owned()
        self.assert_clean()
        self.assertEqual(
            self.events,
            [
                "host-pin",
                "engine-pin",
                "start",
                "access",
                "stop-intent",
                "stop",
                "init-exited",
                "engine-release",
                "host-release",
            ],
        )
        self.assertFalse(
            workspace_control.unchanged(self.path, self.binding.overlap.stop_token)
        )
        self.assertFalse(oc_box.session_file(self.path).exists())
        self.assertNotIn("History synced", self.stdout.getvalue())
