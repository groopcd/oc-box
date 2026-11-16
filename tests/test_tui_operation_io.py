"""Synthetic private-process/PTY checks, not native or Podman acceptance."""

import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import pty
import signal
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    oc_box as box,
    private_memory,
    tui_operation_io as channel,
    tui_operations,
)

VALUE = "synthetic-Private-猫-0987"
SOURCE = Path(channel.__file__).resolve().parent.parent


FIXTURE = r'''
import errno,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,SOURCE)
from oc_box import backup_crypto,secret_prompt,tui_operation_io as channel,tui_operations
fds=tuple(int(v) for v in sys.argv[2::2])
def execute(runtime,request,*,progress,interaction):
 scenario=request["scenario"]
 progress(operation="backup")
 assert all(not os.get_inheritable(fd) for fd in fds[1:])
 try: os.fstat(fds[0])
 except OSError as error: assert error.errno==errno.EBADF
 else: raise AssertionError("request descriptor not closed")
 command="import os,sys; fds="+repr(fds[1:])+"; "+"""
for fd in fds:
 try: os.fstat(fd)
 except OSError: pass
 else: sys.exit(71)
"""
 assert subprocess.run([sys.executable,"-I","-B","-c",command],close_fds=False).returncode==0
 if scenario in ("age","age-wrong"):
  private,_=backup_crypto.generate_key([],Path(request["archive"]),minimum=8)
  original=backup_crypto.small
  def no_direct_age(*args,**kwargs):
   assert not kwargs.get("tty_errors"),"embedded unlock fell back to unobserved age"
   return original(*args,**kwargs)
  backup_crypto.small=no_direct_age
  assert backup_crypto.load_key(Path(request["archive"]),[])==private
 elif scenario=="prompt":
  assert secret_prompt.new_passphrase("backup",8)==VALUE
  assert interaction.review({"format":"session","session_count":1})
 elif scenario=="short-unlock":
  assert secret_prompt.read_secret("Backup passphrase: ",purpose="backup-unlock")=="x"
 elif scenario=="zero":
  return 0
 elif scenario=="slow":
  progress(dispatched=True)
  try:
   interaction.stage("image")
   while True: time.sleep(0.1)
  finally:
   interaction.stage("cleanup")
   time.sleep(0.35)
   interaction.inspection({"format":"session","session_count":0})
 else:
  raise RuntimeError("PRIVATE_RAW_ERROR")
 interaction.inspection({"format":"session","session_count":1})
 progress(completed=True)
 return 0
sys.exit(channel.child(None,fds,execute=execute,check_request=tui_operations._checked_memory_fd))
'''


