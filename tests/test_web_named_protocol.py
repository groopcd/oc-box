"""Real local control UDS and lifecycle lock, synthetic container/native I/O."""

import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import (
    oc_box as box,
    opencode_v2_io as worker,
    ui_state,
    web_local_origins,
    web_named_owner,
    web_named_protocol as protocol,
    web_native_pair,
    workspace_control,
    workspace_service as service,
)


_CONTAINER = "e" * 64
_STARTED = "2026-02-01T04:05:06.123456789Z"
_ENDPOINT = "http://127.0.0.1:51303"
_SECRET = "T" * 43
_PRIVATE = "synthetic-private-inspect-diagnostic"


class OwnerProtocolTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory(prefix="ocb-named-")
        self.addCleanup(private.cleanup)
        self.root = Path(private.name)
        self.patch(
            patch.dict(
                os.environ,
                {
                    "OC_BOX_STATE_HOME": str(self.root / "state"),
                    "XDG_RUNTIME_DIR": str(self.root),
                    "OC_BOX_CONTAINER": "0",
                    "SYNTHETIC_PROVIDER_TOKEN": "provider-fixture",
                },
            )
        )
        project = self.root / "project"
        project.mkdir()
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data.update(ready=True, opencode_version="2.3.2")
        box.atomic_json(self.path, self.data)
        self.stop_token = workspace_control.capture(self.path)
        self.info = {
            "Id": _CONTAINER,
            "State": {
                "Status": "running",
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "StartedAt": _STARTED,
            },
            "Config": {"Labels": {box.LABEL: self.data["id"]}},
            "HostConfig": {
                "NetworkMode": "pasta:--no-map-gw,--map-host-loopback,169.254.1.2",
                "PortBindings": {},
            },
            "Pod": "",
            "Mounts": [
                {"Type": "bind", "Source": str(project), "Destination": str(project)},
                {
                    "Type": "volume",
                    "Name": self.data["home_volume"],
                    "Destination": self.data["home"],
                },
            ],
        }
        self.inspect = self.patch(
            patch.object(box, "podman", side_effect=self.local_inspect)
        )
        self.exec = self.patch(
            patch.object(box, "exec_command", side_effect=self.local_exec)
        )
        self.patch(patch.object(box, "inspect_owned", return_value=self.info))
        self.patch(patch.object(ui_state, "available", return_value=True))
        self.patch(
            patch.object(service.Owner, "default_session", lambda owner: owner.session)
        )
        self.connector = Mock()

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def local_inspect(self, *args, **kwargs):
        self.assertIn(
            args,
            (
                ("container", "inspect", self.data["container"]),
                ("container", "inspect", _CONTAINER),
            ),
        )
        self.assertTrue(kwargs["private"])
        self.assertEqual(kwargs["timeout"], 2)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("SYNTHETIC_PROVIDER_TOKEN", kwargs["env"])
        kwargs["stdout"].write(json.dumps([self.info]).encode())
        return subprocess.CompletedProcess(args, 0)

    def local_exec(self, data, arguments):
        # Replace ONLY the actual Podman exec/native process with a held pipe.
        self.assertIn(arguments[-2], ("lease", "named-lease"))
        expected = (
            _CONTAINER if arguments[-2] == "named-lease" else self.data["container"]
        )
        self.assertEqual(data["container"], expected)
        program = (
            "import json,os,sys;"
            "print(json.dumps({'url':os.environ['URL'],'password':os.environ['SECRET']}),"
            "flush=True); sys.stdin.buffer.read()"
        )
        return box.ContainerCommand(
            [sys.executable, "-I", "-S", "-B", "-c", program],
            {"URL": _ENDPOINT, "SECRET": _SECRET},
        )

    @contextlib.contextmanager
    def owner(self, *, named=True):
        with box.workspace_lock(self.path):
            with service.Owner(
                box,
                self.path,
                self.data,
                ui_bridge=True,
                named_stop_token=self.stop_token if named else None,
            ) as owner:
                owner.publish("ses_fixture")
                yield owner

    def lease(self, owner):
        return protocol.verified_owner_lease(owner, connect_native=self.connector)

    def wire(self, owner, send):
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(4)
            stream.connect(str(owner.address))
            send(stream)
            return stream.recv(4096)

    def denied(self, owner):
        with self.assertRaisesRegex(
            protocol.OwnerLeaseError, "^Named workspace owner lease"
        ):
            with self.lease(owner):
                self.fail("unexpected named lease")

    def test_explicit_mode_holds_original_authenticated_stream_and_only_one_claim(self):
        with self.owner() as owner:
            # Old clients are indistinguishable by purpose; neither action
            # may receive the private native credential in named-only mode.
            with self.assertRaises(box.BoxError):
                with service.attach(box, self.path, self.data):
                    self.fail("normal attach unexpectedly succeeded")
            for action in ("info", "named-attach"):
                with self.subTest(action=action):
                    self.assertEqual(
                        self.wire(
                            owner,
                            lambda s: s.sendall(
                                json.dumps(
                                    {
                                        "action": action,
                                        "workspace": self.data["id"],
                                    }
                                ).encode()
                                + b"\n"
                            ),
                        ),
                        b"",
                    )
            with (
                patch.object(service, "Gateway") as gateway,
                self.assertRaisesRegex(box.BoxError, "one-use link is unavailable"),
            ):
                service.web_session(
                    box,
                    self.data,
                    {"url": _ENDPOINT, "password": _SECRET},
                    SimpleNamespace(),
                )
            gateway.assert_not_called()
            captured = []
            original = owner._named_control.lease

            def create(attached, connector):
                captured.append(attached)
                return original(attached, connector)

            with patch.object(owner._named_control, "lease", side_effect=create):
                with self.lease(owner) as lease:
                    self.assertIs(lease.control, captured[0].stream)
                    self.assertEqual(captured[0].connection["started_at"], _STARTED)
                    self.assertEqual(lease.identity.container_id, _CONTAINER)
                    self.assertEqual(lease.identity.endpoint, _ENDPOINT)
                    self.assertEqual(lease.identity.version, web_native_pair._VERSION)
                    self.assertNotEqual(
                        lease.identity.generation,
                        owner.connection["ui_state"]["generation"],
                    )
                    self.assertNotIn(_SECRET, repr(lease))
                    self.assertEqual(owner.clients, 1)
                    self.assertIs(lease.observe(), lease.identity)
                    bound = web_named_owner.NamedOwnerBinding(
                        box, lease=lease, origins=web_local_origins.LocalOrigins()
                    )
                    self.addCleanup(bound.close)
                    self.assertEqual(
                        bound.binding.owner_generation, lease.identity.generation
                    )
                    self.denied(owner)
                    with self.assertRaises(box.BoxError):
                        with service.attach(box, self.path, self.data):
                            self.fail("legacy attach bypassed the active named claim")
                    self.assertEqual(owner.clients, 1)
            self.assertEqual(lease.control.fileno(), -1)
            self.assertIsNone(lease.observe())
            with owner.condition:
                self.assertTrue(
                    owner.condition.wait_for(lambda: not owner.clients, timeout=1)
                )
            self.denied(owner)
        self.connector.assert_not_called()
        self.assertEqual(
            self.inspect.call_args_list[0].args[-1], self.data["container"]
        )
        self.assertTrue(
            all(call.args[-1] == _CONTAINER for call in self.inspect.call_args_list[1:])
        )

    def test_default_and_preexisting_legacy_owner_are_never_upgraded(self):
        with self.owner(named=False) as owner:
            self.denied(owner)
            self.assertEqual(
                self.wire(
                    owner,
                    lambda stream: stream.sendall(
                        json.dumps(
                            {
                                "action": "named-attach",
                                "protocol": "oc-box.named-owner/1",
                                "workspace": self.data["id"],
                            }
                        ).encode()
                        + b"\n"
                    ),
                ),
                b"",
            )
            with service.attach(box, self.path, self.data) as legacy:
                self.assertEqual(legacy["session"], "ses_fixture")
                self.assertNotIn("web_mode", legacy)
                for root in (self.root, self.root / "other-runtime"):
                    root.mkdir(exist_ok=True)
                    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root)}):
                        with self.assertRaisesRegex(
                            box.BoxError, "existing controller"
                        ):
                            with service.Owner(
                                box,
                                self.path,
                                self.data,
                                named_stop_token=self.stop_token,
                            ):
                                self.fail("legacy owner unexpectedly replaced")
                        with self.assertRaisesRegex(
                            box.BoxError, "already has an active"
                        ):
                            with box.workspace_lock(self.path):
                                self.fail("active lease lost lifecycle lock")
            self.assertEqual(self.exec.call_args.args[1][-2], "lease")
            self.exec.assert_called_once()
            self.inspect.assert_not_called()

    def test_missing_lifecycle_lock_and_unverified_run_fail_before_native_worker(self):
        with self.assertRaisesRegex(box.BoxError, "^Named workspace owner"):
            with service.Owner(
                box, self.path, self.data, named_stop_token=self.stop_token
            ):
                self.fail("unexpected named owner without lock")
        self.inspect.assert_not_called()
        for field, value in (
            ("StartedAt", "0001-01-01T00:00:00Z"),
            ("StartedAt", "2026-02-30T00:00:00Z"),
            ("Paused", True),
            ("Restarting", True),
            ("Running", 1),
            ("Status", "exited"),
        ):
            with self.subTest(field=field, value=value):
                previous = self.info["State"][field]
                self.info["State"][field] = value
                try:
                    with self.assertRaisesRegex(box.BoxError, "^Named workspace owner"):
                        with self.owner():
                            self.fail("unexpected named owner for invalid state")
                finally:
                    self.info["State"][field] = previous
        self.exec.assert_not_called()

    def test_change_during_native_startup_refuses_before_controller_publication(self):
        def replaced(data, arguments):
            self.info["State"]["StartedAt"] = "2026-02-01T04:05:07.123456789Z"
            return self.local_exec(data, arguments)

        with box.workspace_lock(self.path):
            owner = service.Owner(
                box, self.path, self.data, named_stop_token=self.stop_token
            )
            with (
                patch.object(box, "exec_command", side_effect=replaced),
                self.assertRaisesRegex(box.BoxError, "^Named workspace owner"),
            ):
                with owner:
                    self.fail("unexpected named controller for replaced container run")
            self.assertFalse(owner.address.exists())
            self.assertIsNotNone(owner.child.returncode)

    def test_valid_mac_does_not_authorize_wrong_version_or_replayed_nonce(self):
        with self.owner() as owner:
            control = owner._named_control
            self.assertEqual(
                self.wire(
                    owner,
                    lambda s: control._send(
                        s,
                        b"request",
                        {
                            "action": protocol._ACTION,
                            "protocol": "oc-box.named-owner/2",
                            "workspace": self.data["id"],
                            "nonce": "d" * 64,
                        },
                    ),
                ),
                b"",
            )
            original = control._send

            def replay(stream, direction, payload):
                if direction == b"reply":
                    payload = {**payload, "nonce": "c" * 64}
                original(stream, direction, payload)

            with patch.object(control, "_send", side_effect=replay):
                self.denied(owner)
            self.denied(owner)  # A signed but unconfirmed reply consumes the claim.
        self.connector.assert_not_called()

    def test_unsigned_reply_cannot_produce_a_handle_even_for_the_right_identity(self):
        with self.owner() as owner:
            control = owner._named_control
            original = control._send

            def unsigned(stream, direction, payload):
                if direction == b"reply":
                    stream.sendall(json.dumps(payload).encode() + b"\n")
                else:
                    original(stream, direction, payload)

            with patch.object(control, "_send", side_effect=unsigned):
                self.denied(owner)
            self.denied(owner)

    def test_container_run_drift_cannot_rebind_and_errors_remain_fixed(self):
        with self.owner() as owner, self.lease(owner) as lease:
            self.info["State"]["StartedAt"] = "2026-02-01T04:05:07.123456789Z"
            self.assertIsNone(lease.observe())
            self.info["State"]["StartedAt"] = _STARTED
            self.assertIsNone(lease.observe())
        self.connector.assert_not_called()

    def test_container_id_drift_and_inspect_errors_refuse_claim(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), self.owner() as owner:
                options = (
                    {"side_effect": box.BoxError(_PRIVATE)}
                    if unavailable
                    else {"side_effect": self.local_inspect}
                )
                with (
                    patch.dict(self.info, {"Id": "f" * 64}),
                    patch.object(box, "podman", **options),
                    self.assertRaises(protocol.OwnerLeaseError) as refusal,
                ):
                    with self.lease(owner):
                        self.fail("unexpected lease for another container")
                self.assertNotIn(_PRIVATE, str(refusal.exception))
                self.assertNotIn(_SECRET, str(refusal.exception))
                self.denied(owner)

    def test_stop_intent_retires_held_stream_without_releasing_owner_lock(self):
        with self.owner() as owner, self.lease(owner) as lease:
            workspace_control.request_stop(self.path)
            self.assertIsNone(lease.observe())
            with owner.condition:
                self.assertTrue(
                    owner.condition.wait_for(lambda: not owner.clients, timeout=1)
                )
            with self.assertRaises(box.BoxError):
                with box.workspace_lock(self.path):
                    self.fail("stop intent must not release the owner lock")
            self.denied(owner)

    def test_native_worker_exit_in_the_same_container_retires_the_lease(self):
        with self.owner() as owner, self.lease(owner) as lease:
            owner.child.stdin.close()
            owner.child.wait(timeout=2)
            self.assertIsNone(lease.observe())
            with owner.condition:
                self.assertTrue(
                    owner.condition.wait_for(lambda: not owner.clients, timeout=1)
                )
            self.denied(owner)


