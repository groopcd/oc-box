"""Synthetic datagrams and subprocesses; never inspect a real journal."""

import io
import json
import os
import socket
import subprocess
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from oc_box import activity_log, journal_output

_WORKSPACE = "a" * 32
_OTHER_WORKSPACE = "b" * 32
_UID = str(os.getuid())
_REAL_POPEN = subprocess.Popen
_SECRET = "synthetic-provider-secret"


def _record(event="command.started", **fields):
    return {"timestamp": "2026-01-02T03:04:05.001Z", "event": event, **fields}


def _row(record, priority="6"):
    row = {
        "_UID": _UID,
        "SYSLOG_IDENTIFIER": "oc-box",
        "OC_BOX_FORMAT": "oc-box-activity-v1",
        "PRIORITY": priority,
        "MESSAGE": activity_log.format_event(record),
        "OC_BOX_RECORD": json.dumps(record, separators=(",", ":"), ensure_ascii=True),
        "__CURSOR": "s=synthetic",
        "__REALTIME_TIMESTAMP": "1767323045001000",
        "__MONOTONIC_TIMESTAMP": "1234",
        "_BOOT_ID": "0" * 32,
    }
    if "workspace" in record:
        row["OC_BOX_WORKSPACE"] = record["workspace"]
    return row


def _line(row):
    return json.dumps(row).encode("ascii") + b"\n"


@contextmanager
def _child(script, *, use_sender_flags=False):
    """Intercept the exact command but use real pipes and a synthetic child."""
    calls = []

    def spawn(arguments, **options):
        command = (
            [*arguments[:-1], script]
            if use_sender_flags
            else [sys.executable, "-c", script]
        )
        process = _REAL_POPEN(command, **options)
        calls.append((arguments, options, process))
        return process

    with mock.patch.object(journal_output.subprocess, "Popen", side_effect=spawn):
        yield calls


