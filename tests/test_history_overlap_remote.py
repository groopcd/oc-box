from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from oc_box import history_overlap_remote as remote
from oc_box.history_overlap_io import HelperAbsent, SourceAttempt


_CONTROLLER = """
import os

r, w = os.pipe()
child = os.fork()
if child == 0:
    os.close(w)
    os.read(r, 1)
    os._exit(0)
os.close(r)
os.write(1, f"{child}\\n".encode())
command = os.read(0, 1)
os.close(w)
os.waitpid(child, 0)
if command == b'C':
    os.write(1, b'child-reaped\\n')
    os.read(0, 1)
"""
_REAL_LIVE = remote._Process.live


class RemoteWitnessValidationTests(unittest.TestCase):
    def setUp(self):
        self.attempt = SourceAttempt("4" * 64, "runtime")

    def test_strict_numeric_one_to_one_top_layout(self):
        self.assertEqual(
            remote._top(b" PID  HPID\n 1  1001\n 20  1002\n"), {1: 1001, 20: 1002}
        )
        for raw in (
            b"PID HPID\n",
            b"PID HPID\n1 1001",
            b"HPID PID\n1 1001\n",
            b"PID HPID extra\n1 1001\n",
            b"PID HPID\n1 1001 private text\n",
            b"PID HPID\n1 1001\n1 1002\n",
            b"PID HPID\n1 1001\n2 1001\n",
            b"PID HPID\n1 1001\n\n",
            b"PID HPID\n01 1001\n",
            b"PID HPID\n0 1001\n",
            b"PID HPID\n1 2147483648\n",
            b"PID HPID\n" + b"x" * remote.MAX_TOP_BYTES,
        ):
            with (
                self.subTest(raw=raw[:64]),
                self.assertRaisesRegex(
                    remote.RemoteWitnessError, "^remote-top-layout$"
                ),
            ):
                remote._top(raw)

    def test_missing_wrong_or_extra_ready_rejected_before_probe(self):
        good = f"OC-BOX-HISTORY-READY/1 {self.attempt.token} 2 3 4 5\n".encode()
        for ready in (
            None,
            b"",
            good[:-1],
            good + good,
            good.replace(self.attempt.token.encode(), b"0" * 32),
            good.replace(b" 2 3 ", b" 1 3 "),
            good.replace(b" 2 3 ", b" 4 3 "),
            good.replace(b" 2 3 ", b" 2 0 "),
        ):
            probe = mock.Mock()
            with (
                self.subTest(ready=ready is not None),
                self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-ready$"),
            ):
                remote.RemoteWitness.acquire(
                    self.attempt, ready, probe=probe, deadline=time.monotonic() + 1
                )
            probe.assert_not_called()

    def test_proc_leaves_and_namespace_directory_never_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "private").write_bytes(b"synthetic-private")
            (path / "stat").symlink_to("private")
            (path / "ns").symlink_to(".")
            directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    remote._read_leaf(directory, "stat", 4096)
                with self.assertRaises(OSError):
                    remote._namespaces(directory)
                with self.assertRaises(ValueError):
                    remote._read_leaf(directory, "private", 1)
            finally:
                os.close(directory)

    def test_proc_parsers_handle_complex_comm_and_refuse_invalid_binding(self):
        fields = b"S 9" + b" 0" * 17 + b" 901 0\n"
        self.assertEqual(
            remote._stat_fields(b"12 (name) with ) spaces) " + fields, 12), (9, 901)
        )
        for raw in (
            b"13 (name) " + fields,
            b"12 (name) " + fields.replace(b"S ", b"Z "),
            b"12 x",
        ):
            with self.assertRaises(ValueError):
                remote._stat_fields(raw, 12)
        self.assertEqual(remote._pid_vector(b"NSpid:\t12\t3\n", 12), (12, 3))
        for raw in (b"Pid: 12\n", b"NSpid: 14 3\n", b"NSpid: 12 3\nNSpid: 12 3\n"):
            with self.assertRaises(ValueError):
                remote._pid_vector(raw, 12)


@unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfds required")
class RemoteWitnessProcessTests(unittest.TestCase):
    def start(self, code, *, stdout=subprocess.DEVNULL):
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
        )

        def finish():
            if not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            finally:
                if process.stdout is not None:
                    process.stdout.close()

        self.addCleanup(finish)
        return process

    def line(self, process):
        if not select.select([process.stdout], [], [], 2)[0]:
            self.fail("Synthetic process did not announce readiness")
        return process.stdout.readline()

    def test_real_pidfd_and_nofollow_proc_only_mark_terminal_after_exit(self):
        child = self.start("import sys; sys.stdin.buffer.read()")
        with ExitStack() as stack:
            proc = remote._open_proc(stack)
            pinned = remote._pin(stack, proc, child.pid)
            (ppid, start), pids, ns = pinned.live()
            self.assertEqual(ppid, os.getpid())
            self.assertGreater(start, 0)
            self.assertEqual(pids, (child.pid,))
            self.assertTrue(all(value > 0 for value in ns))
            self.assertEqual(remote._poll((pinned.pidfd,)), set())
            child.stdin.close()
            self.assertEqual(child.wait(timeout=2), 0)
            self.assertEqual(remote._poll((pinned.pidfd,)), {pinned.pidfd})
            with self.assertRaisesRegex(
                remote.RemoteWitnessError, "^remote-process-exited$"
            ):
                pinned.live()

    def test_namespace_objects_are_pinned_through_pidfd_not_proc_links(self):
        if os.uname().machine not in remote._IOCTL_ABIS:
            self.skipTest("Unsupported ioctl ABI")
        with ExitStack() as stack:
            proc = remote._open_proc(stack)
            pinned = remote._pin(stack, proc, os.getpid())
            try:
                namespaces = remote._pin_namespaces(stack, pinned)
            except remote.RemoteWitnessError:
                self.skipTest("Kernel does not expose pidfd namespace ioctls")
            self.assertEqual(pinned.live()[2], namespaces)

    def fixture(self):
        # Local child topology and pidfds are real. Only the private namespace
        # metadata is modeled; tests do not create or inspect containers.
        original = self.start("import sys; sys.stdin.buffer.read()")
        controller = self.start(_CONTROLLER, stdout=subprocess.PIPE)
        child = int(self.line(controller))
        host_to_inner = {original.pid: 1, controller.pid: 7, child: 8}
        attempt = SourceAttempt("4" * 64, "runtime")
        with ExitStack() as stack:
            proc = remote._open_proc(stack)
            ticks = {
                pid: remote._pin(stack, proc, pid).live()[0][1] for pid in host_to_inner
            }
        ready = (
            f"OC-BOX-HISTORY-READY/1 {attempt.token} "
            f"7 {ticks[controller.pid]} 8 {ticks[child]}\n"
        ).encode()
        controls = {"erase": set(), "live_override": None, "probe_override": None}

        def simulated(process):
            fields, pids, namespaces = _REAL_LIVE(process)
            if process.host_pid not in host_to_inner:
                return fields, pids, namespaces
            selected = (
                fields,
                (process.host_pid, host_to_inner[process.host_pid]),
                (1, 2),
            )
            override = controls["live_override"]
            return override(process.host_pid, selected) if override else selected

        def probe(target, deadline, byte_limit):
            self.assertIs(target, attempt)
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(byte_limit, remote.MAX_TOP_BYTES)
            rows = b"PID HPID\n" + b"".join(
                f"{inner} {pid}\n".encode()
                for pid, inner in host_to_inner.items()
                if pid not in controls["erase"]
            )
            snapshot = remote.TopSnapshot(
                attempt, original.pid, ticks[original.pid], time.monotonic_ns(), rows
            )
            override = controls["probe_override"]
            return override(snapshot) if override else snapshot

        patcher = mock.patch.object(remote._Process, "live", new=simulated)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(remote, "_pin_namespaces", return_value=(1, 2))
        patcher.start()
        self.addCleanup(patcher.stop)
        return attempt, ready, probe, original, controller, child, controls

    def acquire(self, attempt, ready, probe):
        witness = remote.RemoteWitness.acquire(
            attempt, ready, probe=probe, deadline=time.monotonic() + 2
        )
        self.addCleanup(witness.close)
        return witness

    def retire_child(self, controller):
        controller.stdin.write(b"C")
        controller.stdin.flush()
        self.assertEqual(self.line(controller), b"child-reaped\n")

    def test_positive_mapping_and_both_exact_process_terminal_states_required(self):
        attempt, ready, probe, _, controller, child, controls = self.fixture()
        witness = self.acquire(attempt, ready, probe)
        witness.check_ready(deadline=time.monotonic() + 1)
        # Top alone reporting absence is not evidence while a pinned PID lives.
        controls["erase"] = {controller.pid, child}
        self.assertIsNone(witness.observe(deadline=time.monotonic() + 0.02))
        self.retire_child(controller)
        self.assertIsNone(witness.observe(deadline=time.monotonic() + 0.02))
        controller.stdin.close()
        self.assertEqual(controller.wait(timeout=2), 0)
        before = time.monotonic_ns()
        proof = witness.observe(deadline=time.monotonic() + 1)
        self.assertIsInstance(proof, HelperAbsent)
        self.assertIs(proof.attempt, attempt)
        self.assertGreaterEqual(proof.observed_ns, before)

    def test_missing_mapping_or_unverified_probe_never_mints_witness(self):
        attempt, ready, probe, _, _, child, controls = self.fixture()
        controls["erase"] = {child}
        with self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-mapping$"):
            self.acquire(attempt, ready, probe)

        def opaque(_):
            raise RuntimeError("synthetic-private-engine-error")

        controls["probe_override"] = opaque
        with self.assertRaisesRegex(
            remote.RemoteWitnessError, "^remote-top-probe$"
        ) as caught:
            self.acquire(attempt, ready, probe)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_parent_start_ticks_namespace_or_inner_pid_drift_rejected(self):
        attempt, ready, probe, _, _, child, controls = self.fixture()
        for reason, change in (
            ("remote-parent", lambda fields, pids, ns: ((0, fields[1]), pids, ns)),
            (
                "remote-process-drift",
                lambda fields, pids, ns: ((fields[0], fields[1] + 1), pids, ns),
            ),
            (
                "remote-namespace",
                lambda fields, pids, ns: (fields, pids, (ns[0], ns[1] + 1)),
            ),
            (
                "remote-namespace",
                lambda fields, pids, ns: (fields, pids, (ns[0] + 1, ns[1])),
            ),
            (
                "remote-process-drift",
                lambda fields, pids, ns: (fields, (pids[0], pids[1] + 1), ns),
            ),
        ):
            with self.subTest(reason=reason):
                controls["live_override"] = lambda pid, record: (
                    change(*record) if pid == child else record
                )
                with self.assertRaisesRegex(remote.RemoteWitnessError, f"^{reason}$"):
                    self.acquire(attempt, ready, probe)

    def test_a_delayed_or_already_retired_helper_cannot_be_bound(self):
        attempt, ready, probe, _, controller, _, _ = self.fixture()
        self.retire_child(controller)
        with self.assertRaisesRegex(
            remote.RemoteWitnessError, "^remote-(proc|process-exited)$"
        ):
            self.acquire(attempt, ready, probe)

    def test_unpinnable_namespaces_cannot_fall_back_to_proc_labels(self):
        attempt, ready, probe, _, _, _, _ = self.fixture()
        for kwargs in (
            {"return_value": (1, 3)},
            {"side_effect": remote.RemoteWitnessError("remote-namespace")},
        ):
            with (
                self.subTest(kwargs=kwargs),
                mock.patch.object(remote, "_pin_namespaces", **kwargs),
                self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-namespace$"),
            ):
                self.acquire(attempt, ready, probe)

    def test_pre_release_drift_invalidates_retirement_witness(self):
        attempt, ready, probe, _, controller, child, controls = self.fixture()
        witness = self.acquire(attempt, ready, probe)

        def reparented(pid, row):
            return ((0, row[0][1]), row[1], row[2]) if pid == child else row

        controls["live_override"] = reparented
        with self.assertRaisesRegex(
            remote.RemoteWitnessError, "^remote-process-drift$"
        ):
            witness.check_ready(deadline=time.monotonic() + 1)
        controls["live_override"] = None
        controller.stdin.close()
        self.assertEqual(controller.wait(timeout=2), 0)
        controls["erase"] = {controller.pid, child}
        with self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-owner$"):
            witness.observe(deadline=time.monotonic() + 1)

    def test_retirement_mapping_drift_refuses_even_after_exact_terminal(self):
        attempt, ready, probe, _, controller, child, controls = self.fixture()
        witness = self.acquire(attempt, ready, probe)
        controller.stdin.close()
        self.assertEqual(controller.wait(timeout=2), 0)
        with self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-original$"):
            witness.observe(deadline=time.monotonic() + 1)
        controls["erase"] = {controller.pid, child}
        with self.assertRaisesRegex(remote.RemoteWitnessError, "^remote-owner$"):
            witness.observe(deadline=time.monotonic() + 1)

    def test_missing_original_is_not_a_whole_stop_or_helper_absence_proof(self):
        attempt, ready, probe, original, controller, child, controls = self.fixture()
        witness = self.acquire(attempt, ready, probe)
        controller.stdin.close()
        self.assertEqual(controller.wait(timeout=2), 0)
        original.stdin.close()
        self.assertEqual(original.wait(timeout=2), 0)
        controls["erase"] = {controller.pid, child}
        with self.assertRaisesRegex(
            remote.RemoteWitnessError, "^remote-process-exited$"
        ):
            witness.observe(deadline=time.monotonic() + 1)

    def test_retirement_probe_opaque_error_and_drift_do_not_recover(self):
        def opaque(_):
            raise RuntimeError("synthetic-private-diagnostic")

        for reason, override in (
            ("remote-top-probe", opaque),
            ("remote-top-probe", lambda snapshot: replace(snapshot, observed_ns=0)),
            (
                "remote-top-probe",
                lambda snapshot: replace(
                    snapshot, attempt=SourceAttempt("4" * 64, "runtime")
                ),
            ),
            (
                "remote-original",
                lambda snapshot: replace(
                    snapshot, init_start_ticks=snapshot.init_start_ticks + 1
                ),
            ),
        ):
            with self.subTest(reason=reason):
                attempt, ready, probe, _, controller, child, controls = self.fixture()
                witness = self.acquire(attempt, ready, probe)
                controller.stdin.close()
                self.assertEqual(controller.wait(timeout=2), 0)
                controls["erase"] = {controller.pid, child}
                controls["probe_override"] = override
                with self.assertRaisesRegex(remote.RemoteWitnessError, f"^{reason}$"):
                    witness.observe(deadline=time.monotonic() + 1)
                controls["probe_override"] = None
                with self.assertRaisesRegex(
                    remote.RemoteWitnessError, "^remote-owner$"
                ):
                    witness.observe(deadline=time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
