import contextlib
import fcntl
import io
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import activity_log as journal


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


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-activity-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "state"
        self.log = self.root / "log"
        self.active = self.log / "activity.jsonl"

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


if __name__ == "__main__":
    unittest.main()
