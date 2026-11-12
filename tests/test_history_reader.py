"""Synthetic SQLite/pipe tests only; no native executables or container services."""

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import history_reader as reader, workspace_service


SCHEMA = "CREATE TABLE schedule (id TEXT PRIMARY KEY, session_id TEXT, status TEXT, last_message_id TEXT)"
ENDPOINT = {"url": "http://127.0.0.1:23456", "password": "synthetic_" + "x" * 32}
DATA = {
    "id": "synthetic-workspace",
    "container": "a" * 64,
    "home": "/home/example",
    "opencode_version": "2.2.6",
}


class BoxError(RuntimeError):
    pass


class ContainerCommand(list):
    def __init__(self, arguments, environment):
        super().__init__(arguments)
        self.environment = environment


class RecordingInput:
    def __init__(self):
        self.stream = tempfile.TemporaryFile()

    def fileno(self):
        return self.stream.fileno()

    @property
    def closed(self):
        return self.stream.closed

    def close(self):
        if self.closed:
            return
        self.stream.seek(0)
        self.saved = self.stream.read()
        self.stream.close()


class FakeChild:
    """Public readiness bytes through a real local pipe, not a native process."""

    def __init__(self, readiness=None, *, status=None, waits=()):
        incoming, outgoing = os.pipe()
        self.stdout = os.fdopen(incoming, "rb", buffering=0)
        self.outgoing = outgoing
        self.stdin = RecordingInput()
        self.pid = 987654
        self.status = status
        self.waits = list(waits)
        self.timeouts = []
        os.write(
            outgoing,
            readiness
            if readiness is not None
            else json.dumps(ENDPOINT).encode() + b"\n",
        )

    def poll(self):
        return self.status

    def wait(self, *, timeout):
        self.timeouts.append(timeout)
        value = 0
        if self.waits:
            value = self.waits.pop(0)
            if isinstance(value, BaseException):
                raise value
        self.status = value
        if self.outgoing is not None:
            os.close(self.outgoing)
            self.outgoing = None
        return value

    def dispose(self):
        if self.outgoing is not None:
            os.close(self.outgoing)
            self.outgoing = None
        if not self.stdout.closed:
            self.stdout.close()
        if not self.stdin.closed:
            self.stdin.close()


def fake_box():
    captures = []

    @contextlib.contextmanager
    def captured_output(*, private=False):
        captures.append(private)
        with io.BytesIO() as stream:
            yield stream

    result = SimpleNamespace(
        BoxError=BoxError,
        ContainerCommand=ContainerCommand,
        PACKAGE=Path(reader.__file__).parent,
        CONTAINER_OPENCODE="/opt/oc-box/runtime/bin/opencode",
        maintenance_host_env=Mock(
            return_value={"HOME": "/home/example", "PATH": "/usr/bin"}
        ),
        captured_output=captured_output,
        captures=captures,
        host_command=Mock(side_effect=lambda data, *args: ["opencode", *args]),
        run=Mock(return_value=SimpleNamespace(stdout='{"data": {}}')),
    )
    result.run.side_effect = lambda command, **_kwargs: (
        SimpleNamespace(stdout="opencode v2.2.6\n")
        if command[1:] == ["--version"]
        else result.run.return_value
    )
    return result


class SchedulerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="history-reader-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "synthetic.db"

    def create(self, schema=SCHEMA):
        with (
            contextlib.closing(sqlite3.connect(self.database)) as connection,
            connection,
        ):
            connection.execute(schema)

    def refusal(self, category, **options):
        with self.assertRaises(reader.Refusal) as raised:
            reader._check_database(self.database, **options)
        self.assertEqual(raised.exception.category, category)
        self.assertEqual(str(raised.exception), f"History reader refused ({category}).")

    def test_empty_known_table_is_read_only_without_side_files_or_transcript_queries(
        self,
    ):
        self.create()
        before = self.database.read_bytes()
        connect = sqlite3.connect
        queries = []

        def observed(*args, **kwargs):
            self.assertTrue(args[0].endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            result = connect(*args, **kwargs)
            result.set_trace_callback(queries.append)
            return result

        with patch.object(reader.sqlite3, "connect", side_effect=observed):
            reader._check_database(self.database)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.database])
        self.assertIn("PRAGMA query_only=ON", queries)
        self.assertTrue(any("SELECT 1" in query for query in queries))
        self.assertFalse(
            any(
                "session_v2" in query
                or "message" in query
                and "table_info" not in query
                for query in queries
            )
        )

    def test_every_status_and_nonempty_last_message_refuse_without_row_edits(self):
        self.create()
        for status in (
            "active",
            "paused",
            "done",
            "error",
            "cancelled",
            "unknown",
            None,
        ):
            with self.subTest(status=status):
                with (
                    contextlib.closing(sqlite3.connect(self.database)) as connection,
                    connection,
                ):
                    connection.execute("DELETE FROM schedule")
                    connection.execute(
                        "INSERT INTO schedule VALUES (?, ?, ?, ?)",
                        ("private-id", "private-session", status, "private-message"),
                    )
                before = self.database.read_bytes()
                self.refusal("schedule-present")
                self.assertEqual(self.database.read_bytes(), before)

    def test_committed_wal_rows_are_not_hidden_by_immutable_reading(self):
        self.create()
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO schedule (id, status) VALUES ('private-id', 'done')"
        )
        connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").is_file())
        self.refusal("schedule-present")

    def test_missing_source_refuses_and_missing_destination_never_creates_file(self):
        self.refusal("database-missing")
        reader._check_database(self.database, allow_missing=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_schema_view_columns_types_and_primary_key_refuse(self):
        cases = (
            "CREATE TABLE unrelated (id TEXT)",
            "CREATE VIEW schedule AS SELECT 'id' AS id, 'session' AS session_id, 'done' AS status, NULL AS last_message_id",
            "CREATE TABLE schedule (id TEXT PRIMARY KEY, session_id TEXT, status TEXT)",
            "CREATE TABLE schedule (id TEXT PRIMARY KEY, session_id INTEGER, status TEXT, last_message_id TEXT)",
            "CREATE TABLE schedule (id TEXT, session_id TEXT, status TEXT, last_message_id TEXT)",
        )
        for schema in cases:
            with self.subTest(schema=schema):
                self.database.unlink(missing_ok=True)
                self.create(schema)
                self.refusal("schedule-schema")

    def test_final_symlink_directory_fifo_and_foreign_owner_refuse(self):
        real = self.root / "target.db"
        real.write_bytes(b"not a database")
        self.database.symlink_to(real)
        self.refusal("database-unsafe", allow_missing=True)
        self.database.unlink()
        self.database.mkdir()
        self.refusal("database-unsafe")
        self.database.rmdir()
        os.mkfifo(self.database)
        self.refusal("database-unsafe")
        self.database.unlink()
        self.create()
        with patch.object(reader.os, "getuid", return_value=os.getuid() + 1):
            self.refusal("database-unsafe")

    def test_malformed_database_has_only_fixed_category(self):
        self.database.write_bytes(b"private-sentinel-not-sqlite")
        self.refusal("database-invalid")

    def test_hardlinked_or_other_writable_database_is_not_a_recovery_source(self):
        self.create()
        link = self.root / "linked.db"
        os.link(self.database, link)
        self.refusal("database-unsafe")
        link.unlink()
        self.database.chmod(0o666)
        self.refusal("database-unsafe")


class SystemConfigGuardTests(unittest.TestCase):
    def test_absent_or_empty_system_directory_only_is_acceptable(self):
        with tempfile.TemporaryDirectory(prefix="history-system-config-") as temporary:
            root = Path(temporary) / "opencode"
            with patch.object(reader, "Path", return_value=root):
                reader._check_system_config()
                root.mkdir(mode=0o755)
                reader._check_system_config()
                managed = root / "opencode.json"
                managed.write_text('{"synthetic": true}')
                with self.assertRaisesRegex(reader.Refusal, "system-config"):
                    reader._check_system_config()
                managed.unlink()
                root.chmod(0o777)
                with self.assertRaisesRegex(reader.Refusal, "system-config"):
                    reader._check_system_config()
                root.chmod(0o755)
                root.rmdir()
                root.symlink_to(temporary)
                with self.assertRaisesRegex(reader.Refusal, "system-config"):
                    reader._check_system_config()


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        system = patch.object(reader, "_check_system_config")
        system.start()
        self.addCleanup(system.stop)

    def test_changed_host_native_version_refuses_before_path_or_store_projection(self):
        box = fake_box()
        box.run.side_effect = None
        box.run.return_value.stdout = "opencode v2.2.7\n"
        with patch.object(reader, "_check_database") as guard:
            with self.assertRaisesRegex(BoxError, "unsupported-version"):
                reader.assert_host_safe(
                    box, Path("/synthetic/workspace.json"), DATA, {}, lambda: 3
                )
        guard.assert_not_called()
        self.assertEqual(box.run.call_count, 1)
        self.assertEqual(box.run.call_args.args[0], ["opencode", "--version"])

    def test_fence_keeps_public_store_selectors_but_no_credentials_or_execution_config(
        self,
    ):
        original = {
            "PATH": "/usr/bin",
            "HOME": "/home/example",
            "XDG_DATA_HOME": "/synthetic/data",
            "XDG_STATE_HOME": "/synthetic/state",
            "OPENCODE_DB": "target.db",
            "OPENCODE_CONFIG_DIR": "/synthetic/config",
            "OPENCODE_TEST_HOME": "/synthetic/empty",
            "OPENCODE_DISABLE_CHANNEL_DB": "1",
            "OPENCODE_FLAGS": "all",
            "OPENCODE_CONFIG": "/private/profile",
            "OPENCODE_CONFIG_CONTENT": "private-sentinel",
            "OPENCODE_AUTH_CONTENT": "private-sentinel",
            "OPENCODE_PASSWORD": "private-sentinel",
            "ANTHROPIC_API_KEY": "private-sentinel",
            "CUSTOM_SECRET": "private-sentinel",
            "NODE_OPTIONS": "private-sentinel",
            "PYTHONPATH": "private-sentinel",
        }
        copy = dict(original)
        result = reader.fence_environment(original)
        self.assertEqual(original, copy)
        self.assertNotIn("private-sentinel", repr(result))
        for name in (
            "HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "OPENCODE_DB",
            "OPENCODE_CONFIG_DIR",
            "OPENCODE_TEST_HOME",
            "OPENCODE_DISABLE_CHANNEL_DB",
        ):
            self.assertEqual(result[name], original[name])
        self.assertEqual(result["OPENCODE_FLAGS"], "-schedules")
        self.assertEqual(result["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        settings = json.loads(result["OPENCODE_CONFIG_CONTENT"])
        for name in ("snapshots", "formatter", "warming"):
            self.assertIs(settings[name], False)
        self.assertEqual(settings["update"], "disable")
        self.assertEqual(
            settings["policies"],
            [
                {"action": action, "resource": "*", "effect": "deny"}
                for action in ("plugin.use", "provider.use", "tool.use")
            ],
        )

    def test_host_destination_uses_native_path_handler_and_local_guard_only(self):
        box = fake_box()
        with tempfile.TemporaryDirectory(prefix="history-host-test-") as temporary:
            database = Path(temporary) / "absent.db"
            box.run.return_value.stdout = str(database) + "\n"
            result = reader.assert_host_safe(
                box,
                Path(temporary) / "workspace.json",
                DATA,
                {"OPENCODE_DB": "custom.db", "CUSTOM_SECRET": "private-sentinel"},
                lambda: 3,
            )
            self.assertFalse(database.exists())
            self.assertEqual(result["OPENCODE_DB"], "custom.db")
            box.host_command.assert_called_once_with(DATA, "debug", "paths", "db")
            call = box.run.call_args
            self.assertEqual(call.args[0], ["opencode", "debug", "paths", "db"])
            self.assertTrue(call.kwargs["private"])
            self.assertTrue(call.kwargs["capture"])
            self.assertEqual(call.kwargs["timeout"], 3)
            self.assertNotIn("private-sentinel", repr(call))
            with (
                contextlib.closing(sqlite3.connect(database)) as connection,
                connection,
            ):
                connection.execute(SCHEMA)
                connection.execute("INSERT INTO schedule (id) VALUES ('private-id')")
            with self.assertRaisesRegex(BoxError, "schedule-present"):
                reader.assert_host_safe(box, None, DATA, {}, lambda: 3)

    def test_host_probe_masks_errors_and_refuses_bad_paths_and_unknown_versions(self):
        for value in ("", ":memory:", "/tmp/a\n/tmp/b", "/tmp/\0bad", "/" + "x" * 4097):
            with self.subTest(value=value):
                box = fake_box()
                box.run.return_value.stdout = value
                with self.assertRaisesRegex(BoxError, "host-database-path"):
                    reader.assert_host_safe(box, None, DATA, {}, lambda: 5)
        box = fake_box()
        box.run.side_effect = BoxError("private-sentinel")
        with self.assertRaisesRegex(BoxError, "host-database-probe") as raised:
            reader.assert_host_safe(box, None, DATA, {}, lambda: 5)
        self.assertNotIn("private-sentinel", str(raised.exception))
        box.run.reset_mock()
        with self.assertRaisesRegex(BoxError, "unsupported-version"):
            reader.assert_host_safe(
                box, None, {**DATA, "opencode_version": "2.2.8"}, {}, lambda: 5
            )
        box.run.assert_not_called()

    def test_native_destination_filename_whitespace_is_not_stripped(self):
        box = fake_box()
        with tempfile.TemporaryDirectory(prefix="history-host-test-") as temporary:
            database = Path(temporary) / "store with trailing space "
            with (
                contextlib.closing(sqlite3.connect(database)) as connection,
                connection,
            ):
                connection.execute(SCHEMA)
                connection.execute("INSERT INTO schedule (id) VALUES ('private-id')")
            box.run.return_value.stdout = str(database) + "\n"
            with self.assertRaisesRegex(BoxError, "schedule-present"):
                reader.assert_host_safe(box, None, DATA, {}, lambda: 5)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.box = fake_box()
        self.remaining = Mock(return_value=4)
        self.lease = reader.Reader(self.box, DATA, remaining=self.remaining)
        self.children = []
        self.addCleanup(self.dispose)

    def dispose(self):
        for child in self.children:
            child.dispose()

    def child(self, *args, **kwargs):
        value = FakeChild(*args, **kwargs)
        self.children.append(value)
        return value

    def test_one_private_lease_scoped_connections_idle_boundaries_and_no_election(self):
        child = self.child()
        old = {"workspace": "previous-context"}
        token = workspace_service.CONNECTION.set(old)
        self.addCleanup(workspace_service.CONNECTION.reset, token)
        with patch.object(reader.subprocess, "Popen", return_value=child) as popen:
            with self.lease as entered:
                self.assertIs(entered, self.lease)
                self.assertIs(reader.CURRENT.get(), self.lease)
                self.assertIs(workspace_service.CONNECTION.get(), self.lease.connection)
                self.assertEqual(self.lease.connection["workspace"], DATA["id"])
                self.assertEqual(popen.call_count, 1)
                args = popen.call_args.args[0]
                self.assertEqual(
                    args[:6],
                    ["podman", "exec", "-i", "--workdir", "/", DATA["container"]],
                )
                self.assertIn("/usr/bin/env", args)
                self.assertIn("-i", args)
                self.assertEqual(
                    args[-3:], ["lease", DATA["home"], self.box.CONTAINER_OPENCODE]
                )
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                self.assertEqual(self.box.captures, [True])
            self.assertEqual(popen.call_count, 1)
        self.assertIsNone(reader.CURRENT.get())
        self.assertIs(workspace_service.CONNECTION.get(), old)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertEqual(child.timeouts, [10])
        payload = json.loads(child.stdin.saved)
        self.assertEqual(set(payload), {"bridge", "version"})
        self.assertEqual(payload["version"], DATA["opencode_version"])
        self.assertEqual(
            payload["bridge"], (self.box.PACKAGE / "opencode_v2_io.py").read_text()
        )
        self.assertEqual(self.box.run.call_count, 2)
        for call in self.box.run.call_args_list:
            command = call.args[0]
            self.assertEqual(
                command[-6:],
                [
                    self.box.CONTAINER_OPENCODE,
                    "api",
                    "get",
                    "/api/session/active",
                    "--server",
                    ENDPOINT["url"],
                ],
            )
            self.assertEqual(
                command.environment["OPENCODE_PASSWORD"], ENDPOINT["password"]
            )
            self.assertNotIn(ENDPOINT["password"], " ".join(command))
            self.assertTrue(call.kwargs["private"])
            self.assertEqual(call.kwargs["timeout"], 4)

    def test_source_command_clears_container_env_and_never_uses_host_provider_values(
        self,
    ):
        sentinel = {
            "CUSTOM_SECRET": "private-sentinel",
            "ANTHROPIC_API_KEY": "private-sentinel",
            "OPENCODE_AUTH_CONTENT": "private-sentinel",
        }
        with patch.dict(os.environ, sentinel):
            before = dict(os.environ)
            self.lease.connection = {**ENDPOINT, "workspace": DATA["id"]}
            command = self.lease.command(
                [self.box.CONTAINER_OPENCODE, "session", "list"]
            )
            self.assertEqual(dict(os.environ), before)
        self.assertNotIn("private-sentinel", repr(command))
        self.assertNotIn("private-sentinel", repr(command.environment))
        self.assertEqual(
            set(command.environment), {"HOME", "PATH", "OPENCODE_PASSWORD"}
        )
        self.assertIn("-I", command)
        self.assertEqual(
            command[-5:],
            [
                self.box.CONTAINER_OPENCODE,
                "session",
                "list",
                "--server",
                ENDPOINT["url"],
            ],
        )
        for args in (
            ["--standalone"],
            ["serve"],
            ["service", "status"],
            ["api", "get", "/", "--server=http://127.0.0.1:1"],
            ["api", "get", "/", "--server", "http://127.0.0.1:1"],
        ):
            with (
                self.subTest(args=args),
                self.assertRaisesRegex(BoxError, "client-command"),
            ):
                self.lease.command([self.box.CONTAINER_OPENCODE, *args])

    def test_foreground_or_malformed_idle_response_refuses_without_querying_jobs(self):
        for response, category in (
            (None, "active-response"),
            ({}, "active-response"),
            ({"data": []}, "active-response"),
            ({"data": {"private-session": {"type": "running"}}}, "foreground-active"),
        ):
            with self.subTest(category=category, response=response):
                lease = reader.Reader(self.box, DATA, remaining=lambda: 2)
                child = self.child()
                with patch.object(reader.subprocess, "Popen", return_value=child):
                    with lease:
                        self.box.run.return_value.stdout = json.dumps(response)
                        with self.assertRaisesRegex(BoxError, category) as raised:
                            lease.assert_idle()
                        self.assertNotIn("private-session", str(raised.exception))
                        self.box.run.return_value.stdout = '{"data": {}}'

    def test_before_and_after_activity_failures_cleanup_and_restore_context(self):
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                lease = reader.Reader(self.box, DATA, remaining=lambda: 2)
                child = self.child()
                busy = SimpleNamespace(
                    stdout='{"data": {"private-id": {"type": "running"}}}'
                )
                idle = SimpleNamespace(stdout='{"data": {}}')
                self.box.run.side_effect = [busy] if stage == "before" else [idle, busy]
                with patch.object(reader.subprocess, "Popen", return_value=child):
                    with self.assertRaises(BoxError) as raised:
                        with lease:
                            pass
                self.assertNotIn("private-id", str(raised.exception))
                self.assertIsNone(reader.CURRENT.get())
                self.assertIsNone(workspace_service.CONNECTION.get(None))
                self.assertTrue(child.stdin.closed)
                self.assertEqual(child.timeouts, [10])
                self.box.run.side_effect = None

    def test_startup_error_whitelist_hides_all_private_native_output(self):
        for category in ("schedule-present", "schedule-schema", "private-sentinel"):
            with self.subTest(category=category):
                lease = reader.Reader(self.box, DATA, remaining=lambda: 2)
                child = self.child(status=2)

                def start(*args, category=category, child=child, **kwargs):
                    kwargs["stderr"].write(
                        (
                            "private-sentinel\n" + reader.MARKER + category + "\n"
                        ).encode()
                    )
                    return child

                with patch.object(reader.subprocess, "Popen", side_effect=start):
                    with self.assertRaises(BoxError) as raised:
                        lease.__enter__()
                expected = (
                    category
                    if category in reader.GUARD_CATEGORIES
                    else "startup-failed"
                )
                self.assertEqual(
                    str(raised.exception), f"History reader refused ({expected})."
                )
                self.assertTrue(child.stdin.closed)
                self.assertIsNone(reader.CURRENT.get())

    def test_invalid_or_duplicate_control_records_never_publish_a_connection(self):
        for payload in (
            b"private-sentinel\n",
            b"{}\n",
            json.dumps({**ENDPOINT, "url": "http://example.test:1"}).encode() + b"\n",
            json.dumps(ENDPOINT).encode() + b"\n{}\n",
            b"x" * 129,
        ):
            with self.subTest(payload=payload):
                lease = reader.Reader(self.box, DATA, remaining=lambda: 2)
                child = self.child(payload)
                with (
                    patch.object(reader, "MAX_CONTROL", 128),
                    patch.object(reader.subprocess, "Popen", return_value=child),
                ):
                    with self.assertRaises(BoxError) as raised:
                        lease.__enter__()
                self.assertNotIn("private-sentinel", str(raised.exception))
                self.assertIsNone(reader.CURRENT.get())
                self.assertIsNone(workspace_service.CONNECTION.get(None))

    def test_cleanup_deadline_is_independent_and_kills_only_owned_wrapper_group(self):
        child = self.child(waits=(subprocess.TimeoutExpired("private-sentinel", 10), 0))
        with (
            patch.object(reader.subprocess, "Popen", return_value=child),
            patch.object(reader.os, "killpg") as kill,
        ):
            with self.assertRaisesRegex(RuntimeError, "caller-failure"):
                with self.lease:
                    self.remaining.return_value = 0
                    raise RuntimeError("caller-failure")
            kill.assert_called_once_with(child.pid, signal.SIGKILL)
        self.assertEqual(child.timeouts, [10, 5])
        self.assertIsNone(reader.CURRENT.get())
        self.assertIsNone(workspace_service.CONNECTION.get(None))

    def test_cleanup_failure_still_resets_both_tokens(self):
        child = self.child(
            waits=(
                subprocess.TimeoutExpired("private", 10),
                subprocess.TimeoutExpired("private", 5),
            )
        )
        with (
            patch.object(reader.subprocess, "Popen", return_value=child),
            patch.object(reader.os, "killpg"),
        ):
            with self.assertRaisesRegex(BoxError, "cleanup-failed"):
                with self.lease:
                    pass
        self.assertIsNone(reader.CURRENT.get())
        self.assertIsNone(workspace_service.CONNECTION.get(None))
        self.assertEqual(self.lease.tokens, [])

    def test_forced_or_failed_shutdown_cannot_report_success(self):
        for waits in ((7,), (subprocess.TimeoutExpired("private", 10), 0)):
            with self.subTest(waits=tuple(type(value).__name__ for value in waits)):
                lease = reader.Reader(self.box, DATA, remaining=lambda: 2)
                child = self.child(waits=waits)
                with (
                    patch.object(reader.subprocess, "Popen", return_value=child),
                    patch.object(reader.os, "killpg"),
                    self.assertRaisesRegex(BoxError, "cleanup-failed"),
                ):
                    with lease:
                        pass
                self.assertIsNone(reader.CURRENT.get())
                self.assertIsNone(workspace_service.CONNECTION.get(None))

    def test_caller_exception_identity_survives_failed_shutdown(self):
        original = KeyboardInterrupt()
        child = self.child(waits=(7,))
        with patch.object(reader.subprocess, "Popen", return_value=child):
            with self.assertRaises(KeyboardInterrupt) as raised:
                with self.lease:
                    raise original
        self.assertIs(raised.exception, original)
        self.assertIsNone(reader.CURRENT.get())

    def test_readiness_timeout_is_capped_at_45_seconds_even_with_larger_budget(self):
        self.remaining.return_value = 1000
        child = self.child(b"")
        with (
            patch.object(reader.subprocess, "Popen", return_value=child),
            patch.object(reader.time, "monotonic", side_effect=[100, 100, 145]),
            patch.object(
                reader.select, "select", return_value=([], [], [])
            ) as select_call,
        ):
            with self.assertRaisesRegex(BoxError, "startup-timeout"):
                self.lease.__enter__()
        select_call.assert_called_once()
        self.assertEqual(select_call.call_args.args[3], 0.1)
        self.assertEqual(child.timeouts, [10])

    def test_guard_marker_is_preserved_when_stdout_eof_precedes_visible_exit(self):
        child = self.child(b"")
        os.close(child.outgoing)
        child.outgoing = None

        def spawn(*args, **kwargs):
            kwargs["stderr"].write(
                (reader.MARKER + "schedule-present\nprivate-sentinel\n").encode()
            )
            return child

        with patch.object(reader.subprocess, "Popen", side_effect=spawn):
            with self.assertRaisesRegex(BoxError, "schedule-present") as raised:
                self.lease.__enter__()
        self.assertNotIn("private-sentinel", str(raised.exception))
        self.assertIsNone(reader.current())

    def test_deadline_after_active_query_refuses_success_and_still_cleans_up(self):
        child = self.child()

        def request(*args, **kwargs):
            self.remaining.return_value = 0
            return SimpleNamespace(stdout='{"data": {}}')

        self.box.run.side_effect = request
        with patch.object(reader.subprocess, "Popen", return_value=child):
            with self.assertRaises(BoxError):
                self.lease.__enter__()
        self.assertEqual(self.box.run.call_count, 1)
        self.assertEqual(child.timeouts, [10])
        self.assertIsNone(reader.CURRENT.get())
        self.assertIsNone(workspace_service.CONNECTION.get(None))

    def test_delayed_second_control_record_and_nested_reader_are_refused(self):
        child = self.child()
        with patch.object(reader.subprocess, "Popen", return_value=child) as popen:
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                with self.lease:
                    with self.assertRaisesRegex(BoxError, "reader-reuse"):
                        reader.Reader(self.box, DATA, remaining=lambda: 2).__enter__()
                    self.assertIs(reader.CURRENT.get(), self.lease)
                    os.write(child.outgoing, b"{}\n")
                    with self.assertRaisesRegex(BoxError, "startup-response"):
                        self.lease.assert_idle()
                    # Propagate uncertainty so __exit__ only cleans up.
                    raise RuntimeError("uncertain")
            self.assertEqual(popen.call_count, 1)

    def test_invalid_versions_deadlines_and_reentry_never_start_another_service(self):
        with patch.object(reader.subprocess, "Popen") as popen:
            for release in ("1.19.5", "2.2.5", "2.2.8", "2.2.7+unknown"):
                with (
                    self.subTest(release=release),
                    self.assertRaisesRegex(BoxError, "unsupported-version"),
                ):
                    reader.Reader(
                        self.box,
                        {**DATA, "opencode_version": release},
                        remaining=lambda: 1,
                    ).__enter__()
            for value in (0, -1, float("nan"), float("inf")):
                with (
                    self.subTest(remaining=value),
                    self.assertRaisesRegex(BoxError, "deadline"),
                ):
                    reader.Reader(
                        self.box, DATA, remaining=lambda value=value: value
                    ).__enter__()
            popen.assert_not_called()
        child = self.child()
        with patch.object(reader.subprocess, "Popen", return_value=child) as popen:
            with self.lease:
                pass
            with self.assertRaisesRegex(BoxError, "reader-reuse"):
                self.lease.__enter__()
            self.assertEqual(popen.call_count, 1)


class RemoteHelperTests(unittest.TestCase):
    def setUp(self):
        system = patch.object(reader, "_check_system_config")
        system.start()
        self.addCleanup(system.stop)

    def test_changed_source_binary_version_refuses_before_native_service_or_bridge(
        self,
    ):
        inputs = SimpleNamespace(
            buffer=io.BytesIO(b'{"bridge":"public","version":"2.2.6"}\n')
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(reader.sys, "stdin", inputs),
            patch.object(reader.os, "chdir"),
            patch.object(reader, "_check_database"),
            patch.object(
                reader.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="opencode v2.2.7\n"),
            ),
            patch.object(reader, "exec", create=True) as execute,
        ):
            with self.assertRaisesRegex(reader.Refusal, "unsupported-version"):
                reader._remote(["lease", DATA["home"], "synthetic-opencode"])
        execute.assert_not_called()

    def test_remote_guard_precedes_single_bridge_and_empty_private_config_is_cleaned(
        self,
    ):
        events = []
        roots = []
        output = io.StringIO()
        inputs = SimpleNamespace(
            buffer=io.BytesIO(b'{"bridge":"public bridge source","version":"2.2.6"}\n')
        )

        @contextlib.contextmanager
        def server(binary, *, exit_info):
            events.append("server")
            self.assertEqual(binary, "synthetic-opencode")
            self.assertNotIn("CUSTOM_SECRET", os.environ)
            self.assertEqual(
                os.environ["XDG_DATA_HOME"], DATA["home"] + "/.local/share"
            )
            self.assertEqual(os.environ["OPENCODE_DB"], "oc-box-v2.db")
            self.assertEqual(os.environ["OPENCODE_FLAGS"], "-schedules")
            root = Path(os.environ["HOME"]).parent
            roots.append(root)
            self.assertEqual(list((root / "home").iterdir()), [])
            self.assertEqual(list((root / "config").iterdir()), [])
            try:
                yield SimpleNamespace(endpoint=ENDPOINT, alive=lambda: True)
            finally:
                events.append("reaped")
                exit_info.update(code=0, forced=False)

        def execute(source, namespace):
            self.assertEqual(source, "public bridge source")
            self.assertNotEqual(namespace["__name__"], "__main__")
            namespace["server"] = server
            events.append("bridge")

        def version(command, **_kwargs):
            self.assertEqual(command, ["synthetic-opencode", "--version"])
            events.append("version")
            return SimpleNamespace(stdout="opencode v2.2.6\n", returncode=0)

        with (
            patch.dict(os.environ, {"CUSTOM_SECRET": "private-sentinel"}, clear=True),
            patch.object(reader.sys, "stdin", inputs),
            patch.object(reader.sys, "stdout", output),
            patch.object(reader.os, "chdir"),
            patch.object(reader.os, "read", return_value=b""),
            patch.object(reader.select, "select", return_value=([inputs], [], [])),
            patch.object(
                reader,
                "_check_database",
                side_effect=lambda *args: events.append("guard"),
            ) as guard,
            patch.object(reader, "exec", side_effect=execute, create=True),
            patch.object(reader.subprocess, "run", side_effect=version),
        ):
            self.assertEqual(
                reader._remote(["lease", DATA["home"], "synthetic-opencode"]), 0
            )
        guard.assert_called_once_with(
            Path(DATA["home"]) / ".local/share/opencode/oc-box-v2.db"
        )
        self.assertEqual(events, ["guard", "version", "bridge", "server", "reaped"])
        self.assertEqual(json.loads(output.getvalue()), ENDPOINT)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_guard_refusal_never_executes_bridge_or_native(self):
        inputs = SimpleNamespace(buffer=io.BytesIO(b'{"bridge":"public"}\n'))
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(reader.sys, "stdin", inputs),
            patch.object(
                reader,
                "_check_database",
                side_effect=reader.Refusal("schedule-present"),
            ),
            patch.object(reader, "exec", create=True) as execute,
            patch.object(reader.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(reader.Refusal, "schedule-present"):
                reader._remote(["lease", DATA["home"], "synthetic-opencode"])
        execute.assert_not_called()
        popen.assert_not_called()

    def test_remote_client_uses_only_ephemeral_token_and_owns_empty_config_lifetime(
        self,
    ):
        roots = []

        def run(arguments, *, env):
            self.assertEqual(
                arguments,
                [
                    "synthetic-opencode",
                    "api",
                    "get",
                    "/api/session/active",
                    "--server",
                    ENDPOINT["url"],
                ],
            )
            self.assertEqual(env["OPENCODE_PASSWORD"], ENDPOINT["password"])
            self.assertNotIn("CUSTOM_SECRET", env)
            self.assertNotIn("OPENCODE_AUTH_CONTENT", env)
            self.assertEqual(env["XDG_DATA_HOME"], DATA["home"] + "/.local/share")
            roots.append(Path(env["HOME"]).parent)
            self.assertTrue(roots[-1].is_dir())
            return 0

        with (
            patch.dict(
                os.environ,
                {
                    "OPENCODE_PASSWORD": ENDPOINT["password"],
                    "OPENCODE_AUTH_CONTENT": "private-sentinel",
                    "CUSTOM_SECRET": "private-sentinel",
                },
                clear=True,
            ),
            patch.object(reader.os, "chdir"),
            patch.object(reader.subprocess, "call", side_effect=run),
            patch.object(reader, "_check_database") as guard,
        ):
            self.assertEqual(
                reader._remote(
                    [
                        "command",
                        DATA["home"],
                        "synthetic-opencode",
                        "api",
                        "get",
                        "/api/session/active",
                        "--server",
                        ENDPOINT["url"],
                    ]
                ),
                0,
            )
        guard.assert_not_called()
        self.assertTrue(all(not root.exists() for root in roots))

    def test_public_module_top_level_imports_are_only_stdlib(self):
        tree = ast.parse(Path(reader.__file__).read_text())
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    self.assertIn(
                        alias.name.split(".")[0], __import__("sys").stdlib_module_names
                    )
            elif isinstance(statement, ast.ImportFrom):
                self.assertEqual(statement.level, 0)
                self.assertIn(
                    statement.module.split(".")[0],
                    __import__("sys").stdlib_module_names,
                )


if __name__ == "__main__":
    unittest.main()
