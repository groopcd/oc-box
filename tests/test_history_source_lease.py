"""Metadata/pipe safety matrices for ordinary pass-local source reuse."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import history_reader, history_recovery
from oc_box import opencode_v2_io as bridge, workspace_service as service
from test_history_reader import BoxError, ENDPOINT, FakeChild, SCHEMA, fake_box


DATA = {
    "id": "b" * 32,
    "container": "c" * 64,
    "home": "/home/example",
    "opencode_version": "2.3.0",
}
SCHEMAS = (
    SCHEMA,
    "CREATE TABLE job (notification_id TEXT PRIMARY KEY, id TEXT, session_id TEXT, "
    "kind TEXT, status TEXT, recovery TEXT)",
    "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE session_inbox (id TEXT PRIMARY KEY, session_id TEXT, delivery TEXT)",
    "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, time_suspended INTEGER, "
    "time_compacting INTEGER, time_busy INTEGER, time_idle INTEGER)",
)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="history-source-")
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "source.db"
        self.guards = {
            "Refusal": history_reader.Refusal,
            "_check_system_config": Mock(),
            "_check_database": history_reader._check_database,
        }

    def create(self):
        with (
            contextlib.closing(sqlite3.connect(self.database)) as connection,
            connection,
        ):
            for schema in SCHEMAS:
                connection.execute(schema)

    def query(self, query, parameters=()):
        with (
            contextlib.closing(sqlite3.connect(self.database)) as connection,
            connection,
        ):
            connection.execute(query, parameters)

    def check(self):
        return bridge.history_source_database(self.guards, self.database)

    def test_quiet_metadata_is_read_only_without_history_or_secret_queries(self):
        self.create()
        before = self.database.read_bytes()
        connect = sqlite3.connect
        queries = []

        def observed(*args, **kwargs):
            self.assertTrue(args[0].endswith("?mode=ro"))
            self.assertNotIn("immutable", args[0])
            result = connect(*args, **kwargs)
            result.set_trace_callback(queries.append)
            return result

        with patch.object(bridge.sqlite3, "connect", side_effect=observed):
            self.assertIsNone(self.check())
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(list(self.database.parent.iterdir()), [self.database])
        self.assertFalse(any("SELECT *" in query for query in queries))
        for forbidden in ("session_message", "session_part", "credential", "auth"):
            self.assertFalse(any(forbidden in query for query in queries))

    def test_all_job_statuses_and_legacy_jobs_fall_back_without_edits(self):
        self.create()
        for status in ("running", "completed", "error", "cancelled", "unknown", None):
            with self.subTest(status=status):
                self.query("DELETE FROM job")
                self.query(
                    "INSERT INTO job VALUES (?,?,?,?,?,?)",
                    (
                        "private-notice",
                        "private-job",
                        "private-session",
                        "shell",
                        status,
                        "private-recovery",
                    ),
                )
                before = self.database.read_bytes()
                self.assertEqual(self.check(), "jobs-present")
                self.assertEqual(self.database.read_bytes(), before)
        self.query("DELETE FROM job")
        self.query("INSERT INTO kv VALUES ('job.background/private', 'private-value')")
        self.assertEqual(self.check(), "jobs-present")

    def test_every_schedule_status_and_queued_work_exclude_optimization(self):
        self.create()
        for status in ("active", "paused", "done", "cancelled", "unknown", None):
            with self.subTest(status=status):
                self.query("DELETE FROM schedule")
                self.query(
                    "INSERT INTO schedule VALUES (?,?,?,?)",
                    ("private", "private", status, "private"),
                )
                self.assertEqual(self.check(), "schedule-present")
        self.query("DELETE FROM schedule")
        self.query("INSERT INTO session_inbox VALUES ('private','private','unknown')")
        self.assertEqual(self.check(), "queued-work")

    def test_claim_compaction_and_busy_watermark_matrix(self):
        self.create()
        for values, expected in (
            ((None, None, None, None), None),
            ((None, None, 2, 2), None),
            ((None, None, 1, 2), None),
            ((0, None, None, None), "execution-present"),
            ((None, 0, None, None), "execution-present"),
            ((None, None, 0, None), "execution-present"),
            ((None, None, 3, 2), "execution-present"),
        ):
            with self.subTest(values=values):
                self.query("DELETE FROM session_v2")
                self.query("INSERT INTO session_v2 VALUES ('private',?,?,?,?)", values)
                self.assertEqual(self.check(), expected)

    def test_wal_jobs_are_visible_not_hidden_by_immutable_mode(self):
        self.create()
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO job (notification_id,status) VALUES ('private','completed')"
        )
        connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").is_file())
        self.assertEqual(self.check(), "jobs-present")

    def test_dynamic_sqlite_timestamp_types_are_not_quiet_execution_metadata(self):
        self.create()
        for column in ("time_suspended", "time_compacting", "time_busy", "time_idle"):
            with self.subTest(column=column):
                self.query("DELETE FROM session_v2")
                self.query(
                    "INSERT INTO session_v2 (id," + column + ") VALUES (?,?)",
                    ("private", "private-not-a-timestamp"),
                )
                with self.assertRaisesRegex(
                    bridge.HistorySourceError, "execution-schema"
                ):
                    self.check()

    def test_missing_database_can_fall_back_but_unsafe_malformed_and_schema_abort(self):
        self.assertEqual(self.check(), "database-missing")
        self.assertFalse(self.database.exists())
        self.database.write_bytes(b"private-sentinel")
        with self.assertRaisesRegex(bridge.HistorySourceError, "database-invalid"):
            self.check()
        self.database.unlink()
        self.create()
        for query, category in (
            ("DROP TABLE job", "job-schema"),
            ("DROP TABLE session_inbox", "execution-schema"),
        ):
            with self.subTest(category=category):
                self.query(query)
                with self.assertRaisesRegex(bridge.HistorySourceError, category):
                    self.check()
                self.database.unlink()
                self.create()
        self.database.chmod(0o666)
        with self.assertRaisesRegex(bridge.HistorySourceError, "database-unsafe"):
            self.check()

    def test_views_wrong_types_and_primary_keys_are_not_empty_known_tables(self):
        for statement in (
            "CREATE VIEW job AS SELECT 'x' AS notification_id",
            "CREATE TABLE job (notification_id TEXT, id TEXT, session_id TEXT, "
            "kind TEXT, status TEXT, recovery TEXT)",
            "CREATE TABLE job (notification_id TEXT PRIMARY KEY, id TEXT, "
            "session_id TEXT, kind TEXT, status INTEGER, recovery TEXT)",
        ):
            with self.subTest(statement=statement):
                self.create()
                self.query("DROP TABLE job")
                self.query(statement)
                with self.assertRaisesRegex(bridge.HistorySourceError, "job-schema"):
                    self.check()
                self.database.unlink()

    def test_system_guard_precedes_database_and_diagnostics_are_fixed(self):
        self.guards["_check_system_config"].side_effect = history_reader.Refusal(
            "system-config"
        )
        self.guards["_check_database"] = Mock()
        with self.assertRaisesRegex(bridge.HistorySourceError, "^system-config$"):
            self.check()
        self.guards["_check_database"].assert_not_called()

    def test_original_validated_binding_is_not_replaced_by_a_new_baseline(self):
        for race in ("replace", "symlink", "chmod", "hardlink"):
            with self.subTest(race=race):
                self.create()
                original = history_reader._check_database(self.database)
                self.assertEqual(original.st_ino, self.database.lstat().st_ino)
                saved = self.database.with_name("saved-" + race)
                real_guard = history_reader._check_database

                def changed(path):
                    binding = real_guard(path)
                    if race in ("replace", "symlink"):
                        path.rename(saved)
                        if race == "replace":
                            self.create()
                        else:
                            path.symlink_to(saved)
                    elif race == "chmod":
                        path.chmod(0o666)
                    else:
                        os.link(path, saved)
                    return binding

                self.guards["_check_database"] = changed
                with self.assertRaisesRegex(
                    bridge.HistorySourceError, "database-unsafe"
                ):
                    self.check()
                self.database.unlink()
                saved.unlink(missing_ok=True)
                self.guards["_check_database"] = real_guard

    def test_binding_is_checked_after_sqlite_open_and_execution_queries(self):
        for stage in ("open", "query"):
            with self.subTest(stage=stage):
                self.create()
                connect = sqlite3.connect
                calls = 0

                def changed(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    self.assertTrue(args[0].endswith("?mode=ro"))
                    self.assertNotIn("/proc/", args[0])
                    self.assertNotIn("immutable", args[0])
                    connection = connect(*args, **kwargs)
                    if calls == 2:
                        if stage == "open":
                            self.database.chmod(0o666)
                        else:

                            def trace(query):
                                if query == 'SELECT 1 FROM "job" LIMIT 1':
                                    os.link(
                                        self.database,
                                        self.database.with_suffix(".linked"),
                                    )

                            connection.set_trace_callback(trace)
                    return connection

                with patch.object(bridge.sqlite3, "connect", side_effect=changed):
                    with self.assertRaisesRegex(
                        bridge.HistorySourceError, "database-unsafe"
                    ):
                        self.check()
                self.database.unlink()
                self.database.with_suffix(".linked").unlink(missing_ok=True)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.box = fake_box()
        self.children = []
        token = service.CONNECTION.set(None)
        self.addCleanup(service.CONNECTION.reset, token)

    def child(self, response=None, **kwargs):
        value = FakeChild(
            json.dumps(
                {"endpoint": ENDPOINT} if response is None else response
            ).encode()
            + b"\n",
            **kwargs,
        )
        self.children.append(value)
        self.addCleanup(value.dispose)
        return value

    def test_one_unregistered_lease_sets_resets_connection_and_never_mutates_host(self):
        child = self.child()
        before = dict(os.environ)
        cwd = os.getcwd()
        with (
            patch.object(service.subprocess, "Popen", return_value=child) as popen,
            patch.object(service, "socket_path") as socket_path,
        ):
            with service.history_source(self.box, DATA) as connection:
                self.assertEqual(connection["workspace"], DATA["id"])
                self.assertIs(service.live_connection(), connection)
                self.assertIsNone(history_reader.current())
                self.assertIsNone(history_recovery.FENCE.get())
                with service.history_source(self.box, DATA) as same:
                    self.assertIs(same, connection)
                self.assertEqual(popen.call_count, 1)
            socket_path.assert_not_called()
        self.assertIsNone(service.live_connection())
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(os.getcwd(), cwd)
        command = popen.call_args.args[0]
        self.assertEqual(
            command[:6], ["podman", "exec", "-i", "--workdir", "/", DATA["container"]]
        )
        self.assertIn("/usr/bin/env", command)
        self.assertIn("-I", command)
        self.assertIn("-S", command)
        self.assertEqual(command[-2:], ["history-source", self.box.CONTAINER_OPENCODE])
        self.assertNotIn(ENDPOINT["password"], repr(command))
        self.assertNotIn(ENDPOINT["password"], repr(command.environment))
        payload = json.loads(child.stdin.saved)
        self.assertEqual(set(payload), {"guards", "version", "home"})
        self.assertEqual(payload["guards"], Path(history_reader.__file__).read_text())
        self.assertEqual(child.timeouts, [20])
        self.assertTrue(child.stdout.closed)

    def test_existing_owner_foreign_context_reader_fence_and_unsupported_skip_start(
        self,
    ):
        with patch.object(service.subprocess, "Popen") as popen:
            for live in (
                {**ENDPOINT, "workspace": DATA["id"]},
                {"workspace": "foreign"},
            ):
                token = service.CONNECTION.set(live)
                try:
                    with service.history_source(self.box, DATA) as selected:
                        self.assertIs(selected, live)
                finally:
                    service.CONNECTION.reset(token)
            for variable in (history_reader.CURRENT, history_recovery.FENCE):
                token = variable.set(object())
                try:
                    with service.history_source(self.box, DATA) as selected:
                        self.assertIsNone(selected)
                finally:
                    variable.reset(token)
            for version in (None, "1.19.5", "2.2.6", "2.2.7", "2.3.1", "2.3.0+unknown"):
                with self.subTest(version=version):
                    with service.history_source(
                        self.box, {**DATA, "opencode_version": version}
                    ) as selected:
                        self.assertIsNone(selected)
            popen.assert_not_called()

    def test_only_known_prestartup_fallbacks_allow_original_path(self):
        for category in bridge.HISTORY_SOURCE_FALLBACKS:
            with self.subTest(category=category):
                child = self.child({"fallback": category})
                with patch.object(
                    service.subprocess, "Popen", return_value=child
                ) as popen:
                    with service.history_source(self.box, DATA) as selected:
                        self.assertIsNone(selected)
                        self.assertIsNone(service.live_connection())
                    self.assertEqual(popen.call_count, 1)
                self.assertEqual(child.timeouts, [20])

    def test_invalid_fallback_endpoint_and_duplicate_record_abort_without_replay(self):
        cases = (
            {"fallback": "system-config"},
            {"fallback": "private-sentinel"},
            {
                "endpoint": {
                    "url": "http://example.test:1",
                    "password": ENDPOINT["password"],
                }
            },
            {"endpoint": ENDPOINT, "fallback": "jobs-present"},
            {"endpoint": None},
        )
        for response in cases:
            with self.subTest(response=response):
                child = self.child(response)
                with patch.object(
                    service.subprocess, "Popen", return_value=child
                ) as popen:
                    with self.assertRaises(BoxError) as raised:
                        with service.history_source(self.box, DATA):
                            self.fail("Invalid startup reached caller.")
                    self.assertEqual(popen.call_count, 1)
                self.assertNotIn("private-sentinel", str(raised.exception))
                self.assertIsNone(service.live_connection())
        child = self.child()
        os.write(child.outgoing, b"{}\n")
        with patch.object(service.subprocess, "Popen", return_value=child):
            with self.assertRaises(BoxError):
                with service.history_source(self.box, DATA):
                    pass

    def test_all_base_exceptions_clean_up_and_original_identity_survives_clean_exit(
        self,
    ):
        for original in (RuntimeError("private"), KeyboardInterrupt(), SystemExit(0)):
            with self.subTest(kind=type(original).__name__):
                child = self.child()
                with patch.object(service.subprocess, "Popen", return_value=child):
                    with self.assertRaises(type(original)) as raised:
                        with service.history_source(self.box, DATA):
                            raise original
                self.assertIs(raised.exception, original)
                self.assertTrue(child.stdin.closed)
                self.assertTrue(child.stdout.closed)
                self.assertIsNone(service.live_connection())

    def test_nonzero_forced_or_failed_cleanup_prevents_success_and_resets_context(self):
        for waits in (
            (7,),
            (subprocess.TimeoutExpired("private", 20), 0),
            (
                subprocess.TimeoutExpired("private", 20),
                subprocess.TimeoutExpired("private", 5),
            ),
        ):
            with self.subTest(waits=tuple(type(item).__name__ for item in waits)):
                child = self.child(waits=waits)
                with (
                    patch.object(service.subprocess, "Popen", return_value=child),
                    patch.object(service.os, "killpg") as kill,
                    self.assertRaisesRegex(BoxError, "cleanup-failed"),
                ):
                    with service.history_source(self.box, DATA):
                        pass
                self.assertIsNone(service.live_connection())
                if len(waits) > 1:
                    kill.assert_called_once_with(child.pid, signal.SIGKILL)

    def test_cleanup_failure_preserves_interruption_and_bad_startup_is_not_replayed(
        self,
    ):
        for original in (KeyboardInterrupt(), SystemExit(0), SystemExit(7)):
            with self.subTest(kind=type(original).__name__):
                child = self.child(waits=(7,))
                with patch.object(service.subprocess, "Popen", return_value=child):
                    with self.assertRaises(type(original)) as raised:
                        with service.history_source(self.box, DATA):
                            raise original
                self.assertIs(raised.exception, original)
                self.assertTrue(original.history_source_cleanup_failed)
                self.assertIsNone(service.live_connection())
        child = self.child(status=2)
        with patch.object(service.subprocess, "Popen", return_value=child) as popen:
            with self.assertRaisesRegex(BoxError, "startup-failed"):
                with service.history_source(self.box, DATA):
                    self.fail("Failed startup reached caller.")
            self.assertEqual(popen.call_count, 1)

    def test_startup_interrupt_identity_survives_clean_or_failed_owned_cleanup(self):
        for original, waits in (
            (KeyboardInterrupt(), ()),
            (SystemExit(0), ()),
            (KeyboardInterrupt(), (7,)),
            (SystemExit(0), (7,)),
        ):
            with self.subTest(kind=type(original).__name__, waits=waits):
                child = self.child(waits=waits)
                with (
                    patch.object(service.subprocess, "Popen", return_value=child),
                    patch.object(service.HistorySource, "_write", side_effect=original),
                ):
                    with self.assertRaises(type(original)) as raised:
                        with service.history_source(self.box, DATA):
                            self.fail("Interrupted startup reached caller.")
                self.assertIs(raised.exception, original)
                self.assertTrue(child.stdout.closed)
                self.assertTrue(child.stdin.closed)
                self.assertIsNone(service.live_connection())

    def test_wait_interrupt_identity_survives_bounded_kill_and_reap_matrix(self):
        for original in (KeyboardInterrupt(), SystemExit(0), SystemExit(7)):
            for outcome in (0, 7, subprocess.TimeoutExpired("synthetic", 5)):
                with self.subTest(
                    kind=type(original).__name__, outcome=type(outcome).__name__
                ):
                    child = self.child(waits=(original, outcome))
                    with (
                        patch.object(service.subprocess, "Popen", return_value=child),
                        patch.object(service.os, "killpg") as kill,
                    ):
                        with self.assertRaises(type(original)) as raised:
                            with service.history_source(self.box, DATA):
                                pass
                    self.assertIs(raised.exception, original)
                    self.assertTrue(original.history_source_cleanup_failed)
                    self.assertEqual(child.timeouts, [20, 5])
                    kill.assert_called_once_with(child.pid, signal.SIGKILL)
                    self.assertTrue(child.stdin.closed)
                    self.assertTrue(child.stdout.closed)
                    self.assertIsNone(service.live_connection())

    def test_first_interruption_wins_over_a_cleanup_interruption_in_enter_or_exit(self):
        for startup in (True, False):
            for original in (KeyboardInterrupt(), SystemExit(7)):
                with self.subTest(startup=startup, kind=type(original).__name__):
                    child = self.child(waits=(KeyboardInterrupt(), 0))
                    with (
                        patch.object(service.subprocess, "Popen", return_value=child),
                        patch.object(service.os, "killpg"),
                        contextlib.ExitStack() as stack,
                    ):
                        if startup:
                            stack.enter_context(
                                patch.object(
                                    service.HistorySource,
                                    "_write",
                                    side_effect=original,
                                )
                            )
                        with self.assertRaises(type(original)) as raised:
                            with service.history_source(self.box, DATA):
                                raise original
                    self.assertIs(raised.exception, original)
                    self.assertTrue(child.stdin.closed)
                    self.assertTrue(child.stdout.closed)
                    self.assertIsNone(service.live_connection())

    def test_unexpected_noninterrupt_wait_failure_is_not_success(self):
        child = self.child(waits=(RuntimeError("private-sentinel"), 0))
        with (
            patch.object(service.subprocess, "Popen", return_value=child),
            patch.object(service.os, "killpg") as kill,
            self.assertRaisesRegex(
                BoxError,
                "^Private history source lease failed "
                r"\(cleanup-failed\)\.$",
            ) as raised,
        ):
            with service.history_source(self.box, DATA):
                pass
        self.assertNotIn("private-sentinel", str(raised.exception))
        kill.assert_called_once_with(child.pid, signal.SIGKILL)
        self.assertEqual(child.timeouts, [20, 5])
        self.assertIsNone(service.live_connection())

    def test_exit_observation_error_still_reaps_and_clears_connection(self):
        child = self.child()
        lease = service.HistorySource(self.box, DATA)
        with patch.object(service.subprocess, "Popen", return_value=child):
            lease.__enter__()
            with (
                patch.object(
                    service.select,
                    "select",
                    side_effect=OSError("fixed-observation-error"),
                ),
                self.assertRaisesRegex(OSError, "fixed-observation-error"),
            ):
                lease.__exit__(None, None, None)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertIsNone(service.live_connection())

    def test_oversized_and_timed_out_startup_are_bounded_and_do_not_replay(self):
        with (
            patch.object(service.subprocess, "Popen") as popen,
            patch.object(bridge, "HISTORY_SOURCE_MAX_PAYLOAD", 4),
            self.assertRaisesRegex(BoxError, "payload"),
        ):
            with service.history_source(self.box, DATA):
                pass
        popen.assert_not_called()
        child = self.child()
        with (
            patch.object(service.subprocess, "Popen", return_value=child) as popen,
            patch.object(service.HistorySource, "_write"),
            patch.object(service.time, "monotonic", side_effect=(100, 161)),
            self.assertRaisesRegex(BoxError, "startup-timeout"),
        ):
            with service.history_source(self.box, DATA):
                pass
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(child.timeouts, [20])

    def test_invalid_resource_metadata_and_reentry_never_start(self):
        with patch.object(service.subprocess, "Popen") as popen:
            for changes in (
                {"id": "../private"},
                {"container": "--private"},
                {"home": "relative"},
                {"home": "/home/../private"},
            ):
                with self.subTest(changes=changes):
                    with self.assertRaisesRegex(BoxError, "source"):
                        with service.history_source(self.box, {**DATA, **changes}):
                            pass
            popen.assert_not_called()
        child = self.child()
        lease = service.HistorySource(self.box, DATA)
        with patch.object(service.subprocess, "Popen", return_value=child) as popen:
            with lease:
                pass
            with self.assertRaisesRegex(BoxError, "reuse"):
                lease.__enter__()
            self.assertEqual(popen.call_count, 1)


class FlatImportTests(unittest.TestCase):
    def test_real_subprocess_exit_status_after_verified_or_failed_cleanup(self):
        script = r"""
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(sys.argv[1])/"src"), str(Path(sys.argv[1])/"tests")]
from test_history_reader import FakeChild, fake_box, ENDPOINT
from oc_box import workspace_service as service
value = None if sys.argv[2] == "none" else int(sys.argv[2])
failed = sys.argv[3] == "failed"
wait_interrupt = sys.argv[4] == "wait"
waits = (SystemExit(value), 0) if wait_interrupt else ((7,) if failed else ())
child = FakeChild(json.dumps({"endpoint":ENDPOINT}).encode()+b"\n", waits=waits)
data = {"id":"b"*32,"container":"c"*64,"home":"/home/example","opencode_version":"2.3.0"}
try:
    with patch.object(service.subprocess,"Popen",return_value=child), \
         patch.object(service.os,"killpg"):
        with service.history_source(fake_box(), data):
            if not wait_interrupt:
                raise SystemExit(value)
