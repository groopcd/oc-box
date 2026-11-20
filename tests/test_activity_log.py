import contextlib
import fcntl
import io
import json
import multiprocessing
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import activity_log as journal
from oc_box import journal_output
from oc_box.log_config import LogConfigError, overrides


def _writer(root, start, count, ready, result):
    ready.wait(timeout=10)
    # Test serialization, not the production 250 ms best-effort deadline under
    # slow/contended fsync. Keep the longer bounded wait local to each child;
    # every append still executes real locking, writes, fsync and rotation once.
    with patch.object(journal, "_LOCK_TIMEOUT", 5.0):
        result.put(
            [
                journal.record_event(
                    "command.finished", command="status", duration_ms=index, root=root
                )
                for index in range(start, start + count)
            ]
        )


class ActivityJournalFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-activity-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "state"
        self.log = self.root / "log"
        self.active = self.log / "activity.jsonl"
        self.config = self.base / "config" / "oc-box" / "config.json"
        self.enterContext(
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config.parents[1])})
        )

    def configure(self, **logs):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"logs": logs}))
        self.config.chmod(0o600)

    def record(self, **kwargs):
        return journal.record_event(
            "command.finished", command="status", root=self.root, **kwargs
        )

    def seed(self):
        self.assertTrue(
            self.record(
                workspace="a" * 32, outcome="success", exit_code=0, duration_ms=25
            )
        )
        return journal.read_events(self.root)[0]

    def rotated_journal(self):
        # A small write bound puts one distinguishable record in each file.
        with patch.object(journal, "_MAX_FILE_BYTES", 160):
            for duration in (1, 2, 3):
                self.assertTrue(self.record(duration_ms=duration))
        self.assertFalse((self.log / "activity.jsonl.3").exists())
        self.assertTrue((self.log / "activity.jsonl.2").is_file())
        return self.log / "activity.jsonl.1"

    def durations(self, *, strict_snapshot=False):
        return [
            record["duration_ms"]
            for record in journal.read_events(
                self.root, strict_snapshot=strict_snapshot
            )
        ]