class PrivateProtocolTests(unittest.TestCase):
    def test_fragmented_frames_duplicate_keys_limits_and_no_nonfinite_json(self):
        frame = channel._frame({"version": 1, "id": 2, "approved": True})
        parser = channel.Frames()
        result = []
        for byte in frame:
            result.extend(parser.feed(bytes([byte])))
        parser.end()
        self.assertEqual(result, [{"version": 1, "id": 2, "approved": True}])
        for content in (b'{"id":1,"id":2}', b'{"a":NaN}', b"[]", b"no"):
            with (
                self.subTest(content=content),
                self.assertRaises(channel.OperationIOError),
            ):
                channel.Frames().feed(struct.pack("!I", len(content)) + content)
        with self.assertRaises(channel.OperationIOError):
            channel.Frames().feed(struct.pack("!I", channel.MAX_FRAME + 1))
        with self.assertRaises(channel.OperationIOError):
            parser = channel.Frames()
            parser.feed(frame[:-1])
            parser.end()

    def test_full_event_pipe_write_is_nonblocking_and_bounded(self):
        read, write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            while True:
                try:
                    os.write(write, b"x" * 4096)
                except BlockingIOError:
                    break
            start = time.monotonic()
            with self.assertRaises(channel.OperationIOError):
                channel._write(write, {"version": 1})
            self.assertLess(time.monotonic() - start, 1)
        finally:
            os.close(read)
            os.close(write)

    def test_pipe_access_directions_and_same_underlying_object_are_not_capabilities(
        self,
    ):
        read, write = os.pipe2(os.O_CLOEXEC)
        try:
            with self.assertRaises(channel.OperationIOError):
                channel._pipe(read, os.O_WRONLY)
            self.assertEqual(
                channel._pipe(read, os.O_RDONLY), channel._pipe(write, os.O_WRONLY)
            )
        finally:
            os.close(read)
            os.close(write)

    def test_child_rejects_underlying_pipe_alias_before_target_or_tty_setup(self):
        source = private_memory.create("oc-box-dashboard-operation")
        read, write = os.pipe2(os.O_CLOEXEC)
        master, slave = pty.openpty()
        try:
            with patch.object(tui_operations, "_checked_memory_fd") as check:
                with self.assertRaises(channel.OperationIOError):
                    channel.child(
                        None,
                        (source, write, read, slave),
                        execute=Mock(),
                        check_request=check,
                    )
                check.assert_not_called()
        finally:
            for fd in (source, read, write, master, slave):
                os.close(fd)

    def test_metadata_projection_rejects_unknown_raw_control_and_unbounded_values(self):
        for extra in (
            {"raw": "PRIVATE"},
            {"project": "private\x1b[31m"},
            {"project": "x" * 4097},
            {"workspace_id": "wrong"},
            {"session_count": True},
            {"planned_ids": ["existing-foreign-id"]},
        ):
            with (
                self.subTest(keys=extra.keys()),
                self.assertRaises(channel.OperationIOError),
            ):
                channel.metadata({"format": "session", **extra})

    def test_prompt_and_review_ids_are_monotonic_and_success_needs_completion(self):
        process = channel.OperationProcess(None, {})

        def event(kind, **fields):
            return {"version": 1, "seq": process._sequence + 1, "kind": kind, **fields}

        process._accept(event("started"))
        with self.assertRaises(channel.OperationIOError):
            process._accept(event("prompt", id=2, purpose="backup-new", maximum=1024))
        process = channel.OperationProcess(None, {})
        process._accept({"version": 1, "seq": 1, "kind": "started"})
        with self.assertRaises(channel.OperationIOError):
            process._accept(
                event(
                    "result",
                    code=0,
                    notice=None,
                    dispatched=True,
                    completed=False,
                    cancelled=False,
                    uncertain=False,
                )
            )

    def test_embedded_production_host_guard_precedes_all_descriptor_access(self):
        with (
            patch.object(
                tui_operations,
                "_host_guard",
                side_effect=tui_operations.OperationError("host-only"),
            ),
            patch.object(channel, "child") as child,
            patch("sys.stdout"),
        ):
            self.assertEqual(
                tui_operations.main(
                    [
                        "--request-fd",
                        "10",
                        "--event-fd",
                        "11",
                        "--control-fd",
                        "12",
                        "--tty-fd",
                        "13",
                    ]
                ),
                1,
            )
        child.assert_not_called()


class PrivateProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-private-operation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        code = FIXTURE.replace("SOURCE", repr(str(SOURCE))).replace(
            "VALUE", repr(VALUE)
        )
        (self.root / "tui_operations.py").write_text(code)
        self.runtime = SimpleNamespace(
            PACKAGE=self.root,
            state_root=lambda: self.root / "state",
            maintenance_host_env=lambda: {
                "PATH": os.environ["PATH"],
                "HOME": str(self.root),
            },
        )

    def launch(self, scenario, **kwargs):
        process = channel.OperationProcess(
            self.runtime, {"scenario": scenario, **kwargs}
        )
        process.start()
        # A broken test must not leave a synthetic child behind.
        self.addCleanup(self.finish_process, process)
        return process

    def finish_process(self, process):
        if process.process.poll() is None:
            process.cancel()
            process.process.wait(timeout=10)
        process.close()

    def test_original_controlling_tty_editor_complete_submission_and_explicit_review(
        self,
    ):
        process = self.launch("prompt")
        events = []

        def received(event):
            events.append(event)
            if event["kind"] == "prompt":
                content = bytearray(VALUE.encode())
                process.submit_secret(event["id"], content)
                self.assertEqual(content, bytearray())
            elif event["kind"] == "review":
                process.approve(event["id"], True)

        result = process.run(received)
        self.assertEqual(result["code"], 0)
        self.assertTrue(result["completed"])
        self.assertEqual(
            [e["purpose"] for e in events if e["kind"] == "prompt"],
            ["backup-new", "backup-confirm"],
        )
        self.assertNotIn(VALUE, json.dumps(events))
        self.assertEqual(resource.getrlimit(resource.RLIMIT_CORE)[0], 0)

    def test_existing_short_unlock_is_not_new_password_policy(self):
        process = self.launch("short-unlock")
        result = process.run(
            lambda e: (
                process.submit_secret(e["id"], bytearray(b"x"))
                if e["kind"] == "prompt"
                else None
            )
        )
        self.assertEqual(result["code"], 0)

    @unittest.skipUnless(Path("/usr/bin/age").exists(), "age unavailable")
    def test_real_age_default_key_envelope_with_embedded_unlock_no_identity_file(self):
        archive = self.root / "key.age"
        process = self.launch("age", archive=str(archive))
        prompts = []

        def received(event):
            if event["kind"] == "prompt":
                prompts.append(event["purpose"])
                process.submit_secret(event["id"], bytearray(VALUE.encode()))

        result = process.run(received)
        self.assertEqual(result["code"], 0)
        self.assertEqual(prompts, ["backup-new", "backup-confirm", "backup-unlock"])
        self.assertNotIn(VALUE.encode(), archive.read_bytes())
        self.assertNotIn(b"AGE-SECRET-KEY-1", archive.read_bytes())
        self.assertEqual(
            {p.name for p in self.root.iterdir()}, {"tui_operations.py", "key.age"}
        )

    def test_embedded_wrong_password_and_unlock_cancel_never_fall_back_or_succeed(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                archive = self.root / ("cancel.age" if cancel else "wrong.age")
                process = self.launch("age-wrong", archive=str(archive))
                events = []

                def received(event):
                    events.append(event)
                    if event["kind"] == "prompt":
                        if event["purpose"] == "backup-unlock":
                            if cancel:
                                process.cancel()
                                return
                            content = "synthetic-Wrong-Password"
                        else:
                            content = VALUE
                        process.submit_secret(event["id"], bytearray(content.encode()))

                result = process.run(received)
                self.assertNotEqual(result["code"], 0)
                self.assertNotIn(VALUE, json.dumps(events))
                self.assertNotIn("synthetic-Wrong-Password", json.dumps(events))

    def test_repeat_cancel_waits_for_cleanup_without_second_interrupt(self):
        process = self.launch("slow")
        events = []
        start = time.monotonic()

        def received(event):
            events.append(event)
            if event["kind"] == "stage":
                process.cancel()
                process.cancel()

        result = process.run(received)
        self.assertGreaterEqual(time.monotonic() - start, 0.35)
        self.assertEqual(result["code"], 130)
        self.assertTrue(result["uncertain"])
        self.assertIn("inspection", [event["kind"] for event in events])

    def test_parent_control_eof_interrupts_blocked_engine_and_waits_for_cleanup(self):
        process = self.launch("slow")

        def received(event):
            if event["kind"] == "stage" and event["stage"] == "image":
                os.close(process.controls)
                process.controls = None
            elif event["kind"] == "stage" and event["stage"] == "cleanup":
                process.cancel()  # a second path cannot interrupt child finally

        result = process.run(received)
        self.assertEqual(result["code"], 130)
        self.assertTrue(result["cancelled"])

    def test_zero_without_completed_acknowledgement_and_private_error_fail_closed(self):
        for scenario in ("zero", "error"):
            with self.subTest(scenario=scenario):
                process = self.launch(scenario)
                events = []
                result = process.run(events.append)
                self.assertNotEqual(result["code"], 0)
                self.assertNotIn("PRIVATE_RAW_ERROR", json.dumps(events))

    def test_production_embedded_child_still_refuses_container_without_guard_bypass(
        self,
    ):
        if not (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or Path("/run/.containerenv").exists()
            or Path("/.dockerenv").exists()
            or os.geteuid() == 0
        ):
            self.skipTest("requires a real production host refusal environment")
        process = channel.OperationProcess(box, {"version": 1})
        process.start()
        self.addCleanup(self.finish_process, process)
        with self.assertRaises(channel.OperationIOError):
            process.run(lambda _: self.fail("guard refusal emitted an operation event"))


if __name__ == "__main__":
    unittest.main()