finally:
    child.dispose()
"""
        source = Path(service.__file__).resolve().parents[2]
        for code in ("none", "0", "7"):
            for failed in (False, True):
                with self.subTest(code=code, failed=failed):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-S",
                            "-B",
                            "-c",
                            script,
                            str(source),
                            code,
                            "failed" if failed else "clean",
                            "body",
                        ],
                        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                        capture_output=True,
                        timeout=20,
                    )
                    expected = 7 if code == "7" else (1 if failed else 0)
                    self.assertEqual(result.returncode, expected)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    script,
                    str(source),
                    code,
                    "failed",
                    "wait",
                ],
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 7 if code == "7" else 1)

    def test_actual_flat_modules_run_source_lifecycle_in_isolated_subprocess(self):
        """Exercise every new import site without the oc_box package existing."""
        script = r"""
import contextlib, io, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import workspace_service as service
import history_reader, history_recovery, opencode_v2_io as bridge
assert service.__package__ == ""
assert "oc_box" not in sys.modules
assert Path(service.__file__).parent == Path(sys.argv[1])
class BoxError(RuntimeError):
    pass
class Command(list):
    def __init__(self, arguments, environment):
        super().__init__(arguments)
        self.environment = environment
class Child:
    def __init__(self):
        incoming, self.outgoing = os.pipe()
        self.stdout = os.fdopen(incoming, "rb", buffering=0)
        self.stdin = tempfile.TemporaryFile()
        self.status = None
        self.timeouts = []
        endpoint = {"url":"http://127.0.0.1:23456",
                    "password":"synthetic_" + "x" * 32}
        os.write(self.outgoing, json.dumps({"endpoint":endpoint}).encode() + b"\n")
    def poll(self):
        return self.status
    def wait(self, *, timeout):
        self.timeouts.append(timeout)
        self.status = 0
        if self.outgoing is not None:
            os.close(self.outgoing)
            self.outgoing = None
        return 0
