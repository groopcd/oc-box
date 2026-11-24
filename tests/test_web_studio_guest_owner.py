"""Original owner generation and guest-only private readiness tests."""

from concurrent.futures import Future, ThreadPoolExecutor
import contextlib
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box
from oc_box import opencode_v2_io as worker
from oc_box import web_guest_duplex_exec as exec_bridge
from oc_box import web_guest_worker_drain as worker_drain
from oc_box import web_studio_guest_binding as binding
from oc_box import web_studio_guest_owner as guest
from oc_box import workspace_control, workspace_service as service

from smoke_web_named_native import _CONTAINER, fixture_inspect


_ENDPOINT = "http://127.0.0.1:22001"
_PID = 341


def frame():
    return {
        "protocol": worker.GUEST_OWNER_PROTOCOL,
        "endpoint": {"url": _ENDPOINT, "password": "a" * 42 + "A"},
        "native": {"version": "2.4.1", "pid": _PID, "urls": [_ENDPOINT]},
    }


def wire(row=None):
    return json.dumps(frame() if row is None else row).encode("ascii") + b"\n"


class GuestWorkerTests(unittest.TestCase):
    def test_strict_reap_requires_an_exact_kernel_exit_not_an_absent_child(self):
        child = SimpleNamespace(pid=_PID, returncode=None)
        with patch.object(worker.os, "waitpid", return_value=(_PID, 7 << 8)) as wait:
            worker._wait_exact_child(child, 1)
        wait.assert_called_once_with(_PID, os.WNOHANG)
        self.assertEqual(child.returncode, 7)
        child.returncode = None
        with (
            patch.object(worker.os, "waitpid", side_effect=ChildProcessError),
            self.assertRaises(ChildProcessError),
        ):
            worker._wait_exact_child(child, 1)
        with (
            patch.object(worker.os, "waitpid", return_value=(0, 0)),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            worker._wait_exact_child(child, 0)
        self.assertIsNone(child.returncode)

    def test_authenticated_identity_must_match_the_child_the_worker_spawned(self):
        native = {
            **frame()["native"],
            "hostname": "synthetic",
            "paths": {"tmp": "/tmp/synthetic"},
            "capabilities": {"persistentPty": True},
        }
        request = Mock(return_value=native)
        request.endpoint = frame()["endpoint"]
        request.alive = lambda: True
        request.child_pid = _PID
        self.assertEqual(worker._guest_owner_record(request, monotonic() + 2), frame())
        self.assertEqual(request.call_args.args, ("GET", "/api/info"))
        self.assertEqual(request.call_args.kwargs["max_body"], 8192)
        for change in ({"pid": _PID + 1}, {"version": "2.4.0"}):
            request.return_value = native | change
            with self.subTest(change=change), self.assertRaises(worker.FixedError):
                worker._guest_owner_record(request, monotonic() + 2)
        request.return_value = native
        for deadline, alive in ((monotonic() - 1, True), (monotonic() + 1, False)):
            request.alive = lambda: alive
            with self.assertRaises(worker.FixedError):
                worker._guest_owner_record(request, deadline)

    def test_guest_exit_zero_requires_observed_eof_and_clean_native_exit(self):
        for alive, native_exit, expected in (
            ([True, True], {"forced": False, "code": 0}, True),
            ([True, False], {"forced": False, "code": 0}, False),
            ([True, True], {"forced": True, "code": 0}, False),
            ([True, True], {"forced": False, "code": 1}, False),
            ([False], {"forced": False, "code": 0}, False),
        ):
            request = SimpleNamespace(alive=Mock(side_effect=alive))

            @contextlib.contextmanager
            def server(*args, exit_info, **kwargs):
                try:
                    yield request
                finally:
                    exit_info.update(native_exit)

            with (
                self.subTest(alive=alive, native_exit=native_exit),
                patch.object(worker, "server", server),
                patch.object(worker, "require_control_pipe", return_value=1),
                patch.object(worker, "ready_plugins"),
                patch.object(worker, "_guest_owner_record", return_value=frame()),
                patch.object(worker, "write_control_record") as write,
                patch.object(worker.select, "select", return_value=([1], [], [])),
                patch.object(worker.sys, "stdin", SimpleNamespace(fileno=lambda: 0)),
                patch.object(worker.os, "read", return_value=b""),
            ):
                args = ["guest-proof-lease", "/synthetic/opencode", "2.4.1"]
                if expected:
                    worker.main(args)
                else:
                    with self.assertRaises(worker.FixedError):
                        worker.main(args)
                write.assert_called_once_with(1, frame())

    def test_guest_frame_is_not_an_ordinary_endpoint_frame(self):
        endpoint, pid = guest._ready_frame(wire())
        self.assertEqual((endpoint, pid), (frame()["endpoint"], _PID))
        for bad in (
            wire(frame()["endpoint"]),
            wire() + b"{}\n",
            wire()[:-1],
            wire().replace(b'"pid": 341', b'"pid": 341, "pid": 341'),
            wire().replace(b'"pid": 341', b'"pid": true'),
            wire().replace(b"2.4.1", b"2.4.2"),
            wire().replace(b"127.0.0.1", b"localhost"),
        ):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                guest._ready_frame(bad)


class GuestOwnerFixture:
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory(prefix="ocb-guest-owner-")))
        project = root / "project"
        project.mkdir(mode=0o700)
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "OC_BOX_STATE_HOME": str(root / "state"),
                    "XDG_RUNTIME_DIR": str(root),
                    "OC_BOX_CONTAINER": "0",
                },
            )
        )
        stack.enter_context(patch.object(guest, "_regular_host", return_value=True))
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data.update(ready=True, opencode_version="2.4.1")
        box.atomic_json(self.path, self.data)
        self.podman = stack.enter_context(
            patch.object(box, "podman", side_effect=fixture_inspect(self.data))
        )
        self.token = workspace_control.capture(self.path)
        self.owner = service.Owner(
            box, self.path, self.data, guest_stop_token=self.token
        )

    def seal(self, stack, *, endpoint=_ENDPOINT):
        stack.enter_context(box.workspace_lock(self.path))
        proof = self.owner._guest_proof = guest.GuestOwnerProof(self.owner, self.token)
        self.assertEqual(proof.pinned_data()["container"], _CONTAINER)
        with workspace_control.admission(self.path):
            proof.before_spawn()
        self.owner.child = Mock()
        self.owner.child.poll.return_value = None
        ready = frame()
        ready["endpoint"]["url"] = endpoint
        ready["native"]["urls"] = [endpoint]
        self.owner.connection = proof.seal(wire(ready))
        self.owner.publish(None)
        return proof


