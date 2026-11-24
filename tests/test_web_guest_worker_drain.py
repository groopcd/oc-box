"""Deterministic lifecycle fixtures; no Gateway, native service or child exec."""

from concurrent.futures import ThreadPoolExecutor
import errno
import gc
import os
from queue import Queue
import socket
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
import weakref

from oc_box import web_guest_worker_drain as drain


class AcceptedWorkerTests(unittest.TestCase):
    def pair(self):
        stream, peer = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(stream.close)
        stream.settimeout(3)
        peer.settimeout(3)
        return stream, peer

    def test_close_winning_after_accept_leaves_the_unregistered_socket_to_acceptor(
        self,
    ):
        workers = drain.AcceptedWorkers()
        stream, peer = self.pair()
        dispatch = Mock()
        self.assertTrue(workers.close(timeout=0))
        self.assertFalse(workers.dispatch(stream, dispatch))
        dispatch.assert_not_called()
        peer.sendall(b"x")
        self.assertEqual(stream.recv(1), b"x")

    def test_registration_precedes_start_and_close_can_win_before_dispatch(self):
        workers = drain.AcceptedWorkers(capacity=1)
        stream, _ = self.pair()
        starting, let_start = Event(), Event()
        dispatch = Mock()
        real_start = Thread.start

        def paused_start(thread):
            starting.set()
            if not let_start.wait(3):
                raise RuntimeError
            real_start(thread)

        with ThreadPoolExecutor(1) as acceptor:
            # Warm the pool before intercepting only the registered start.
            acceptor.submit(lambda: None).result(timeout=3)
            with patch.object(drain.Thread, "start", paused_start):
                future = acceptor.submit(workers.dispatch, stream, dispatch)
                try:
                    self.assertTrue(starting.wait(3))
                    self.assertEqual(len(workers._workers), 1)
                    use, row = next(iter(workers._workers.items()))
                    self.assertIs(use.stream, stream)
                    self.assertEqual(row.stamp, drain._stamp(stream))
                    self.assertIsNone(row.thread.ident)
                    # A stalled start is already charged, but cannot hold
                    # the registration fence or dispatch after revocation.
                    self.assertFalse(workers.close(timeout=0))
                    self.assertTrue(use.cancelled)
                finally:
                    let_start.set()
                self.assertTrue(future.result(timeout=3))
        self.assertTrue(workers.close(timeout=3))
        self.assertEqual(stream.fileno(), -1)
        dispatch.assert_not_called()

    def test_wake_browser_and_independent_activity_then_confirm_exact_join(self):
        workers = drain.AcceptedWorkers()
        stream, peer = self.pair()
        started, woken, let_finish = Event(), Event(), Event()
        verdicts = Queue()
        wake = Mock(side_effect=woken.set)

        def run(use):
            activity = workers.admit(use, wake=wake)
            started.set()
            try:
                downstream = use.stream.recv(1)
                upstream = woken.wait(3) and let_finish.wait(3)
                retired = workers.complete(use, activity, confirmed=upstream)
                verdicts.put((downstream, use.cancelled, retired))
            finally:
                started.set()

        self.assertTrue(workers.dispatch(stream, run))
        try:
            self.assertTrue(started.wait(3))
            self.assertFalse(workers.close(timeout=0))
            self.assertEqual(peer.recv(1), b"")
            self.assertFalse(workers.close(timeout=0))
            wake.assert_called_once_with()
        finally:
            let_finish.set()
        self.assertEqual(verdicts.get(timeout=3), (b"", True, True))
        self.assertTrue(workers.close(timeout=3))
        self.assertTrue(workers.close(timeout=0))

    def test_fence_during_admitted_start_wakes_before_native_work_and_does_not_reopen(
        self,
    ):
        workers = drain.AcceptedWorkers()
        stream, _ = self.pair()
        parked, let_launch, cancelled = Event(), Event(), Event()
        results = Queue()

        def run(use):
            activity = workers.admit(use, wake=cancelled.set)
            parked.set()
            let_launch.wait(3)
            allowed = workers.current(use) and not cancelled.is_set()
            # Positive evidence in this fixture is that launch was never
            # attempted. Production must separately know this, not guess it.
            results.put(
                (allowed, workers.complete(use, activity, confirmed=not allowed))
            )

        self.assertTrue(workers.dispatch(stream, run))
        try:
            self.assertTrue(parked.wait(3))
            workers.fence()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(workers.close(timeout=0))
        finally:
            let_launch.set()
        self.assertEqual(results.get(timeout=3), (False, True))
        self.assertTrue(workers.close(timeout=3))

    def test_sticky_activity_uncertainty_or_missing_receipt_never_becomes_empty_success(
        self,
    ):
        for result in ("missing", False, 1):
            with self.subTest(result=result):
                workers = drain.AcceptedWorkers()
                stream, _ = self.pair()
                started = Event()
                finished = Queue()
                wake = Mock()

                def run(use):
                    activity = workers.admit(use, wake=wake)
                    started.set()
                    if result == "missing":
                        return
                    finished.put(
                        (
                            workers.complete(use, activity, confirmed=result),
                            workers.complete(use, activity, confirmed=True),
                        )
                    )

                self.assertTrue(workers.dispatch(stream, run))
                self.assertTrue(started.wait(3))
                if result != "missing":
                    self.assertEqual(finished.get(timeout=3), (False, False))
                self.assertFalse(workers.close(timeout=3))
                self.assertEqual(stream.fileno(), -1)
                self.assertFalse(workers.close(timeout=0))
                wake.assert_called_once_with()

    def test_cancel_hook_failure_is_sticky_and_does_not_skip_peer_activity(self):
        workers = drain.AcceptedWorkers()
        stream, _ = self.pair()
        started, finish = Event(), Event()
        good = Mock()
        bad = Mock(side_effect=OSError("private synthetic diagnostic"))
        outcomes = Queue()

        def run(use):
            items = [workers.admit(use, wake=callback) for callback in (good, bad)]
            started.set()
            finish.wait(3)
            outcomes.put(
                [workers.complete(use, item, confirmed=True) for item in items]
            )

        self.assertTrue(workers.dispatch(stream, run))
        try:
            self.assertTrue(started.wait(3))
            workers.fence()
            good.assert_called_once_with()
            bad.assert_called_once_with()
        finally:
            finish.set()
        self.assertEqual(outcomes.get(timeout=3), [True, True])
        self.assertFalse(workers.close(timeout=3))

    def test_concurrent_close_never_claims_drain_while_wake_is_in_flight(self):
        workers = drain.AcceptedWorkers()
        stream, _ = self.pair()
        ready, entered, release_wake, ended = Event(), Event(), Event(), Event()

        def slow_wake():
            entered.set()
            if not release_wake.wait(3):
                raise RuntimeError

        def run(use):
            activity = workers.admit(use, wake=slow_wake)
            ready.set()
            entered.wait(3)
            workers.complete(use, activity, confirmed=True)
            ended.set()

        self.assertTrue(workers.dispatch(stream, run))
        self.assertTrue(ready.wait(3))
        with ThreadPoolExecutor(1) as pool:
            fence = pool.submit(workers.fence)
            try:
                self.assertTrue(ended.wait(3))
                self.assertFalse(workers.close(timeout=1))
            finally:
                release_wake.set()
            fence.result(timeout=3)
        self.assertTrue(workers.close(timeout=3))

    def test_ambiguous_start_before_or_after_os_start_never_dispatches_or_drains(self):
        start = Thread.start
        for spawned in (False, True):
            with self.subTest(spawned=spawned):
                workers = drain.AcceptedWorkers()
                stream, _ = self.pair()
                dispatch = Mock()

                def ambiguous(thread):
                    if spawned:
                        start(thread)
                    raise RuntimeError("private synthetic startup diagnostic")

                with patch.object(drain.Thread, "start", ambiguous):
                    with self.assertRaisesRegex(
                        drain.WorkerDrainError, "^The browser worker"
                    ):
                        workers.dispatch(stream, dispatch)
                self.assertFalse(workers.close(timeout=3))
                self.assertFalse(workers.close(timeout=0))
                dispatch.assert_not_called()
                self.assertEqual(stream.fileno() < 0, spawned)

    def test_capacity_and_completion_are_bound_to_exact_live_worker(self):
        workers = drain.AcceptedWorkers(capacity=2, activities_per_worker=1)
        first, _ = self.pair()
        second, _ = self.pair()
        third, _ = self.pair()
        first_item, results = Queue(), Queue()
        release = Event()

        def primary(use):
            item = workers.admit(use, wake=lambda: None)
            first_item.put((use, item))
            too_many = workers.admit(use, wake=lambda: None)
            release.wait(3)
            results.put((too_many, workers.complete(use, item, confirmed=True)))

        def other(use):
            results.put(workers.complete(use, item, confirmed=True))
            release.wait(3)

        self.assertTrue(workers.dispatch(first, primary))
        use, item = first_item.get(timeout=3)
        try:
            with first.dup() as alias:
                self.assertFalse(workers.dispatch(alias, Mock()))
            self.assertTrue(workers.dispatch(second, other))
            self.assertFalse(workers.dispatch(third, Mock()))
            self.assertFalse(
                workers.complete(use, item, confirmed=True)
            )  # Wrong thread.
            self.assertIs(results.get(timeout=3), False)  # Wrong worker.
        finally:
            release.set()
        self.assertEqual(results.get(timeout=3), (None, True))
        self.assertTrue(workers.close(timeout=3))

    def test_replaced_descriptor_is_neither_woken_nor_closed_and_never_revives(self):
        workers = drain.AcceptedWorkers()
        stream, _ = self.pair()
        foreign, foreign_peer = self.pair()
        ready, release, returned = Event(), Event(), Event()
        verdicts = Queue()

        def run(use):
            ready.set()
            release.wait(3)
            verdicts.put(workers.current(use))
            returned.set()

        self.assertTrue(workers.dispatch(stream, run))
        try:
            self.assertTrue(ready.wait(3))
            fd = os.dup2(foreign.fileno(), stream.fileno(), inheritable=False)
            workers.fence()
            self.assertEqual(stream.fileno(), -1)
            self.assert_foreign_socket(fd, foreign, foreign_peer)
        finally:
            release.set()
        self.assertTrue(returned.wait(3))
        self.assertIs(verdicts.get(timeout=3), False)
        self.assertFalse(workers.close(timeout=3))

    def test_unconnected_inheritable_or_nonstream_is_not_admitted(self):
        workers = drain.AcceptedWorkers()
        dispatch = Mock()
        stream, _ = self.pair()
        stream.set_inheritable(True)
        with socket.socket() as bare, socket.socket(type=socket.SOCK_DGRAM) as datagram:
            for peer in (stream, bare, datagram):
                self.assertFalse(workers.dispatch(peer, dispatch))
        dispatch.assert_not_called()
        self.assertTrue(workers.close(timeout=0))

    def test_disowned_fd_cannot_be_reintroduced_as_a_new_worker_handle(self):
        workers = drain.AcceptedWorkers()
        stream, _ = self.pair()
        foreign, _ = self.pair()
        verdicts = Queue()

        def run(use):
            fd = os.dup2(foreign.fileno(), use.stream.fileno(), inheritable=False)
            first = workers.current(use)
            with socket.socket(fileno=fd) as replacement:
                replacement_use = drain.AcceptedWorker(replacement)
                verdicts.put(
                    (first, workers.current(use), workers.current(replacement_use))
                )

        self.assertTrue(workers.dispatch(stream, run))
        self.assertEqual(verdicts.get(timeout=3), (False, False, False))
        self.assertFalse(workers.close(timeout=3))

    def assert_foreign_socket(self, fd, foreign, peer):
        # Wrap the exact numeric fd, not foreign's surviving source fd.
        with socket.socket(fileno=fd) as replacement:
            self.assertTrue(os.path.samestat(os.fstat(fd), os.fstat(foreign.fileno())))
            replacement.settimeout(3)
            peer.sendall(b"x")
            self.assertEqual(replacement.recv(1), b"x")
            replacement.sendall(b"y")
            self.assertEqual(peer.recv(1), b"y")

    def test_exact_foreign_fd_survives_worker_and_registry_finalization(self):
        for phase in (
            "worker-exit",
            "activation",
            "ambiguous-start",
            "after-fence",
            "wake-failure",
        ):
            with self.subTest(phase=phase):
                self.check_finalization(phase)

    def check_finalization(self, phase):
        foreign, peer = self.pair()
        # This wrapper must not have an addCleanup reference: verify that
        # both it and the sticky-failed registry really are collected.
        workers = drain.AcceptedWorkers()
        stream, original_peer = socket.socketpair()
        stream_ref, workers_ref = weakref.ref(stream), weakref.ref(workers)
        entered, release = Event(), Event()
        real_start = Thread.start
        dispatched = []
        replaced = Event()
        fd = stream.fileno()

        def replace():
            os.dup2(foreign.fileno(), fd, inheritable=False)
            replaced.set()

        def wake():
            replace()
            raise OSError("synthetic wake failure")

        def run(use):
            dispatched.append(True)
            if phase == "worker-exit":
                replace()
            elif phase == "wake-failure":
                workers.admit(use, wake=wake)
                entered.set()
                release.wait(3)

        def start(thread):
            if phase in ("activation", "ambiguous-start"):
                replace()
            if phase in ("ambiguous-start", "after-fence"):
                raise RuntimeError("synthetic ambiguous thread start")
            real_start(thread)

        try:
            with original_peer, patch.object(drain.Thread, "start", start):
                if phase in ("ambiguous-start", "after-fence"):
                    with self.assertRaises(drain.WorkerDrainError):
                        workers.dispatch(stream, run)
                else:
                    self.assertTrue(workers.dispatch(stream, run))
                if phase == "after-fence":
                    replace()
                elif phase == "worker-exit":
                    self.assertTrue(replaced.wait(3))
                elif phase == "wake-failure":
                    try:
                        self.assertTrue(entered.wait(3))
                        workers.fence()
                    finally:
                        release.set()
                self.assertFalse(workers.close(timeout=3))
                self.assertFalse(workers.close(timeout=0))
                self.assertEqual(
                    dispatched,
                    [True] if phase in ("worker-exit", "wake-failure") else [],
                )
            del stream, workers
            gc.collect()
            self.assertIsNone(stream_ref())
            self.assertIsNone(workers_ref())
            self.assertTrue(replaced.is_set())
            self.assert_foreign_socket(fd, foreign, peer)
        finally:
            if replaced.is_set():
                try:
                    if os.path.samestat(os.fstat(fd), os.fstat(foreign.fileno())):
                        os.close(fd)
                except OSError:
                    pass

    def test_uncertain_identity_error_does_not_disown_the_original_socket(self):
        stream, peer = self.pair()
        fd, *_ = stamp = drain._stamp(stream)
        with patch.object(
            drain.os, "fstat", side_effect=OSError(errno.EIO, "synthetic")
        ):
            self.assertFalse(drain._same(stream, stamp, disown=True))
        self.assertEqual(stream.fileno(), fd)
        peer.sendall(b"x")
        self.assertEqual(stream.recv(1), b"x")

    def test_os_closed_fd_is_disowned_before_it_can_be_reused(self):
        stream, _ = self.pair()
        foreign, peer = self.pair()
        fd, *_ = stamp = drain._stamp(stream)
        os.close(fd)
        self.assertFalse(drain._same(stream, stamp, disown=True))
        self.assertEqual(stream.fileno(), -1)
        os.dup2(foreign.fileno(), fd, inheritable=False)
        stream.close()
        self.assert_foreign_socket(fd, foreign, peer)


if __name__ == "__main__":
    unittest.main()