@contextlib.contextmanager
def capture(*, private):
    assert private
    with io.BytesIO() as output:
        yield output
box = SimpleNamespace(
    BoxError=BoxError, ContainerCommand=Command, PACKAGE=Path(sys.argv[1]),
    CONTAINER_OPENCODE="/synthetic/native", captured_output=capture,
    maintenance_host_env=lambda: {"PATH":"/usr/bin"},
)
data = {"id":"b"*32, "container":"c"*64, "home":"/home/example",
        "opencode_version":"2.3.0"}
probe = service.HistorySource(box, data)
probe.command()
probe.error_file = io.BytesIO((bridge.HISTORY_SOURCE_MARKER+"system-config\n").encode())
assert probe._category() == "system-config"
probe.error_file.close()
child = Child()
with patch.object(service.subprocess, "Popen", return_value=child) as popen:
    with service.history_source(box, data) as connection:
        assert connection["workspace"] == data["id"]
        assert service.live_connection() is connection
        with service.history_source(box, data) as nested:
            assert nested is connection
    assert popen.call_count == 1
assert service.live_connection() is None
assert child.timeouts == [20]
assert child.stdout.closed and child.stdin.closed
with patch.object(service.subprocess, "Popen") as popen:
    with service.history_source(box, {**data,"opencode_version":"2.2.6"}) as result:
        assert result is None
    popen.assert_not_called()
