"""Bounded private handoff results; no command output or real lifecycle actions."""

from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import errno
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, session_backups, tui_operations as operations
from oc_box.tui_notifications import failure_notice


@contextmanager
def channels(request):
    with (
        os.fdopen(
            os.memfd_create("oc-box-dashboard-operation", os.MFD_CLOEXEC), "w+b"
        ) as source,
        os.fdopen(
            os.memfd_create("oc-box-dashboard-result", os.MFD_CLOEXEC), "w+b"
        ) as result,
    ):
        source.write(json.dumps(request).encode())
        source.seek(0)
        yield source, result


class OperationResultTests(unittest.TestCase):
    def test_worker_thread_handoff_refuses_before_launch_without_changing_parent_signal(
        self,
    ):
        previous = signal.getsignal(signal.SIGINT)
        with (
            patch.object(operations.subprocess, "call") as launch,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(operations.handoff, box, {"version": 1})
            with self.assertRaises(operations.OperationError) as caught:
                future.result(timeout=3)
        self.assertEqual(caught.exception.reason, "host-terminal-required")
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        launch.assert_not_called()

    def test_foreground_child_interrupt_handler_restored_on_success_and_launch_failure(
        self,
    ):
        previous = signal.getsignal(signal.SIGINT)

        def child(*args, **kwargs):
            self.assertTrue(callable(signal.getsignal(signal.SIGINT)))
            self.assertNotEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGINT)
            return 0

        with patch.object(operations.subprocess, "call", side_effect=child):
            self.assertEqual(operations.handoff(box, {"version": 1}), 0)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        with patch.object(
            operations.subprocess,
            "call",
            side_effect=OSError("synthetic launch failure"),
        ):
            with self.assertRaises(OSError):
                operations.handoff(box, {"version": 1})
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_ctrl_c_allows_real_operation_child_cleanup_before_dashboard_resumes(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-interrupt-") as temporary:
            root = Path(temporary)
            child = root / "tui_operations.py"
            child.write_text("""import json,os,signal,sys,time
result=int(sys.argv[sys.argv.index("--result-fd")+1])
def cancelled(number,frame):
 time.sleep(0.65)
 notice={"title":"Synthetic cancellation","message":"Cleanup completed","severity":"information","timeout":8.0}
 os.write(result,json.dumps({"code":130,"notice":notice}).encode())
 print("CLEANUP_FINISHED",flush=True)
 raise SystemExit(130)
signal.signal(signal.SIGINT,cancelled)
signal.alarm(8)
print("READY",flush=True)
while True: signal.pause()
""")
            # CI imports from PYTHONPATH rather than an editable install. Give
            # this isolated test driver the exact subject package parent; the
            # production handoff and synthetic child retain their normal -I.
            controller = """import json,os,signal,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[2])
from oc_box import tui_operations as operations
root=Path(sys.argv[1])
runtime=SimpleNamespace(PACKAGE=root,state_root=lambda:root/"state",maintenance_host_env=lambda:{"PATH":os.defpath,"HOME":str(root)})
previous=signal.getsignal(signal.SIGINT)
notices=[]
code=operations.handoff(runtime,{"version":1},on_result=notices.append)
print(json.dumps({"code":code,"restored":signal.getsignal(signal.SIGINT) is previous,"cleanup_result":bool(notices) and notices[0].message=="Cleanup completed"}),flush=True)
"""
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    controller,
                    str(root),
                    str(Path(operations.__file__).resolve().parent.parent),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.defpath, "HOME": str(root)},
                start_new_session=True,
            )
            try:
                self.assertTrue(
                    select.select([process.stdout], [], [], 10)[0],
                    "synthetic child did not become ready",
                )
                self.assertEqual(process.stdout.readline(), b"READY\n")
                self.assertIsNone(process.poll())
                # This is only the new, isolated group launched by this test.
                os.killpg(process.pid, signal.SIGINT)
                output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
                lines = output.splitlines()
                self.assertIn(b"CLEANUP_FINISHED", lines)
                self.assertEqual(
                    json.loads(lines[-1]),
                    {"code": 130, "restored": True, "cleanup_result": True},
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def invoke_child(self, execute):
        output = io.StringIO()
        with (
            channels({"version": 1}) as (source, result),
            patch.object(operations, "_host_guard"),
            patch.object(operations, "execute", side_effect=execute),
            redirect_stdout(output),
        ):
            code = operations.main(
                [
                    "--request-fd",
                    str(source.fileno()),
                    "--result-fd",
                    str(result.fileno()),
                ]
            )
            notice = operations._result_notice(result, code)
        return code, notice, output.getvalue()

    def test_native_result_notice_crosses_anonymous_memory_only(self):
        notice = failure_notice("workspace-purge", reason="owner-active")
        received = []

        def child(command, **kwargs):
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            source, result = kwargs["pass_fds"]
            self.assertEqual(
                json.loads(os.pread(source, operations.MAX_REQUEST, 0)), {"version": 1}
            )
            self.assertNotIn(notice.message, command)
            operations._publish_result(result, 1, notice)
            return 1

        with patch.object(operations.subprocess, "call", side_effect=child):
            self.assertEqual(
                operations.handoff(box, {"version": 1}, on_result=received.append), 1
            )
        self.assertEqual(received, [notice])

    def test_missing_or_malformed_result_is_not_displayed_as_private_output(self):
        notice = asdict(failure_notice("delete", reason="read-only"))
        variants = [
            b"PRIVATE_NATIVE_OUTPUT",
            b"x" * (operations.MAX_RESULT + 1),
            json.dumps({"code": 0, "notice": notice}).encode(),
            json.dumps({"code": True, "notice": notice}).encode(),
            json.dumps(
                {"code": 1, "notice": {**notice, "private_extra": "PRIVATE"}}
            ).encode(),
            json.dumps(
                {"code": 1, "notice": {**notice, "severity": "PRIVATE"}}
            ).encode(),
            json.dumps(
                {"code": 1, "notice": {**notice, "message": "\x1bPRIVATE"}}
            ).encode(),
            json.dumps(
                {"code": 1, "notice": {**notice, "timeout": float("inf")}}
            ).encode(),
        ]
        for value in variants:
            with self.subTest(value_length=len(value)):
                self.assertIsNone(operations._result_notice(io.BytesIO(value), 1))

    def test_regular_files_cannot_be_used_as_operation_result_channels(self):
        with tempfile.TemporaryFile() as source:
            with self.assertRaises(operations.OperationError) as caught:
                operations._publish_result(source.fileno(), 1, failure_notice("delete"))
            self.assertEqual(caught.exception.reason, "invalid-request-descriptor")
            source.seek(0)
            self.assertEqual(source.read(), b"")

    def test_host_refusal_precedes_descriptor_or_target_inspection(self):
        with (
            patch.object(
                operations,
                "_host_guard",
                side_effect=operations.OperationError("host-only"),
            ),
            patch.object(operations, "_checked_memory_fd") as descriptors,
            patch.object(operations, "execute") as execute,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                operations.main(["--request-fd", "123", "--result-fd", "124"]), 1
            )
        descriptors.assert_not_called()
        execute.assert_not_called()

    def test_active_owner_has_specific_refusal_without_native_error_text(self):
        def execute(runtime, request, *, progress):
            progress(operation="workspace-purge", dispatched=True)
            raise box.BoxError(
                "This workspace already has an active oc-box process. Exit it before changing its environment."
            )

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 1)
        self.assertEqual(notice.title, "Workspace purge refused")
        self.assertIn("active owner", notice.message)
        self.assertNotIn("Traceback", output)

    def test_post_dispatch_permission_failure_preserves_cause_and_uncertainty(self):
        def execute(runtime, request, *, progress):
            progress(operation="workspace-purge", dispatched=True)
            raise PermissionError(errno.EACCES, "PRIVATE_ERROR", "PRIVATE_FILENAME")

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 1)
        self.assertIn("outcome unverified", notice.title)
        self.assertIn("permission", notice.message.casefold())
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn("PRIVATE", notice.message)

    def test_partial_restore_is_unverified_even_with_a_preflight_reason(self):
        def execute(runtime, request, *, progress):
            progress(operation="restore", dispatched=True, uncertain=True)
            raise session_backups.SessionBackupError("session-active")

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 1)
        self.assertEqual(notice.title, "Restore outcome unverified")
        self.assertIn("active", notice.message)

    def test_completed_operation_with_refresh_failure_is_not_reported_refused(self):
        def execute(runtime, request, *, progress):
            progress(operation="backup-file-delete", dispatched=True, completed=True)
            raise RuntimeError("PRIVATE_ERROR")

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 1)
        self.assertIn("completed; refresh unavailable", notice.title)
        self.assertNotIn("PRIVATE", output)

    def test_cancelled_native_review_has_informational_notice(self):
        def execute(runtime, request, *, progress):
            progress(operation="restore", cancelled=True)
            return 1

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 1)
        self.assertEqual(notice.title, "Restore cancelled")
        self.assertEqual(notice.severity, "information")

    def test_successful_child_produces_no_failure_notice(self):
        def execute(runtime, request, *, progress):
            progress(operation="backup", dispatched=True, completed=True)
            return 0

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 0)
        self.assertIsNone(notice)
        self.assertEqual(output, "")

    def test_interrupted_dispatched_operation_is_uncertain_not_silently_cancelled(self):
        def execute(runtime, request, *, progress):
            progress(operation="workspace-purge", dispatched=True)
            raise KeyboardInterrupt()

        code, notice, output = self.invoke_child(execute)
        self.assertEqual(code, 130)
        self.assertIn("outcome unverified", notice.title)


if __name__ == "__main__":
    unittest.main()
