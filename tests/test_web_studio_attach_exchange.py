"""Synthetic, already-counted AF_UNIX uses; no production attach or native I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import select
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    oc_box as box,
    studio_registry,
    web_named_stop_fence as fence,
    web_studio_attach_exchange as attach,
    workspace_identity,
)


_ID = "9" * 32
_ERROR = "Studio control attach could not be verified."
_R, _O, _A = (b"OCBSTA1\n" + tag for tag in (b"R", b"O", b"A"))
_N1, _N2, _N3 = (bytes((i,)) * 32 for i in (1, 2, 3))


class _CountedUse:
    """Stand-in for the missing trusted producer, not owner attestation."""

    def __init__(self, stream, generation):
        self.stream, self.generation = stream, generation
        self.use, self.owner, self.listener = object(), object(), object()
        self.counted, self.observations = True, 0

    def observe(self, use, *, deadline):
        self.observations += 1
        if self.counted and use is self.use:
            return self.stream, self.owner, self.listener, self.generation
        return None

    def exchange(self, role, witness, *, stream=None, observe=None):
        return role(
            witness,
            self.stream if stream is None else stream,
            use=self.use,
            owner=self.owner,
            listener=self.listener,
            observe=self.observe if observe is None else observe,
        )


def _read(stream, size):
    frame = bytearray()
    while len(frame) < size:
        part = stream.recv(size - len(frame))
        if not part:
            raise AssertionError("Synthetic control stream closed early")
        frame.extend(part)
    return bytes(frame)


def _owner_peer(scope, address, control):
    with (
        control,
        box.workspace_lock(scope.path),
        fence.arm(scope) as arm,
        socket.socket(socket.AF_UNIX) as listener,
    ):
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(4)
        witness = fence.observe_attach(scope)
        control.send("ready")
        stream, _ = listener.accept()
        with stream:
            row = _CountedUse(stream, witness.generation)
            exchange = row.exchange(attach.OwnerExchange, witness)
            with patch.object(socket.socket, "connect", side_effect=AssertionError):
                control.send("counted")
                exchange.run()
                control.send((os.getpid(), exchange.current(), row.counted))
                if not control.poll(4) or control.recv() != "extra input":
                    raise AssertionError("Synthetic owner was not released")
                # The exact counted socket is still held; late input poisons
                # only this exchange, which does not read or close the stream.
                readable, _, _ = select.select([stream], [], [], 2)
                if readable != [stream]:
                    raise AssertionError("Synthetic peer did not send extra input")
                control.send((exchange.current(), _read(stream, 1), exchange.current()))
                if not control.poll(4) or control.recv() != "release":
                    raise AssertionError("Synthetic owner was not released")
        # Fixture outcome only; there is no real application or container.
        arm.complete(drained=True, stopped=True)


class StudioAttachExchangeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="studio-attach-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home, state, project = (self.root / p for p in ("home", "state", "project"))
        for path in (home, state, project):
            path.mkdir(mode=0o700)
        ambient = patch.dict(os.environ, {"HOME": str(home)}, clear=True)
        ambient.start()
        self.addCleanup(ambient.stop)
        self.marker = project / studio_registry.MARKER_NAME
        self.marker.write_text(f"studio-v1:{_ID}\n")
        namespace = state / studio_registry.NAMESPACE
        namespace.mkdir(mode=0o700)
        folder = namespace / _ID
        folder.mkdir(mode=0o700)
        record = folder / studio_registry.RECORD_NAME
        value = {"version": 1, "role": "studio", "id": _ID, "project": str(project)}
        workspace_identity.record(value, *workspace_identity.snapshot(project))
        record.write_text(json.dumps(value))
        record.chmod(0o600)
        self.scope = fence.studio_scope(state, _ID)
        with box.workspace_lock(record):
            fence.enroll_new(self.scope)
        self.pair_count = 0

    @contextmanager
    def armed(self):
        with box.workspace_lock(self.scope.path), fence.arm(self.scope) as arm:
            yield arm, fence.observe_attach(self.scope)
            arm.complete(drained=True, stopped=True)

    @contextmanager
    def peers(self):
        self.pair_count += 1
        address = str(self.root / f"control-{self.pair_count}")
        with (
            socket.socket(socket.AF_UNIX) as listener,
            socket.socket(socket.AF_UNIX) as client,
        ):
            listener.bind(address)
            listener.listen(1)
            client.connect(address)
            server, _ = listener.accept()
            with server:
                yield client, server

    def refused(self, exchange, timeout=1.5):
        with self.assertRaises(attach.AttachExchangeError) as caught:
            exchange.run(timeout=timeout)
        self.assertEqual(str(caught.exception), _ERROR)
        self.assertFalse(exchange.current())

    def assert_no_reply(self, peer):
        with self.assertRaises(BlockingIOError):
            peer.recv(1, socket.MSG_DONTWAIT)

    def test_exact_cross_process_exchange_no_second_dial_or_release(self):
        ctx = multiprocessing.get_context("fork")
        address = str(self.root / "control-process")
        parent, child = ctx.Pipe()
        peer = ctx.Process(target=_owner_peer, args=(self.scope, address, child))
        with parent, child, socket.socket(socket.AF_UNIX) as stream:
            peer.start()
            try:
                self.assertTrue(parent.poll(4))
                self.assertEqual(parent.recv(), "ready")
                witness = fence.observe_attach(self.scope)
                stream.connect(address)
                self.assertTrue(parent.poll(4))
                self.assertEqual(parent.recv(), "counted")
                held = stream.fileno(), os.fstat(stream.fileno())
                row = _CountedUse(stream, witness.generation)
                exchange = row.exchange(attach.ClientExchange, witness)
                with patch.object(
                    socket.socket, "connect", side_effect=AssertionError
                ) as redial:
                    self.assertIsNone(exchange.run())
                    self.assertTrue(exchange.current())
                    self.assertTrue(parent.poll(4))
                    self.assertEqual(parent.recv(), (peer.pid, True, True))
                    self.assertNotEqual(peer.pid, os.getpid())
                    with self.assertRaises(attach.AttachExchangeError):
                        exchange.run()
                    self.assertTrue(exchange.current())
                    self.assertEqual(held, (stream.fileno(), os.fstat(stream.fileno())))
                    stream.sendall(b"x")
                    parent.send("extra input")
                    self.assertTrue(parent.poll(4))
                    self.assertEqual(parent.recv(), (False, b"x", False))
                    parent.send("release")
                    redial.assert_not_called()
                peer.join(4)
                self.assertEqual(peer.exitcode, 0)
                self.assertFalse(exchange.current())
            finally:
                if peer.is_alive():
                    peer.kill()
                    peer.join(3)
                peer.close()

    def test_replaced_arm_refuses_stale_witness_and_stale_request(self):
        with box.workspace_lock(self.scope.path):
            with fence.arm(self.scope) as original:
                stale = fence.observe_attach(self.scope)
                original.complete(drained=True, stopped=True)
            with fence.arm(self.scope) as successor:
                witness = fence.observe_attach(self.scope)
                for candidate in (stale, witness):
                    with (
                        self.subTest(stale_witness=candidate is stale),
                        self.peers() as (
                            client,
                            server,
                        ),
                    ):
                        row = _CountedUse(server, candidate.generation)
                        exchange = row.exchange(attach.OwnerExchange, candidate)
                        client.sendall(_R + stale.generation + _N1)
                        self.refused(exchange)
                        self.assert_no_reply(client)
                        self.assertEqual(row.observations, int(candidate is witness))
                successor.complete(drained=True, stopped=True)

    def test_duplicate_or_other_stream_cannot_spend_exact_accepted_use(self):
        with (
            self.armed() as (_, witness),
            self.peers() as (
                original_client,
                original_server,
            ),
            self.peers() as (other_client, other_server),
            original_server.dup() as alias,
        ):
            row = _CountedUse(original_server, witness.generation)
            frame = _R + witness.generation + _N1
            original_client.sendall(frame)
            other_client.sendall(frame)
            for wrong in (alias, other_server):
                with self.subTest(fd=wrong.fileno()):
                    self.refused(
                        row.exchange(attach.OwnerExchange, witness, stream=wrong)
                    )
            self.assert_no_reply(original_client)
            self.assert_no_reply(other_client)
            self.assertEqual(_read(original_server, len(frame)), frame)
            self.assertEqual(_read(other_server, len(frame)), frame)

    def test_callback_claim_is_not_stream_count_or_owner_listener_authority(self):
        with self.armed() as (_, witness), self.peers() as (client, server):
            row = _CountedUse(server, witness.generation)
            # A fresh fixture use for each case, with no reads, writes or dial
            # attempted by the module when the trusted observation is missing.
            for claim in (
                True,
                (server, object(), row.listener, witness.generation),
                (server, row.owner, object(), witness.generation),
                (server, row.owner, row.listener, _N3),
            ):
                with self.subTest(claim=claim):
                    different = _CountedUse(server, witness.generation)
                    different.owner, different.listener = row.owner, row.listener
                    self.refused(
                        different.exchange(
                            attach.OwnerExchange,
                            witness,
                            observe=Mock(return_value=claim),
                        )
                    )
                    self.assert_no_reply(client)
            # A callback exception must not leak its diagnostic, even via chaining.
            lost = _CountedUse(client, witness.generation)
            bad = Mock(side_effect=RuntimeError("private synthetic diagnostic"))
            self.refused(lost.exchange(attach.ClientExchange, witness, observe=bad))
            self.assert_no_reply(server)

    def test_owner_refuses_lost_evidence_between_offer_and_ack(self):
        for loss in ("count", "owner", "listener", "stop"):
            with (
                self.subTest(loss=loss),
                self.armed() as (
                    arm,
                    witness,
                ),
                self.peers() as (client, server),
            ):
                row = _CountedUse(server, witness.generation)
                exchange = row.exchange(attach.OwnerExchange, witness)
                direct = Mock(return_value=True)
                client.settimeout(2)

                def respond():
                    client.sendall(_R + witness.generation + _N1)
                    offer = _read(client, len(_O) + 96)
                    if loss == "count":
                        row.counted = False
                    elif loss in ("owner", "listener"):
                        setattr(row, loss, object())
                    else:
                        self.assertIs(
                            fence.route_stop(self.scope, direct_stop=direct),
                            fence.StopRoute.PENDING,
                        )
                    client.sendall(_A + offer[len(_O) :])

                with ThreadPoolExecutor(1) as pool:
                    reply = pool.submit(respond)
                    self.refused(exchange)
                    reply.result(timeout=3)
                self.assertEqual(row.counted, loss != "count")
                self.assertEqual(arm.stop_requested(), loss == "stop")
                direct.assert_not_called()
                row.counted = True
                self.refused(exchange)  # No retry of this attempted exchange.

    def test_extra_or_incomplete_request_refuses_without_offer(self):
        with self.armed() as (_, witness):
            for suffix in (_N1 + b"x", _N1[:-1]):
                with self.subTest(size=len(suffix)), self.peers() as (client, server):
                    row = _CountedUse(server, witness.generation)
                    client.sendall(_R + witness.generation + suffix)
                    self.refused(
                        row.exchange(attach.OwnerExchange, witness), timeout=0.1
                    )
                    self.assert_no_reply(client)

    def test_owner_rejects_replayed_or_extra_ack_after_a_fresh_offer(self):
        for case in ("old-offer-nonce", "extra"):
            with (
                self.subTest(case=case),
                self.armed() as (_, witness),
                self.peers() as (
                    client,
                    server,
                ),
            ):
                row = _CountedUse(server, witness.generation)
                client.settimeout(2)

                def respond():
                    # Fragment the legitimate request to exercise fixed read bounds.
                    client.sendall(_R[:4])
                    client.sendall(_R[4:] + witness.generation + _N1)
                    offer = _read(client, len(_O) + 96)
                    self.assertEqual(offer, _O + witness.generation + _N1 + _N2)
                    end = _N3 if case == "old-offer-nonce" else _N2 + b"x"
                    client.sendall(_A + witness.generation + _N1 + end)

                with (
                    ThreadPoolExecutor(1) as pool,
                    patch.object(attach, "_challenge", return_value=_N2),
                ):
                    reply = pool.submit(respond)
                    self.refused(row.exchange(attach.OwnerExchange, witness))
                    reply.result(timeout=3)

    def test_client_refuses_replayed_or_extra_offer_without_ack(self):
        for case in ("old-request-nonce", "wrong-generation", "extra"):
            with (
                self.subTest(case=case),
                self.armed() as (_, witness),
                self.peers() as (
                    client,
                    server,
                ),
            ):
                row = _CountedUse(client, witness.generation)
                server.settimeout(2)

                def respond():
                    self.assertEqual(
                        _read(server, len(_R) + 64), _R + witness.generation + _N1
                    )
                    generation = (
                        _N3 if case == "wrong-generation" else witness.generation
                    )
                    nonce = _N3 if case == "old-request-nonce" else _N1
                    suffix = b"x" if case == "extra" else b""
                    server.sendall(_O + generation + nonce + _N2 + suffix)

                with (
                    ThreadPoolExecutor(1) as pool,
                    patch.object(attach, "_challenge", return_value=_N1),
                ):
                    reply = pool.submit(respond)
                    self.refused(row.exchange(attach.ClientExchange, witness))
                    reply.result(timeout=3)
                server.setblocking(False)
                self.assert_no_reply(server)

    def test_socket_mode_and_producer_lifetime_are_not_changed(self):
        with self.armed() as (_, witness), self.peers() as (client, server):
            client.settimeout(1)
            row = _CountedUse(client, witness.generation)
            with self.assertRaises(attach.AttachExchangeError):
                row.exchange(attach.ClientExchange, witness)
            self.assertEqual(client.gettimeout(), 1)
            self.assert_no_reply(server)
            client.setblocking(False)
            self.refused(row.exchange(attach.ClientExchange, witness), timeout=0.1)
            self.assertEqual(_read(server, len(_R) + 64)[: len(_R)], _R)
            self.assertEqual(client.gettimeout(), 0.0)
            self.assertNotEqual(client.fileno(), -1)
