"""Verify CLI reporting integration without starting an actual workspace."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oc_box import (
    activity_log,
    history_recovery,
    host_drop,
    journal_output,
    lifecycle_output,
    log_config,
)
from oc_box import oc_box as box

WORKSPACE = "a" * 32


class LifecycleOutputTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory(prefix="oc-box-lifecycle-output-")
        self.addCleanup(area.cleanup)
        self.root = Path(area.name) / "state"
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_CONTAINER": "0",
                "OC_BOX_STATE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(Path(area.name) / "config"),
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def saved_logs(self, **settings):
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "oc-box/config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"logs": settings}))
        return config

    def test_preparation_output_is_quiet_by_default_and_live_with_verbose_all(self):
        driver = """
import sys
from pathlib import Path
from oc_box import lifecycle_output, oc_box as box
with lifecycle_output.invocation("reset", Path(sys.argv[1]), console_level=sys.argv[2] or None):
    lifecycle_output.bind_workspace(sys.argv[3])
    box.run(
        [sys.executable, "-c", "import os, sys; assert sys.stdin.read() == 'setup input'; "
         "os.write(1, b'SYNTHETIC_STDOUT\\\\n'); os.write(2, b'SYNTHETIC_STDERR\\\\n')"],
        input="setup input",
        preparation="baseline-tools",
    )
    box.run([sys.executable, "-c", "print('NATIVE_OUTPUT_UNCHANGED')"])
