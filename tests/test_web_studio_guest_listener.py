"""Kernel socket checks on transient loopback-only Gateway reservations."""

from contextlib import ExitStack
import os
import socket
from threading import Event
import unittest
from unittest.mock import patch

from oc_box import workspace_service as service


LOOPBACK = "127.0.0.1"


def gateway(port=0):
    # No project, native service, credentials or owner are needed to prove
    # the socket this Gateway actually reserves.
    return service.Gateway(None, None, None, port)


class GatewayListenerProofTests(unittest.TestCase):
    def test_private_probe_uses_actual_canonical_bound_port_without_listening(self):
        relay = gateway()
        with (
            patch.object(service, "preflight_port") as disposable,
            patch.object(service.Gateway, "serve") as serve,
            relay._listener_preflight() as proof,
        ):
            listener = relay.listener
            address = listener.getsockname()
            self.assertEqual(address[0], LOOPBACK)
            self.assertGreater(address[1], 0)
            self.assertEqual(
                proof.origin(relay, listening=False), f"http://{LOOPBACK}:{address[1]}"
            )
            self.assertIsNone(proof.origin(relay, listening=True))
            self.assertEqual(
                listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 0
            )
            self.assertFalse(listener.get_inheritable())
            with self.assertRaises(service._GatewayListenerError):
                relay._start_listener(proof)
            self.assertEqual(
                listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 0
            )
            disposable.assert_not_called()
            serve.assert_not_called()
        self.assertEqual(listener.fileno(), -1)
        self.assertIsNone(proof.origin(relay, listening=False))
        with self.assertRaises(service._GatewayListenerError):
            relay._reserve_listener()

    def test_actual_gateway_transitions_same_object_inode_and_address_before_serving(
        self,
    ):
        started = Event()
        observations = []
        relay = gateway()
        reserve = relay._reserve_listener

        def capture():
            proof = reserve()
            observations.append(
                (
                    proof,
                    relay.listener,
                    service._gateway_socket_identity(relay.listener),
                    proof.origin(relay, listening=False),
                )
            )
            return proof

        def observe_serve():
            proof = relay._listener_proof
            observations.append(
                (
                    proof,
                    relay.listener,
                    service._gateway_socket_identity(relay.listener),
                    proof.origin(relay, listening=True),
                )
            )
            started.set()

        with (
            patch.object(relay, "_reserve_listener", side_effect=capture),
            patch.object(relay, "serve", side_effect=observe_serve),
            relay as active,
        ):
            self.assertTrue(started.wait(3))
            self.assertEqual(len(observations), 2)
            self.assertIs(observations[0][0], observations[1][0])
            self.assertIs(observations[0][1], observations[1][1])
            self.assertEqual(observations[0][2:], observations[1][2:])
            self.assertEqual(active.url, observations[1][3])
            proof = observations[1][0]
            self.assertIsNone(proof.origin(active, listening=False))
            self.assertEqual(proof.origin(active, listening=True), active.url)
            active.url = "http://localhost:1"  # Presentation is not proof.
            self.assertEqual(proof.origin(active, listening=True), observations[1][3])
            self.assertEqual(active.listener.gettimeout(), 0.2)
            self.assertTrue(active.thread.daemon)
        self.assertIsNone(proof.origin(relay, listening=True))

    def test_duplicate_object_cannot_borrow_original_proof_even_for_the_same_inode(
        self,
    ):
        relay = gateway()
        with ExitStack() as stack:
            proof = relay._reserve_listener()
            original = relay.listener
            stack.callback(original.close)
            with self.assertRaises(service._GatewayListenerError):
                service._GatewayListenerProof(relay, probe_only=False)
            with original.dup() as duplicate:
                self.assertEqual(
                    os.fstat(original.fileno()).st_ino,
                    os.fstat(duplicate.fileno()).st_ino,
                )
                relay.listener = duplicate
                self.assertIsNone(proof.origin(relay, listening=False))
                relay.listener = original
            # An observed identity failure is latched. Restoring the original
            # object while the socket is still open does not revive it.
            self.assertIsNone(proof.origin(relay, listening=False))
            with self.assertRaises(service._GatewayListenerError):
                relay._start_listener(proof)

    def test_rebind_same_address_does_not_migrate_the_proof_to_another_gateway(self):
        relay = gateway()
        with relay._listener_preflight() as old:
            address = relay.listener.getsockname()
            origin = old.origin(relay, listening=False)
            inode = os.fstat(relay.listener.fileno()).st_ino
        replacement = gateway(address[1])
        with replacement._listener_preflight() as new:
            self.assertEqual(new.origin(replacement, listening=False), origin)
            self.assertNotEqual(os.fstat(replacement.listener.fileno()).st_ino, inode)
            self.assertIsNone(old.origin(replacement, listening=False))
            with self.assertRaises(service._GatewayListenerError):
                replacement._start_listener(old)
            self.assertEqual(new.origin(replacement, listening=False), origin)

    def test_open_socket_in_same_descriptor_and_python_object_is_not_the_original(self):
        relay = gateway()
        with socket.socket() as foreign, ExitStack() as stack:
            foreign.bind((LOOPBACK, 0))
            proof = relay._reserve_listener()
            listener = relay.listener
            fd = listener.fileno()

            def close_original_or_disown():
                proof._close_original()
                if listener.fileno() >= 0:
                    listener.detach()

            stack.callback(close_original_or_disown)
            identity = service._gateway_socket_identity(listener)
            # Keep the original kernel socket alive while replacing this fd.
            with listener.dup() as original:
                try:
                    os.dup2(foreign.fileno(), fd, inheritable=False)
                    self.assertIs(relay.listener, listener)
                    self.assertEqual(service._gateway_socket_identity(listener)[0], fd)
                    self.assertNotEqual(
                        service._gateway_socket_identity(listener), identity
                    )
                    self.assertIsNone(proof.origin(relay, listening=False))
                    with self.assertRaises(service._GatewayListenerError):
                        relay._start_listener(proof)
                    proof._close_original()
                    self.assertEqual(listener.getsockname(), foreign.getsockname())
                finally:
                    os.dup2(original.fileno(), fd, inheritable=False)
                self.assertIsNone(proof.origin(relay, listening=False))

    def test_out_of_band_listen_and_unsafe_option_changes_do_not_revive_proof(self):
        cases = (
            lambda peer: peer.listen(1),
            lambda peer: peer.set_inheritable(True),
            lambda peer: peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
            lambda peer: peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1),
        )
        for change in cases:
            with self.subTest(change=change), ExitStack() as stack:
                relay = gateway()
                proof = relay._reserve_listener()
                stack.callback(relay.listener.close)
                change(relay.listener)
                self.assertIsNone(proof.origin(relay, listening=False))
                self.assertIsNone(proof.origin(relay, listening=True))
                with self.assertRaises(service._GatewayListenerError):
                    relay._start_listener(proof)

    def test_foreign_nonloopback_or_nonreservation_sockets_cannot_supply_a_snapshot(
        self,
    ):
        with ExitStack() as stack:
            invalid = []
            for host in ("0.0.0.0", "127.0.0.2"):
                listener = stack.enter_context(socket.socket())
                listener.bind((host, 0))
                invalid.append(listener)
            if socket.has_ipv6:
                ipv6 = stack.enter_context(socket.socket(socket.AF_INET6))
                invalid.append(ipv6)
            udp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            udp.bind((LOOPBACK, 0))
            invalid.append(udp)
            invalid.append(stack.enter_context(socket.socket()))  # Unbound.
            server = stack.enter_context(socket.socket())
            server.bind((LOOPBACK, 0))
            server.listen(1)
            invalid.append(server)  # Already serving cannot be captured.
            client = stack.enter_context(
                socket.create_connection(server.getsockname(), timeout=3)
            )
            server.settimeout(3)
            accepted, _ = server.accept()
            invalid.extend((client, stack.enter_context(accepted)))
            for listener in invalid:
                with self.subTest(family=listener.family, kind=listener.type):
                    with self.assertRaises(service._GatewayListenerError):
                        service._gateway_listener_snapshot(listener, accepting=False)

    def test_failed_start_closes_original_socket_without_claiming_success(self):
        relay = gateway()
        proof = relay._reserve_listener()
        original = relay.listener
        with patch.object(service.threading.Thread, "start", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                relay._start_listener(proof)
        self.assertTrue(relay.closed.is_set())
        self.assertEqual(original.fileno(), -1)
        self.assertIsNone(proof.origin(relay, listening=True))

    def test_ordinary_bind_collision_still_surfaces_os_error(self):
        with socket.socket() as occupied:
            occupied.bind((LOOPBACK, 0))
            relay = gateway(occupied.getsockname()[1])
            with self.assertRaises(OSError):
                relay.__enter__()
            self.assertEqual(relay.listener.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
