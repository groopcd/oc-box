import contextlib
import os
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from oc_box import history_overlap_io as overlap
from oc_box import history_overlap_remote as remote


_RECEIVE = """
import os, select, sys, time

def exact(count):
    seen = b""
    while len(seen) < count:
        chunk = os.read(0, count - len(seen))
        if not chunk:
            sys.exit(22)
        seen += chunk
    return seen

payload = exact(int.from_bytes(exact(4), "big"))
"""

_REPLY = """
assert not select.select([0], [], [], 0)[0], "early stdin closure or extra request"
os.write(1, (5).to_bytes(4, "big") + b"reply")
assert os.read(0, 1) == b"", "a second command was sent"
"""

_BARRIER_SETUP = """
token, credential = payload.split(b":", 1)
assert credential.endswith(b"credential")
ready = b"OC-BOX-HISTORY-READY/1 " + token + b" 7 11 8 12\\n"
go = b"OC-BOX-HISTORY-GO/1 " + token + b"\\n"
"""

_WAIT_GO = """
received = os.read(0, len(go))
if not received:
    sys.exit(31)
assert received == go
"""


@unittest.skipUnless(overlap._pidfd_available(), "Linux pidfd supervision required")
class HistoryOverlapIOTests(unittest.TestCase):
    def setUp(self):
        self.attempt = overlap.SourceAttempt("1" * 64, "generation")

    def start(self, program, **kwargs):
        options = {
            "attempt": self.attempt,
            "env": {},
            "payload": b"synthetic-endpoint-credential",
            "deadline": time.monotonic() + 2,
            "admission": lambda _: contextlib.nullcontext(),
            "max_output": 1024,
            "cleanup": 0.6,
        }
        options.update(kwargs)
        read = overlap.SourceRead.start(
            [sys.executable, "-I", "-c", program], **options
        )
        self.addCleanup(read.cancel)
        return read

    def proof(self):
        return overlap.HelperAbsent(self.attempt, time.monotonic_ns())

    def assert_reaped(self, child):
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)

    def barrier(self, body, **kwargs):
        return self.start(
            _RECEIVE + _BARRIER_SETUP + body,
            two_phase=True,
            payload=self.attempt.token.encode() + b":synthetic-endpoint-credential",
            **kwargs,
        )

    def witness(self, ready, *, check_ready=None, attempt=None):
        # Pure substitution for proc identity in protocol failure tests.
        # The positive end-to-end test below uses RemoteWitness.acquire.
        witness = remote.RemoteWitness()
        witness._attempt = self.attempt if attempt is None else attempt
        witness._bindings = [None]
        for pid, ticks in remote._ready(self.attempt, ready):
            witness._bindings.append((None, ((0, ticks), (pid,), ())))
        witness.check_ready = mock.Mock(side_effect=check_ready)
        return witness

    def test_exact_popen_under_gate_private_input_and_graceful_result(self):
        events = []
        children = []
        popen = subprocess.Popen

        @contextlib.contextmanager
        def gate(deadline):
            events.append("entered")
            self.assertLessEqual(deadline - time.monotonic(), overlap.ADMISSION_SECONDS)
            yield
            events.append("released")

        def tracked(*args, **kwargs):
            self.assertEqual(events, ["entered"])
            self.assertNotIn("synthetic-endpoint-credential", repr((args, kwargs)))
            self.assertEqual(kwargs["env"], {})
            events.append("spawned")
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        marker_check = """
assert payload.startswith(b"synthetic-endpoint-")
assert payload.endswith(b"credential")
"""
        with mock.patch.object(overlap.subprocess, "Popen", side_effect=tracked):
            read = self.start(_RECEIVE + marker_check + _REPLY, admission=gate)
        self.assertEqual(events, ["entered", "spawned", "released"])
        early = self.proof()
        outcome = read.finish()
        self.assertEqual(outcome.status, "local-complete")
        self.assertEqual(outcome.returncode, 0)
        self.assertTrue(outcome.stdout_eof and outcome.stderr_eof)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        self.assertEqual(outcome.remote_cleanup(early), "unverified")
        self.assertIsNone(read.response(early))
        proof = self.proof()
        self.assertEqual(outcome.remote_cleanup(proof), "helper-retired")
        self.assertEqual(read.response(proof), b"reply")
        self.assertIsNone(read.response(proof))
        self.assert_reaped(children[0])

    def test_no_spawn_after_refusal_timeout_preexisting_cancel_or_reuse(self):
        @contextlib.contextmanager
        def refuse(_):
            raise RuntimeError("private path")
            yield

        cases = (
            ("admission-refused", {"admission": refuse}),
            ("timeout-before-start", {"deadline": time.monotonic() - 0.01}),
            ("cancelled-before-start", {"cancelled": lambda: True}),
        )
        for expected, options in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(overlap.subprocess, "Popen") as popen,
            ):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                outcome = self.start("", **options).finish()
                self.assertEqual(outcome.status, expected)
                self.assertNotIn("private path", repr(outcome))
                self.assertFalse(outcome.spawn_attempted)
                self.assertEqual(outcome.remote_cleanup(), "not-started")
                with self.assertRaisesRegex(ValueError, "single-use"):
                    self.start("")
                popen.assert_not_called()

    def test_cancel_or_expired_admission_checked_again_inside_gate(self):
        for reason in ("cancelled-before-start", "admission-timeout"):
            stop = False

            @contextlib.contextmanager
            def gate(_):
                nonlocal stop
                if reason == "cancelled-before-start":
                    stop = True
                else:
                    time.sleep(0.02)
                yield

            with (
                self.subTest(reason=reason),
                mock.patch.object(overlap, "ADMISSION_SECONDS", 0.01),
                mock.patch.object(overlap.subprocess, "Popen") as popen,
            ):
                self.attempt = overlap.SourceAttempt("2" * 64, "generation")
                outcome = self.start(
                    "", admission=gate, cancelled=lambda: stop
                ).finish()
                self.assertEqual(outcome.status, reason)
                self.assertEqual(outcome.remote_cleanup(), "not-started")
                popen.assert_not_called()

    def test_gate_release_failure_reaps_exact_wrapper_without_sending_input(self):
        popen = subprocess.Popen
        children = []

        def tracked(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        for error in (RuntimeError("private path"), KeyboardInterrupt()):

            @contextlib.contextmanager
            def gate(_):
                yield
                raise error

            with (
                self.subTest(error=type(error)),
                mock.patch.object(overlap.subprocess, "Popen", side_effect=tracked),
            ):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                argv = "import os,sys; sys.exit(0 if not os.read(0, 1) else 25)"
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self.start(argv, admission=gate)
                    self.assertIs(caught.exception, error)
                    outcome = caught.exception.source_outcome
                else:
                    outcome = self.start(argv, admission=gate).finish()
                self.assertEqual(outcome.status, "admission-refused")
                self.assertTrue(outcome.spawn_attempted)
                self.assertTrue(outcome.local_cleanup_verified)
                self.assertEqual(outcome.returncode, 0)
                self.assertEqual(outcome.remote_cleanup(), "unverified")
                self.assertEqual(
                    outcome.remote_cleanup(
                        overlap.OriginalStopped(self.attempt, time.monotonic_ns())
                    ),
                    "original-stopped",
                )
                self.assert_reaped(children[-1])

    def test_malformed_excess_output_extra_frame_stderr_and_nonzero_refused(self):
        cases = (
            ("response-limit", "os.write(1, b'\\x00' * 4)"),
            ("response-limit", "os.write(1, (1025).to_bytes(4, 'big'))"),
            ("response-framing", "os.write(1, b'\\x00\\x00')"),
            ("response-framing", "os.write(1, b'\\x00\\x00\\x00\\x09x')"),
            ("response-framing", "os.write(1, b'\\x00\\x00\\x00\\x01xr')"),
            # The extra byte arrives only after the parent has seen the result.
            ("response-framing", _REPLY + "\nos.write(1, b'x')"),
            ("unexpected-stderr", "os.write(2, b'synthetic-private-diagnostic')"),
            ("wrapper-exit", _REPLY + "\nsys.exit(29)"),
        )
        for expected, body in cases:
            with self.subTest(body=body):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.start(_RECEIVE + "\n" + body)
                outcome = read.finish()
                self.assertEqual(outcome.status, expected)
                self.assertTrue(outcome.local_cleanup_verified)
                self.assertIsNone(read.response(self.proof()))
                self.assertNotIn("synthetic-private-diagnostic", repr(outcome))
                if expected == "wrapper-exit":
                    self.assertEqual(outcome.returncode, 29)

    def test_cooperative_cancel_and_stop_readback_do_not_admit_reply(self):
        read = self.start(
            _RECEIVE
            + """
os.write(1, b"\\x00\\x00\\x00\\x03")
sys.exit(17 if os.read(0, 1) == b"" else 19)
"""
        )
        self.assertIsNone(read.poll())
        outcome = read.cancel()
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(outcome.returncode, 17)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        stopped = overlap.OriginalStopped(self.attempt, time.monotonic_ns())
        self.assertEqual(outcome.remote_cleanup(stopped), "original-stopped")
        self.assertIsNone(read.response(stopped))
        self.assertIs(outcome, read.cancel())

    def test_fragmented_max_request_and_bounded_response(self):
        read = self.start(
            _RECEIVE
            + """
assert len(payload) == 16384
assert not select.select([0], [], [], 0)[0]
for byte in (131072).to_bytes(4, "big"):
    assert os.write(1, bytes([byte])) == 1
for _ in range(32):
    assert os.write(1, b"x" * 4096) == 4096
assert not os.read(0, 1)
""",
            payload=b"x" * overlap.MAX_REQUEST,
            max_output=131072,
        )
        self.assertEqual(read.finish().status, "local-complete")
        self.assertEqual(read.response(self.proof()), b"x" * 131072)

    def test_owner_failure_preserved_with_separate_local_cleanup_observation(self):
        read = self.start(_RECEIVE + "\nos.read(0, 1)")
        original_error = RuntimeError("destination")
        with self.assertRaises(RuntimeError) as caught, read:
            raise original_error
        self.assertIs(caught.exception, original_error)
        outcome = caught.exception.source_outcome
        self.assertEqual(outcome.status, "cancelled")
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")

    def test_missing_popen_handle_fails_closed_even_after_independent_stop(self):
        with mock.patch.object(
            overlap.subprocess, "Popen", side_effect=OSError("private command")
        ):
            outcome = self.start("").finish()
        self.assertEqual(outcome.status, "spawn-refused")
        self.assertTrue(outcome.spawn_attempted)
        self.assertFalse(outcome.local_cleanup_verified)
        self.assertEqual(
            outcome.remote_cleanup(
                overlap.OriginalStopped(self.attempt, time.monotonic_ns())
            ),
            "unverified",
        )
        self.assertNotIn("private command", repr(outcome))

    def test_deadline_gets_separate_local_reap_budget(self):
        read = self.start(
            _RECEIVE + "\ntime.sleep(10)",
            deadline=time.monotonic() + 0.15,
        )
        outcome = read.finish()
        self.assertEqual(outcome.status, "timeout")
        self.assertEqual(outcome.returncode, -signal.SIGKILL)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        self.assertIsNone(read.response(self.proof()))

    def test_wrapper_exit_and_held_pipe_does_not_prove_local_cleanup(self):
        popen = subprocess.Popen
        held = []

        def start_holder(*args, **kwargs):
            child = popen(*args, **kwargs)
            held.append(os.open(f"/proc/{child.pid}/fd/1", os.O_WRONLY))
            self.addCleanup(os.close, held[-1])
            return child

        with mock.patch.object(overlap.subprocess, "Popen", side_effect=start_holder):
            read = self.start(
                _RECEIVE + _REPLY,
                deadline=time.monotonic() + 0.2,
                cleanup=0.25,
            )
        outcome = read.finish()
        self.assertEqual(outcome.status, "timeout")
        self.assertEqual(outcome.returncode, 0)
        self.assertTrue(outcome.stderr_eof)
        self.assertFalse(outcome.stdout_eof)
        self.assertFalse(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(self.proof()), "unverified")
        self.assertIsNone(read.response(self.proof()))

    def test_missing_owned_wait_status_is_not_synthetic_exit_zero(self):
        popen = subprocess.Popen
        children = []

        def tracked(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(overlap.subprocess, "Popen", side_effect=tracked):
            read = self.start(_RECEIVE + "\nos.write(1, b'\\x00\\x00\\x00\\x01x')")
        # Model a competing reaper violating exclusive wait ownership.
        pid, status = os.waitpid(children[0].pid, 0)
        self.assertEqual(pid, children[0].pid)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        outcome = read.finish()
        self.assertEqual(outcome.status, "wrapper-status-unverified")
        self.assertIsNone(outcome.returncode)
        self.assertTrue(outcome.stdout_eof and outcome.stderr_eof)
        self.assertFalse(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(self.proof()), "unverified")
        # This test itself has the wait status the supervisor never observed.
        children[0].returncode = os.waitstatus_to_exitcode(status)

    def test_foreign_stale_and_future_witnesses_or_late_stop_not_consumed(self):
        stopped = False
        read = self.start(_RECEIVE + _REPLY, cancelled=lambda: stopped)
        outcome = read.finish()
        duplicate = overlap.SourceAttempt(
            self.attempt.container_id, self.attempt.generation
        )
        for proof in (
            overlap.HelperAbsent(duplicate, time.monotonic_ns()),
            overlap.HelperAbsent(self.attempt, 0),
            overlap.HelperAbsent(self.attempt, time.monotonic_ns() + 10**9),
            overlap.OriginalStopped(duplicate, time.monotonic_ns()),
            True,
        ):
            with self.subTest(proof=proof):
                self.assertEqual(outcome.remote_cleanup(proof), "unverified")
                self.assertIsNone(read.response(proof))
        exact_stop = overlap.OriginalStopped(self.attempt, time.monotonic_ns())
        self.assertEqual(outcome.remote_cleanup(exact_stop), "original-stopped")
        self.assertIsNone(read.response(exact_stop))
        stopped = True
        self.assertIsNone(read.response(self.proof()))
        stopped = False
        self.assertIsNone(read.response(self.proof()))

    def test_fragmented_ready_pauses_and_go_runs_once_inside_a_distinct_gate(self):
        events = []

        @contextlib.contextmanager
        def gate(stage, deadline):
            self.assertGreater(deadline, time.monotonic())
            self.assertLessEqual(deadline - time.monotonic(), overlap.ADMISSION_SECONDS)
            events.append(f"{stage}-enter")
            yield
            events.append(f"{stage}-exit")

        read = self.barrier(
            "\nfor value in ready:\n    os.write(1, bytes([value]))\n"
            + _WAIT_GO
            + _REPLY,
            admission=lambda deadline: gate("spawn", deadline),
        )
        expected = f"OC-BOX-HISTORY-READY/1 {self.attempt.token} 7 11 8 12\n".encode()
        ready = read.wait_ready()
        self.assertEqual(ready, expected)
        self.assertEqual(read.wait_ready(), expected)
        self.assertEqual(events, ["spawn-enter", "spawn-exit"])
        self.assertIsNone(read.poll())

        def check_ready(*, deadline):
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(events[-1], "go-enter")
            events.append("witness")

        witness = self.witness(ready, check_ready=check_ready)
        write = os.write

        def observe(fd, data):
            self.assertEqual(fd, read._child.stdin.fileno())
            self.assertEqual(events[-1], "witness")
            self.assertEqual(
                data, f"OC-BOX-HISTORY-GO/1 {self.attempt.token}\n".encode()
            )
            return write(fd, data)

        with mock.patch.object(overlap.os, "write", side_effect=observe) as call:
            self.assertIsNone(read.go(witness, admission=lambda end: gate("go", end)))
            call.assert_called_once()
        self.assertEqual(
            events, ["spawn-enter", "spawn-exit", "go-enter", "witness", "go-exit"]
        )
        outcome = read.finish()
        self.assertEqual(outcome.status, "local-complete")
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        self.assertEqual(read.response(self.proof()), b"reply")

    def test_binary_foreign_duplicate_or_oversized_pre_go_records_fail_closed(self):
        for expression in (
            "b'\\x00\\x00\\x00\\x01x'",
            "ready.replace(token, b'0' * 32)",
            "ready.replace(b' 7 11 ', b' 1 11 ')",
            "ready + ready",
            "ready + b'\\x00\\x00\\x00\\x01x'",
            "ready[:-1] + b'x' * 200",
        ):
            with self.subTest(expression=expression):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.barrier(f"\nos.write(1, {expression})\n" + _WAIT_GO)
                self.assertIsNone(read.wait_ready())
                outcome = read.finish()
                self.assertEqual(outcome.status, "ready-framing")
                self.assertEqual(outcome.returncode, 31)
                self.assertTrue(outcome.local_cleanup_verified)
                self.assertEqual(outcome.remote_cleanup(), "unverified")
                self.assertIsNone(read.response(self.proof()))

    def test_cancellation_during_witness_check_never_dispatches_go(self):
        stopped = False
        read = self.barrier(
            "\nos.write(1, ready)\n" + _WAIT_GO + _REPLY,
            cancelled=lambda: stopped,
        )
        ready = read.wait_ready()

        def rotate(*, deadline):
            nonlocal stopped
            stopped = True

        witness = self.witness(ready, check_ready=rotate)
        with mock.patch.object(overlap.os, "write", wraps=os.write) as write:
            outcome = read.go(witness, admission=lambda _: contextlib.nullcontext())
            write.assert_not_called()
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertIsNone(read.response(self.proof()))

    def test_ready_then_eof_before_go_does_not_dispatch(self):
        for descriptor in (1, 2):
            with self.subTest(descriptor=descriptor):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.barrier(
                    f"\nos.write(1, ready)\nos.close({descriptor})\n" + _WAIT_GO
                )
                ready = read.wait_ready()
                if ready is not None:
                    stream = (
                        read._child.stdout if descriptor == 1 else read._child.stderr
                    )
                    self.assertTrue(select.select([stream], [], [], 1)[0])
                witness = self.witness(ready) if ready is not None else None
                gate = mock.Mock(side_effect=lambda _: contextlib.nullcontext())
                outcome = (
                    read.finish() if ready is None else read.go(witness, admission=gate)
                )
                self.assertEqual(outcome.status, "ready-eof")
                self.assertEqual(outcome.returncode, 31)
                self.assertTrue(outcome.local_cleanup_verified)
                gate.assert_not_called()

    def test_output_arriving_during_witness_recheck_is_refused_before_go(self):
        read = self.barrier("\nos.write(1, ready)\n" + _WAIT_GO)
        ready = read.wait_ready()

        def send_extra(*, deadline):
            fd = os.open(f"/proc/{read._child.pid}/fd/1", os.O_WRONLY)
            try:
                os.write(fd, ready)
            finally:
                os.close(fd)

        witness = self.witness(ready, check_ready=send_extra)
        outcome = read.go(witness, admission=lambda _: contextlib.nullcontext())
        self.assertEqual(outcome.status, "ready-framing")
        self.assertEqual(outcome.returncode, 31)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        witness.check_ready.assert_called_once()

    def test_cancellation_inside_go_gate_and_missing_or_wrong_witness_fail_closed(self):
        cases = ("none", "foreign", "different-ready", "stop")
        for scenario in cases:
            with self.subTest(scenario=scenario):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                stop = False
                read = self.barrier(
                    "\nos.write(1, ready)\n" + _WAIT_GO, cancelled=lambda: stop
                )
                ready = read.wait_ready()
                witness = None
                if scenario != "none":
                    different = (
                        overlap.SourceAttempt("1" * 64, "generation")
                        if scenario == "foreign"
                        else None
                    )
                    witness = self.witness(
                        ready.replace(b" 7 11 ", b" 7 13 ")
                        if scenario == "different-ready"
                        else ready,
                        attempt=different,
                    )

                @contextlib.contextmanager
                def stop_gate(_):
                    nonlocal stop
                    stop = True
                    yield

                gate = mock.Mock(side_effect=stop_gate)
                outcome = read.go(witness, admission=gate)
                if scenario == "stop":
                    self.assertEqual(outcome.status, "cancelled")
                    gate.assert_called_once()
                    witness.check_ready.assert_not_called()
                else:
                    self.assertEqual(outcome.status, "go-witness-refused")
                    gate.assert_not_called()
                self.assertEqual(outcome.returncode, 31)
                self.assertEqual(outcome.remote_cleanup(), "unverified")
                self.assertTrue(outcome.local_cleanup_verified)

    def test_go_gate_release_failure_preserves_possible_dispatch_and_exact_reap(self):
        read = self.barrier("\nos.write(1, ready)\n" + _WAIT_GO + "\nsys.exit(40)\n")
        witness = self.witness(read.wait_ready())

        @contextlib.contextmanager
        def gate(_):
            yield
            raise RuntimeError("private engine error")

        outcome = read.go(witness, admission=gate)
        self.assertEqual(outcome.status, "go-admission-refused")
        self.assertEqual(outcome.returncode, 40)
        self.assertTrue(outcome.stdout_eof and outcome.stderr_eof)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")
        self.assertIsNone(read.response(self.proof()))
        self.assertIs(read.go(witness, admission=gate), outcome)

    def test_interrupted_go_release_attaches_exact_local_outcome(self):
        read = self.barrier("\nos.write(1, ready)\n" + _WAIT_GO + "\nsys.exit(40)\n")
        witness = self.witness(read.wait_ready())
        interrupt = KeyboardInterrupt()

        @contextlib.contextmanager
        def gate(_):
            yield
            raise interrupt

        with self.assertRaises(KeyboardInterrupt) as caught:
            read.go(witness, admission=gate)
        self.assertIs(caught.exception, interrupt)
        outcome = caught.exception.source_outcome
        self.assertEqual(outcome.status, "go-admission-refused")
        self.assertEqual(outcome.returncode, 40)
        self.assertTrue(outcome.local_cleanup_verified)
        self.assertEqual(outcome.remote_cleanup(), "unverified")

    def test_no_retry_of_go_backpressure_or_short_write(self):
        for kwargs in ({"return_value": 1}, {"side_effect": BlockingIOError()}):
            with self.subTest(kwargs=kwargs):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.barrier("\nos.write(1, ready)\n" + _WAIT_GO)
                witness = self.witness(read.wait_ready())
                with mock.patch.object(overlap.os, "write", **kwargs) as write:
                    outcome = read.go(
                        witness, admission=lambda _: contextlib.nullcontext()
                    )
                    write.assert_called_once()
                self.assertEqual(outcome.status, "go-write")
                self.assertEqual(outcome.returncode, 31)
                self.assertTrue(outcome.local_cleanup_verified)
                self.assertEqual(outcome.remote_cleanup(), "unverified")

    def test_expired_or_suppressed_go_gate_never_grants_response(self):
        for scenario in ("expired", "suppressed"):
            with self.subTest(scenario=scenario):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.barrier("\nos.write(1, ready)\n" + _WAIT_GO)
                failure = remote.RemoteWitnessError("synthetic-witness")
                witness = self.witness(read.wait_ready(), check_ready=failure)

                @contextlib.contextmanager
                def expired(_):
                    time.sleep(0.02)
                    yield

                gate = (
                    expired
                    if scenario == "expired"
                    else lambda _: contextlib.suppress(remote.RemoteWitnessError)
                )
                with mock.patch.object(
                    overlap, "ADMISSION_SECONDS", 0.01 if scenario == "expired" else 1.0
                ):
                    outcome = read.go(witness, admission=gate)
                if scenario == "expired":
                    self.assertEqual(outcome.status, "go-admission-timeout")
                    witness.check_ready.assert_not_called()
                else:
                    self.assertEqual(outcome.status, "go-witness-refused")
                    witness.check_ready.assert_called_once()
                self.assertEqual(outcome.returncode, 31)
                self.assertTrue(outcome.local_cleanup_verified)

    def test_no_second_go_and_no_late_ready_marker(self):
        for scenario in ("duplicate", "late marker", "finish before go"):
            with self.subTest(scenario=scenario):
                self.attempt = overlap.SourceAttempt("1" * 64, "generation")
                read = self.barrier(
                    "\nos.write(1, ready)\n"
                    + _WAIT_GO
                    + (
                        "\nos.write(1, ready)\nos.read(0, 1)\n"
                        if scenario == "late marker"
                        else _REPLY
                    )
                )
                witness = self.witness(read.wait_ready())
                gate = mock.Mock(side_effect=lambda _: contextlib.nullcontext())
                if scenario == "finish before go":
                    outcome = read.finish()
                    self.assertEqual(outcome.status, "go-required")
                    self.assertEqual(outcome.returncode, 31)
                    gate.assert_not_called()
                else:
                    self.assertIsNone(read.go(witness, admission=gate))
                    if scenario == "late marker":
                        outcome = read.finish()
                        self.assertEqual(outcome.status, "response-limit")
                    else:
                        outcome = read.go(witness, admission=gate)
                        self.assertEqual(outcome.status, "go-unexpected")
                    gate.assert_called_once()
                self.assertTrue(outcome.local_cleanup_verified)
                self.assertIsNone(read.response(self.proof()))

    def test_real_witness_gates_exact_controller_and_child_then_proves_absence(self):
        program = """
def ticks(pid):
    with open(f"/proc/{pid}/stat", "rb") as stream:
        return stream.read().rpartition(b") ")[2].split()[19]

parent = os.getpid()
r, w = os.pipe()
child = os.fork()
if child == 0:
    os.close(w)
    os.close(0)
    os.close(1)
    os.close(2)
    os._exit(0 if os.read(r, 1) == b"x" else 23)
os.close(r)
ready = b"OC-BOX-HISTORY-READY/1 " + token + b" " + b" ".join((
    str(parent).encode(), ticks(parent), str(child).encode(), ticks(child),
)) + b"\\n"
os.write(1, ready)
command = os.read(0, len(go))
if command == go:
    os.write(w, b"x")
os.close(w)
_, status = os.waitpid(child, 0)
assert command == go and os.waitstatus_to_exitcode(status) == 0
"""
        with (
            subprocess.Popen(
                [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
                stdin=subprocess.PIPE,
            ) as init,
            self.barrier(program + _REPLY) as read,
        ):
            ready = read.wait_ready()
            controller_id, child_id = remote._ready(self.attempt, ready)
            self.assertEqual(controller_id[0], read._child.pid)
            self.assertNotEqual(child_id[0], controller_id[0])
            with contextlib.ExitStack() as stack:
                proc = remote._open_proc(stack)
                init_ticks = remote._pin(stack, proc, init.pid).live()[0][1]
            mapped = {
                init.pid: 1,
                controller_id[0]: controller_id[0],
                child_id[0]: child_id[0],
            }
            actual_live = remote._Process.live
            posts = []

            def simulated(process):
                fields, pids, ns = actual_live(process)
                if process.host_pid in mapped:
                    return fields, (process.host_pid, mapped[process.host_pid]), (1, 2)
                return fields, pids, ns

            def probe(attempt, deadline, byte_limit):
                self.assertIs(attempt, self.attempt)
                self.assertGreater(deadline, time.monotonic())
                self.assertEqual(byte_limit, remote.MAX_TOP_BYTES)
                rows = mapped.items() if not posts else [(init.pid, 1)]
                posts.append(True)
                output = b"PID HPID\n" + b"".join(
                    f"{inner} {pid}\n".encode() for pid, inner in rows
                )
                return remote.TopSnapshot(
                    attempt, init.pid, init_ticks, time.monotonic_ns(), output
                )

            with (
                mock.patch.object(remote._Process, "live", new=simulated),
                mock.patch.object(remote, "_pin_namespaces", return_value=(1, 2)),
                remote.RemoteWitness.acquire(
                    self.attempt, ready, probe=probe, deadline=time.monotonic() + 1
                ) as witness,
            ):
                self.assertIsNone(
                    read.go(witness, admission=lambda _: contextlib.nullcontext())
                )
                outcome = read.finish()
                self.assertEqual(outcome.status, "local-complete")
                self.assertEqual(outcome.remote_cleanup(), "unverified")
                proof = witness.observe(deadline=time.monotonic() + 1)
                self.assertEqual(outcome.remote_cleanup(proof), "helper-retired")
                self.assertEqual(read.response(proof), b"reply")
                self.assertEqual(len(posts), 2)


if __name__ == "__main__":
    unittest.main()