class ActivityLogTests(ActivityJournalFixture):
    def test_roundtrip_private_permissions_and_fixed_schema(self):
        record = self.seed()
        self.assertEqual(
            set(record),
            {
                "timestamp",
                "event",
                "command",
                "workspace",
                "outcome",
                "exit_code",
                "duration_ms",
            },
        )
        self.assertEqual(record["workspace"], "a" * 32)
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["duration_ms"], 25)
        self.assertRegex(
            record["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        )
        for path in (self.root, self.log):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.active, self.log / "activity.lock"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_all_fixed_events_commands_and_outcomes_are_supported(self):
        for event in journal.EVENTS:
            self.assertTrue(journal.record_event(event, root=self.root))
        for command in journal.COMMANDS:
            self.assertTrue(
                journal.record_event("command.started", command=command, root=self.root)
            )
        for outcome in journal.OUTCOMES:
            self.assertTrue(self.record(outcome=outcome))
        self.assertEqual(
            len(journal.read_events(self.root)),
            len(journal.EVENTS) + len(journal.COMMANDS) + len(journal.OUTCOMES),
        )

    def test_structured_history_record_fits_existing_journal_bounds(self):
        metrics = {name: journal.MAX_METRIC_VALUE for name in journal.METRICS}
        self.assertTrue(
            journal.record_event(
                "history.recovery",
                command="start",
                workspace="a" * 32,
                outcome="failure",
                exit_code=1,
                duration_ms=2**53 - 1,
                level="warn",
                stage="history-recovery",
                reason="cleanup-unverified",
                metrics=metrics,
                root=self.root,
            )
        )
        payload = self.active.read_bytes()
        self.assertLessEqual(len(payload), journal._MAX_RECORD_BYTES)
        [record] = journal.read_events(self.root)
        self.assertEqual(record, json.loads(payload))
        self.assertEqual(record["stage"], "history-recovery")
        self.assertEqual(record["reason"], "cleanup-unverified")
        self.assertEqual(record["metrics"], metrics)
        self.assertEqual(journal.event_level(record), "warn")
        self.assertEqual(
            sorted(path.name for path in self.log.iterdir()),
            [
                "activity.jsonl",
                "activity.lock",
            ],
        )

    def test_derived_levels_keep_legacy_behavior_and_allow_stored_override(self):
        self.assertEqual(journal.LEVELS, ("debug", "info", "warn", "error"))
        for event, fields, expected in (
            ("command.started", {}, "info"),
            ("command.failed", {}, "error"),
            ("command.failed", {"level": "warn"}, "warn"),
            ("command.finished", {"outcome": "failure"}, "error"),
            ("command.finished", {"outcome": "refused"}, "error"),
            ("command.finished", {"outcome": "unavailable"}, "error"),
            ("command.finished", {"outcome": "cancelled"}, "info"),
            ("lifecycle.stage", {}, "debug"),
            ("lifecycle.notice", {}, "warn"),
            ("history.wait", {}, "debug"),
            ("history.pass", {}, "debug"),
            ("history.recovery", {}, "info"),
            ("history.result", {}, "info"),
        ):
            with self.subTest(event=event, fields=fields):
                self.assertEqual(
                    journal.event_level({"event": event, **fields}), expected
                )

    def test_history_diagnostic_stage_and_stop_reason_are_persisted(self):
        events = (
            ("history.pass", "standalone", None),
            ("history.pass", "private-endpoint", None),
            ("history.pass", "post-stop-reader", None),
            ("history.wait", "foreground-query", None),
            ("history.wait", "background-query", None),
            ("history.wait", "poll-delay", None),
            ("history.recovery", "history-recovery", "stop-requested"),
        )
        for event, stage, reason in events:
            with self.subTest(stage=stage):
                self.assertTrue(
                    journal.record_event(
                        event, stage=stage, reason=reason, root=self.root
                    )
                )
        self.assertEqual(
            [
                (record["event"], record["stage"], record.get("reason"))
                for record in journal.read_events(self.root, strict_snapshot=True)
            ],
            list(events),
        )

    def test_invalid_freeform_fields_do_not_create_or_write_a_journal(self):
        sensitive = "synthetic-private-value"
        for field, values in {
            "command": ["status --token=" + sensitive, sensitive, "logs", [], 1],
            "workspace": [
                sensitive,
                "A" * 32,
                "a" * 31,
                "a" * 32 + "\n",
                "/project",
                1,
            ],
            "outcome": [sensitive, "success\n" + sensitive, {}],
            "exit_code": [True, "0", 1.0, -256, 256, sensitive],
            "duration_ms": [True, -1, 0.1, 2**53, sensitive],
            "level": [sensitive, "warning", "WARN", "warn\n", [], 1],
            "stage": [sensitive, "/project/configuration", "shutdown\n", {}],
            "reason": [sensitive, "timeout: " + sensitive, ["timeout"]],
            "metrics": [
                sensitive,
                [("sessions", 1)],
                {sensitive: 1},
                {"sessions": 1, "raw": 0},
                {"sessions": sensitive},
                {"sessions": {"total": 1}},
                {"sessions": True},
                {"sessions": None},
                {"sessions": 1.0},
                {"sessions": -1},
                {"sessions": journal.MAX_METRIC_VALUE + 1},
                {1: 1},
            ],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    arguments = {"command": "status", field: value}
                    self.assertFalse(
                        journal.record_event(
                            "command.failed", root=self.root, **arguments
                        )
                    )
        for event in (sensitive, "command.failed\n" + sensitive, [], None):
            self.assertFalse(journal.record_event(event, root=self.root))
        self.assertFalse(self.root.exists())
        self.assertTrue(self.record(exit_code=-255, duration_ms=2**53 - 1))
        self.assertNotIn(sensitive, self.active.read_text())

    def test_default_state_root_matches_environment_selection(self):
        oc_root = self.base / "explicit"
        xdg_root = self.base / "xdg"
        with patch.dict(
            os.environ,
            {"OC_BOX_STATE_HOME": str(oc_root), "XDG_STATE_HOME": str(xdg_root)},
        ):
            self.assertTrue(journal.record_event("tui.started"))
            self.assertEqual(len(journal.read_events()), 1)
        self.assertTrue((oc_root / "log/activity.jsonl").is_file())
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_root)}, clear=True):
            self.assertTrue(journal.record_event("tui.finished"))
        self.assertTrue((xdg_root / "oc-box/log/activity.jsonl").is_file())
        home = self.base / "synthetic-home"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "home", return_value=home),
        ):
            self.assertTrue(journal.record_event("tui.started"))
        self.assertTrue((home / ".local/state/oc-box/log/activity.jsonl").is_file())

    def test_read_missing_state_is_empty_without_creating_directories(self):
        self.assertEqual(journal.read_events(self.root), [])
        self.assertEqual(
            journal.read_events(self.root, level="warn", workspace="a" * 32), []
        )
        self.assertFalse(self.root.exists())

    def test_journal_mirror_is_host_only_using_saved_policy_or_cli_override(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(journal_output, "emit") as emit,
        ):
            self.assertTrue(self.record(duration_ms=1))
            self.configure(journald=False)
            self.assertTrue(self.record(duration_ms=2))
            emit.assert_not_called()
            with overrides(journald=True):
                self.assertTrue(self.record(duration_ms=3))
                emit.assert_called_once_with(journal.read_events(self.root)[-1])
                self.assertEqual(journal.log_path(self.root), self.active)
                self.assertFalse(self.record(duration_ms="invalid"))
                with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
                    self.assertTrue(self.record(duration_ms=4))
                emit.assert_called_once()
            self.configure(journald=True)
            with overrides(journald=False):
                self.assertTrue(self.record(duration_ms=5))
            emit.assert_called_once()
            self.assertTrue(self.record(duration_ms=6))
            self.assertEqual(self.durations(), [1, 2, 3, 4, 5, 6])
            self.assertEqual(
                [call.args[0]["duration_ms"] for call in emit.call_args_list], [3, 6]
            )

    def test_mirror_failure_does_not_change_successful_private_append(self):
        self.configure(journald=True)
        seen = []

        for duration, failure in ((1, False), (2, RuntimeError("synthetic error"))):

            def failing_sink(_event, failure=failure):
                seen.append(json.loads(self.active.read_bytes().splitlines()[-1]))
                if isinstance(failure, Exception):
                    raise failure
                return failure

            with (
                self.subTest(failure=failure),
                patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
                patch.object(journal, "_policy", wraps=journal._policy) as policy,
                patch.object(journal_output, "emit", side_effect=failing_sink) as emit,
            ):
                self.assertTrue(self.record(duration_ms=duration))
                policy.assert_called_once()
                emit.assert_called_once_with(seen[-1])
        self.assertEqual([row["duration_ms"] for row in seen], [1, 2])
        self.assertEqual(journal.read_events(self.root), seen)

    def test_rotation_retention_byte_limits_and_latest_oldest_first(self):
        with (
            patch.object(journal, "_MAX_FILE_BYTES", 512),
            patch.object(journal, "_BACKUP_COUNT", 2),
        ):
            for index in range(25):
                self.assertTrue(self.record(duration_ms=index))
            records = journal.read_events(self.root)
            numbers = [item["duration_ms"] for item in records]
            self.assertEqual(numbers, list(range(25 - len(numbers), 25)))
            self.assertLess(len(records), 25)
            self.assertEqual(journal.read_events(self.root, limit=2), records[-2:])
            files = sorted(self.log.glob("activity.jsonl*"))
            self.assertEqual(len(files), 3)
            self.assertTrue(all(path.stat().st_size <= 512 for path in files))
            self.assertTrue(
                all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
            )

    def test_strict_snapshot_allows_absent_oldest_prefix_but_refuses_holes(self):
        middle = self.rotated_journal()
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])
        for path, surviving in ((middle, [1, 3]), (self.active, [1, 2])):
            with self.subTest(missing=path.name):
                aside = self.base / "aside"
                path.rename(aside)
                self.assertEqual(self.durations(), surviving)
                self.assertEqual(self.durations(strict_snapshot=True), [])
                aside.rename(path)
                self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])

    def test_strict_snapshot_refuses_unsafe_or_oversized_rotation_fds(self):
        middle = self.rotated_journal()
        link = self.base / "second-link"
        # Open succeeds, but validation must reject this checked descriptor.
        os.link(middle, link)
        self.assertEqual(self.durations(), [1, 3])
        self.assertEqual(self.durations(strict_snapshot=True), [])
        link.unlink()
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])

        payload = middle.read_bytes()
        with patch.object(journal, "_MAX_FILE_BYTES", 160):
            middle.write_bytes(b"x" * 161)
            self.assertEqual(self.durations(), [1, 3])
            self.assertEqual(self.durations(strict_snapshot=True), [])
            middle.write_bytes(payload)
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])

    def test_strict_snapshot_refuses_unreadable_or_short_rotation(self):
        middle = self.rotated_journal()
        real_open = journal._open_file

        def guarded_open(directory, name, **kwargs):
            if name == middle.name:
                raise PermissionError("synthetic read refusal")
            return real_open(directory, name, **kwargs)

        with patch.object(journal, "_open_file", side_effect=guarded_open):
            self.assertEqual(self.durations(), [1, 3])
            self.assertEqual(self.durations(strict_snapshot=True), [])

        inode = middle.stat().st_ino
        real_read = os.read

        def early_eof(fd, length):
            if os.fstat(fd).st_ino == inode:
                return b""
            return real_read(fd, length)

        with patch.object(journal.os, "read", side_effect=early_eof):
            self.assertEqual(self.durations(), [1, 3])
            self.assertEqual(self.durations(strict_snapshot=True), [])
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])

    def test_severity_and_workspace_filter_before_tail_across_rotations(self):
        selected, other = "a" * 32, "b" * 32
        with patch.object(journal, "_MAX_FILE_BYTES", 512):
            for index, (event, level, workspace) in enumerate(
                (
                    ("command.failed", None, selected),
                    ("lifecycle.notice", "warn", selected),
                    ("history.wait", "debug", selected),
                    ("command.finished", "info", other),
                    ("command.failed", None, other),
                    ("command.failed", None, None),
                    ("command.finished", "info", selected),
                ),
                start=1,
            ):
                self.assertTrue(
                    journal.record_event(
                        event,
                        level=level,
                        workspace=workspace,
                        command="start",
                        duration_ms=index,
                        root=self.root,
                    )
                )
            self.assertTrue((self.log / "activity.jsonl.1").exists())

            def durations(**filters):
                return [
                    record["duration_ms"]
                    for record in journal.read_events(self.root, **filters)
                ]

            self.assertEqual(durations(level="debug"), list(range(1, 8)))
            self.assertEqual(durations(level="info", workspace=selected), [1, 2, 7])
            self.assertEqual(durations(level="warn"), [1, 2, 5, 6])
            self.assertEqual(durations(workspace=selected), [1, 2, 3, 7])
            self.assertEqual(
                durations(level="warn", workspace=selected, limit=2), [1, 2]
            )
            self.assertEqual(durations(level="error", workspace=selected, limit=1), [1])
            self.assertEqual(durations(workspace="c" * 32), [])
            for value in ("warning", "", True, []):
                self.assertEqual(durations(level=value), [])
            for value in (selected[:-1], selected.upper(), selected + "\n", [], 1):
                self.assertEqual(durations(workspace=value), [])

    def test_invalid_and_unknown_record_fields_never_reach_readers(self):
        valid = self.seed()
        malformed = [
            b"not json",
            b"[]",
            b"{}",
            b"null",
            b"\xff",
            b"x" * (journal._MAX_RECORD_BYTES + 1),
        ]
        for extra in (
            {"message": "synthetic-private-value"},
            {"event": "arbitrary-event"},
            {"command": "private argv"},
            {"workspace": "b" * 32 + "\n"},
            {"outcome": "raw error text"},
            {"exit_code": True},
            {"duration_ms": float("nan")},
            {"timestamp": "2026-99-99T12:00:00.000Z"},
            {"timestamp": "\x1b[31m2026-11-01T12:00:00.000Z"},
            {"level": "error\nsynthetic-private-value"},
            {"level": None},
            {"stage": "private path"},
            {"reason": "raw error text"},
            {"metrics": None},
            {"metrics": {"raw": 42}},
            {"metrics": {"sessions": "synthetic-private-value"}},
            {"metrics": {"sessions": True}},
            {"metrics": {"sessions": journal.MAX_METRIC_VALUE + 1}},
        ):
            injected = {**valid, **extra}
            malformed.append(json.dumps(injected).encode())
            self.assertEqual(journal.format_event(injected), "")
        self.active.write_bytes(
            b"\n".join(
                [json.dumps(valid).encode(), *malformed, json.dumps(valid).encode()]
            )
            + b"\n"
        )
        self.assertEqual(journal.read_events(self.root), [valid, valid])
        self.assertEqual(journal.read_events(self.root, limit=1), [valid])
        for limit in (0, -1, True, "1", None):
            self.assertEqual(journal.read_events(self.root, limit=limit), [])
        with patch.object(journal, "_MAX_READ_EVENTS", 1):
            self.assertEqual(journal.read_events(self.root, limit=1000000), [valid])

    def test_partial_final_record_does_not_swallow_next_append(self):
        first = self.seed()
        with self.active.open("ab") as stream:
            stream.write(b'{"event":')
        self.assertTrue(self.record(duration_ms=99))
        records = journal.read_events(self.root)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], first)
        self.assertEqual(records[1]["duration_ms"], 99)

    def test_unsafe_permissions_are_refused_without_repair(self):
        self.seed()
        for path, mode in (
            (self.root, 0o775),
            (self.log, 0o750),
            (self.active, 0o644),
            (self.log / "activity.lock", 0o660),
        ):
            with self.subTest(path=path.name):
                original_mode = stat.S_IMODE(path.stat().st_mode)
                original_data = self.active.read_bytes()
                path.chmod(mode)
                try:
                    self.assertFalse(self.record())
                    self.assertEqual(journal.read_events(self.root), [])
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                    self.assertEqual(self.active.read_bytes(), original_data)
                finally:
                    path.chmod(original_mode)
        unsafe_root = self.base / "public"
        unsafe_root.mkdir(mode=0o775)
        unsafe_root.chmod(0o775)
        self.assertFalse(journal.record_event("tui.started", root=unsafe_root))
        self.assertFalse((unsafe_root / "log").exists())

    def test_existing_owned_legacy_state_root_is_preserved_with_private_log_child(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.assertTrue(self.record(exit_code=0))
        self.assertEqual(len(journal.read_events(self.root)), 1)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.active.stat().st_mode), 0o600)

    def test_foreign_ownership_is_refused_without_mutation(self):
        self.seed()
        before = self.active.read_bytes()
        with patch.object(journal.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(self.record())
            self.assertEqual(journal.read_events(self.root), [])
        self.assertEqual(self.active.read_bytes(), before)

    def test_root_ancestor_and_log_symlinks_are_not_followed(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        link = self.base / "linked"
        link.symlink_to(target, target_is_directory=True)
        for root in (link, link / "nested"):
            self.assertFalse(journal.record_event("tui.started", root=root))
            self.assertEqual(journal.read_events(root), [])
        self.assertEqual(list(target.iterdir()), [])
        self.root.mkdir(mode=0o700)
        self.log.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.record())
        self.assertEqual(journal.read_events(self.root), [])
        self.assertEqual(list(target.iterdir()), [])

    def test_file_symlinks_hardlinks_fifos_and_backup_hazards_are_refused(self):
        self.seed()
        foreign = self.base / "foreign"
        foreign.write_text("synthetic-private-value")
        foreign.chmod(0o600)
        for name in (
            "activity.lock",
            "activity.jsonl",
            "activity.jsonl.1",
            "activity.jsonl.3",
        ):
            path = self.log / name
            previous = path.read_bytes() if path.exists() else None
            for kind in ("symlink", "hardlink", "fifo"):
                with self.subTest(name=name, kind=kind):
                    path.unlink(missing_ok=True)
                    if kind == "symlink":
                        path.symlink_to(foreign)
                    elif kind == "hardlink":
                        os.link(foreign, path)
                    else:
                        os.mkfifo(path, 0o600)
                    try:
                        self.assertFalse(self.record())
                        # A bad backup is skipped, a bad lock/current file gives
                        # no records. No foreign file is ever returned.
                        self.assertNotIn(
                            "synthetic-private-value",
                            str(journal.read_events(self.root)),
                        )
                        self.assertEqual(foreign.read_text(), "synthetic-private-value")
                    finally:
                        path.unlink()
            if previous is not None:
                path.write_bytes(previous)
                path.chmod(0o600)

    def test_oversized_file_is_not_read_or_appended(self):
        self.seed()
        self.active.write_bytes(b"x" * (journal._MAX_FILE_BYTES + 1))
        self.assertFalse(self.record())
        self.assertEqual(journal.read_events(self.root), [])
        self.assertEqual(self.active.stat().st_size, journal._MAX_FILE_BYTES + 1)

    def test_busy_lock_is_bounded_and_diagnostic_failures_are_silent(self):
        self.assertEqual(journal._LOCK_TIMEOUT, 0.25)
        self.seed()
        before = self.active.read_bytes()
        with (
            (self.log / "activity.lock").open("rb") as lock,
            patch.object(journal, "_LOCK_TIMEOUT", 0.01),
        ):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.record())
            self.assertEqual(journal.read_events(self.root), [])
        self.assertEqual(self.active.read_bytes(), before)
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
            patch.object(
                journal.os, "fsync", side_effect=RuntimeError("synthetic-private-error")
            ),
        ):
            self.assertFalse(self.record())
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("synthetic-private-error", self.active.read_text())

    def test_concurrent_process_appends_and_rotation_are_serialized(self):
        for case, bound in (("append", journal._MAX_FILE_BYTES), ("rotate", 1024)):
            with (
                self.subTest(case=case),
                patch.object(journal, "_MAX_FILE_BYTES", bound),
            ):
                root = self.base / case
                context = multiprocessing.get_context("fork")
                ready = context.Barrier(4)
                result = context.Queue()
                processes = [
                    context.Process(
                        target=_writer, args=(root, index * 20, 20, ready, result)
                    )
                    for index in range(4)
                ]
                try:
                    for process in processes:
                        process.start()
                    for _ in processes:
                        self.assertTrue(all(result.get(timeout=15)))
                    for process in processes:
                        process.join(timeout=15)
                        self.assertFalse(process.is_alive())
                        self.assertEqual(process.exitcode, 0)
                    records = journal.read_events(root)
                    numbers = [record["duration_ms"] for record in records]
                    self.assertEqual(len(numbers), len(set(numbers)))
                    if case == "append":
                        self.assertEqual(sorted(numbers), list(range(80)))
                    else:
                        self.assertGreater(len(numbers), 0)
                        self.assertLess(len(numbers), 80)
                    files = list((root / "log").glob("activity.jsonl*"))
                    self.assertLessEqual(len(files), 4)
                    self.assertTrue(all(path.stat().st_size <= bound for path in files))
                    # Every retained physical line is complete and readable.
                    lines = sum(len(path.read_bytes().splitlines()) for path in files)
                    self.assertEqual(lines, len(records))
                finally:
                    for process in processes:
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=5)
                    result.close()


