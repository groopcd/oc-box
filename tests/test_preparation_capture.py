import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from oc_box import preparation_capture
from oc_box.preparation_capture import capture_npm


_FLOOD_BOTH_STREAMS = """
import concurrent.futures
import sys

def write_many(stream):
    line = b"private fixture payload " * 2048 + b"\\n"
    for _ in range(32):
        stream.write(line)
    stream.flush()

with concurrent.futures.ThreadPoolExecutor(2) as pool:
    stdout = pool.submit(write_many, sys.stdout.buffer)
    stderr = pool.submit(write_many, sys.stderr.buffer)
    stdout.result()
    stderr.result()
"""


class PreparationCaptureTests(unittest.TestCase):
    def run_child(self, script, outputs, *, timeout=5):
        stdout, stderr = outputs
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            text=True,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
        )

    def test_pipe_setup_leaves_descriptor_headroom_for_the_original_child(self):
        script = """
import os
import resource
import subprocess
import sys
from oc_box.preparation_capture import capture_npm

_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
# Two pipes and the selector can fit, but the child needs its stdin and
# exec-error descriptors too. Lower this fixture process's limit only.
highest = max(map(int, os.listdir("/proc/self/fd")))
resource.setrlimit(resource.RLIMIT_NOFILE, (highest + 6, hard))
with capture_npm() as capture:
    assert capture is None
    result = subprocess.run(
        [sys.executable, "-B", "-c", "raise SystemExit(37)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=5,
    )
assert result.returncode == 37
"""
        result = self.run_child(script, (subprocess.PIPE, subprocess.PIPE))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_exact_allowlisted_lines_from_both_streams(self):
        with capture_npm() as capture:
            result = self.run_child(
                r"""
import os
os.write(1, b"npm ERR! code EUSAGE\nnpm ERR! code EUSAGE\n"
            b"private fixture payload\nnpm error code EPRIVATE\n"
            b"npm ERR! code E404 private\n npm error code E401\n"
            b"npm error code\nE401\nnpm error code E404\xff\n"
            b"npm error code ERESOLVE\nnpm error code ETIMEDOUT")
os.write(2, b"npm error code E403\r\nnpm ERR! code ENOENT\n"
            b"\x1b[31mnpm error code E500\x1b[0m\n")
raise SystemExit(17)
""",
                capture.outputs,
            )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (17, None, None)
        )
        self.assertFalse(capture.unavailable)
        self.assertEqual(
            [
                (code, count)
                for source, code, count in capture.records
                if source == "stdout"
            ],
            [("EUSAGE", 2), ("omitted", 7), ("ERESOLVE", 1), ("omitted", 1)],
        )
        self.assertEqual(
            [
                (code, count)
                for source, code, count in capture.records
                if source == "stderr"
            ],
            [("E403", 1), ("ENOENT", 1), ("omitted", 1)],
        )
        self.assertNotIn("private", repr(capture))

    def test_records_keep_the_readers_cross_stream_order(self):
        observed = queue.Queue()
        read = os.read

        def observe(fd, size):
            chunk = read(fd, size)
            if chunk:
                observed.put_nowait(None)
            return chunk

        with mock.patch.object(preparation_capture.os, "read", side_effect=observe):
            with capture_npm() as capture:
                for index in (1, 0, 1):
                    os.write(capture.outputs[index], b"npm error code EACCES\n")
                    observed.get(timeout=1)
        self.assertFalse(capture.unavailable)
        self.assertEqual(
            capture.records,
            (("stderr", "EACCES", 1), ("stdout", "EACCES", 1), ("stderr", "EACCES", 1)),
        )

    def test_reader_reassembles_chunks_but_never_an_oversized_line_suffix(self):
        read = os.read
        with mock.patch.object(
            preparation_capture.os, "read", side_effect=lambda fd, size: read(fd, 4)
        ):
            with capture_npm() as capture:
                os.write(
                    capture.outputs[0],
                    b"npm error code EAI_AGAIN\n"
                    + b"x" * 2048
                    + b"npm ERR! code EACCES\n"
                    + b"npm ERR! code ENOSPC\n",
                )
        self.assertFalse(capture.unavailable)
        self.assertEqual(
            capture.records,
            (
                ("stdout", "EAI_AGAIN", 1),
                ("stdout", "omitted", 1),
                ("stdout", "ENOSPC", 1),
            ),
        )

    def test_large_concurrent_streams_finish_after_capture_budget_is_exhausted(self):
        with capture_npm() as capture:
            result = self.run_child(_FLOOD_BOTH_STREAMS, capture.outputs)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, None, None)
        )
        self.assertFalse(capture.unavailable)
        self.assertEqual(
            {code for _, code, _ in capture.records}, {"omitted", "output-limit"}
        )
        self.assertEqual(capture.records[-1][1:], ("output-limit", 1))

    def test_bounds_records_and_repeat_counts(self):
        cases = (
            (
                {"_MAX_RECORDS": 3},
                b"npm ERR! code E401\nnpm error code E403\nnpm error code E404\n",
                (("E401", 1), ("E403", 1), ("output-limit", 1)),
            ),
            (
                {"_MAX_COUNT": 2},
                b"private text\n" * 10,
                (("omitted", 2), ("output-limit", 1)),
            ),
        )
        for limits, data, expected in cases:
            with (
                self.subTest(limits=limits),
                mock.patch.multiple(preparation_capture, **limits),
            ):
                with capture_npm() as capture:
                    os.write(capture.outputs[1], data)
                self.assertFalse(capture.unavailable)
                self.assertEqual(
                    capture.records,
                    tuple(("stderr", code, count) for code, count in expected),
                )

    def test_empty_capture_closes_outputs(self):
        with capture_npm() as capture:
            outputs = capture.outputs
        self.assertEqual(capture.records, ())
        self.assertFalse(capture.unavailable)
        for fd in outputs:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_early_interruption_keeps_the_original_exception(self):
        interrupt = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as caught:
            with capture_npm() as capture:
                os.write(capture.outputs[1], b"npm error code EPIPE\nprivate fragment")
                raise interrupt
        self.assertIs(caught.exception, interrupt)
        self.assertFalse(capture.unavailable)
        self.assertEqual(
            capture.records, (("stderr", "EPIPE", 1), ("stderr", "omitted", 1))
        )

    def test_subprocess_timeout_has_no_output_payload(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            with capture_npm() as capture:
                self.run_child(
                    "import os,time; os.write(2, b'private output'); time.sleep(10)",
                    capture.outputs,
                    timeout=0.25,
                )
        self.assertIsNone(caught.exception.stdout)
        self.assertIsNone(caught.exception.stderr)
        self.assertFalse(capture.unavailable)

    def test_partial_setup_failure_closes_pipes_and_yields_none(self):
        fds = os.pipe()
        error = RuntimeError("original caller exception")
        with mock.patch.object(
            preparation_capture.os,
            "pipe",
            side_effect=[fds, OSError("private setup error")],
        ):
            with self.assertRaises(RuntimeError) as caught:
                with capture_npm() as capture:
                    self.assertIsNone(capture)
                    for fd in fds:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    raise error
        self.assertIs(caught.exception, error)
        self.assertIsNone(caught.exception.__context__)

    def test_reader_failure_signals_unavailable_and_drains_both_streams(self):
        read = os.read

        def fail_capture_read(fd, size):
            if threading.current_thread() is not threading.main_thread():
                raise OSError("private read error")
            return read(fd, size)

        with mock.patch.object(
            preparation_capture.os, "read", side_effect=fail_capture_read
        ):
            with capture_npm() as capture:
                result = self.run_child(_FLOOD_BOTH_STREAMS, capture.outputs)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, None, None)
        )
        self.assertTrue(capture.unavailable)
        self.assertEqual(capture.records, ())

    def test_inherited_writers_do_not_wait_for_eof(self):
        holder = None
        try:
            with capture_npm() as capture:
                # This process models a descendant retaining the redirections
                # beyond invoke's exit. Keep it directly reapable by the test.
                holder = subprocess.Popen(
                    [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read()"],
                    stdin=subprocess.PIPE,
                    stdout=capture.outputs[0],
                    stderr=capture.outputs[1],
                )
                result = self.run_child(
                    r"import os; os.write(2, b'npm error code ETIMEDOUT\n')",
                    capture.outputs,
                )
                began = time.monotonic()
            self.assertLess(time.monotonic() - began, 1)
            self.assertIsNone(holder.poll())
            self.assertEqual(result.returncode, 0)
            self.assertTrue(capture.unavailable)
            self.assertEqual(capture.records, (("stderr", "ETIMEDOUT", 1),))
        finally:
            if holder is not None:
                holder.stdin.close()
                try:
                    holder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
