"""Offline owner-control FD contracts, not host/Podman acceptance.

Real Python peers exercise the pipe and the real temporary-location helper.
They never run a fixture main, bypass a host guard, or invoke native/engine tools.
"""

import contextlib
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import test_sharing_podman as runner


CONTROL_ENV = "OC_BOX_SHARING_CONTROL_FD"
PRIVATE = "SYNTHETIC_PRIVATE_OWNER_OUTPUT_NOT_FOR_REPORTS"
ROOT = Path(__file__).resolve().parents[1]


class OwnerControlCase(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="sharing-control-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def enter(self, manager):
        return self.stack.enter_context(manager)

    def refused(self, category):
        return self.assertRaisesRegex(runner.Refusal, "^" + category + "$")

    @contextlib.contextmanager
    def pipe_fds(self):
        read_fd, write_fd = os.pipe()
        try:
            yield read_fd, write_fd
        finally:
            for fd in (read_fd, write_fd):
                try:
                    os.close(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise

    def command(self, script):
        return [sys.executable, "-I", "-B", "-u", "-c", script]

    def peer(self, script, *, env=None, require_terminal=False, expected_stop=False):
        # Importing these stdlib-only transport definitions does not run main.
        prefix = (
            "import os, sys\n"
            "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
            "from tools import test_sharing_podman as runner\n"
            "control = runner.owner_control()\n"
        )
        child = runner.PipeChild(
            self.command(prefix + script),
            {"PATH": os.defpath, "LANG": "C.UTF-8"} if env is None else env,
            self.parent,
            require_terminal=require_terminal,
            expected_stop=expected_stop,
        )
        self.addCleanup(child.close, 0.1)
        return child


class OwnerControlDescriptorTests(OwnerControlCase):
    def test_owner_control_consumes_fd_env_and_returns_private_text_writer(self):
        with self.pipe_fds() as (read_fd, write_fd):
            os.set_inheritable(write_fd, True)
            with patch.dict(os.environ, {CONTROL_ENV: str(write_fd)}):
                control = runner.owner_control()
                try:
                    self.assertNotIn(CONTROL_ENV, os.environ)
                    self.assertEqual(control.fileno(), write_fd)
                    self.assertTrue(stat.S_ISFIFO(os.fstat(write_fd).st_mode))
                    self.assertEqual(
                        fcntl.fcntl(write_fd, fcntl.F_GETFL) & os.O_ACCMODE,
                        os.O_WRONLY,
                    )
                    self.assertFalse(os.get_inheritable(write_fd))
                    self.assertTrue(
                        fcntl.fcntl(write_fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
                    )
                    control.write('{"ready":true}\n')
                    control.flush()
                    self.assertEqual(os.read(read_fd, 128), b'{"ready":true}\n')
                finally:
                    control.close()
            with self.assertRaises(OSError) as raised:
                os.fstat(write_fd)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_missing_bad_and_standard_descriptor_values_refuse_without_output(self):
        for value in (
            None,
            "",
            PRIVATE,
            "3.5",
            "-1",
            "0",
            "1",
            "2",
            "9999999999",
            "9" * 100,
        ):
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {}, clear=True),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                if value is not None:
                    os.environ[CONTROL_ENV] = value
                with self.refused("owner-control-protocol") as raised:
                    runner.owner_control()
                self.assertIn(str(raised.exception), runner.CATEGORIES)
                self.assertNotIn(PRIVATE, str(raised.exception))
                self.assertNotIn(CONTROL_ENV, os.environ)
            self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_closed_regular_and_read_only_pipe_descriptors_are_refused(self):
        regular = os.memfd_create("sharing-control-regular-test", os.MFD_CLOEXEC)
        self.addCleanup(os.close, regular)
        with self.pipe_fds() as (read_fd, _write_fd):
            closed = os.dup(regular)
            os.close(closed)
            for kind, fd in (
                ("closed", closed),
                ("regular", regular),
                ("read-only-pipe", read_fd),
            ):
                with (
                    self.subTest(kind=kind),
                    patch.dict(os.environ, {CONTROL_ENV: str(fd)}),
                ):
                    with self.refused("owner-control-protocol"):
                        runner.owner_control()
                    self.assertNotIn(CONTROL_ENV, os.environ)

    def test_read_write_fifo_is_not_a_write_only_control_descriptor(self):
        fifo = self.parent / "read-write-fifo"
        os.mkfifo(fifo, 0o600)
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        with (
            patch.dict(os.environ, {CONTROL_ENV: str(descriptor)}),
            self.refused("owner-control-protocol"),
        ):
            runner.owner_control()

    def test_control_descriptor_is_not_inherited_by_actual_exec(self):
        script = (
            "import errno, os, sys\n"
            "try:\n"
            "    os.fstat(int(sys.argv[1]))\n"
            "except OSError as error:\n"
            "    sys.exit(0 if error.errno == errno.EBADF else 2)\n"
            "sys.exit(1)\n"
        )
        with self.pipe_fds() as (_read_fd, write_fd):
            os.set_inheritable(write_fd, True)
            with patch.dict(os.environ, {CONTROL_ENV: str(write_fd)}):
                control = runner.owner_control()
                try:
                    result = subprocess.run(
                        [*self.command(script), str(write_fd)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        # Do not let Popen's default close_fds mask a missing
                        # CLOEXEC on the descriptor returned by owner_control.
                        close_fds=False,
                        env={"PATH": os.defpath},
                        timeout=5,
                    )
                finally:
                    control.close()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, b"")

    def test_owner_stage_refuses_private_values_before_writing(self):
        control = io.StringIO()
        with self.refused("owner-control-protocol"):
            runner.owner_stage(control, PRIVATE)
        self.assertEqual(control.getvalue(), "")


class OwnerControlTransportTests(OwnerControlCase):
    def test_child_timeout_frame_cannot_swallow_the_local_drain_deadline(self):
        child = self.peer(
            "print('{\"ready\":true}',file=control,flush=True)\n"
            'print(\'{"error":"owner-control-timeout","failure_trace":[]}\',file=control,flush=True)\n'
            "sys.stdin.buffer.read()\n"
        )
        child.ready(timeout=5)

        def expired(*_):
            raise AssertionError("Control drain ignored its bounded deadline")

        old_handler = signal.signal(signal.SIGALRM, expired)
        old_timer = signal.setitimer(signal.ITIMER_REAL, 1)
        started = time.monotonic()
        try:
            with self.refused("owner-control-timeout") as refusal:
                child.drain(timeout=0.05)
            self.assertIs(type(refusal.exception), runner.Refusal)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0]:
                signal.setitimer(
                    signal.ITIMER_REAL,
                    max(0.001, old_timer[0] - (time.monotonic() - started)),
                    old_timer[1],
                )
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(child.child_failure["error"], "owner-control-timeout")
        child.finish(timeout=5)

    def test_forced_termination_fact_survives_missing_terminal_validation(self):
        child = runner.PipeChild.__new__(runner.PipeChild)
        child.close = Mock()
        child.forced = True
        child.require_terminal = True
        child.child_failure = child.control_category = child.terminal = None
        child.process = SimpleNamespace(returncode=-signal.SIGTERM)
        harness = runner.Harness.__new__(runner.Harness)
        harness.owners, harness.shares = [child], []
        harness.secondary, harness.expected_inputs = [], {}
        harness.close()
        child.close.assert_called_once_with()
        self.assertEqual(
            harness.secondary, ["forced-owner-termination", "owner-control-protocol"]
        )

    def test_finish_drains_terminal_stages_after_readiness_and_actual_exit(self):
        child = self.peer(
            "print('{\"ready\":true}',file=control,flush=True)\n"
            "sys.stdin.buffer.read()\n"
            "runner.owner_stage(control,'owner-finished')\n"
            "runner.owner_stage(control,'source-recheck')\n"
            'print(\'{"owner_exit":0,"intentional_stop":false}\',file=control,flush=True)\n',
            require_terminal=True,
        )
        child.ready(timeout=5)
        self.assertEqual(child.finish(timeout=5), 0)
        self.assertEqual(child.last_stage, "source-recheck")
        self.assertEqual(child.terminal, {"owner_exit": 0, "intentional_stop": False})
        self.assertIsNone(child.child_failure)

    def test_requested_stop_one_requires_consistent_explicit_terminal_event(self):
        child = self.peer(
            "print('{\"ready\":true}',file=control,flush=True)\n"
            "sys.stdin.buffer.read()\n"
            "runner.owner_stage(control,'source-recheck')\n"
            'print(\'{"owner_exit":1,"intentional_stop":true}\',file=control,flush=True)\n'
            "sys.exit(1)\n",
            require_terminal=True,
            expected_stop=True,
        )
        child.ready(timeout=5)
        self.assertEqual(child.finish(timeout=5), 1)
        child.validate_terminal()
        self.assertIsNone(child.child_failure)

    def test_unexpected_post_readiness_error_one_is_not_an_allowed_stop(self):
        for expected_stop in (False, True):
            with self.subTest(expected_stop=expected_stop):
                child = self.peer(
                    "print('{\"ready\":true}',file=control,flush=True)\n"
                    "sys.stdin.buffer.read()\n"
                    "runner.owner_stage(control,'owner-wait')\n"
                    'print(\'{"error":"private-store","failure_trace":[]}\',file=control,flush=True)\n'
                    "sys.exit(1)\n",
                    require_terminal=True,
                    expected_stop=expected_stop,
                )
                child.ready(timeout=5)
                with self.refused("private-store"):
                    child.finish(timeout=5)
                self.assertEqual(child.last_stage, "owner-wait")
                self.assertEqual(child.child_failure["error"], "private-store")
                with self.refused("private-store"):
                    child.validate_terminal()

    def test_missing_inconsistent_or_unrequested_terminal_prevents_acceptance(self):
        cases = (
            (None, 0, False),
            ({"owner_exit": 1, "intentional_stop": True}, 0, True),
            ({"owner_exit": 1, "intentional_stop": True}, 1, False),
        )
        for terminal, exit_code, expected_stop in cases:
            with self.subTest(terminal=terminal, exit=exit_code, stop=expected_stop):
                event = (
                    ""
                    if terminal is None
                    else "print("
                    + repr(json.dumps(terminal))
                    + ",file=control,flush=True)\n"
                )
                child = self.peer(
                    "print('{\"ready\":true}',file=control,flush=True)\n"
                    "sys.stdin.buffer.read()\n"
                    + event
                    + "sys.exit("
                    + str(exit_code)
                    + ")\n",
                    require_terminal=True,
                    expected_stop=expected_stop,
                )
                child.ready(timeout=5)
                with self.refused("owner-control-protocol"):
                    child.finish(timeout=5)

    def test_close_retains_post_readiness_failure_without_exposing_output(self):
        child = self.peer(
            "print('{\"ready\":true}',file=control,flush=True)\n"
            "sys.stdin.buffer.read()\n"
            "runner.owner_stage(control,'owner-wait')\n"
            'print(\'{"error":"owner-exit","failure_trace":[]}\',file=control,flush=True)\n'
            "print(" + repr(PRIVATE) + ",file=sys.stderr,flush=True)\n"
            "sys.exit(1)\n",
            require_terminal=True,
        )
        child.ready(timeout=5)
        child.close(timeout=5)
        self.assertEqual(child.last_stage, "owner-wait")
        self.assertEqual(child.child_failure["error"], "owner-exit")
        self.assertTrue(child.control.closed)
        with self.refused("owner-exit"):
            child.validate_terminal()
        self.assertNotIn(PRIVATE, json.dumps(child.diagnostic()))

    def test_fixed_progress_frames_are_consumed_before_ready_and_wait_messages(self):
        stages = sorted(runner.OWNER_STAGES)
        self.assertTrue(stages)
        script = (
            "import json\n"
            "runner.owner_stage(control, " + repr(stages[0]) + ")\n"
            "control.write('{\"ready\":true}\\n')\n"
            "control.flush()\n"
            "for stage in " + repr(stages) + ":\n"
            "    runner.owner_stage(control, stage)\n"
            "    print(json.dumps({'wait_clients': 0}), file=control, flush=True)\n"
            "for count in [1, 5]:\n"
            "    print(json.dumps({'wait_clients': count}), file=control, flush=True)\n"
            "sys.stdin.buffer.read()\n"
        )
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.peer(script)
            child.ready(timeout=5)
            self.assertEqual(child.last_stage, stages[0])
            for stage in stages:
                self.assertEqual(child.message(timeout=5), {"wait_clients": 0})
                self.assertEqual(child.last_stage, stage)
            for count in (1, 5):
                self.assertEqual(child.message(timeout=5), {"wait_clients": count})
            child.finish(timeout=5)
        self.assertIsNone(child.child_failure)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_real_temporary_location_stdout_diagnostic_cannot_corrupt_control(self):
        script = (
            "from pathlib import Path\n"
            "from tools.test_v2_podman import temporary_location\n"
            "with temporary_location(Path.cwd()):\n"
            "    control.write('{\"ready\":true}\\n')\n"
            "    control.flush()\n"
            "    sys.stdin.buffer.read()\n"
        )
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.peer(script)
            child.ready(timeout=5)
            child.finish(timeout=5)
        self.assertIsNone(child.process.stdout)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        child.errors.seek(0)
        diagnostic = child.errors.read()
        self.assertIn(
            b"oc-box fixture: Temporary image and encrypted staging directory: ",
            diagnostic,
        )
        self.assertIn(str(self.parent / "temporary").encode(), diagnostic)
        self.assertNotIn(b'"ready"', diagnostic)
        self.assertEqual(child.process.returncode, 0)

    def test_fragmented_control_frame_is_reassembled_from_real_pipe_reads(self):
        child = self.peer(
            "for octet in b'{\"ready\":true}\\n':\n"
            "    os.write(control.fileno(), bytes([octet]))\n"
            "sys.stdin.buffer.read()\n"
        )
        original_read = os.read

        def fragment(fd, count):
            return original_read(fd, min(count, 1))

        with patch.object(runner.os, "read", side_effect=fragment) as reads:
            child.ready(timeout=5)
        self.assertGreaterEqual(reads.call_count, len(b'{"ready":true}\n'))
        child.finish(timeout=5)

    def test_stdout_json_is_never_a_fallback_readiness_lane(self):
        child = self.peer(
            "print('{\"ready\":true}', flush=True)\n"
            "control.close()\n"
            "sys.stdin.buffer.read()\n"
        )
        with self.refused("owner-control-eof"):
            child.ready(timeout=5)
        self.assertIsNone(child.process.stdout)
        child.finish(timeout=5)

    def test_malformed_machine_json_is_a_fixed_refusal_without_private_output(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.peer(
                "print(" + repr(PRIVATE) + ", flush=True)\n"
                "print(" + repr(PRIVATE) + ", file=sys.stderr, flush=True)\n"
                "control.write(" + repr(PRIVATE + "\n") + ")\n"
                "control.flush()\n"
                "sys.stdin.buffer.read()\n"
            )
            with self.refused("owner-control-protocol") as raised:
                child.ready(timeout=5)
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        child.errors.seek(0)
        self.assertIn(PRIVATE.encode(), child.errors.read())
        child.finish(timeout=5)

    def test_exited_child_buffered_error_frame_retains_only_safe_failure_fields(self):
        failing_code = (
            "def bootstrap():\n    raise RuntimeError("
            + repr(PRIVATE)
            + ")\nbootstrap()\n"
        )
        script = (
            "import json\n"
            "runner.owner_stage(control, 'owner-launch')\n"
            "print('{\"wait_clients\":0}', file=control, flush=True)\n"
            "try:\n"
            "    exec(compile("
            + repr(failing_code)
            + ", "
            + repr("/synthetic/" + PRIVATE + "/owner_bootstrap.py")
            + ", 'exec'))\n"
            "except RuntimeError as cause:\n"
            "    try:\n"
            "        raise runner.Refusal('private-store') from cause\n"
            "    except runner.Refusal as error:\n"
            "        print(json.dumps({'error': runner.category(error),\n"
            "                          'failure_trace': runner.failure_trace(error)}),\n"
            "              file=control, flush=True)\n"
            "print(" + repr(PRIVATE) + ", flush=True)\n"
            "print(" + repr(PRIVATE) + ", file=sys.stderr, flush=True)\n"
            "sys.exit(1)\n"
        )
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            child = self.peer(script)
            self.assertEqual(child.process.wait(timeout=5), 1)
            with self.refused("private-store") as raised:
                child.ready(timeout=5)
        self.assertEqual(child.last_stage, "owner-launch")
        self.assertEqual(child.premature_wait_clients, 0)
        self.assertEqual(child.control_category, "private-store")
        self.assertEqual(set(child.child_failure), {"error", "failure_trace"})
        self.assertEqual(child.child_failure["error"], "private-store")
        trace = child.child_failure["failure_trace"]
        self.assertTrue(runner.valid_trace(trace))
        self.assertEqual([item["type"] for item in trace], ["Refusal", "RuntimeError"])
        self.assertIn("owner_bootstrap.py:bootstrap:2", trace[1]["frames"])
        self.assertIn("owner_bootstrap.py:<module>:3", trace[1]["frames"])
        diagnostic = json.dumps(child.diagnostic())
        self.assertNotIn(PRIVATE, diagnostic)
        self.assertNotIn("/synthetic/", diagnostic)
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        child.errors.seek(0)
        self.assertEqual(child.errors.read().count(PRIVATE.encode()), 2)

    def test_invalid_stage_and_failure_metadata_refuse_without_retaining_private_fields(
        self,
    ):
        valid = {"error": "private-store", "failure_trace": []}
        cases = [
            {"stage": PRIVATE},
            {"stage": None},
            {"stage": []},
            {"error": PRIVATE, "failure_trace": []},
            {"error": "private-store", "failure_trace": PRIVATE},
            {"error": "private-store", "failure_trace": [{"type": PRIVATE}]},
            {
                "error": "private-store",
                "failure_trace": [{"type": "RuntimeError", "frames": [PRIVATE]}],
            },
            {
                "error": "private-store",
                "failure_trace": [
                    {"type": "RuntimeError", "frames": [], "message": PRIVATE}
                ],
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                child = self.peer(
                    "control.write(" + repr(json.dumps(payload) + "\n") + ")\n"
                    "control.flush()\nsys.stdin.buffer.read()\n"
                )
                with self.refused("owner-control-protocol"):
                    child.message(timeout=5)
                self.assertIsNone(child.child_failure)
                self.assertNotIn(PRIVATE, json.dumps(child.diagnostic()))
                child.finish(timeout=5)
        # A fixed category with an empty trace is legitimate when no safe Python
        # code frame exists (for example a native failure converted to Refusal).
        child = self.peer(
            "control.write(" + repr(json.dumps(valid) + "\n") + ")\n"
            "control.flush()\nsys.stdin.buffer.read()\n"
        )
        with self.refused("private-store"):
            child.message(timeout=5)
        self.assertEqual(child.child_failure, valid)
        child.finish(timeout=5)

    def test_wait_client_messages_require_an_exact_nonnegative_integer_shape(self):
        for payload in (
            {"wait_clients": -1},
            {"wait_clients": True},
            {"wait_clients": 1.0},
            {"wait_clients": "1"},
            {"wait_clients": None},
            {"wait_clients": 1, "extra": PRIVATE},
            {"unknown": PRIVATE},
        ):
            with self.subTest(payload=payload):
                child = self.peer(
                    "control.write(" + repr(json.dumps(payload) + "\n") + ")\n"
                    "control.flush()\nsys.stdin.buffer.read()\n"
                )
                with self.refused("owner-control-protocol"):
                    child.message(timeout=5)
                self.assertNotIn(PRIVATE, json.dumps(child.diagnostic()))
                child.finish(timeout=5)

    def test_control_eof_and_live_peer_timeout_have_distinct_fixed_categories(self):
        eof = self.peer("control.close()\nsys.stdin.buffer.read()\n")
        with self.refused("owner-control-eof"):
            eof.ready(timeout=5)
        eof.finish(timeout=5)
        waiting = self.peer("sys.stdin.buffer.read()\n")
        with self.refused("owner-control-timeout"):
            waiting.ready(timeout=0.1)
        waiting.finish(timeout=5)

    def test_parent_env_is_copied_and_normal_stdin_eof_never_forces_termination(self):
        env = {"PATH": os.defpath, CONTROL_ENV: PRIVATE}
        child = self.peer(
            "assert 'OC_BOX_SHARING_CONTROL_FD' not in os.environ\n"
            "control.write('{\"ready\":true}\\n')\n"
            "control.flush()\n"
            "assert sys.stdin.buffer.read() == b''\n",
            env=env,
        )
        self.assertEqual(env, {"PATH": os.defpath, CONTROL_ENV: PRIVATE})
        child.ready(timeout=5)
        child.finish(timeout=5)
        with patch.object(runner.os, "killpg") as signals:
            child.close(0.1)
        signals.assert_not_called()
        self.assertEqual(child.process.returncode, 0)
        self.assertFalse(getattr(child, "forced", False))
        self.assertTrue(child.process.stdin.closed)
        self.assertIsNone(child.process.stdout)
        self.assertTrue(child.control.closed)
        self.assertTrue(child.errors.closed)

    def test_failed_popen_closes_both_control_pipe_ends_and_private_memfd(self):
        original_pipe, original_memfd = os.pipe, os.memfd_create
        pipe_fds, memfds = [], []
        env = {"PATH": os.defpath}

        def pipe():
            pair = original_pipe()
            pipe_fds.extend(pair)
            return pair

        def memfd(*args, **kwargs):
            descriptor = original_memfd(*args, **kwargs)
            memfds.append(descriptor)
            return descriptor

        def fail_spawn(*args, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.PIPE)
            self.assertIs(kwargs["stdout"], kwargs["stderr"])
            self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["pass_fds"], (pipe_fds[1],))
            self.assertEqual(kwargs["env"][CONTROL_ENV], str(pipe_fds[1]))
            self.assertTrue(kwargs["start_new_session"])
            raise OSError(errno.EACCES, PRIVATE)

        with (
            patch.object(runner.os, "pipe", side_effect=pipe),
            patch.object(runner.os, "memfd_create", side_effect=memfd),
            patch.object(runner.subprocess, "Popen", side_effect=fail_spawn) as spawn,
            self.assertRaises(OSError),
        ):
            runner.PipeChild(self.command("pass"), env, self.parent)
        spawn.assert_called_once()
        self.assertEqual(env, {"PATH": os.defpath})
        self.assertEqual(len(pipe_fds), 2)
        self.assertEqual(len(memfds), 1)
        for fd in (*pipe_fds, *memfds):
            with self.subTest(fd=fd), self.assertRaises(OSError) as raised:
                os.fstat(fd)
            self.assertEqual(raised.exception.errno, errno.EBADF)


if __name__ == "__main__":
    unittest.main()