class ActivityRotationTests(ActivityJournalFixture):
    def files(self):
        return {
            path.name: path.read_bytes() for path in self.log.glob("activity.jsonl*")
        }

    def record_range(self, start, stop):
        for duration in range(start, stop):
            self.assertTrue(self.record(duration_ms=duration))

    def test_custom_path_is_selected_without_creating_log_or_workspace_state(self):
        custom = self.base / "private" / "journal"
        alternate = self.base / "alternate"
        self.assertEqual(journal.log_path(self.root), self.active)
        self.configure(directory=str(custom))
        with patch.object(journal, "_log_directory") as open_log:
            self.assertEqual(journal.log_path(self.root), custom / self.active.name)
            open_log.assert_not_called()
        self.assertEqual(self.durations(strict_snapshot=True), [])
        self.assertFalse(custom.parent.exists())
        self.assertFalse(self.root.exists())

        self.assertTrue(self.record(duration_ms=1))
        self.assertEqual(self.durations(strict_snapshot=True), [1])
        self.assertFalse(self.root.exists())
        for name, mode in ((custom, 0o700), (custom / self.active.name, 0o600)):
            self.assertEqual(stat.S_IMODE(name.stat().st_mode), mode)
        with overrides(directory=alternate):
            self.assertEqual(journal.log_path(self.root), alternate / self.active.name)
            self.assertEqual(self.durations(strict_snapshot=True), [])
            self.assertFalse(alternate.exists())
            self.assertTrue(self.record(duration_ms=2))
            self.assertEqual(self.durations(strict_snapshot=True), [2])
        self.assertEqual(self.durations(strict_snapshot=True), [1])
        self.assertFalse(self.root.exists())

    def test_custom_path_refuses_symlink_ancestors_and_nonprivate_leaf(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        target.chmod(0o755)
        for custom in (link, link / "subdir", target):
            with self.subTest(custom=custom):
                self.configure(directory=str(custom))
                self.assertEqual(journal.log_path(self.root), custom / self.active.name)
                self.assertEqual(self.durations(strict_snapshot=True), [])
                self.assertFalse(self.record())
                self.assertEqual(list(target.iterdir()), [])
                self.assertFalse(self.root.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def custom_journal(self, **policy):
        custom = self.base / "custom"
        custom.mkdir(mode=0o700, exist_ok=True)
        self.configure(directory=str(custom), **policy)
        return custom

    def custom_file(self, custom, index, raw):
        path = custom / (self.active.name + (f".{index}" if index else ""))
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def legacy_row(self, duration=1, *, terminator=b"\n"):
        return (
            json.dumps(
                {
                    "timestamp": "2000-01-01T00:00:00.000Z",
                    "event": "command.finished",
                    "command": "status",
                    "duration_ms": duration,
                }
            ).encode("ascii")
            + terminator
        )

    def test_custom_active_refuses_unrecognized_first_record_without_appending(self):
        custom = self.custom_journal(journald=True)
        valid = self.legacy_row()
        for raw in (
            b"unrelated private data\n" + valid,
            b"{}\n" + valid,
            valid.replace(
                b'"duration_ms": 1', b'"duration_ms": "foreign", "duration_ms": 1'
            ),
        ):
            with self.subTest(raw=raw):
                active = self.custom_file(custom, 0, raw)
                with patch.object(journal_output, "emit") as mirror:
                    self.assertFalse(self.record(duration_ms=2))
                    mirror.assert_not_called()
                self.assertEqual(active.read_bytes(), raw)
                self.assertFalse((custom / "activity.jsonl.1").exists())

    def test_quiet_custom_append_uses_bounded_active_guard_and_no_full_scan(self):
        # A sizeable valid ring and active age policy must not turn every
        # ordinary append into a full-content read.
        custom = self.custom_journal(SystemMaxFileSize="4M", MaxFileSec="1d")
        self.assertTrue(self.record(duration_ms=1))
        active = custom / self.active.name
        raw = active.read_bytes()
        active.write_bytes(raw * 4096)
        archive = self.custom_file(custom, 1, self.legacy_row() * 4096)
        with (
            patch.object(
                journal, "_read_file", side_effect=AssertionError("full scan")
            ),
            patch.object(journal.os, "pread", wraps=os.pread) as pread,
        ):
            self.assertTrue(self.record(duration_ms=2))
        self.assertLessEqual(sum(call.args[1] for call in pread.call_args_list), 1027)
        self.assertEqual(active.stat().st_size, len(raw) * 4097)
        self.assertEqual(archive.read_bytes(), self.legacy_row() * 4096)

    def test_custom_transactions_prove_all_potential_mutation_candidates_first(self):
        def verify(operation, settings, foreign_index):
            custom = self.custom_journal(**settings)
            valid = self.legacy_row()
            foreign = valid + b"unrelated private data\n"
            files = {
                self.custom_file(custom, 0, valid): valid,
                self.custom_file(custom, foreign_index, foreign): foreign,
            }
            if operation in ("rotation", "keep-free"):
                files[self.custom_file(custom, 2, valid)] = valid
            with (
                patch.object(journal.os, "rename", wraps=os.rename) as rename,
                patch.object(journal.os, "unlink", wraps=os.unlink) as unlink,
                patch.object(journal, "_available_bytes", return_value=0),
            ):
                self.assertFalse(self.record(duration_ms=2))
                rename.assert_not_called()
                unlink.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in custom.glob("activity.jsonl*")},
                files,
            )
            for path in files:
                path.unlink()

        for operation, settings, foreign_index in (
            ("rotation", {"MaxFileSec": "1h", "SystemMaxFiles": 2}, 1),
            ("active rotation", {"MaxFileSec": "1h"}, 0),
            ("prune", {"SystemMaxFiles": 1}, 1),
            ("gap", {}, 2),
            ("keep-free", {"SystemKeepFree": "1K", "SystemMaxFiles": 2}, 1),
        ):
            with self.subTest(operation=operation):
                verify(operation, settings, foreign_index)

    def test_custom_accepts_legacy_and_complete_no_lf_but_preserves_partial_record(
        self,
    ):
        custom = self.custom_journal()
        full = self.legacy_row(1, terminator=b"\r\n") + self.legacy_row(
            2, terminator=b""
        )
        active = self.custom_file(custom, 0, full)
        self.assertTrue(self.record(duration_ms=3))
        self.assertTrue(active.read_bytes().startswith(full + b"\n"))
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])
        self.configure(directory=str(custom), MaxFileSec="1h")
        previous = active.read_bytes()
        self.assertTrue(self.record(duration_ms=4))
        archive = custom / "activity.jsonl.1"
        self.assertEqual(archive.read_bytes(), previous)
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3, 4])
        partial = self.legacy_row(5).split(b'"duration_ms"')[0]
        damaged = active.read_bytes() + partial
        active.write_bytes(damaged)
        self.assertFalse(self.record(duration_ms=6))
        self.assertEqual(active.read_bytes(), damaged)
        self.assertEqual(archive.read_bytes(), previous)
        self.assertEqual(self.durations(), [1, 2, 3, 4])

    def test_invalid_policy_refuses_all_private_access_with_fixed_path_error(self):
        self.seed()
        before = self.files()
        self.configure(SystemMaxFiles="synthetic-private-value")
        alternate = self.base / "alternate"
        with (
            overrides(directory=alternate, journald=True),
            patch.object(journal_output, "emit") as emit,
        ):
            with self.assertRaisesRegex(
                LogConfigError, "^Activity log configuration is invalid or unsafe\\.$"
            ):
                journal.log_path(self.root)
            self.assertFalse(self.record())
            self.assertEqual(self.durations(), [])
            emit.assert_not_called()
        self.assertFalse(alternate.exists())
        self.assertEqual(self.files(), before)

    def test_size_and_count_rotation_read_latest_in_order(self):
        self.configure(SystemMaxFileSize="1K", SystemMaxFiles=2)
        self.record_range(1000, 1001)
        rows_per_file = 1024 // self.active.stat().st_size
        end = 1000 + 3 * rows_per_file + 2
        self.record_range(1001, end)

        self.assertEqual(sorted(self.files()), ["activity.jsonl", "activity.jsonl.1"])
        self.assertTrue(all(len(data) <= 1024 for data in self.files().values()))
        self.assertEqual(
            self.durations(strict_snapshot=True),
            list(range(end - rows_per_file - 2, end)),
        )

    def test_total_use_prunes_and_can_force_rotation_below_file_size(self):
        self.configure(SystemMaxFileSize="1K", SystemMaxFiles=8, SystemMaxUse="2K")
        self.record_range(1000, 1001)
        rows_per_file = 1024 // self.active.stat().st_size
        end = 1000 + 3 * rows_per_file + 2
        self.record_range(1001, end)
        self.assertEqual(sorted(self.files()), ["activity.jsonl", "activity.jsonl.1"])
        self.assertLessEqual(sum(map(len, self.files().values())), 2048)
        self.assertEqual(
            self.durations(strict_snapshot=True),
            list(range(end - rows_per_file - 2, end)),
        )

        self.configure(SystemMaxFileSize="8K", SystemMaxFiles=8, SystemMaxUse="1K")
        self.record_range(end, end + 2 * rows_per_file)
        self.assertLessEqual(sum(map(len, self.files().values())), 1024)
        retained = self.durations(strict_snapshot=True)
        self.assertEqual(
            retained,
            list(
                range(end + 2 * rows_per_file - len(retained), end + 2 * rows_per_file)
            ),
        )

    def test_smaller_limits_do_not_make_valid_existing_history_unreadable(self):
        self.configure(SystemMaxFileSize="8K", SystemMaxUse="32K")
        self.record_range(1000, 1020)
        existing = self.active.read_bytes()
        self.assertGreater(len(existing), 1024)
        self.configure(SystemMaxFileSize="1K", SystemMaxUse="32K")
        self.assertEqual(self.durations(strict_snapshot=True), list(range(1000, 1020)))
        self.assertEqual(self.files(), {self.active.name: existing})

        self.assertTrue(self.record(duration_ms=1020))
        self.assertEqual(self.durations(strict_snapshot=True), list(range(1000, 1021)))
        self.assertEqual(self.files()["activity.jsonl.1"], existing)
        before = self.files()
        self.configure(SystemMaxFileSize="1K", SystemMaxFiles=1)
        self.assertEqual(self.durations(strict_snapshot=True), list(range(1000, 1021)))
        self.assertEqual(self.files(), before)
        self.assertTrue(self.record(duration_ms=1021))
        self.assertEqual(self.durations(strict_snapshot=True), [1020, 1021])
        self.assertEqual(sorted(self.files()), ["activity.jsonl"])

    def test_active_file_age_comes_from_first_valid_event_not_recent_mtime(self):
        seed = self.seed()
        old = {**seed, "duration_ms": 24, "timestamp": "2000-01-01T00:00:00.000Z"}
        recent = self.active.read_bytes()
        original = b"{\n" + json.dumps(old).encode("ascii") + b"\n" + recent
        self.active.write_bytes(original)
        os.utime(self.active, None)
        self.configure(MaxFileSec="1h")
        self.assertEqual(self.durations(strict_snapshot=True), [24, 25])
        self.assertEqual(self.files(), {self.active.name: original})
        self.assertTrue(self.record(duration_ms=26))
        self.assertEqual(self.files()["activity.jsonl.1"], original)
        self.assertEqual(self.durations(strict_snapshot=True), [24, 25, 26])

    def test_expired_active_is_eligible_for_pruning_only_after_rotation(self):
        seed = self.seed()
        seed["timestamp"] = "2000-01-01T00:00:00.000Z"
        self.active.write_bytes(json.dumps(seed).encode("ascii") + b"\n")
        old = time.time() - 100
        os.utime(self.active, (old, old))
        self.configure(MaxFileSec="30s", MaxRetentionSec="30s")
        self.assertEqual(self.durations(strict_snapshot=True), [25])
        self.assertTrue(self.record(duration_ms=26))
        self.assertEqual(sorted(self.files()), [self.active.name])
        self.assertEqual(self.durations(strict_snapshot=True), [26])

    def test_expiry_runs_only_on_append_and_preserves_a_suffix_for_follow(self):
        middle = self.rotated_journal()
        now = time.time()
        os.utime(middle, (now - 100, now - 100))
        oldest = self.log / "activity.jsonl.2"
        os.utime(oldest, (now, now))
        self.configure(MaxRetentionSec="30s")
        before = self.files()
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3])
        self.assertEqual(self.files(), before)

        # An expired mtime on an interior file cannot create a follow gap.
        self.assertTrue(self.record(duration_ms=4))
        self.assertEqual(self.durations(strict_snapshot=True), [1, 2, 3, 4])
        self.assertEqual(self.files()[middle.name], before[middle.name])
        self.assertEqual(self.files()[oldest.name], before[oldest.name])

        os.utime(oldest, (now - 101, now - 101))
        self.assertTrue(self.record(duration_ms=5))
        self.assertFalse(oldest.exists())
        self.assertFalse(middle.exists())
        self.assertEqual(self.durations(strict_snapshot=True), [3, 4, 5])

    @staticmethod
    def filesystem(available, *, blocks=100):
        # Reserved blocks must not count towards free space for the caller.
        return SimpleNamespace(
            f_bavail=available,
            f_bfree=blocks,
            f_frsize=512,
            f_bsize=4096,
            f_blocks=blocks,
        )

    def test_keep_free_prunes_oldest_and_remeasures_actual_available_blocks(self):
        self.rotated_journal()
        self.configure(SystemKeepFree="1K")
        measured = []
        block_counts = iter((1, 2, 4))

        def statvfs(fd):
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            measured.append(sorted(self.files()))
            return self.filesystem(next(block_counts))

        with patch.object(journal.os, "fstatvfs", side_effect=statvfs):
            self.assertTrue(self.record(duration_ms=4))
        self.assertEqual(
            measured,
            [
                ["activity.jsonl", "activity.jsonl.1", "activity.jsonl.2"],
                ["activity.jsonl", "activity.jsonl.1"],
                ["activity.jsonl"],
            ],
        )
        self.assertEqual(self.durations(strict_snapshot=True), [3, 4])

    def test_impossible_keep_free_reservation_refuses_before_other_policy_pruning(self):
        self.rotated_journal()
        before = self.files()
        self.configure(SystemMaxFiles=1, SystemKeepFree="1K")
        # Capacity can hold the reservation by itself but not one more append.
        with patch.object(
            journal.os, "fstatvfs", return_value=self.filesystem(2, blocks=2)
        ):
            self.assertFalse(self.record(duration_ms=4))
        self.assertEqual(self.files(), before)

    def test_keep_free_refusal_and_unsynced_appends_do_not_mirror(self):
        self.rotated_journal()
        before = self.active.read_bytes()
        self.configure(SystemKeepFree="1K", journald=True)
        emit = Mock()
        with (
            patch.object(journal_output, "emit", emit),
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
        ):
            with patch.object(journal.os, "fstatvfs", return_value=self.filesystem(1)):
                self.assertFalse(self.record(duration_ms=4))
            self.assertEqual(self.files(), {self.active.name: before})
            emit.assert_not_called()

            self.configure(journald=True)
            with patch.object(journal.os, "fsync", side_effect=OSError):
                self.assertFalse(self.record(duration_ms=5))
            emit.assert_not_called()
            self.assertTrue(self.record(duration_ms=6))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0]["duration_ms"], 6)

    def test_filled_journal_recovers_after_failed_sync_on_single_file_rotation(self):
        self.assertTrue(self.record(duration_ms=1000))
        file_bytes = self.active.stat().st_size
        total_bytes = 4 * file_bytes
        with (
            patch.object(journal, "_MAX_FILE_BYTES", file_bytes),
            patch.object(journal, "_HARD_FILE_BYTES", file_bytes),
            patch.object(journal, "_HARD_TOTAL_BYTES", total_bytes),
        ):
            self.record_range(1001, 1004)
            self.assertEqual(sum(map(len, self.files().values())), total_bytes)
            self.assertEqual(
                self.durations(strict_snapshot=True), [1000, 1001, 1002, 1003]
            )
            before = self.active.read_bytes()
            self.configure(SystemMaxFiles=1)
            with patch.object(journal.os, "fsync", side_effect=OSError):
                self.assertFalse(self.record(duration_ms=1004))
            self.assertEqual(self.files()["activity.jsonl.1"], before)
            self.assertLessEqual(sum(map(len, self.files().values())), total_bytes)
            self.assertIn(1003, self.durations(strict_snapshot=True))
            self.assertTrue(self.record(duration_ms=1005))
        self.assertEqual(sorted(self.files()), [self.active.name])
        self.assertEqual(self.durations(strict_snapshot=True), [1005])

    def test_unsupported_archive_removal_is_silent_and_never_mirrored(self):
        self.configure(SystemMaxFiles=1)
        emit = Mock()
        output = io.StringIO()
        with patch.object(journal, "_MAX_FILE_BYTES", 160):
            self.assertTrue(self.record(duration_ms=1))
            before = self.active.read_bytes()
            with (
                patch.object(
                    journal.os,
                    "unlink",
                    side_effect=NotImplementedError("synthetic-private-error"),
                ) as remove,
                patch.object(journal_output, "emit", emit),
                patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
                overrides(journald=True),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                self.assertFalse(self.record(duration_ms=2))
            self.assertEqual(
                [call.args for call in remove.call_args_list], [("activity.jsonl.1",)]
            )
            self.assertEqual(output.getvalue(), "")
            emit.assert_not_called()
            self.assertEqual(self.files()["activity.jsonl.1"], before)
            self.assertEqual(self.durations(strict_snapshot=True), [1, 2])
            self.assertTrue(self.record(duration_ms=3))
        self.assertEqual(self.durations(strict_snapshot=True), [3])

    def test_fixed_filename_bound_and_safe_pruning_even_after_count_decrease(self):
        with (
            patch.object(journal, "_MAX_FILE_BYTES", 160),
            patch.object(journal, "_BACKUP_COUNT", 15),
        ):
            self.record_range(1000, 1018)
        self.assertEqual(len(self.files()), 16)
        self.assertEqual(self.durations(strict_snapshot=True), list(range(1002, 1018)))
        last = self.log / "activity.jsonl.15"
        unmanaged = self.log / "activity.jsonl.16"
        unmanaged.write_bytes(self.active.read_bytes())
        unmanaged.chmod(0o600)
        extra_link = self.base / "archive-hardlink"
        os.link(last, extra_link)

        self.configure(SystemMaxFiles=1)
        before = self.files()
        self.assertFalse(self.record(duration_ms=1018))
        self.assertEqual(self.files(), before)
        self.assertEqual(extra_link.read_bytes(), before[last.name])
        extra_link.unlink()
        self.assertTrue(self.record(duration_ms=1018))
        self.assertEqual(sorted(self.files()), [self.active.name, unmanaged.name])
        self.assertEqual(unmanaged.read_bytes(), before[unmanaged.name])
        self.assertEqual(self.durations(strict_snapshot=True), [1017, 1018])

    def test_absolute_file_and_total_bounds_are_independent_of_policy(self):
        self.assertEqual(journal._HARD_FILE_BYTES, 4 * 1024 * 1024)
        self.assertEqual(journal._HARD_TOTAL_BYTES, 16 * 1024 * 1024)
        self.rotated_journal()
        before = self.files()
        two_latest_bytes = len(before[self.active.name]) + len(
            before["activity.jsonl.1"]
        )
        with patch.object(journal, "_HARD_TOTAL_BYTES", two_latest_bytes):
            read = os.read
            for strict, expected in ((True, []), (False, [2, 3])):
                with patch.object(journal.os, "read", wraps=read) as reads:
                    self.assertEqual(self.durations(strict_snapshot=strict), expected)
                self.assertEqual(
                    sum(call.args[1] for call in reads.call_args_list), two_latest_bytes
                )
            self.assertFalse(self.record(duration_ms=4))
        self.assertEqual(self.files(), before)

        self.configure(SystemMaxFileSize="4M")
        with self.active.open("r+b") as output:
            output.truncate(journal._HARD_FILE_BYTES + 1)
        with patch.object(journal, "_read_file") as read_file:
            self.assertEqual(self.durations(strict_snapshot=True), [])
            self.assertFalse(self.record(duration_ms=4))
            read_file.assert_not_called()
        self.assertEqual(self.active.stat().st_size, journal._HARD_FILE_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