"""
        repo = Path(__file__).resolve().parents[1]
        for level in ("", "debug", "all"):
            with self.subTest(level=level):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        driver,
                        str(self.root),
                        level,
                        WORKSPACE,
                    ],
                    env={**os.environ, "PYTHONPATH": str(repo / "src")},
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                self.assertIn("NATIVE_OUTPUT_UNCHANGED", result.stdout)
                self.assertEqual("SYNTHETIC_STDOUT" in result.stdout, level == "all")
                self.assertEqual("SYNTHETIC_STDERR" in result.stderr, level == "all")
                self.assertEqual(
                    "Setting up container runtime tools (details: oc-box logs --tail)"
                    in result.stderr,
                    level != "all",
                )
        events = activity_log.read_events(self.root, preparation_only=True)
        self.assertEqual(
            [(row["event"], row.get("outcome"), row["workspace"]) for row in events],
            [
                ("preparation.started", None, WORKSPACE),
                ("preparation.finished", "success", WORKSPACE),
            ]
            * 3,
        )
        payload = (self.root / "log/preparation/activity.jsonl").read_text()
        self.assertNotIn("SYNTHETIC_", payload)
        self.assertNotIn("NATIVE_OUTPUT", payload)
        self.assertNotIn("setup input", payload)

    def test_preparation_start_is_readable_before_child_exit_and_refusals_stay_fixed(
        self,
    ):
        command = ["synthetic"]
        for fault, exception, reason, outcome in (
            (subprocess.TimeoutExpired(command, 2), box.BoxError, "timeout", "failure"),
            (KeyboardInterrupt(), KeyboardInterrupt, None, "cancelled"),
            (None, box.BoxError, None, "failure"),
        ):
            with self.subTest(outcome=outcome, reason=reason):

                def child(_args, **kwargs):
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
                    self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                    rows = activity_log.read_events(self.root, preparation_only=True)
                    self.assertEqual(rows[-1]["event"], "preparation.started")
                    if fault is not None:
                        raise fault
                    return subprocess.CompletedProcess(command, 7)

                stderr = io.StringIO()
                with (
                    lifecycle_output.invocation("start", self.root),
                    contextlib.redirect_stderr(stderr),
                    patch.object(box.subprocess, "run", side_effect=child),
                    self.assertRaises(exception) as result,
                ):
                    box.run(command, preparation="container-image", timeout=2)
                self.assertIn(
                    "Creating container and pulling its image if needed",
                    stderr.getvalue(),
                )
                rows = activity_log.read_events(self.root, preparation_only=True)
                self.assertEqual(rows[-1]["outcome"], outcome)
                self.assertEqual(rows[-1].get("reason"), reason)
                self.assertEqual(
                    rows[-1].get("exit_code"), 7 if fault is None else None
                )
                if exception is box.BoxError:
                    self.assertIn("oc-box logs --tail", str(result.exception))

    def test_opt_in_npm_detail_keeps_only_recognized_codes_and_counts(self):
        command = [
            sys.executable,
            "-c",
            (
                "import os; "
                "os.write(1, b'PRIVATE_SYNTHETIC_STDOUT\\n'); "
                "os.write(2, b'npm error code EUSAGE\\nnpm error PRIVATE_SYNTHETIC_STDERR\\n'); "
                "raise SystemExit(7)"
            ),
        ]
        destination = self.root.parent / "custom diagnostics"
        private_context = ContextVar("fixture_private_context", default=None)
        private_context.set("PRIVATE_SYNTHETIC_CONTEXT")
        observed_context = []
        append = activity_log.record_event

        def append_with_context_check(event, **fields):
            if event == activity_log.PREPARATION_DETAIL_EVENT:
                observed_context.append(private_context.get())
            return append(event, **fields)

        with (
            patch.object(
                activity_log, "record_event", side_effect=append_with_context_check
            ),
            log_config.overrides(directory=destination),
            lifecycle_output.invocation("start", self.root, capture_preparation=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            lifecycle_output.bind_workspace(WORKSPACE)
            with self.assertRaisesRegex(box.BoxError, "exit 7"):
                box.run(
                    command,
                    preparation="plugin-dependencies",
                    detail_step="npm-install",
                    timeout=5,
                )
        with log_config.overrides(directory=destination):
            records = activity_log.read_preparation_details(self.root)
        self.assertTrue(observed_context)
        self.assertTrue(all(value is None for value in observed_context))
        self.assertIn(
            ("stderr", "EUSAGE"), [(row["source"], row["code"]) for row in records]
        )
        self.assertIn(
            ("stdout", "omitted"), [(row["source"], row["code"]) for row in records]
        )
        self.assertEqual(records[-1]["exit_code"], 7)
        self.assertTrue(all(row["workspace"] == WORKSPACE for row in records))
        self.assertNotIn("PRIVATE_SYNTHETIC", stderr.getvalue())
        for file in destination.rglob("*.jsonl"):
            self.assertNotIn(b"PRIVATE_SYNTHETIC", file.read_bytes())

    def test_failed_detail_sink_preserves_the_actual_setup_result(self):
        logging = activity_log.record_event

        def append(event, **fields):
            if event == activity_log.PREPARATION_DETAIL_EVENT:
                raise OSError("PRIVATE_LOG_FAILURE")
            return logging(event, **fields)

        command = [sys.executable, "-c", "raise SystemExit(0)"]
        with (
            lifecycle_output.invocation("sync", self.root, capture_preparation=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            patch.object(activity_log, "record_event", side_effect=append),
        ):
            completed = box.run(
                command,
                preparation="plugin-dependencies",
                detail_step="npm-install",
                timeout=5,
            )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("preparation diagnostics are unavailable", stderr.getvalue())
        self.assertNotIn("PRIVATE_LOG_FAILURE", stderr.getvalue())

    def test_capture_setup_failure_executes_the_original_child_once(self):
        from oc_box import preparation_capture

        with (
            lifecycle_output.invocation("start", self.root, capture_preparation=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            patch.object(
                preparation_capture,
                "capture_npm",
                side_effect=OSError("PRIVATE_PIPE_FAULT"),
            ),
            patch.object(
                box.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)
            ) as child,
            self.assertRaisesRegex(box.BoxError, "exit 3"),
        ):
            box.run(
                ["synthetic"], preparation="plugin-dependencies", detail_step="npm-lock"
            )
        child.assert_called_once()
        self.assertEqual(child.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(child.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("preparation diagnostics are unavailable", stderr.getvalue())
        self.assertNotIn("PRIVATE_PIPE_FAULT", stderr.getvalue())

    def test_details_do_not_redirect_live_or_unrelated_subprocesses(self):
        from oc_box import preparation_capture

        with (
            lifecycle_output.invocation(
                "start", self.root, console_level="all", capture_preparation=True
            ),
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(preparation_capture, "capture_npm") as capture,
            patch.object(
                box.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as child,
        ):
            box.run(
                ["synthetic"],
                preparation="plugin-dependencies",
                detail_step="npm-install",
            )
            capture.assert_not_called()
            self.assertNotIn("stdout", child.call_args.kwargs)
            self.assertNotIn("stderr", child.call_args.kwargs)

        with (
            lifecycle_output.invocation("start", self.root, capture_preparation=True),
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(preparation_capture, "capture_npm") as capture,
            patch.object(
                box.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as child,
        ):
            box.run(["synthetic"], preparation="baseline-tools")
            capture.assert_not_called()
            self.assertEqual(child.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_details_do_not_retry_an_admission_refusal(self):
        command = box.ContainerCommand(["synthetic"], {})
        command.guarded_spawn = True
        guarded = SimpleNamespace(
            engine_env=None,
            launching=True,
            spawn=MagicMock(side_effect=box.BoxError("Guarded setup refused.")),
        )
        with (
            lifecycle_output.invocation("start", self.root, capture_preparation=True),
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(box.runtime_safeguard, "current", return_value=guarded),
            patch.object(box.subprocess, "Popen") as spawn,
            self.assertRaisesRegex(box.BoxError, "Guarded setup refused"),
        ):
            box.run(
                command, preparation="plugin-dependencies", detail_step="npm-install"
            )
        guarded.spawn.assert_called_once()
        spawn.assert_not_called()

    def test_unavailable_preparation_journal_does_not_expose_output_or_block_child(
        self,
    ):
        destination = self.root.parent / "unsafe"
        destination.mkdir(mode=0o755)
        with (
            lifecycle_output.invocation("start", self.root),
            log_config.overrides(directory=destination),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            patch.object(
                box.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as child,
        ):
            box.run(["synthetic"], preparation="native-cli-install")
        self.assertIn("log unavailable; use --verbose", stderr.getvalue())
        self.assertEqual(child.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(self.root.exists())

    def test_preparation_failure_honors_selected_log_and_late_interrupt(self):
        destination = self.root.parent / "custom journal"
        hint = f"oc-box logs --tail --log-dir '{destination}'"
        for final_append in (True, False, KeyboardInterrupt()):
            with (
                self.subTest(final_append=final_append),
                lifecycle_output.invocation("reset", self.root),
                log_config.overrides(directory=destination),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                patch.object(
                    activity_log, "record_event", side_effect=[True, final_append]
                ),
                patch.object(
                    box.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 9),
                ),
                self.assertRaisesRegex(box.BoxError, "failed \\(exit 9\\)") as result,
            ):
                box.run(["synthetic"], preparation="baseline-tools")
            self.assertIn(hint, stderr.getvalue())
            if final_append is True:
                self.assertIn(hint, str(result.exception))
            else:
                self.assertNotIn("logs --tail", str(result.exception))
                self.assertIn("could not be logged", str(result.exception))

    def test_scoped_report_context_resets_after_error_and_nested_invocation(self):
        self.assertFalse(lifecycle_output.concise())
        with lifecycle_output.invocation("start", self.root):
            self.assertTrue(lifecycle_output.concise())
            lifecycle_output.bind_workspace(WORKSPACE)
            with (
                self.assertRaises(RuntimeError),
                lifecycle_output.invocation("sync", self.root, verbose=True),
            ):
                self.assertFalse(lifecycle_output.concise())
                self.assertIsNone(lifecycle_output.workspace())
                raise RuntimeError("synthetic")
            self.assertEqual(lifecycle_output.workspace(), WORKSPACE)
        self.assertFalse(lifecycle_output.concise())
        self.assertIsNone(lifecycle_output.workspace())
        self.assertFalse(self.root.exists())

    def test_entrypoint_enables_concise_output_and_logs_only_allowlisted_metadata(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):

                def run_start(verbose=verbose):
                    self.assertIs(lifecycle_output.concise(), not verbose)
                    lifecycle_output.bind_workspace(WORKSPACE)
                    display = io.StringIO()
                    with box.loading_stage(
                        "PRIVATE_STAGE_LABEL", stage="configuration", stream=display
                    ):
                        pass
                    self.assertIs(bool(display.getvalue()), verbose)
                    # The diagnostic input is not a free-form message sink.
                    lifecycle_output.record(
                        "history.result", level="warn", reason="DO_NOT_PERSIST"
                    )
                    return 0

                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "oc-box",
                            "start",
                            *(["--lifecycle-diagnostics"] if verbose else []),
                        ],
                    ),
                    patch.object(box, "main", side_effect=run_start),
                ):
                    self.assertEqual(box.entrypoint(), 0)
                self.assertFalse(lifecycle_output.concise())

        # A real CLI read dispatches before any workspace/backend startup.
        output = io.StringIO()
        with (
            patch.object(
                box, "find_workspace", side_effect=AssertionError("workspace lookup")
            ),
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            patch.object(
                box, "launch_credentials", side_effect=AssertionError("vault")
            ),
            patch.object(sys, "argv", ["oc-box", "logs", "--json", "--level", "debug"]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(box.entrypoint(), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [row["event"] for row in records],
            ["command.started", "lifecycle.stage", "command.finished"] * 2,
        )
        self.assertEqual(len(activity_log.read_events(self.root)), 6)
        for event in records[1:3] + records[4:6]:
            self.assertEqual(event["workspace"], WORKSPACE)
        for secret in ("DO_NOT_PERSIST", "PRIVATE_STAGE_LABEL"):
            self.assertNotIn(secret, output.getvalue())

    def test_lifecycle_recording_defers_disk_writes_until_reporting_boundary(self):
        with (
            patch.object(activity_log, "record_event", return_value=True) as writer,
            lifecycle_output.invocation("sync", self.root),
        ):
            for count in range(65):
                lifecycle_output.record(
                    "history.pass", metrics={"sessions": count}, level="debug"
                )
            writer.assert_not_called()
            lifecycle_output.flush()
            self.assertEqual(writer.call_count, 64)
            self.assertEqual(
                writer.call_args_list[0].kwargs["metrics"], {"sessions": 1}
            )
            lifecycle_output.flush()
            self.assertEqual(writer.call_count, 64)

    def test_verbose_launcher_levels_print_only_validated_details(self):
        def launch():
            lifecycle_output.bind_workspace(WORKSPACE)
            lifecycle_output.record(
                "lifecycle.stage", stage="configuration", level="debug"
            )
            lifecycle_output.record("history.result", outcome="success", level="info")
            lifecycle_output.record("lifecycle.notice", reason="pending", level="warn")
            lifecycle_output.record("history.result", outcome="failure", level="error")
            lifecycle_output.record(
                "history.result", reason="PRIVATE_SENTINEL", level="error"
            )
            return 7

        messages = (
            "DEBUG lifecycle.stage",
            "INFO history.result",
            "WARN lifecycle.notice",
            "ERROR history.result",
        )
        cases = (
            ([], 4),
            (["--verbose"], 0),
            (["--verb"], 0),
            (["--verbose=ALL"], 0),
            (["--verbose", "debug"], 0),
            (["--verbose=INFO"], 1),
            (["--verbose=warn"], 2),
            (["--verbose=error"], 3),
        )
        for flags, index in cases:
            with (
                self.subTest(flags=flags),
                patch.object(sys, "argv", ["oc-box", *flags, "sync"]),
                patch.object(box, "main", side_effect=launch),
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(box.entrypoint(), 7)
            text = output.getvalue()
            for message in messages[:index]:
                self.assertNotIn(message, text)
            for message in messages[index:]:
                self.assertIn(message, text)
            self.assertEqual("INFO command.started" in text, index <= 1)
            self.assertEqual("ERROR command.failed" in text, bool(flags))
            self.assertNotIn(WORKSPACE, text)
            self.assertNotIn("PRIVATE_SENTINEL", text)
        records = activity_log.read_events(self.root)
        self.assertEqual(len(records), len(cases) * 6)
        self.assertTrue(any(row.get("workspace") == WORKSPACE for row in records))

    def test_verbose_details_wait_for_reporting_and_do_not_depend_on_a_journal_write(
        self,
    ):
        with (
            contextlib.redirect_stderr(io.StringIO()) as output,
            patch.object(activity_log, "record_event", return_value=False) as writer,
            lifecycle_output.invocation("sync", self.root, console_level="all"),
        ):
            history_recovery.report_skipped(
                ["clients-not-detached", "PRIVATE_SENTINEL"]
            )
            writer.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            lifecycle_output.flush()
            self.assertIn("clients-not-detached, unavailable", output.getvalue())
            self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())
            writer.assert_called_once()

    def test_verbose_launch_does_not_flush_before_main_and_preserves_start_interrupt(
        self,
    ):
        errors = io.StringIO()

        def interrupt_start(event, **fields):
            if event == "command.started":
                self.assertEqual(errors.getvalue(), "")
                raise KeyboardInterrupt
            return True

        with (
            patch.object(sys, "argv", ["oc-box", "reset", "--verbose=all"]),
            contextlib.redirect_stderr(errors),
            patch.object(box, "main") as launch,
            patch.object(activity_log, "record_event", side_effect=interrupt_start),
        ):
            self.assertEqual(box.entrypoint(), 130)
            launch.assert_not_called()
            self.assertIn("INFO command.started", errors.getvalue())

    def test_verbose_recovery_reports_specific_fixed_failure_category(self):
        source = SimpleNamespace(container_id="PRIVATE_CONTAINER")
        with (
            contextlib.redirect_stderr(io.StringIO()) as errors,
            patch.object(activity_log, "record_event", return_value=False),
            patch.object(box.workspace_service, "live_connection", return_value={}),
            lifecycle_output.invocation("sync", self.root, console_level="all"),
        ):
            self.assertFalse(history_recovery.recover(box, self.root, {}, source))
            self.assertEqual(errors.getvalue(), "")
            lifecycle_output.flush()
            self.assertIn("snapshot unavailable (old-connection)", errors.getvalue())
            self.assertNotIn("PRIVATE_CONTAINER", errors.getvalue())

    def test_feature_startup_notices_only_expand_at_informational_verbosity(self):
        for level, legacy, symlinks, details in (
            (None, False, True, False),
            ("warn", False, True, False),
            ("info", False, True, True),
            ("all", False, False, True),
            (None, True, True, True),
        ):
            relay, reader = MagicMock(), MagicMock()
            with (
                self.subTest(level=level, legacy=legacy, symlinks=symlinks),
                lifecycle_output.invocation(
                    "start", verbose=legacy, console_level=level
                ),
                patch.object(box, "validate_host_drop", return_value="konsole"),
                patch.object(host_drop, "Relay", return_value=relay),
                patch.object(box, "run_tui", return_value=7),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    box.run_native_tui(
                        {"opencode_version": "2.3.0"},
                        ["native-fixture"],
                        {},
                        host_drop_enabled=True,
                        host_clipboard_reader=reader,
                        host_clipboard_symlinks=symlinks,
                    ),
                    7,
                )
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 5 if details else 2)
            self.assertIn("oc-box: " + host_drop.DROP_ENABLED, lines)
            self.assertIn("oc-box: " + host_drop.CLIPBOARD_ENABLED, lines)
            self.assertEqual(host_drop.DROP_DETAILS in output.getvalue(), details)
            self.assertEqual(host_drop.CLIPBOARD_DETAILS in output.getvalue(), details)
            self.assertEqual(
                host_drop.CLIPBOARD_SYMLINKS_WARNING in output.getvalue(),
                details and symlinks,
            )
            self.assertEqual(
                host_drop.CLIPBOARD_STRICT_WARNING in output.getvalue(),
                details and not symlinks,
            )
            reader.read_selection.assert_not_called()

    def test_relay_failure_warns_without_announcing_that_imports_are_enabled(self):
        relay = MagicMock()
        relay.__enter__.side_effect = host_drop.DropError("PRIVATE_SENTINEL")
        with (
            lifecycle_output.invocation("start", self.root, console_level="all"),
            patch.object(box, "validate_host_drop", return_value="konsole"),
            patch.object(host_drop, "Relay", return_value=relay),
            patch.object(box, "run_tui", return_value=7) as native,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                box.run_native_tui(
                    {"opencode_version": "2.3.0"},
                    ["native"],
                    {},
                    host_drop_enabled=True,
                    host_drop_required=False,
                ),
                7,
            )
        native.assert_called_once_with(["native"])
        self.assertIn("could not start", output.getvalue())
        self.assertNotIn("enabled", output.getvalue())
        self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())

    def test_log_help_and_empty_read_never_create_state(self):
        with (
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as result:
                box.main(["logs", "--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertIn("--level", output.getvalue())
            output.seek(0)
            output.truncate()
            self.assertEqual(box.main(["logs", "--json"]), 0)
            self.assertEqual(output.getvalue(), "")
        self.assertFalse(self.root.exists())

    def test_journald_mirror_is_opt_in_and_cannot_fail_private_append(self):
        with patch.object(
            journal_output, "emit", side_effect=OSError("private")
        ) as emit:
            self.assertTrue(
                activity_log.record_event("command.started", command="sync")
            )
            emit.assert_not_called()
            with log_config.overrides(journald=True):
                self.assertFalse(
                    activity_log.record_event("history.result", reason="DO_NOT_PERSIST")
                )
                emit.assert_not_called()
                self.assertTrue(
                    activity_log.record_event(
                        "history.result",
                        command="sync",
                        level="info",
                        outcome="success",
                    )
                )
                emit.assert_called_once()
                with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
                    self.assertTrue(activity_log.record_event("command.finished"))
                emit.assert_called_once()
        self.assertEqual(len(activity_log.read_events(self.root)), 3)

    def test_journald_cli_is_explicit_and_never_falls_back_to_private_records(self):
        with (
            patch.object(
                activity_log, "read_events", side_effect=AssertionError("file reader")
            ),
            patch.object(journal_output, "read_events", return_value=[]) as read,
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                box.main(
                    ["logs", "--journald", "--level", "warn", "--limit", "3", "--json"]
                ),
                0,
            )
            self.assertEqual(output.getvalue(), "")
            read.assert_called_once_with(level="warn", limit=3, workspace=None)
            failure = journal_output.JournalError("unavailable")
            failure.args = ("PRIVATE_SENTINEL",)
            read.side_effect = failure
            with self.assertRaisesRegex(
                box.BoxError, "system journal is unavailable"
            ) as raised:
                box.main(["logs", "--journald"])
            self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
        self.assertFalse(self.root.exists())

    def test_misordered_read_only_logs_invocation_does_not_append_or_mirror(self):
        self.saved_logs(journald=True)
        with (
            patch.object(sys, "argv", ["oc-box", "--project", ".", "logs"]),
            patch.object(journal_output, "emit") as mirror,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(box.entrypoint(), 1)
            self.assertIn("Use 'oc-box logs' first", errors.getvalue())
        mirror.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_logging_flags_scope_every_launcher_event_without_mutating_config_or_env(
        self,
    ):
        saved = self.root.parent / "saved"
        config = self.saved_logs(directory=str(saved), journald=False)
        before, environment = config.read_bytes(), dict(os.environ)
        cases = (
            ([], "start"),
            (["start"], "start"),
            (["reset"], "reset"),
            (["--project", ".", "sync"], "sync"),
            (["stop"], "stop"),
            (["terminal"], "terminal"),
            (["web"], "web"),
        )
        for index, (prefix, command) in enumerate(cases):
            with self.subTest(command=command, prefix=prefix):
                selected = self.root.parent / f"override-{index}"
                capture = command in ("start", "reset", "sync", "web")

                def launch(selected=selected, command=command):
                    self.assertEqual(dict(os.environ), environment)
                    policy = log_config.load()
                    self.assertEqual(policy.directory, selected)
                    self.assertTrue(policy.journald)
                    self.assertEqual(lifecycle_output.command_name(), command)
                    self.assertFalse(lifecycle_output.concise())
                    self.assertEqual(lifecycle_output.details_enabled(), capture)
                    lifecycle_output.record(
                        "lifecycle.stage", stage="configuration", level="debug"
                    )
                    return 0

                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "oc-box",
                            *prefix,
                            "--log-dir",
                            str(selected),
                            "--journald",
                            "--lifecycle-diagnostics",
                            *(["--capture-preparation"] if capture else []),
                        ],
                    ),
                    patch.object(box, "main", side_effect=launch),
                    patch.object(journal_output, "emit") as mirror,
                ):
                    self.assertEqual(box.entrypoint(), 0)
                    self.assertEqual(
                        [event.args[0]["event"] for event in mirror.call_args_list],
                        ["command.started", "lifecycle.stage", "command.finished"],
                    )
                    self.assertTrue(
                        all(
                            event.args[0]["command"] == command
                            for event in mirror.call_args_list
                        )
                    )
                self.assertTrue((selected / "activity.jsonl").is_file())
                self.assertEqual(log_config.load().directory, saved)
                self.assertFalse(log_config.load().journald)
                self.assertIsNone(lifecycle_output.command_name())
                self.assertEqual(config.read_bytes(), before)
        self.assertFalse(saved.exists())
        self.assertFalse(self.root.exists())

    def test_no_journald_overrides_saved_enable_for_one_launcher(self):
        config = self.saved_logs(journald=True)
        before = config.read_bytes()
        with (
            patch.object(sys, "argv", ["oc-box", "sync", "--no-journald"]),
            patch.object(box, "main", return_value=0),
            patch.object(journal_output, "emit") as mirror,
        ):
            self.assertEqual(box.entrypoint(), 0)
            mirror.assert_not_called()
        self.assertEqual(len(activity_log.read_events(self.root)), 2)
        self.assertTrue(log_config.load().journald)
        self.assertEqual(config.read_bytes(), before)

    def test_incompatible_flags_refuse_before_lifecycle_or_journal(self):
        self.saved_logs(journald=True)
        directory = self.root.parent / "must-not-be-created"
        for arguments in (
            ["--status", "--journald"],
            ["--log-dir", str(directory), "--journald", "web"],
            ["upgrade", "--check", "--journald"],
            ["upgrade", "--check", "--journa"],
            ["upgrade", "--check", "--verbose=all"],
            ["--status", "--verbose=all"],
            ["--status", "--capture-preparation"],
            ["stop", "--capture-preparation"],
            ["terminal", "--capture-preparation"],
            ["tui", "--read-only", "--log-dir", str(directory)],
            ["backup", "--table"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", ["oc-box", *arguments]),
                patch.object(box, "main") as main,
                patch.object(journal_output, "emit") as mirror,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(box.entrypoint(), 1)
                main.assert_not_called()
                mirror.assert_not_called()
            self.assertFalse(directory.exists())
            self.assertFalse(self.root.exists())

    def test_abbreviated_status_is_read_only_even_when_saved_mirroring_is_enabled(self):
        self.saved_logs(journald=True)
        for options in (["--statu"], ["--statu", "--tab"]):
            with (
                self.subTest(options=options),
                patch.object(sys, "argv", ["oc-box", "start", *options]),
                patch.object(box, "report_status", return_value=0) as report,
                patch.object(journal_output, "emit") as mirror,
            ):
                self.assertEqual(box.entrypoint(), 0)
                report.assert_called_once()
                mirror.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_completion_table_is_parser_error_without_journaling(self):
        self.saved_logs(journald=True)
        with (
            patch.object(sys, "argv", ["oc-box", "completion", "bash", "--table"]),
            patch.object(journal_output, "emit") as mirror,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as result,
        ):
            box.entrypoint()
        self.assertEqual(result.exception.code, 2)
        mirror.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_direct_main_can_select_options_without_harness_environment_switches(self):
        directory = self.root.parent / "direct"

        def launch(args, _argv):
            self.assertEqual(args.command, "stop")
            self.assertEqual(log_config.load().directory, directory)
            self.assertTrue(log_config.load().journald)
            self.assertTrue(lifecycle_output.concise())
            return 0

        with patch.object(box, "_main_command", side_effect=launch):
            self.assertEqual(
                box.main(["stop", "--log-dir", str(directory), "--journald"]), 0
            )
        self.assertIsNone(log_config.load().directory)
        self.assertFalse(log_config.load().journald)
        self.assertFalse(directory.exists())

    def test_already_reported_history_failure_does_not_print_a_second_error(self):
        for error in (
            box.BoxError("PRIVATE_SENTINEL"),
            RuntimeError("PRIVATE_SENTINEL"),
        ):
            with self.subTest(error=type(error).__name__):
                error.history_outcome_reported = True
                with (
                    patch.object(sys, "argv", ["oc-box", "sync"]),
                    patch.object(box, "main", side_effect=error),
                    contextlib.redirect_stderr(io.StringIO()) as output,
                ):
                    self.assertEqual(box.entrypoint(), 1)
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