class JournalEmitTests(unittest.TestCase):
    def test_requires_exact_host_opt_in(self):
        for environment in (
            {},
            {"OC_BOX_JOURNALD": "true"},
            {"OC_BOX_JOURNALD": "1", "OC_BOX_CONTAINER": "1"},
        ):
            with (
                self.subTest(environment=environment),
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(journal_output.subprocess, "Popen") as opened,
            ):
                self.assertFalse(journal_output.emit(_record()))
                opened.assert_not_called()

    def test_native_packet_contains_only_fixed_fields_and_maps_priority(self):
        for level, priority in (
            ("debug", "7"),
            ("info", "6"),
            ("warn", "4"),
            ("error", "3"),
        ):
            with (
                self.subTest(level=level),
                mock.patch.dict(
                    os.environ,
                    {
                        "OC_BOX_JOURNALD": "1",
                        "AWS_SECRET_ACCESS_KEY": _SECRET,
                        "PYTHONPATH": f"/fake/{_SECRET}",
                    },
                    clear=True,
                ),
                mock.patch.object(sys, "argv", ["oc-box", "--podman-config", _SECRET]),
                mock.patch.object(journal_output.subprocess, "Popen") as opened,
            ):
                process = opened.return_value
                process.returncode = process.poll.return_value = 0
                workspace = {"workspace": _WORKSPACE} if level != "info" else {}
                record = _record(
                    command="sync", level=level, metrics={"sessions": 2}, **workspace
                )
                self.assertTrue(journal_output.emit(record))
                opened.assert_called_once_with(
                    ["python3", "-I", "-S", "-B", "-c", journal_output._SEND_SCRIPT],
                    executable=sys.executable,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"LC_ALL": "C"},
                    cwd="/",
                    close_fds=True,
                    bufsize=0,
                )
                process.communicate.assert_called_once()
                (payload,) = process.communicate.call_args.args
                self.assertGreater(process.communicate.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(
                    process.communicate.call_args.kwargs["timeout"], 0.75
                )
                process.kill.assert_not_called()
                metadata = repr(opened.call_args)
                for value in (
                    record["timestamp"],
                    record["event"],
                    _WORKSPACE,
                    _SECRET,
                ):
                    self.assertNotIn(value, metadata)
                self.assertNotIn(_SECRET.encode(), payload)
                self.assertTrue(payload.endswith(b"\n"))
                expected = {
                    "PRIORITY": priority,
                    "SYSLOG_IDENTIFIER": "oc-box",
                    "OC_BOX_FORMAT": "oc-box-activity-v1",
                    "MESSAGE": activity_log.format_event(record),
                    "OC_BOX_RECORD": json.dumps(record, separators=(",", ":")),
                }
                if workspace:
                    expected["OC_BOX_WORKSPACE"] = _WORKSPACE
                fields = [
                    line.split("=", 1) for line in payload.decode("ascii").splitlines()
                ]
                self.assertEqual(len(fields), len(expected))
                self.assertEqual(dict(fields), expected)

    def test_allowlist_denies_raw_fields_and_unbounded_values_before_launch(self):
        for record in (
            _record(prompt=_SECRET),
            _record(workspace=f"{_WORKSPACE}\nMESSAGE={_SECRET}"),
            _record(reason=_SECRET),
            _record(metrics={"sessions": 2**31}),
        ):
            with (
                self.subTest(record=record),
                mock.patch.dict(os.environ, {"OC_BOX_JOURNALD": "1"}, clear=True),
                mock.patch.object(journal_output.subprocess, "Popen") as opened,
            ):
                self.assertFalse(journal_output.emit(record))
                opened.assert_not_called()

    def test_sender_script_sends_one_nonblocking_datagram_or_fails_silently(self):
        payload = b"SYSLOG_IDENTIFIER=oc-box\n"
        for result in (
            len(payload),
            FileNotFoundError(_SECRET),
            BlockingIOError(_SECRET),
            0,
        ):
            with (
                self.subTest(result=result),
                mock.patch.object(
                    sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))
                ),
                mock.patch.object(socket, "socket") as opened,
            ):
                sender = opened.return_value.__enter__.return_value
                if isinstance(result, OSError):
                    sender.sendto.side_effect = result
                else:
                    sender.sendto.return_value = result
                with self.assertRaises(SystemExit) as error:
                    exec(journal_output._SEND_SCRIPT, {})  # noqa: S102 - fixed shipped sender
                self.assertEqual(
                    error.exception.code, 0 if result == len(payload) else 1
                )
                opened.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.assertEqual(
                    sender.mock_calls,
                    [
                        mock.call.setblocking(False),
                        mock.call.sendto(payload, "/run/systemd/journal/socket"),
                    ],
                )

    def test_sender_script_rejects_empty_or_oversized_input(self):
        for payload in (b"", b"x" * 4097):
            with (
                mock.patch.object(
                    sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))
                ),
                mock.patch.object(socket, "socket") as opened,
                self.assertRaises(SystemExit) as error,
            ):
                exec(journal_output._SEND_SCRIPT, {})  # noqa: S102 - fixed shipped sender
            self.assertEqual(error.exception.code, 1)
            opened.assert_not_called()

    def test_isolated_python_flags_and_stdin_work_in_a_synthetic_child(self):
        script = """\
import os
import socket
import sys

assert sys.argv == ["-c"]
assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
assert os.getcwd() == "/"
assert os.environ.get("LC_ALL") == "C" and "AWS_SECRET_ACCESS_KEY" not in os.environ
assert b"OC_BOX_FORMAT=oc-box-activity-v1\\n" in sys.stdin.buffer.read(4097)
"""
        with (
            mock.patch.dict(
                os.environ,
                {"OC_BOX_JOURNALD": "1", "AWS_SECRET_ACCESS_KEY": _SECRET},
                clear=True,
            ),
            _child(script, use_sender_flags=True) as calls,
        ):
            self.assertTrue(journal_output.emit(_record()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2].returncode, 0)

    def test_child_start_or_exit_failure_is_silent_and_never_retried(self):
        with mock.patch.dict(os.environ, {"OC_BOX_JOURNALD": "1"}, clear=True):
            with mock.patch.object(
                journal_output.subprocess,
                "Popen",
                side_effect=FileNotFoundError(_SECRET),
            ) as opened:
                self.assertFalse(journal_output.emit(_record()))
                opened.assert_called_once()
            with _child("raise SystemExit(1)", use_sender_flags=True) as calls:
                self.assertFalse(journal_output.emit(_record()))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2].returncode, 1)

    def test_slow_child_hits_deadline_and_is_reaped_without_retry(self):
        script = "import sys,time; sys.stdin.buffer.read(); time.sleep(2)"
        with (
            mock.patch.dict(os.environ, {"OC_BOX_JOURNALD": "1"}, clear=True),
            mock.patch.object(journal_output, "_SEND_TIMEOUT", 0.2),
            _child(script, use_sender_flags=True) as calls,
        ):
            self.assertFalse(journal_output.emit(_record()))
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0][2].returncode)


class JournalReadTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_uid_query_with_clean_environment_and_preserved_output_order(self):
        older_row = _record("lifecycle.notice", workspace=_WORKSPACE, stage="service")
        # A discontinuous wall clock must not reorder the returned journal rows.
        newer_row = _record("command.failed", workspace=_WORKSPACE, command="sync")
        newer_row["timestamp"] = "2026-01-01T03:04:05.001Z"
        newer = {**_row(newer_row, "3"), "__SEQNUM": "2", "__SEQNUM_ID": "1" * 32}
        payload = _line(_row(older_row, "4")) + _line(newer)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PATH": "/untrusted",
                    "AWS_SECRET_ACCESS_KEY": _SECRET,
                    "PAGER": "bad",
                },
            ),
            _child(f"import os; os.write(1, {payload!r})") as calls,
        ):
            self.assertEqual(
                journal_output.read_events(limit=2, level="warn", workspace=_WORKSPACE),
                [older_row, newer_row],
            )
        self.assertEqual(len(calls), 1)
        arguments, options, process = calls[0]
        self.assertEqual(
            arguments,
            [
                "journalctl",
                "--quiet",
                "--no-pager",
                "--output=json",
                "--output-fields=_UID,SYSLOG_IDENTIFIER,OC_BOX_FORMAT,PRIORITY,OC_BOX_WORKSPACE,MESSAGE,OC_BOX_RECORD",
                "--lines=2",
                "--priority=4",
                "--",
                f"_UID={_UID}",
                "SYSLOG_IDENTIFIER=oc-box",
                "OC_BOX_FORMAT=oc-box-activity-v1",
                f"OC_BOX_WORKSPACE={_WORKSPACE}",
            ],
        )
        self.assertEqual(
            options["env"],
            {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
        )
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(process.returncode, 0)

    def test_rows_are_revalidated_even_if_the_child_ignores_filters(self):
        record = _record("history.result", level="warn", workspace=_WORKSPACE)
        good = _row(record, "4")
        bad = [
            {**good, "_UID": str(os.getuid() + 1)},
            {**good, "_UID": [_UID]},
            {**good, "SYSLOG_IDENTIFIER": "other"},
            {name: value for name, value in good.items() if name != "OC_BOX_FORMAT"},
            {**good, "OC_BOX_FORMAT": "oc-box-activity-v2"},
            {**good, "PRIORITY": "3"},
            {**good, "PRIORITY": 4},
            {**good, "OC_BOX_WORKSPACE": _OTHER_WORKSPACE},
            {**good, "MESSAGE": _SECRET},
            {**good, "CONTAINER_STDERR": _SECRET},
            {**good, "OC_BOX_RECORD": None},
            _row(_record("command.started", workspace=_WORKSPACE)),
            _row(_record("lifecycle.notice", workspace=_OTHER_WORKSPACE), "4"),
            _row({**record, "prompt": _SECRET}, "4"),
            {**good, "OC_BOX_RECORD": good["OC_BOX_RECORD"][:-1] + ',"level":"warn"}'},
        ]
        line = _line(good)
        duplicate_uid = line[:-2] + f', "_UID": "{_UID}"'.encode("ascii") + b"}\n"
        payload = b"".join(map(_line, bad)) + duplicate_uid + b"{junk}\n\xff\n" + line
        with _child(f"import os; os.write(1, {payload!r})"):
            self.assertEqual(
                journal_output.read_events(
                    limit=30, level="warn", workspace=_WORKSPACE
                ),
                [record],
            )

    def test_unscoped_query_has_no_workspace_match_but_requires_consistent_metadata(
        self,
    ):
        record = _record("history.recovery")
        good = _row(record)
        with_workspace = _row(_record(workspace=_WORKSPACE))
        without_workspace_field = {
            name: value
            for name, value in with_workspace.items()
            if name != "OC_BOX_WORKSPACE"
        }
        payload = (
            _line({**good, "OC_BOX_WORKSPACE": _WORKSPACE})
            + _line(without_workspace_field)
            + _line(good)
            + _line(with_workspace)
        )
        with _child(f"import os; os.write(1, {payload!r})") as calls:
            self.assertEqual(
                journal_output.read_events(limit=4, level=None),
                [record, _record(workspace=_WORKSPACE)],
            )
        arguments = calls[0][0]
        self.assertIn("--priority=7", arguments)
        self.assertFalse(any(arg.startswith("OC_BOX_WORKSPACE=") for arg in arguments))

    def test_invalid_filters_and_container_never_start_journalctl(self):
        with mock.patch.object(journal_output.subprocess, "Popen") as opened:
            for arguments in (
                {"limit": False, "level": "debug"},
                {"limit": 1, "level": "-f"},
                {"limit": 1, "level": "info", "workspace": "+"},
            ):
                self.assertEqual(journal_output.read_events(**arguments), [])
            with mock.patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
                with self.assertRaises(journal_output.JournalError) as error:
                    journal_output.read_events(limit=1, level="info")
                self.assertEqual(error.exception.reason, "host-only")
            opened.assert_not_called()

    def test_no_matches_is_empty_and_requested_limit_is_capped(self):
        with _child("pass") as calls:
            self.assertEqual(
                journal_output.read_events(limit=50_000, level="error"), []
            )
        self.assertIn("--lines=10000", calls[0][0])
        self.assertIn("--priority=3", calls[0][0])

    def test_partial_reads_preserve_complete_rows_but_not_an_unterminated_fragment(
        self,
    ):
        complete = _record(command="start")
        line = _line(_row(complete))
        fragment = _line(_row(_record(command="stop")))[:-1]
        script = (
            f"import os,time; os.write(1, {line[:64]!r}); time.sleep(0.02); "
            f"os.write(1, {line[64:] + fragment!r})"
        )
        with _child(script):
            self.assertEqual(
                journal_output.read_events(limit=2, level="info"), [complete]
            )

    def test_missing_tool_and_failed_query_have_fixed_guidance(self):
        with mock.patch.object(
            journal_output.subprocess, "Popen", side_effect=FileNotFoundError(_SECRET)
        ):
            with self.assertRaises(journal_output.JournalError) as error:
                journal_output.read_events(limit=1, level="info")
            self.assertEqual(error.exception.reason, "missing-tool")
            self.assertNotIn(_SECRET, str(error.exception))
            self.assertIn("private activity log", str(error.exception))

        payload = _line(_row(_record()))
        script = f"import os; os.write(1, {payload!r}); os.write(2, {_SECRET.encode()!r}); os._exit(1)"
        with _child(script), self.assertRaises(journal_output.JournalError) as error:
            journal_output.read_events(limit=1, level="info")
        self.assertEqual(error.exception.reason, "unavailable")
        self.assertNotIn(_SECRET, str(error.exception))
        self.assertIn("private activity log", str(error.exception))

    def test_total_bytes_per_row_bytes_and_row_count_are_capped(self):
        for script, limit in (
            ("import os; os.write(1, b'x' * 4097)", 1),
            ("import os; os.write(1, b'x' * 4096 + b'\\n')", 2),
            ("import os; os.write(1, b'{}\\n' * 3)", 2),
        ):
            with self.subTest(script=script), _child(script) as calls:
                with self.assertRaises(journal_output.JournalError) as error:
                    journal_output.read_events(limit=limit, level="debug")
                self.assertEqual(error.exception.reason, "output-limit")
                self.assertIsNotNone(calls[0][2].returncode)

    def test_read_and_exit_deadline_reap_the_child(self):
        for script in (
            "import time; time.sleep(2)",
            "import os,time; os.close(1); time.sleep(2)",
        ):
            with (
                self.subTest(script=script),
                mock.patch.object(journal_output, "_TIMEOUT", 0.2),
                _child(script) as calls,
            ):
                with self.assertRaises(journal_output.JournalError) as error:
                    journal_output.read_events(limit=1, level="debug")
                self.assertEqual(error.exception.reason, "timeout")
                self.assertIsNotNone(calls[0][2].returncode)


if __name__ == "__main__":
    unittest.main()
