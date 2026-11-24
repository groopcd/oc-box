"""Original owner generation and guest-only private readiness tests."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box
from oc_box import opencode_v2_io as worker
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


class GuestOwnerTests(unittest.TestCase):
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

    def seal(self, stack):
        stack.enter_context(box.workspace_lock(self.path))
        proof = self.owner._guest_proof = guest.GuestOwnerProof(self.owner, self.token)
        self.assertEqual(proof.pinned_data()["container"], _CONTAINER)
        with workspace_control.admission(self.path):
            proof.before_spawn()
        self.owner.child = Mock()
        self.owner.child.poll.return_value = None
        self.owner.connection = proof.seal(wire())
        self.owner.publish(None)
        return proof

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