class NamedNativeWorkerTests(unittest.TestCase):
    def test_only_named_startup_requires_bounded_actual_2_3_2_native_info(self):
        good = {"version": web_native_pair._VERSION, "pid": 42, "urls": [_ENDPOINT]}
        for update in (
            None,
            {},
            {"version": "2.3.3"},
            {"version": "2.3.2-pre.0"},
            {"urls": ["http://127.0.0.2:51303"]},
            {"pid": True},
        ):
            with self.subTest(update=update):
                is_named = update is not None
                request = Mock(return_value=good | (update or {}))
                request.endpoint = {"url": _ENDPOINT, "password": _SECRET}
                request.alive.side_effect = [True, False] if is_named else [False]
                with (
                    patch.object(
                        worker, "server", return_value=contextlib.nullcontext(request)
                    ),
                    patch.object(worker, "ready_plugins"),
                    patch.object(worker, "require_control_pipe", return_value=17),
                    patch.object(worker, "write_control_record") as write,
                ):
                    argv = [
                        "named-lease" if is_named else "lease",
                        "/synthetic/opencode",
                    ]
                    if not update:
                        worker.main(argv)
                        write.assert_called_once_with(17, request.endpoint)
                    else:
                        with self.assertRaises(worker.FixedError):
                            worker.main(argv)
                        write.assert_not_called()
                    if is_named:
                        self.assertEqual(request.call_args.args, ("GET", "/api/info"))
                        self.assertEqual(request.call_args.kwargs["max_body"], 8192)
                        self.assertLessEqual(
                            request.call_args.kwargs["deadline"] - time.monotonic(), 2
                        )
                    else:
                        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
