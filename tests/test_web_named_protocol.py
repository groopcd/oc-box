"""Real local control UDS and lifecycle lock, synthetic container/native I/O."""

import contextlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_web_native_pair import native_info
import test_web_pake_flow as pake_fixtures
import test_web_local_origins as certificate_fixtures
import test_web_named_account_assets as artifact_fixtures

from oc_box import (
    oc_box as box,
    opencode_v2_io as worker,
    ui_state,
    web_account_ingress,
    web_accounts,
    web_app_ingress,
    web_local_origins,
    web_named_bootstrap,
    web_named_account_assets,
    web_named_foreground,
    web_named_handoff,
    web_named_headers,
    web_named_http,
    web_named_listeners,
    web_named_owner,
    web_named_protocol as protocol,
    web_named_services,
    web_named_site_lifecycle,
    web_named_transport,
    web_native_pair,
    web_pake_flow,
    web_pake_profile,
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
        self.assertIn(arguments[3], ("lease", "named-lease"))
        expected = (
            _CONTAINER if arguments[3] == "named-lease" else self.data["container"]
        )
        self.assertEqual(
            arguments[5:],
            [data["opencode_version"]] if arguments[3] == "named-lease" else [],
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

    def named_profile(self):
        origins = web_local_origins.LocalOrigins()
        tls = web_local_origins.LocalTLS(
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
        )
        account, workspace = Mock(), Mock()
        for peer, address in (
            (account, origins.account_bind),
            (workspace, origins.workspace_bind),
        ):
            peer.getsockname.return_value = address
            peer.getsockopt.return_value = 0
        return web_named_listeners.ReservedProfile(origins, tls, account, workspace)

    @contextlib.contextmanager
    def foreground_inputs(self):
        certs = certificate_fixtures.LocalTLSFileTests()
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(certs.doCleanups)
            certs.setUp()
            with socket.socket() as first, socket.socket() as second:
                for stream in (first, second):
                    stream.bind(("127.0.0.1", 0))
                ports = tuple(stream.getsockname()[1] for stream in (first, second))
            original = web_local_origins.LocalOrigins.__init__
            cleanup.enter_context(
                patch.object(
                    web_local_origins.LocalOrigins,
                    "__init__",
                    lambda origins, *args: original(origins, *(args or ports)),
                )
            )
            cleanup.enter_context(
                patch.object(web_named_listeners, "_host_allowed", return_value=True)
            )
            profile = cleanup.enter_context(
                web_named_listeners.reserve_profile(
                    account_cert=certs.cert,
                    account_key=certs.key,
                    workspace_cert=certs.app_cert,
                    workspace_key=certs.app_key,
                )
            )
            wasm = self.root / "browser.wasm"
            verifier = self.root / "verifier"
            wasm.write_bytes(artifact_fixtures.WASM)
            verifier.write_bytes(artifact_fixtures.VERIFIER)
            assets = cleanup.enter_context(
                web_named_account_assets.prepare_account_assets(
                    origins=profile.origins,
                    wasm_file=wasm,
                    wasm_pin=artifact_fixtures.pin(artifact_fixtures.WASM),
                    verifier_file=verifier,
                    verifier_pin=artifact_fixtures.pin(artifact_fixtures.VERIFIER),
                )
            )
            path = self.root / "accounts"
            phrase = "synthetic named account store passphrase"
            web_accounts.initialize(path, phrase, setup=b"s" * 128)
            account = cleanup.enter_context(web_accounts.load_snapshot(path, phrase))
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.load_verify_locations(cafile=certs.app_cert)
            yield profile, account, assets, (path, phrase), client

    @unittest.skipUnless(
        certificate_fixtures.OPENSSL and find_spec("cryptography"),
        "requires openssl and optional account-store crypto",
    )
    def test_foreground_sites_fence_replaced_account_and_hold_the_actual_attach_on_false_drain(
        self,
    ):
        backend = Mock(sizes=web_pake_flow.MessageSizes(96, 320, 64))
        backend.close.return_value = True
        with (
            self.foreground_inputs() as (profile, account, assets, store, client),
            self.owner() as owner,
            patch.object(web_pake_profile, "LocalPakeProfile", return_value=backend),
        ):
            arguments = {"profile": profile, "account": account, "assets": assets}
            expired = web_named_foreground.NamedForeground(owner, **arguments)
            with self.assertRaises(web_named_foreground.ForegroundError):
                expired.start(expires_at=time.monotonic() - 1)
            self.assertTrue(expired.close())
            self.assertEqual(owner.clients, 0)

            foreground = web_named_foreground.NamedForeground(owner, **arguments)
            try:
                entry_url = foreground.start(expires_at=time.monotonic() + 15)
                site = profile.origins.workspace
                entry = web_named_bootstrap.WORKSPACE_ENTRY
                self.assertEqual(entry_url, site.value + entry)
                self.assertEqual(owner.clients, 1)
                with (
                    socket.create_connection(
                        profile.origins.workspace_bind, timeout=2
                    ) as raw,
                    client.wrap_socket(raw, server_hostname=site.hostname) as peer,
                ):
                    peer.sendall(
                        (
                            f"GET {entry} HTTP/1.1\r\n"
                            f"Host: {site.value.removeprefix('https://')}\r\n"
                            "Sec-Fetch-Site: none\r\nSec-Fetch-Mode: navigate\r\n"
                            "Sec-Fetch-Dest: document\r\n\r\n"
                        ).encode("ascii")
                    )
                    with peer.makefile("rb") as incoming:
                        response = incoming.read(16 * 1024)
                self.assertTrue(response.startswith(b"HTTP/1.1 200"))
                self.assertIn(b"Continue to sign in", response)
                self.assertNotIn(_SECRET.encode("ascii"), response)

                web_accounts.add_account(
                    *store,
                    "updated-synthetic",
                    workspace_ids=(self.data["id"],),
                    registration=b"r" * 192,
                )
                self.assertTrue(foreground.wait_stopped(timeout=3))
                self.assertIs(
                    foreground.stop_reason,
                    web_named_site_lifecycle.StopReason.OWNER_OR_ACCOUNT,
                )
                self.assertFalse(account.current())
                backend.close.return_value = False
                self.assertFalse(foreground.close())
                self.assertEqual(owner.clients, 1)
                with self.assertRaises(web_named_foreground.ForegroundError):
                    foreground.start(expires_at=time.monotonic() + 15)
            finally:
                backend.close.return_value = True
                self.assertTrue(foreground.close())
            with owner.condition:
                self.assertTrue(
                    owner.condition.wait_for(lambda: not owner.clients, timeout=1)
                )
            self.assertFalse(owner.closing)

    def test_prepared_named_services_hold_real_attach_and_invalidate_a_stale_account_scope(
        self,
    ):
        profile = self.named_profile()
        approval = Mock(return_value=True)
        store = web_accounts.AccountStore(
            b"s" * 128,
            (
                web_accounts.Account(
                    "synthetic-account", (self.data["id"],), b"r" * 192
                ),
            ),
        )
        backend = Mock(sizes=web_pake_flow.MessageSizes(96, 320, 64))
        backend.close.return_value = False
        with (
            self.owner() as owner,
            self.lease(owner) as lease,
            patch.object(web_pake_profile, "LocalPakeProfile", return_value=backend),
        ):
            services = web_named_services.prepare_services(
                box,
                profile=profile,
                lease=lease,
                store=store,
                account_current=approval,
                verifier_fd=7,
                expected_sha256="a" * 64,
            )
            headers = [
                ("Host", profile.origins.workspace.value.removeprefix("https://")),
                ("Sec-Fetch-Site", "none"),
                ("Sec-Fetch-Mode", "navigate"),
                ("Sec-Fetch-Dest", "document"),
            ]
            request = {
                "method": "GET",
                "target": web_named_bootstrap.WORKSPACE_ENTRY,
                "headers": headers,
                "body": b"",
                "actual_tls": True,
            }
            self.assertIn(
                b"Continue to sign in", services.entry.workspace(**request).html
            )
            self.assertEqual(owner.clients, 1)
            self.assertEqual(
                services.application.sign_in._binding.owner_generation,
                lease.identity.generation,
            )
            self.assertGreaterEqual(lease.control.fileno(), 0)
            self.assertNotIn(_SECRET, repr(services))

            approval.return_value = False
            with self.assertRaises(web_named_headers.NamedRequestError):
                services.entry.workspace(**request)
            approval.return_value = True
            with self.assertRaises(web_named_headers.NamedRequestError):
                services.entry.workspace(**request)

            self.assertFalse(services.close())
            self.assertGreaterEqual(lease.control.fileno(), 0)
            backend.close.return_value = True
            self.assertTrue(services.close())
            self.assertGreaterEqual(lease.control.fileno(), 0)
            self.connector.assert_not_called()
        for peer in (profile.account, profile.workspace):
            peer.listen.assert_not_called()
            peer.close.assert_not_called()

    def test_named_services_reject_listening_or_mismatched_store_before_backend_start(
        self,
    ):
        profile = self.named_profile()
        approval = Mock(return_value=True)
        store = web_accounts.AccountStore(b"s" * 128, ())
        with (
            self.owner() as owner,
            self.lease(owner) as lease,
            patch.object(web_pake_profile, "LocalPakeProfile") as backend,
        ):
            for listening, captured in (
                (1, store),
                (0, web_accounts.AccountStore(b"wrong-size", ())),
            ):
                with self.subTest(listening=listening):
                    profile.workspace.getsockopt.return_value = listening
                    with self.assertRaises(web_named_services.ServicePreparationError):
                        web_named_services.prepare_services(
                            box,
                            profile=profile,
                            lease=lease,
                            store=captured,
                            account_current=approval,
                            verifier_fd=7,
                            expected_sha256="a" * 64,
                        )
            backend.assert_not_called()
            approval.assert_not_called()

    def test_first_party_dispatch_keeps_native_cookie_off_wire_and_refuses_confirm_replay(
        self,
    ):
        profile = self.named_profile()
        reader = web_named_transport.NamedHTTPSReader.from_reservation(profile)
        store = web_accounts.AccountStore(
            b"s" * 128,
            (
                web_accounts.Account(
                    "synthetic-account", (self.data["id"],), b"r" * 192
                ),
            ),
        )
        backend = pake_fixtures.SyntheticBackend()
        backend.sizes = pake_fixtures.SIZES
        backend.close = lambda: True
        native_cookie = "K" * 10 + "." + "s" * 43
        with (
            self.owner() as owner,
            self.lease(owner) as lease,
            patch.object(web_pake_profile, "LocalPakeProfile", return_value=backend),
            patch.object(
                web_native_pair, "_read_bridge", return_value=native_cookie
            ) as mint,
        ):
            services = web_named_services.prepare_services(
                box,
                profile=profile,
                lease=lease,
                store=store,
                account_current=lambda: True,
                verifier_fd=7,
                expected_sha256="a" * 64,
            )
            http = web_named_http.NamedHTTPApplication(
                reader=reader,
                services=services,
                login_document=b"<h1>Local sign in</h1>",
            )

            def dispatch(role, path, body=b"", *, cookie, content_type=None):
                local = getattr(profile.origins, role).value
                headers = (
                    ("Host", local.removeprefix("https://")),
                    ("Origin", local),
                    ("Content-Length", str(len(body))),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Sec-Fetch-Mode", "same-origin"),
                    ("Sec-Fetch-Dest", "empty"),
                    ("Cookie", cookie),
                )
                if content_type:
                    headers += (("Content-Type", content_type),)
                request = web_named_transport.NamedRequest(
                    role, "POST", path, headers, body
                )
                writer, peer = socket.socketpair()
                with writer, peer:
                    http._dispatch(writer, request, time.monotonic() + 3)
                    writer.shutdown(socket.SHUT_WR)
                    with peer.makefile("rb") as response:
                        return response.read(64 * 1024).split(b"\r\n\r\n", 1)

            seed = services._bootstrap.issue(services._flow._binding)
            account_nonce = services._bootstrap.accept(
                services._flow._binding,
                auth_origin=profile.origins.account.value,
                ticket=seed.value,
            )
            account_cookie = "__Host-ocbox-account=" + account_nonce
            start_head, start_payload = dispatch(
                "account",
                web_account_ingress.START_PATH,
                json.dumps(
                    {"username": "synthetic-account", "ke1": pake_fixtures.KE1}
                ).encode("ascii"),
                cookie=account_cookie,
                content_type="application/json",
            )
            self.assertTrue(start_head.startswith(b"HTTP/1.1 200"))
            challenge = json.loads(start_payload)["challenge_id"]
            _, finish_payload = dispatch(
                "account",
                web_account_ingress.FINISH_PATH,
                json.dumps(
                    {"challenge_id": challenge, "ke3": pake_fixtures.PROOF}
                ).encode("ascii"),
                cookie=account_cookie,
                content_type="application/json",
            )
            ticket = json.loads(finish_payload)["ticket"].encode("ascii")
            mint.assert_not_called()

            cookie_headers, content = dispatch(
                "workspace",
                web_named_handoff.CONFIRM_PATH,
                ticket,
                cookie="__Host-ocbox-bootstrap=" + seed.app_nonce,
                content_type="text/plain",
            )
            self.assertTrue(cookie_headers.startswith(b"HTTP/1.1 204"))
            self.assertNotIn(b"Content-Length", cookie_headers)
            self.assertEqual(content, b"")
            self.assertNotIn(native_cookie.encode("ascii"), cookie_headers)
            self.assertNotIn(ticket, cookie_headers)
            app_cookie = next(
                line.removeprefix(b"Set-Cookie: ").split(b";", 1)[0].decode("ascii")
                for line in cookie_headers.split(b"\r\n")
                if line.startswith(b"Set-Cookie: __Host-ocbox-app=")
            )
            mint.assert_called_once()

            replay, _ = dispatch(
                "workspace",
                web_named_handoff.CONFIRM_PATH,
                ticket,
                cookie="__Host-ocbox-bootstrap=" + seed.app_nonce,
                content_type="text/plain",
            )
            self.assertTrue(replay.startswith(b"HTTP/1.1 403"))
            self.assertNotIn(b"Set-Cookie", replay)
            mint.assert_called_once()
            logout, _ = dispatch(
                "workspace", web_app_ingress.LOGOUT_PATH, cookie=app_cookie
            )
            self.assertIn(b"Max-Age=0", logout)
            self.assertTrue(services.close())
            self.assertGreaterEqual(lease.control.fileno(), 0)

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
            received = []
            original = owner._named_control.lease
            read = protocol._read

            def create(attached, connector):
                captured.append(attached)
                return original(attached, connector)

            def receive(stream):
                frame = read(stream)
                if "started_at" in frame:
                    received.append(frame.copy())
                return frame

            with (
                patch.object(owner._named_control, "lease", side_effect=create),
                patch.object(protocol, "_read", side_effect=receive),
            ):
                with self.lease(owner) as lease:
                    self.assertIs(lease.control, captured[0].stream)
                    self.assertEqual(captured[0].connection["started_at"], _STARTED)
                    self.assertEqual(len(received), 1)
                    self.assertEqual(
                        received[0].keys(),
                        {
                            "protocol",
                            "nonce",
                            "workspace",
                            "generation",
                            "container",
                            "started_at",
                            "version",
                            "url",
                            "proof",
                        },
                    )
                    self.assertNotIn(_SECRET, json.dumps(received[0]))
                    self.assertEqual(lease.native_password, _SECRET)
                    self.assertEqual(lease.identity.container_id, _CONTAINER)
                    self.assertEqual(lease.identity.endpoint, _ENDPOINT)
                    self.assertEqual(
                        lease.identity.version, self.data["opencode_version"]
                    )
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
                self.assertEqual(legacy["password"], _SECRET)
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
    def test_only_named_startup_requires_bounded_exact_native_info(self):
        for expected, update in (
            (None, {}),
            ("2.3.2", {}),
            ("2.4.1", {}),
            ("2.3.2", {"version": "2.4.1"}),
            ("2.4.1", {"version": "2.4.2"}),
            ("2.3.2", {"version": "2.3.3"}),
            ("2.3.2", {"version": "2.3.2-pre.0"}),
            ("2.4.1", {"urls": ["http://127.0.0.2:51303"]}),
            ("2.3.2", {"pid": True}),
            ("2.4.1", {"capabilities": {"persistentPty": 1}}),
        ):
            with self.subTest(expected=expected, update=update):
                is_named = expected is not None
                request = Mock(return_value=native_info(_ENDPOINT, expected) | update)
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
                    if is_named:
                        argv.append(expected)
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

    def test_unaudited_or_missing_named_version_refuses_before_service_start(self):
        for extra in ([], ["2.4.1-dev"], ["2.4.2"], ["v2.4.1"], ["2.3.2", "2.4.1"]):
            with (
                self.subTest(extra=extra),
                patch.object(worker, "server") as start,
                self.assertRaises(worker.FixedError),
            ):
                worker.main(["named-lease", "/synthetic/opencode", *extra])
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