assert "oc_box" not in sys.modules
print("flat-history-source-ok")
"""
        with tempfile.TemporaryDirectory(prefix="history-source-flat-") as temporary:
            root = Path(temporary)
            for name in (
                "workspace_service.py",
                "history_reader.py",
                "history_recovery.py",
                "lifecycle_output.py",
                "workspace_control.py",
                "opencode_v2_io.py",
            ):
                (root / name).write_bytes(
                    (Path(service.__file__).parent / name).read_bytes()
                )
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", script, str(root)],
                env={"PATH": os.defpath, "HOME": str(root), "LANG": "C.UTF-8"},
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(
            result.returncode, 0, "Isolated flat lifecycle failed: " + result.stderr
        )
        self.assertEqual(result.stdout.strip(), "flat-history-source-ok")


class DeadlineTests(unittest.TestCase):
    def test_near_deadline_migration_or_prepublication_failure_gets_full_reap_budget(
        self,
    ):
        for stage in ("migration", "prepublication"):
            with self.subTest(stage=stage):
                clock = [0.0]

                class Native(FakeChild):
                    @property
                    def returncode(self):
                        return self.status

                    def wait(self, *, timeout):
                        if not self.timeouts:
                            self.timeouts.append(timeout)
                            clock[0] = 50.0  # Old startup deadline expires here.
                            raise subprocess.TimeoutExpired("synthetic-native", timeout)
                        return super().wait(timeout=timeout)

                child = Native(json.dumps({"url": ENDPOINT["url"]}).encode() + b"\n")
                self.addCleanup(child.dispose)
                calls = []

                def request(method, route, *, deadline):
                    calls.append(deadline)
                    clock[0] = 44.999
                    if stage == "migration":
                        raise bridge.HistorySourceError("activity")
                    return {"data": {"status": "completed"}}

                exit_info = {}
                with (
                    patch.object(bridge.subprocess, "Popen", return_value=child),
                    patch.object(bridge, "endpoint_request", return_value=request),
                    patch.object(
                        bridge.time, "monotonic", side_effect=lambda: clock[0]
                    ),
                    patch.object(bridge.os, "killpg") as kill,
                    patch.object(bridge.signal, "setitimer") as alarm,
                    self.assertRaisesRegex(bridge.HistorySourceError, "activity"),
                ):
                    with bridge.server(
                        "synthetic-native", startup_deadline=45, exit_info=exit_info
                    ):
                        raise bridge.HistorySourceError("activity")
                self.assertEqual(calls, [45])
                self.assertEqual(child.timeouts, [5, 5])
                self.assertEqual(exit_info, {"code": 0, "forced": True})
                kill.assert_called_once_with(child.pid, signal.SIGKILL)
                alarm.assert_not_called()
                self.assertTrue(child.stdout.closed)
                self.assertTrue(child.stdin.closed)

    def test_http_budget_covers_body_and_closes_connection_after_deadline(self):
        clock = [0.0]
        connection = Mock()

        class Response:
            status = 200
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.closed = True

            def isclosed(self):
                return self.closed

            def read1(self, maximum):
                self.assert_maximum = maximum
                if clock[0] == 0:
                    clock[0] = 44
                    return b"{}"
                clock[0] = 45.01
                self.closed = True
                return b""

        connection.getresponse.return_value = Response()
        with (
            patch.object(bridge, "HTTPConnection", return_value=connection),
            patch.object(bridge.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaisesRegex(bridge.FixedError, bridge.NOT_READY),
        ):
            bridge.endpoint_request(ENDPOINT)("GET", "/api/info", deadline=45)
        connection.close.assert_called_once()
        self.assertIn(unittest.mock.call(1), connection.sock.settimeout.call_args_list)

    def test_expired_startup_never_spawns_native(self):
        with (
            patch.object(bridge.time, "monotonic", return_value=45),
            patch.object(bridge.subprocess, "Popen") as popen,
            self.assertRaises(bridge.FixedError),
        ):
            with bridge.server("synthetic-native", startup_deadline=45):
                self.fail("Expired startup entered native lease.")
        popen.assert_not_called()


class RemoteTests(unittest.TestCase):
    def run_remote(self, *, before=None, after=None, exit_info=None, version="2.3.0"):
        settings = {"guards": "PUBLIC_GUARDS", "version": "2.3.0", "home": DATA["home"]}
        inputs = SimpleNamespace(
            buffer=io.BytesIO(json.dumps(settings).encode() + b"\n")
        )
        output = io.StringIO()
        events = []
        request = Mock(
            side_effect=lambda method, route, **kwargs: {
                "data": {} if route == "/api/session/active" else []
            }
        )
        request.endpoint = ENDPOINT
        request.alive.return_value = True

        @contextlib.contextmanager
        def server(binary, *, exit_info, startup_deadline):
            events.append("server")
            self.assertEqual(binary, "synthetic-native")
            self.assertNotIn("PRIVATE_SECRET", os.environ)
            self.assertEqual(os.environ["OPENCODE_DB"], "oc-box-v2.db")
            self.assertEqual(
                os.environ["XDG_DATA_HOME"], DATA["home"] + "/.local/share"
            )
            policy = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
            self.assertIs(policy["warming"], False)
            self.assertEqual(
                {rule["action"] for rule in policy["policies"]},
                {"provider.use", "plugin.use", "tool.use"},
            )
            try:
                yield request
            finally:
                events.append("reaped")
                exit_info.update(self.exit_result)

        def execute(source, namespace):
            self.assertEqual(source, "PUBLIC_GUARDS")
            namespace.update(
                {
                    "_remote_environment": history_reader._remote_environment,
                    "_check_version": history_reader._check_version,
                    "Refusal": history_reader.Refusal,
                }
            )

        outcomes = iter((before, after, None))

        def guard(*_, **kwargs):
            events.append("guard")
            value = next(outcomes)
            if isinstance(value, BaseException):
                raise value
            return value

        self.exit_result = exit_info or {"code": 0, "forced": False}
        with (
            patch.dict(os.environ, {"PRIVATE_SECRET": "private-sentinel"}, clear=True),
            patch.object(bridge.sys, "stdin", inputs),
            patch.object(
                bridge,
                "_history_source_payload",
                return_value=json.dumps(settings).encode() + b"\n",
            ),
            patch.object(bridge.sys, "stdout", output),
            patch.object(bridge.os, "chdir"),
            patch.object(bridge.os, "read", return_value=b""),
            patch.object(bridge.select, "select", return_value=([inputs], [], [])),
            patch.object(bridge.signal, "signal"),
            patch.object(bridge.signal, "setitimer") as timer,
            patch.object(bridge, "exec", side_effect=execute, create=True),
            patch.object(bridge, "history_source_database", side_effect=guard),
            patch.object(
                bridge.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=version),
            ),
            patch.object(bridge, "server", side_effect=server),
        ):
            bridge.history_source_lease("synthetic-native")
        return events, output, request, timer

    def test_guard_precedes_startup_empty_discovery_roots_and_no_cleanup_alarms(self):
        events, output, request, timer = self.run_remote()
        self.assertEqual(events, ["guard", "server", "guard", "guard", "reaped"])
        self.assertEqual(json.loads(output.getvalue()), {"endpoint": ENDPOINT})
        self.assertEqual(request.call_count, 4)
        timer.assert_not_called()

    def test_known_incompatible_metadata_falls_back_before_any_native_service(self):
        for category in bridge.HISTORY_SOURCE_FALLBACKS:
            with self.subTest(category=category):
                events, output, request, _ = self.run_remote(before=category)
                self.assertEqual(events, ["guard"])
                self.assertEqual(json.loads(output.getvalue()), {"fallback": category})
                request.assert_not_called()

    def test_unsafe_startup_changed_version_or_poststart_work_never_falls_back(self):
        for options, category in (
            ({"before": bridge.HistorySourceError("system-config")}, "system-config"),
            ({"version": "2.3.1"}, "unsupported-version"),
            ({"after": "jobs-present"}, "activity"),
        ):
            with self.subTest(options=tuple(options)):
                with self.assertRaisesRegex(bridge.HistorySourceError, category):
                    self.run_remote(**options)

    def test_abnormal_native_cleanup_cannot_succeed(self):
        for result in ({"code": 1, "forced": False}, {"code": 0, "forced": True}):
            with self.subTest(result=result):
                with self.assertRaisesRegex(
                    bridge.HistorySourceError, "cleanup-failed"
                ):
                    self.run_remote(exit_info=result)

    def test_activity_shape_and_nonempty_jobs_are_fatal_not_recovery(self):
        for response in (
            None,
            {},
            {"data": None},
            {"data": ["private"]},
            {"data": {"private": {"type": "running"}}},
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(bridge.HistorySourceError, "^activity$"):
                    bridge.history_source_quiet(Mock(return_value=response))

    def test_payload_is_bounded_and_rejected_before_public_code_or_native(self):
        cases = (
            b"",
            b"{}",
            b"{}\n",
            json.dumps(
                {"guards": "PUBLIC", "version": "2.3.1", "home": DATA["home"]}
            ).encode()
            + b"\n",
            json.dumps(
                {"guards": "PUBLIC", "version": "2.3.0", "home": "/home/../private"}
            ).encode()
            + b"\n",
        )
        for payload in cases:
            with self.subTest(size=len(payload)):
                inputs = SimpleNamespace(buffer=io.BytesIO(payload))
                with (
                    patch.object(bridge.sys, "stdin", inputs),
                    patch.object(
                        bridge, "_history_source_payload", return_value=payload
                    ),
                    patch.object(bridge.signal, "signal"),
                    patch.object(bridge.signal, "setitimer"),
                    patch.object(bridge, "exec", create=True) as execute,
                    patch.object(bridge.subprocess, "run") as native,
                    self.assertRaises(bridge.HistorySourceError),
                ):
                    bridge.history_source_lease("synthetic-native")
                execute.assert_not_called()
                native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
