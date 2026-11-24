"""Original managed Gateway on synthetic registration, TCP and exec pipes."""

from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import replace
import os
import socket
import sys
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from weakref import ref

from oc_box import web_guest_duplex_exec as guest_exec
from oc_box import web_guest_duplex_host as transport
from oc_box import web_guest_worker_drain as worker_drain
from oc_box import web_studio_guest
from oc_box import web_studio_guest_binding as binding
from oc_box import web_studio_guest_gateway as gateway
from oc_box import web_studio_guest_owner as guest_owner
from oc_box import workspace_control, workspace_service as service
from test_web_studio_guest_owner import GuestOwnerFixture


_UPGRADE_REQUEST = (
    b"GET /native/socket HTTP/1.1\r\nHost: synthetic\r\n"
    b"Origin: http://synthetic\r\nCookie: native=synthetic\r\n"
    b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
)
_UPGRADE = (
    b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
    b"Upgrade: websocket\r\n\r\n"
)


class ManagedGatewayTests(GuestOwnerFixture, unittest.TestCase):
    def start_gateway(self, stack, *, proof=None, studio=False):
        if proof is None:
            proof = self.seal(stack)
        relay = gateway._ManagedGateway(proof, studio=studio)
        stack.callback(relay.drain)
        self.assertTrue(relay.start())
        return relay

    def client(self, stack, relay):
        return stack.enter_context(
            socket.create_connection(relay.binding.listener._address, timeout=5)
        )

    def read_exact(self, stream, length):
        result = bytearray()
        while len(result) < length:
            part = stream.recv(length - len(result))
            self.assertTrue(part, "unexpected synthetic peer EOF")
            result.extend(part)
        return bytes(result)

    def test_opt_in_serves_only_pinned_first_party_assets_and_ends_the_exchange(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack, studio=True)
            authority = relay.binding.native.binding.authority
            start = stack.enter_context(patch.object(guest_exec, "_command"))

            def read(target, *, site, mode, dest, extra=b""):
                with socket.create_connection(
                    relay.binding.listener._address, timeout=5
                ) as peer:
                    wire = (
                        f"GET {target} HTTP/1.1\r\nHost: {authority}\r\n"
                        f"Sec-Fetch-Site: {site}\r\nSec-Fetch-Mode: {mode}\r\n"
                        f"Sec-Fetch-Dest: {dest}\r\n"
                    ).encode("ascii")
                    peer.sendall(wire + extra + b"\r\n")
                    output = bytearray()
                    while chunk := peer.recv(4096):
                        output.extend(chunk)
                        self.assertLess(len(output), 65536)
                    return bytes(output)

            page = read(
                web_studio_guest.PAGE_PATH,
                site="none",
                mode="navigate",
                dest="document",
                extra=b"Sec-Fetch-User: ?1\r\n",
            )
            self.assertTrue(page.startswith(b"HTTP/1.1 200 OK\r\n"))
            self.assertIn(b"Connection: close\r\n", page)
            self.assertIn(b"Content-Security-Policy: ", page)
            self.assertIn(b"data-studio-challenge=", page)
            self.assertNotIn(b"<iframe", page)
            asset = read(
                "/oc-box/studio/web_named_studio_entry.mjs",
                site="same-origin",
                mode="cors",
                dest="script",
            )
            self.assertTrue(asset.startswith(b"HTTP/1.1 200 OK\r\n"))
            self.assertNotIn(b"data-studio-challenge=", asset)
            for path, site, mode, dest in (
                (web_studio_guest.PAGE_PATH, "cross-site", "navigate", "document"),
                ("/oc-box/studio/not-pinned.js", "same-origin", "cors", "script"),
                (
                    "/oc-box/studio/web_studio_shell.css",
                    "same-origin",
                    "cors",
                    "script",
                ),
                ("/oc-box/studio/catalog", "same-origin", "same-origin", "empty"),
                (" /oc-box/studio/catalog", "same-origin", "same-origin", "empty"),
                (
                    f"http://{authority}/oc-box/studio/catalog",
                    "same-origin",
                    "same-origin",
                    "empty",
                ),
            ):
                with self.subTest(path=path):
                    self.assertEqual(read(path, site=site, mode=mode, dest=dest), b"")
            self.assertEqual(
                read(
                    "/",
                    site="same-origin",
                    mode="navigate",
                    dest="document",
                    extra=b"X-OC-Box-Studio-App: fixture\r\n",
                ),
                b"",
            )
            start.assert_not_called()
            self.assertTrue(relay.drain())
            self.assertFalse(self.owner.closing)

    def backend(self, stack, *, nonzero=False):
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(5)
        port = listener.getsockname()[1]
        proof = self.seal(stack, endpoint=f"http://127.0.0.1:{port}")
        original_command = guest_exec._command

        def direct_exec(scope):
            command = original_command(scope)
            self.assertIs(scope.owner, proof)
            self.assertEqual(
                command[:10],
                [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    "/",
                    scope.identity.container_id,
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "/usr/bin/python3",
                ],
            )
            self.assertEqual(command[-1], str(port))
            args = [sys.executable, *command[10:]]
            if nonzero:
                # The exact worker performs the wire exchange, followed by a
                # synthetic nonzero terminal status after a valid receipt.
                args[-2] = (
                    "try:\n    exec(" + repr(args[-2]) + ")\n"
                    "except SystemExit:\n    raise SystemExit(7)\n"
                )
            return self.owner.box.ContainerCommand(args, {})

        launch = stack.enter_context(
            patch.object(guest_exec, "_command", side_effect=direct_exec)
        )
        return listener, self.start_gateway(stack, proof=proof), launch

    def test_exact_binding_is_not_a_probe_or_substitutable_dispatch_authority(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            scope = relay.binding
            self.assertIs(type(scope), binding._ManagedBinding)
            self.assertIs(scope.owner, self.owner._guest_proof)
            self.assertIs(scope.identity, scope.owner.current())
            self.assertIs(scope.current(), scope.native.binding)
            self.assertEqual(relay.url, scope.native.binding.origin)
            self.assertIsNone(replace(scope).current())
            self.assertIs(scope.current(), scope.native.binding)
            self.assertTrue(binding._accepts_worker_registry(scope, relay._workers))
            self.assertFalse(
                binding._accepts_worker_registry(replace(scope), relay._workers)
            )
            self.assertFalse(
                binding._accepts_worker_registry(scope, worker_drain.AcceptedWorkers())
            )
            with self.assertRaises(binding.GuestBindingError):
                gateway._ManagedGateway(SimpleNamespace(owner=self.owner))
            with self.assertRaises(binding.GuestBindingError):
                binding.bind_unserved(scope.gateway, scope.listener, scope.owner)
            with self.assertRaises(service._GatewayListenerError):
                scope.gateway._start_listener(scope.listener)
            left, right = socket.socketpair()
            with left, right:
                use = worker_drain.AcceptedWorker(left)
                with self.assertRaises(guest_exec.GuestExecError):
                    guest_exec.GuestDuplexExec(
                        scope, worker_drain.AcceptedWorkers(), use
                    )
                with self.assertRaises(guest_exec.GuestExecError):
                    guest_exec.GuestDuplexExec(replace(scope), relay._workers, use)
            other = gateway._ManagedGateway(scope.owner)
            self.assertFalse(other.start())
            self.assertFalse(other.drain())
            original_lease = service.LIFECYCLE_LOCK.get()
            self.assertTrue(relay.drain())
            self.assertIs(service.LIFECYCLE_LOCK.get(), original_lease)
            self.assertIs(scope.owner.current(), scope.identity)
            self.assertFalse(relay.start())
            self.assertIsNone(relay.binding)
            self.assertIsNone(scope.current())
            self.assertEqual(scope.listener._listener.fileno(), -1)

    def test_worker_record_retains_an_attempt_whose_exec_start_is_uncertain(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            scope = relay.binding
            left, right = socket.socketpair()
            stack.enter_context(right)
            result = Future()

            def work(use):
                try:
                    launch = guest_exec.GuestDuplexExec(scope, relay._workers, use)
                    with patch.object(
                        guest_exec.subprocess, "Popen", side_effect=OSError
                    ):
                        with self.assertRaises(guest_exec.GuestExecError):
                            launch.start(deadline=monotonic() + 4)
                    self.assertFalse(launch.retire())
                    result.set_result(ref(launch))
                except BaseException as error:
                    result.set_exception(error)

            if not relay._workers.dispatch(left, work):
                left.close()
                self.fail("Synthetic worker was not registered.")
            reference = result.result(timeout=5)
            self.assertIs(relay.drain_review(), worker_drain.DrainReview.UNCONFIRMED)
            self.assertIsNotNone(
                reference(), "uncertain exec lost its exact tracked handle"
            )
            self.assertFalse(self.owner.closing)

    def test_silent_workers_fill_capacity_before_native_dispatch_and_all_join(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            all_charged = Event()
            seen = set()
            head = relay._head

            def observe_head(use):
                with relay._workers._lock:
                    if use not in relay._workers._workers:
                        return None
                    seen.add(use)
                    if len(seen) == 32:
                        all_charged.set()
                return head(use)

            stack.enter_context(patch.object(relay, "_head", side_effect=observe_head))
            launch = stack.enter_context(patch.object(guest_exec, "_command"))
            clients = [self.client(stack, relay) for _ in range(32)]
            self.assertTrue(all_charged.wait(5))
            extra = self.client(stack, relay)
            self.assertEqual(extra.recv(1), b"")
            self.assertTrue(relay.drain())
            launch.assert_not_called()
            self.assertFalse(relay._acceptor.is_alive())
            self.assertTrue(all(use.stream.fileno() < 0 for use in seen))
            self.assertTrue(all(client.recv(1) == b"" for client in clients))

    def test_accept_in_flight_cannot_be_ignored_and_same_drain_can_recheck(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            pending, resume = Event(), Event()
            dispatch = relay._dispatch

            def hold(stream):
                pending.set()
                if not resume.wait(5):
                    raise TimeoutError
                return dispatch(stream)

            stack.enter_context(patch.object(relay, "_dispatch", side_effect=hold))
            launch = stack.enter_context(patch.object(guest_exec, "_command"))
            peer = self.client(stack, relay)
            retained = gateway._RetainedRunResult(relay)
            with self.assertRaises(binding.GuestBindingError):
                gateway._RetainedRunResult(relay)
            with self.assertRaises(binding.GuestBindingError):
                retained.capture(True)
            retained.capture(-15)
            try:
                self.assertTrue(pending.wait(5))
                original_accept = relay._pending_accept
                exact_lock = service.LIFECYCLE_LOCK.get()
                self.assertIs(
                    retained.observe(join_timeout=0.01),
                    worker_drain.DrainReview.WAITING,
                )
                self.assertTrue(relay._acceptor.is_alive())
                self.assertIs(relay._pending_accept, original_accept)
                self.assertIs(service.LIFECYCLE_LOCK.get(), exact_lock)
                self.assertFalse(self.owner.closing)
            finally:
                resume.set()
            self.assertIs(retained.observe(), worker_drain.DrainReview.VERIFIED)
            self.assertEqual(retained.raw, -15)
            with self.assertRaises(binding.GuestBindingError):
                retained.capture(0)
            self.assertEqual(original_accept.fileno(), -1)
            self.assertEqual(peer.recv(1), b"")
            launch.assert_not_called()

    def test_retained_drain_interruption_is_not_a_retry_or_a_client_success(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            retained = gateway._RetainedRunResult(relay)
            with self.assertRaises(binding.GuestBindingError):
                retained.observe()
            retained.capture(None)
            exact = service.LIFECYCLE_LOCK.get()
            with patch.object(
                relay, "drain_review", side_effect=KeyboardInterrupt
            ) as drain:
                self.assertIs(retained.observe(), worker_drain.DrainReview.UNCONFIRMED)
                self.assertIs(retained.observe(), worker_drain.DrainReview.UNCONFIRMED)
                drain.assert_called_once_with(join_timeout=5.0)
            self.assertIsNone(retained.raw)
            self.assertIs(service.LIFECYCLE_LOCK.get(), exact)
            self.assertFalse(self.owner.closing)

    def test_unconfirmed_acceptor_still_joins_its_registered_worker(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            entered = Event()
            original = relay._head

            def wait_for_head(use):
                entered.set()
                return original(use)

            stack.enter_context(patch.object(relay, "_head", side_effect=wait_for_head))
            peer = self.client(stack, relay)
            self.assertTrue(entered.wait(5))
            # An independently lost acceptor receipt stays unconfirmed. It
            # must not suppress the bounded join of already-accounted work.
            relay._failed.set()
            self.assertIs(relay.drain_review(), worker_drain.DrainReview.UNCONFIRMED)
            self.assertFalse(relay._workers._workers)
            self.assertEqual(peer.recv(1), b"")
            self.assertFalse(self.owner.closing)

    def test_ambiguous_acceptor_start_never_dispatches_or_reports_drain(self):
        with ExitStack() as stack:
            owner_proof = self.seal(stack)
            relay = gateway._ManagedGateway(owner_proof)
            start = Thread.start

            def started_then_failed(thread):
                start(thread)
                raise RuntimeError("synthetic startup uncertainty")

            with patch.object(Thread, "start", started_then_failed):
                self.assertFalse(relay.start())
            self.assertIsNone(relay.url)
            relay._acceptor.join(timeout=5)
            self.assertFalse(relay._acceptor.is_alive())
            self.assertFalse(relay._accept_finished)
            self.assertTrue(relay._listener_closed)
            self.assertFalse(relay.drain())
            self.assertFalse(relay.start())
            self.assertIs(owner_proof.current(), relay._binding.identity)

    def test_raw_native_pairing_bytes_and_idle_upgrade_retire_exact_pipes(self):
        with ExitStack() as stack:
            listener, relay, launch = self.backend(stack)
            legacy = stack.enter_context(patch.object(service.Gateway, "forward"))
            shutdown = stack.enter_context(patch.object(self.owner, "shutdown"))
            pair = self.client(stack, relay)
            head = b"GET /auth/connect/synthetic HTTP/1.1\r\nHost: synthetic\r\n"
            end = b"Content-Length: 2\r\n\r\n\x00\xff"
            pair.sendall(head[:20])
            pair.sendall(head[20:] + end)
            peer = stack.enter_context(listener.accept()[0])
            peer.settimeout(5)
            self.assertEqual(self.read_exact(peer, len(head + end)), head + end)
            reply = (
                b"HTTP/1.1 302 Found\r\nLocation: /\r\n"
                b"Set-Cookie: native=synthetic; HttpOnly\r\n"
                b"Set-Cookie: another=synthetic\r\nContent-Length: 0\r\n"
            )
            peer.sendall(reply + b"Connection: keep-alive\r\n\r\n")
            peer.shutdown(socket.SHUT_WR)
            expected = reply + b"Connection: close\r\n\r\n"
            self.assertEqual(self.read_exact(pair, len(expected)), expected)
            self.assertEqual(pair.recv(1), b"")

            browser = self.client(stack, relay)
            browser.sendall(_UPGRADE_REQUEST)
            native = stack.enter_context(listener.accept()[0])
            native.settimeout(5)
            self.assertEqual(
                self.read_exact(native, len(_UPGRADE_REQUEST)), _UPGRADE_REQUEST
            )
            native.sendall(_UPGRADE + b"\x82\x02\x00\xff")
            self.assertEqual(
                self.read_exact(browser, len(_UPGRADE) + 4),
                _UPGRADE + b"\x82\x02\x00\xff",
            )
            browser.sendall(b"\x82\x02\xff\x00")
            self.assertEqual(self.read_exact(native, 4), b"\x82\x02\xff\x00")
            with relay._relays_lock:
                (active,) = relay._relays
            self.assertTrue(relay.drain())
            self.assertEqual(native.recv(1), b"")
            self.assertEqual(browser.recv(1), b"")
            self.assertTrue(active._tunnel._receipt and active._tunnel._eof)
            self.assertEqual(active._child.returncode, 0)
            self.assertEqual(launch.call_count, 2)
            legacy.assert_not_called()
            shutdown.assert_not_called()

    def test_nonzero_exec_retirement_retains_original_without_force_or_retry(self):
        with ExitStack() as stack:
            listener, relay, launch = self.backend(stack, nonzero=True)
            abandon = stack.enter_context(patch.object(transport.DuplexHost, "abandon"))
            force = stack.enter_context(
                patch.object(transport.signal, "pidfd_send_signal")
            )
            browser = self.client(stack, relay)
            browser.sendall(_UPGRADE_REQUEST)
            native = stack.enter_context(listener.accept()[0])
            native.settimeout(5)
            self.assertEqual(
                self.read_exact(native, len(_UPGRADE_REQUEST)), _UPGRADE_REQUEST
            )
            native.sendall(_UPGRADE)
            self.assertEqual(self.read_exact(browser, len(_UPGRADE)), _UPGRADE)
            with relay._relays_lock:
                (original,) = relay._relays
            child, tunnel, proof = (
                original._child,
                original._tunnel,
                relay.binding.owner,
            )
            try:
                self.assertFalse(relay.drain())
                self.assertFalse(relay.drain())
                self.assertFalse(relay.start())
                self.assertIn(original, relay._relays)
                self.assertIs(original._tunnel, tunnel)
                self.assertIs(original._child, child)
                self.assertIs(proof.current(), relay._binding.identity)
                self.assertFalse(self.owner.closing)
                self.assertEqual(child.returncode, 7)
                self.assertTrue(tunnel._receipt and tunnel._eof)
                self.assertEqual(native.recv(1), b"")
                self.assertEqual(browser.recv(1), b"")
                abandon.assert_not_called()
                force.assert_not_called()
                launch.assert_called_once()
            finally:
                # The synthetic worker is already reaped. Dispose only its
                # leftover local fixture descriptors, without signals.
                if tunnel._local_reaped and tunnel._pidfd is not None:
                    os.close(tunnel._pidfd)
                    tunnel._pidfd = None
                if tunnel._local_reaped and not tunnel._output_closed:
                    os.close(tunnel._rx_pin[0])
                    tunnel._output_closed = True

    def test_lost_stop_baseline_fences_idle_ingress_and_never_releases_owner(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(gateway, "_OBSERVE_INTERVAL", 0.01))
            relay = self.start_gateway(stack)
            original_child = self.owner.child
            original_scope = relay.binding
            workspace_control.request_stop(self.path)
            relay._acceptor.join(timeout=5)
            self.assertFalse(relay._acceptor.is_alive())
            self.assertTrue(relay._listener_closed)
            self.assertFalse(relay.drain())
            self.assertIs(self.owner.child, original_child)
            self.assertIs(relay._binding, original_scope)
            self.assertIsNone(original_scope.current())
            self.assertFalse(self.owner.closing)

    def test_local_drain_is_not_positive_without_the_exact_owning_lease(self):
        with ExitStack() as stack:
            relay = self.start_gateway(stack)
            scope = relay.binding
            self.assertIs(scope.current(), scope.native.binding)
            # Local drain is verified first. A different container run at
            # the same immutable ID must still invalidate the next verdict.
            self.assertTrue(relay.drain())
            with patch.object(
                guest_owner.web_named_protocol,
                "_running_instance",
                return_value=(scope.identity.container_id, "2026-02-01T04:06:06Z"),
            ) as inspect:
                self.assertFalse(relay.drain())
                inspect.assert_called_once()
            self.assertTrue(relay._accept_finished and relay._listener_closed)
            self.assertFalse(relay.drain())
            self.assertIs(relay._binding, scope)
            self.assertFalse(self.owner.closing)


if __name__ == "__main__":
    unittest.main()