class GuestOwnerTests(GuestOwnerFixture, unittest.TestCase):
    def test_registered_guest_tunnel_binds_one_providerless_exec_and_retires_it(self):
        for scenario in (
            "clean",
            "stop-intent",
            "startup-uncertain",
            "retirement-uncertain",
        ):
            with self.subTest(scenario=scenario), contextlib.ExitStack() as stack:
                self.token = workspace_control.capture(self.path)
                self.owner = service.Owner(
                    box, self.path, self.data, guest_stop_token=self.token
                )
                stack.enter_context(
                    patch.dict(os.environ, {"OPENCODE_PASSWORD": "no-forward-secret"})
                )
                proof = self.seal(stack)
                gateway = service.Gateway(
                    box, proof.pinned_data(), self.owner.connection
                )
                reservation = stack.enter_context(gateway._listener_preflight())
                original = binding.bind_unserved(gateway, reservation, proof)
                registry = worker_drain.AcceptedWorkers()
                left, right = socket.socketpair()
                stack.enter_context(right)
                outcome = Future()
                child = Mock()
                tunnel = Mock()
                tunnel.close.return_value = scenario != "retirement-uncertain"

                def popen(command, **options):
                    self.assertEqual(
                        command[:14],
                        [
                            "podman",
                            "exec",
                            "-i",
                            "--workdir",
                            "/",
                            _CONTAINER,
                            "/usr/bin/env",
                            "-i",
                            "PATH=/usr/bin:/bin",
                            "/usr/bin/python3",
                            "-I",
                            "-S",
                            "-B",
                            "-c",
                        ],
                    )
                    self.assertEqual(command[-1], "22001")
                    self.assertNotIn("no-forward-secret", command[-2])
                    self.assertNotIn("OPENCODE_PASSWORD", options["env"])
                    self.assertEqual(options["env"], proof._engine_env)
                    self.assertIs(options["stdin"], subprocess.PIPE)
                    self.assertIs(options["stdout"], subprocess.PIPE)
                    self.assertEqual(options["bufsize"], 0)
                    self.assertEqual(options["cwd"], "/")
                    with self.assertRaises(workspace_control.ControlError):
                        with workspace_control.admission(self.path, timeout=0):
                            self.fail(
                                "Relay attempted to spawn without stop admission."
                            )
                    if scenario == "startup-uncertain":
                        raise OSError("private synthetic detail")
                    return child

                def run(use):
                    try:
                        relay = exec_bridge.GuestDuplexExec(original, registry, use)
                        if scenario == "stop-intent":
                            workspace_control.request_stop(self.path)
                        if scenario in ("stop-intent", "startup-uncertain"):
                            with self.assertRaisesRegex(
                                exec_bridge.GuestExecError, "^The original Studio"
                            ):
                                relay.start(deadline=monotonic() + 5)
                        else:
                            self.assertIs(relay.start(deadline=monotonic() + 5), tunnel)
                        with self.assertRaises(exec_bridge.GuestExecError):
                            relay.start(deadline=monotonic() + 5)
                        retired = relay.retire()
                        self.assertIs(relay.retire(), retired)
                        outcome.set_result(retired)
                    except BaseException as error:
                        outcome.set_exception(error)

                with (
                    patch.object(
                        exec_bridge.subprocess, "Popen", side_effect=popen
                    ) as spawn,
                    patch.object(
                        exec_bridge.web_guest_duplex_host,
                        "DuplexHost",
                        return_value=tunnel,
                    ) as supervise,
                ):
                    if not registry.dispatch(left, run):
                        left.close()
                        self.fail("Could not register the synthetic browser socket.")
                    retired = outcome.result(timeout=10)
                    self.assertEqual(retired, scenario in ("clean", "stop-intent"))
                    self.assertEqual(registry.close(timeout=3), retired)
                    self.assertEqual(spawn.call_count, int(scenario != "stop-intent"))
                    if scenario in ("clean", "retirement-uncertain"):
                        supervise.assert_called_once_with(child)
                        tunnel.close.assert_called_once()
                    else:
                        supervise.assert_not_called()
                    tunnel.abandon.assert_not_called()

    def test_exact_original_owner_pinned_and_a_stale_connection_cannot_replace_it(self):
        with box.workspace_lock(self.path):
            held = service.LIFECYCLE_LOCK.get()[1]
            fcntl.flock(held, fcntl.LOCK_UN)
            with self.assertRaises(guest.GuestOwnerError):
                guest.GuestOwnerProof(self.owner, self.token)
        self.podman.assert_not_called()
        with contextlib.ExitStack() as stack:
            proof = self.seal(stack)
            observation = proof.current()
            self.assertIsNotNone(observation)
            self.assertEqual(
                (
                    observation.container_id,
                    observation.native_pid,
                    observation.endpoint,
                ),
                (_CONTAINER, _PID, _ENDPOINT),
            )
            self.assertNotIn("password", repr(observation))
            self.assertTrue(
                all(c.args[-1] == _CONTAINER for c in self.podman.call_args_list[1:])
            )
            original = self.owner.connection
            self.owner.connection = dict(original)
            self.assertIsNone(proof.current())
            self.owner.connection = original
            self.assertIsNone(proof.current())

    def test_control_stop_and_interrupted_check_stickily_fence_owner_proof(self):
        for reason in ("stop", "interrupt"):
            with self.subTest(reason=reason), contextlib.ExitStack() as stack:
                self.token = workspace_control.capture(self.path)
                self.owner = service.Owner(
                    box, self.path, self.data, guest_stop_token=self.token
                )
                proof = self.seal(stack)
                self.assertIsNotNone(proof.current())
                if reason == "interrupt":
                    with patch.object(
                        proof, "_check_run", side_effect=KeyboardInterrupt
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            proof.current()
                else:
                    workspace_control.request_stop(self.path)
                self.assertIsNone(proof.current())

    def test_same_container_id_with_changed_start_or_lost_flock_refuses(self):
        for reason in ("restart", "unlock"):
            with self.subTest(reason=reason), contextlib.ExitStack() as stack:
                self.owner = service.Owner(
                    box, self.path, self.data, guest_stop_token=self.token
                )
                proof = self.seal(stack)
                self.assertIsNotNone(proof.current())
                if reason == "restart":
                    with patch.object(
                        guest.web_named_protocol,
                        "_running_instance",
                        return_value=(_CONTAINER, "2026-02-01T04:06:06Z"),
                    ):
                        self.assertIsNone(proof.current())
                else:
                    fcntl.flock(service.LIFECYCLE_LOCK.get()[1], fcntl.LOCK_UN)
                self.assertIsNone(proof.current())

    def test_guest_mode_never_unlinks_even_a_stale_controller(self):
        self.owner.address.write_text("synthetic-stale")
        with (
            box.workspace_lock(self.path),
            self.assertRaisesRegex(box.BoxError, "cannot replace"),
        ):
            self.owner.__enter__()
        self.assertEqual(self.owner.address.read_text(), "synthetic-stale")
        self.podman.assert_not_called()

    def test_unserved_binding_rejects_copies_foreign_reservations_and_reuse(self):
        with contextlib.ExitStack() as stack:
            proof = self.seal(stack)
            connection = self.owner.connection
            actual = service.Gateway(box, proof.pinned_data(), connection)
            copied = service.Gateway(box, proof.pinned_data(), dict(connection))
            listener = stack.enter_context(actual._listener_preflight())
            foreign = stack.enter_context(copied._listener_preflight())
            for gateway, reservation, owner in (
                (actual, foreign, proof),
                (copied, foreign, proof),
                (actual, listener, SimpleNamespace(connection=connection)),
            ):
                with self.assertRaises(binding.GuestBindingError):
                    binding.bind_unserved(gateway, reservation, owner)
            joined = binding.bind_unserved(actual, listener, proof)
            with self.assertRaises(binding.GuestBindingError):
                binding.bind_unserved(actual, listener, proof)
            self.assertIs(joined.current(), joined.native.binding)
            self.assertEqual(
                joined.native.binding.origin,
                f"http://127.0.0.1:{actual.listener.getsockname()[1]}",
            )
            with self.assertRaises(service._GatewayListenerError):
                actual._start_listener(listener)
            actual.connection = dict(connection)
            self.assertIsNone(joined.current())
            actual.connection = connection
            self.assertIsNone(joined.current())
        self.assertIsNone(joined.current())

    def test_only_one_gateway_can_claim_the_original_owner_even_after_loss(self):
        with contextlib.ExitStack() as stack:
            proof = self.seal(stack)
            candidates = [
                service.Gateway(box, proof.pinned_data(), self.owner.connection)
                for _ in range(3)
            ]
            listeners = [
                stack.enter_context(gateway._listener_preflight())
                for gateway in candidates
            ]

            def bind(index):
                try:
                    return binding.bind_unserved(
                        candidates[index], listeners[index], proof
                    )
                except binding.GuestBindingError:
                    return None

            with ThreadPoolExecutor(2) as threads:
                joined = [result for result in threads.map(bind, (0, 1)) if result]
            self.assertEqual(len(joined), 1)
            joined[0].gateway.closed.set()
            self.assertIsNone(joined[0].current())
            self.assertIsNotNone(proof.current())
            self.assertIsNone(bind(2))

    def test_host_retirement_refuses_missing_nonzero_and_unverified_worker_exit(self):
        for verdict in (
            0,
            3,
            ChildProcessError(),
            subprocess.TimeoutExpired("fixture", 10),
        ):
            with self.subTest(verdict=verdict), contextlib.ExitStack() as stack:
                self.owner = service.Owner(
                    box, self.path, self.data, guest_stop_token=self.token
                )
                self.seal(stack)
                child = self.owner.child
                child.returncode = None
                child.stdin.closed = False

                def wait(item, timeout):
                    self.assertIs(item, child)
                    self.assertEqual(timeout, 10)
                    child.stdin.close.assert_called_once_with()
                    if isinstance(verdict, Exception):
                        raise verdict
                    child.returncode = verdict

                with (
                    patch.object(worker, "_wait_exact_child", side_effect=wait),
                    patch.object(service.os, "killpg") as kill,
                ):
                    if verdict == 0:
                        self.owner.shutdown()
                    else:
                        with self.assertRaisesRegex(box.BoxError, "retirement"):
                            self.owner.shutdown()
                    kill.assert_not_called()

    def test_guest_exec_uses_isolated_bootstrap_and_ignores_ambient_engine_selectors(
        self,
    ):
        with contextlib.ExitStack() as stack:
            proof = self.seal(stack)
            stack.enter_context(patch.dict(os.environ, {"CONTAINER_HOST": "synthetic"}))
            stack.enter_context(
                patch.object(
                    box,
                    "container_env",
                    return_value={
                        "HOME": "/container/home",
                        "SYNTHETIC_TOKEN": "private-value",
                    },
                )
            )
            command = proof.worker_command("pass")
            start = command.index(_CONTAINER) + 1
            opts = ["/usr/bin/python3", "-I", "-S", "-B", "-c"]
            self.assertEqual(command[start : start + 5], opts)
            self.assertEqual(command[start + 7 : start + 12], opts)
            self.assertNotIn("CONTAINER_HOST", command.environment)
            self.assertEqual(command.environment["SYNTHETIC_TOKEN"], "private-value")
            self.assertNotIn("private-value", command)
            with patch.object(
                box, "container_env", return_value={"CONTAINER_HOST": "synthetic"}
            ):
                with self.assertRaisesRegex(box.BoxError, "Unsupported Studio owner"):
                    proof.worker_command("pass")
