import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from oc_box import logs_cli

_ID = "a" * 32
_STAMP = "2026-01-01T10:00:00.000Z"


class _BoxError(Exception):
    pass


def _event(duration=1, **fields):
    return {
        "event": "command.finished",
        "command": "status",
        "timestamp": _STAMP,
        "duration_ms": duration,
        **fields,
    }


def _write_events(path, *records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    path.chmod(0o600)


class LogsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-logs-cli-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "oc-box"
        self.config = self.base / "config" / "oc-box" / "config.json"
        self.box = SimpleNamespace(BoxError=_BoxError, MARKER=".fixture-id")
        env = patch.dict(
            os.environ,
            {
                "HOME": str(self.base),
                "OC_BOX_CONTAINER": "0",
                "OC_BOX_STATE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.base / "config"),
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = logs_cli.cli(self.box, args)
        return code, stdout.getvalue()

    def configure_logs(self, **settings):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"logs": settings}))

    def test_tail_reads_only_preparation_with_filters_and_no_writes(self):
        from oc_box import activity_log, log_config

        custom = self.base / "custom"
        with log_config.overrides(directory=custom):
            self.assertTrue(
                activity_log.record_event(
                    "preparation.started", command="start", stage="baseline-packages"
                )
            )
            self.assertTrue(
                activity_log.record_event("command.started", command="start")
            )
            self.assertTrue(
                activity_log.record_event(
                    "preparation.finished",
                    command="start",
                    stage="baseline-packages",
                    outcome="failure",
                    exit_code=7,
                    duration_ms=1500,
                )
            )
            self.assertTrue(
                activity_log.record_event("command.finished", command="start")
            )
        options = ("--tail", "--log-dir", str(custom))
        before = (custom / "activity.jsonl").read_bytes()
        _, text = self.run_cli(*options)
        self.assertIn("Installing container baseline packages — started", text)
        self.assertIn(
            "Installing container baseline packages — failure (exit 7) (1.5s)", text
        )
        self.assertNotIn("command.", text)
        _, jsonl = self.run_cli(*options, "--limit", "1", "--level", "info", "--json")
        self.assertEqual(json.loads(jsonl)["exit_code"], 7)
        _, table = self.run_cli(*options, "--table")
        self.assertIn("Installing container baseline packages", table)
        self.assertIn("ACTIVITY", table)
        self.assertEqual((custom / "activity.jsonl").read_bytes(), before)
        self.assertFalse(self.root.exists())

    def test_tail_unavailable_is_read_only_and_refuses_other_sources(self):
        self.assertEqual(self.run_cli("--tail"), (0, ""))
        for choice in ("--journald", "--path"):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.run_cli("--tail", choice)
        self.assertFalse(self.root.exists())

    def test_tail_follow_prints_live_preparation_only_without_replay(self):
        def append(event, **fields):
            self.assertTrue(
                logs_cli.activity_log.record_event(
                    event, command="reset", stage="native-cli-install", **fields
                )
            )

        def next_poll(_):
            action = next(events, None)
            if action is None:
                raise KeyboardInterrupt
            action()

        events = iter(
            (
                lambda: append("preparation.started"),
                lambda: logs_cli.activity_log.record_event(
                    "command.finished", command="stop"
                ),
                lambda: append("preparation.finished", outcome="success", exit_code=0),
            )
        )
        with patch.object(logs_cli.time, "sleep", side_effect=next_poll):
            code, output = self.run_cli("--tail", "--follow", "--json")
        self.assertEqual(code, 130)
        self.assertEqual(
            [json.loads(line)["event"] for line in output.splitlines()],
            ["preparation.started", "preparation.finished"],
        )

    def test_path_reports_default_configured_and_invocation_override_without_writes(
        self,
    ):
        configured = self.base / "custom logs"
        override = self.base / "override logs"
        with (
            patch.object(logs_cli.activity_log, "_log_directory") as open_log,
            patch.object(logs_cli.activity_log, "read_events") as read,
            patch.object(logs_cli.activity_log, "record_event") as record,
        ):
            self.assertEqual(
                self.run_cli("--path"),
                (0, str(self.root / "log/activity.jsonl") + "\n"),
            )
            self.configure_logs(directory=str(configured))
            before = self.config.read_bytes()
            configured_path = (0, str(configured / "activity.jsonl") + "\n")
            self.assertEqual(self.run_cli("--path"), configured_path)
            self.assertEqual(
                self.run_cli("--path", "--log-dir", "~/override logs"),
                (0, str(override / "activity.jsonl") + "\n"),
            )
            self.assertEqual(self.run_cli("--path"), configured_path)
        open_log.assert_not_called()
        read.assert_not_called()
        record.assert_not_called()
        self.assertFalse(
            any(path.exists() for path in (self.root, configured, override))
        )
        self.assertEqual(self.config.read_bytes(), before)

    def test_path_with_override_still_rejects_selectors_follow_and_json(self):
        with patch.object(logs_cli, "_configured_path") as path:
            for args in (
                ("--project", "."),
                ("--workspace", _ID),
                ("--follow",),
                ("--json",),
                ("--table",),
                ("--verbose",),
            ):
                with (
                    self.subTest(args=args),
                    self.assertRaisesRegex(_BoxError, "does not take event filters"),
                ):
                    self.run_cli("--path", "--log-dir", str(self.base / "log"), *args)
        path.assert_not_called()

    def test_path_and_read_refuse_invalid_config_with_fixed_help(self):
        self.configure_logs(SystemMaxUse="PRIVATE_SENTINEL")
        override = self.base / "override"
        for args in (
            ("--path",),
            ("--json",),
            ("--path", "--log-dir", str(override)),
            ("--json", "--log-dir", str(override)),
        ):
            with self.subTest(args=args), self.assertRaises(_BoxError) as raised:
                self.run_cli(*args)
            self.assertIn("configuration is invalid", str(raised.exception))
            self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
        self.assertFalse(self.root.exists())
        self.assertFalse(override.exists())

    def registration(self):
        project = self.base / "project"
        project.mkdir()
        (project / self.box.MARKER).write_text(_ID + "\n")
        path = self.root / "workspaces" / _ID / "workspace.json"
        for directory in (self.root, path.parent.parent, path.parent):
            directory.mkdir(mode=0o700)
        self.root.chmod(0o755)  # Owned legacy state roots remain usable.
        info = project.stat()
        path.write_text(
            json.dumps(
                {
                    "id": _ID,
                    "project": str(project),
                    "project_identity": [info.st_dev, info.st_ino],
                }
            )
        )
        path.chmod(0o644)
        return project, path

    def test_inside_container_refuses_before_io_but_help_works(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(logs_cli.activity_log, "_root") as root,
            patch.object(logs_cli.activity_log, "read_events") as read,
        ):
            with self.assertRaisesRegex(_BoxError, "on the host"):
                self.run_cli("--json")
            with (
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                self.assertRaises(SystemExit) as result,
            ):
                logs_cli.cli(self.box, ["--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertIn("--follow", stdout.getvalue())
            self.assertIn("--log-dir", stdout.getvalue())
        root.assert_not_called()
        read.assert_not_called()

    def test_bad_arguments_refuse_before_io(self):
        from oc_box import journal_output

        with (
            patch.object(logs_cli.activity_log, "_root") as root,
            patch.object(logs_cli, "_configured_path") as path,
            patch.object(logs_cli.activity_log, "read_events") as read,
            patch.object(journal_output, "read_events") as system,
        ):
            for args in (
                ("--project", ".", "--workspace", _ID),
                ("--limit", "0"),
                ("--limit", "10001"),
                ("--limit", "1.5"),
                ("--level", "warning"),
                ("--verbose=PRIVATE_SENTINEL",),
                ("--verbose=all", "--level", "debug"),
                ("--table", "--json"),
                ("--log-dir",),
                ("--log-dir", ""),
                ("--log-dir", "/"),
                ("--log-dir", "relative/PRIVATE_SENTINEL"),
                ("--log-dir", str(self.base / "../PRIVATE_SENTINEL")),
                ("--log-dir", str(self.base / "\nPRIVATE_SENTINEL")),
                ("--journald", "--log-dir", str(self.base / "override")),
                ("--journald", "--path"),
            ):
                with (
                    self.subTest(args=args),
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                    self.assertRaises(SystemExit) as result,
                ):
                    self.run_cli(*args)
                self.assertEqual(result.exception.code, 2)
                self.assertNotIn("PRIVATE_SENTINEL", stderr.getvalue())
                if "--journald" in args and "--log-dir" in args:
                    self.assertIn(
                        "--log-dir cannot be combined with --journald",
                        stderr.getvalue(),
                    )
        root.assert_not_called()
        path.assert_not_called()
        read.assert_not_called()
        system.assert_not_called()

    def test_human_default_and_unscoped_json_show_fixed_records(self):
        legacy = _event()
        debug = _event(2, level="debug", workspace=_ID)
        with patch.object(
            logs_cli.activity_log,
            "read_events",
            side_effect=[[legacy], [legacy, debug]],
        ) as read:
            self.assertEqual(
                self.run_cli(),
                (0, f"{_STAMP} INFO  command.finished command=status duration_ms=1\n"),
            )
            code, output = self.run_cli(
                "--json", "--level", "debug", "--limit", "10000"
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            [json.loads(line) for line in output.splitlines()],
            [{**legacy, "level": "info"}, debug],
        )
        self.assertEqual(
            read.call_args_list,
            [
                call(None, limit=100, level="info", workspace=None),
                call(None, limit=10000, level="debug", workspace=None),
            ],
        )

    def test_verbose_all_removes_the_default_limit_but_respects_an_explicit_limit(self):
        for source in ((), ("--journald",)):
            for verbosity, limit, expected in (
                (("--verbose",), (), (None, 10_000)),
                (("--verbose=ALL",), ("--limit", "4"), (None, 4)),
                (("--verbose", "WARN"), (), ("warn", 100)),
            ):
                with (
                    self.subTest(source=source, verbosity=verbosity, limit=limit),
                    patch.object(logs_cli, "_read_events", return_value=[]) as read,
                ):
                    self.assertEqual(self.run_cli(*source, *verbosity, *limit), (0, ""))
                    self.assertEqual(
                        (
                            read.call_args.kwargs["level"],
                            read.call_args.kwargs["limit"],
                        ),
                        expected,
                    )
                    self.assertEqual(read.call_args.kwargs["journald"], bool(source))
        self.assertFalse(self.root.exists())

    def test_missing_journal_is_empty_and_never_initialized(self):
        override = self.base / "missing logs"
        self.assertEqual(self.run_cli("--json"), (0, ""))
        self.assertEqual(self.run_cli("--log-dir", str(override), "--json"), (0, ""))
        self.assertFalse(self.root.exists())
        self.assertFalse(override.exists())

    def test_linked_state_ancestor_is_empty_unscoped_and_refused_for_selectors(self):
        project, registration = self.registration()
        self.assertTrue(
            logs_cli.activity_log.record_event(
                "command.finished", command="status", workspace=_ID, root=self.root
            )
        )
        active = self.root / "log" / "activity.jsonl"
        before = (registration.read_bytes(), active.read_bytes())
        code, output = self.run_cli("--json", "--workspace", _ID)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["workspace"], _ID)

        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        for state_variable, path in (
            ("OC_BOX_STATE_HOME", alias / self.root.name),
            ("XDG_STATE_HOME", alias),
        ):
            with (
                self.subTest(state_variable=state_variable),
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.base),
                        "XDG_CONFIG_HOME": str(self.base / "config"),
                        state_variable: str(path),
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(self.run_cli("--json"), (0, ""))
                with patch.object(logs_cli.activity_log, "read_events") as read:
                    for selector in (("--workspace", _ID), ("--project", str(project))):
                        with self.assertRaisesRegex(
                            _BoxError, "valid workspace registration"
                        ):
                            self.run_cli(*selector)
                read.assert_not_called()

        self.assertTrue(alias.is_symlink())
        self.assertEqual((registration.read_bytes(), active.read_bytes()), before)

    def test_log_dir_override_reads_only_selected_private_records(self):
        project, _ = self.registration()
        configured = self.base / "configured"
        override = self.base / "override"
        configured_record = _event(1, level="warn", workspace=_ID)
        selected = _event(3, level="warn", workspace=_ID)
        self.configure_logs(directory=str(configured))
        for directory, records in (
            (configured, [configured_record]),
            (
                override,
                [
                    _event(2, level="error", workspace=_ID),
                    selected,
                    _event(4, level="debug", workspace=_ID),
                    _event(5, level="error", workspace="b" * 32),
                ],
            ),
        ):
            directory.mkdir(mode=0o700)
            (directory / "activity.lock").touch(mode=0o600)
            _write_events(directory / "activity.jsonl", *records)

        for selector in (("--project", str(project)), ("--workspace", _ID)):
            with self.subTest(selector=selector):
                code, output = self.run_cli(
                    "--log-dir",
                    str(override),
                    "--json",
                    "--level",
                    "warn",
                    "--limit",
                    "1",
                    *selector,
                )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output), selected)

        alias = self.base / "linked"
        alias.symlink_to(override, target_is_directory=True)
        self.assertEqual(self.run_cli("--log-dir", str(alias), "--json"), (0, ""))
        code, output = self.run_cli("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), configured_record)
        self.assertFalse((self.root / "log").exists())

    def test_project_and_id_resolve_same_exact_filter_even_after_project_removal(self):
        project, path = self.registration()
        previous = path.read_bytes()
        with patch.object(
            logs_cli.activity_log, "read_events", return_value=[]
        ) as read:
            self.assertEqual(
                self.run_cli(
                    "--project", str(project), "--level", "warn", "--limit", "1"
                ),
                (0, ""),
            )
            (project / self.box.MARKER).unlink()
            project.rmdir()
            self.assertEqual(
                self.run_cli("--workspace", _ID, "--level", "error"), (0, "")
            )
        self.assertEqual(
            read.call_args_list,
            [
                call(self.root, limit=1, level="warn", workspace=_ID),
                call(self.root, limit=100, level="error", workspace=_ID),
            ],
        )
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse((self.root / "log").exists())

    def test_invalid_or_missing_selector_never_falls_back_to_unscoped_view(self):
        project, path = self.registration()
        copied = self.base / "different-project"
        copied.mkdir()
        (copied / self.box.MARKER).write_text(_ID + "\n")
        record = json.loads(path.read_text())
        record["project_identity"] = [0, 0]
        path.write_text(json.dumps(record))
        with patch.object(logs_cli.activity_log, "read_events") as read:
            for args in (
                ("--workspace", ""),
                ("--workspace", "x\nprivate"),
                ("--workspace", "b" * 32),
                ("--project", ""),
                ("--project", str(self.base / "absent")),
                ("--project", str(copied)),
                ("--project", str(project)),
            ):
                with (
                    self.subTest(args=args),
                    self.assertRaisesRegex(
                        _BoxError,
                        "^The logs selector requires an existing valid workspace registration\\.$",
                    ),
                ):
                    self.run_cli(*args)
            (project / self.box.MARKER).unlink()
            with self.assertRaises(_BoxError):
                self.run_cli("--project", str(project))
        read.assert_not_called()

    def test_project_marker_from_replacement_cannot_authorize_original_path(self):
        project, _ = self.registration()
        original = self.base / "original-project"
        project.rename(original)
        project.mkdir()
        (project / self.box.MARKER).write_text(_ID + "\n")
        bounded_read = logs_cli._bounded_read

        def restore_original(directory, name, maximum, **options):
            data = bounded_read(directory, name, maximum, **options)
            if name == self.box.MARKER:
                # The marker came from the copy. A second pathname lookup would
                # see the genuine registered directory after this swap.
                project.rename(self.base / "copied-project")
                original.rename(project)
            return data

        with (
            patch.object(logs_cli, "_bounded_read", side_effect=restore_original),
            patch.object(logs_cli.activity_log, "read_events") as read,
            self.assertRaisesRegex(_BoxError, "valid workspace registration"),
        ):
            self.run_cli("--project", str(project))
        read.assert_not_called()

    def test_project_selector_rechecks_canonical_entry_after_pin(self):
        project, _ = self.registration()
        moved = self.base / "moved-project"
        registration = logs_cli._registration

        def replace_entry(root, workspace):
            record = registration(root, workspace)
            project.rename(moved)
            project.symlink_to(moved, target_is_directory=True)
            return record

        with (
            patch.object(logs_cli, "_registration", side_effect=replace_entry),
            patch.object(logs_cli.activity_log, "read_events") as read,
            self.assertRaisesRegex(_BoxError, "valid workspace registration"),
        ):
            self.run_cli("--project", str(project))
        read.assert_not_called()

    def test_unsafe_registry_record_is_refused_before_journal_io(self):
        _, path = self.registration()
        valid = path.read_text()
        with patch.object(logs_cli.activity_log, "read_events") as read:
            for invalid in (
                "private malformed content",
                f'{{"id":"{_ID}","id":"{_ID}"}}',
                json.dumps({"id": "b" * 32, "project": str(self.base)}),
                json.dumps({"id": _ID, "project": "not/absolute"}),
            ):
                path.write_text(invalid)
                with self.assertRaises(_BoxError):
                    self.run_cli("--workspace", _ID)

            path.unlink()
            target = self.base / "untrusted.json"
            target.write_text(valid)
            path.symlink_to(target)
            with self.assertRaises(_BoxError):
                self.run_cli("--workspace", _ID)

            path.unlink()
            os.mkfifo(path, 0o600)
            with self.assertRaises(_BoxError):
                self.run_cli("--workspace", _ID)
        read.assert_not_called()

    def test_follow_counts_duplicates_and_rotation_without_replaying(self):
        a, b, c, d, e = (_event(number, level="warn") for number in range(1, 6))
        snapshots = [
            [a, b, b],
            [a, b, b],
            [],
            [a, b],
            [b, b, c],
            [b, b, c, c],
            [d, e],
            [d, e],
        ]
        override = self.base / "follow logs"
        environment = dict(os.environ)
        retained = iter(snapshots)

        def read_snapshot(root, **_):
            self.assertEqual(
                logs_cli.activity_log.log_path(root), override / "activity.jsonl"
            )
            self.assertEqual(dict(os.environ), environment)
            return next(retained)

        with (
            patch.object(
                logs_cli.activity_log, "read_events", side_effect=read_snapshot
            ) as read,
            patch.object(
                logs_cli.time, "sleep", side_effect=[None] * 7 + [KeyboardInterrupt]
            ) as sleep,
        ):
            code, output = self.run_cli(
                "--json",
                "--follow",
                "--level",
                "warn",
                "--limit",
                "1",
                "--log-dir",
                str(override),
            )
        self.assertEqual(code, 130)
        self.assertEqual(
            [json.loads(line) for line in output.splitlines()], [b, c, c, d, e]
        )
        self.assertEqual(
            read.call_args_list,
            [
                call(
                    None,
                    limit=10000,
                    level="warn",
                    workspace=None,
                    strict_snapshot=True,
                )
            ]
            * len(snapshots),
        )
        self.assertEqual(sleep.call_count, len(snapshots))
        self.assertTrue(all(0 < delay.args[0] <= 1 for delay in sleep.call_args_list))
        self.assertEqual(
            self.run_cli("--path"), (0, str(self.root / "log/activity.jsonl") + "\n")
        )

    def test_table_follow_prints_its_heading_once_on_first_match(self):
        first, second = (_event(number, level="warn") for number in (1, 2))
        with (
            patch.object(
                logs_cli.activity_log,
                "read_events",
                side_effect=[[], [first], [first, second]],
            ),
            patch.object(
                logs_cli.time, "sleep", side_effect=[None, None, KeyboardInterrupt]
            ),
        ):
            code, output = self.run_cli("--table", "--follow", "--level", "warn")
        self.assertEqual(code, 130)
        self.assertEqual(output.count("TIMESTAMP"), 1)
        self.assertEqual(
            [line.split()[-1] for line in output.splitlines()[1:]],
            ["duration_ms=1", "duration_ms=2"],
        )
        self.assertFalse(self.root.exists())

    def test_private_follow_ignores_gap_and_resumes_without_replay(self):
        log = self.root / "log"
        self.root.mkdir(mode=0o700)
        log.mkdir(mode=0o700)
        (log / "activity.lock").touch(mode=0o600)
        active = log / "activity.jsonl"
        middle = log / "activity.jsonl.1"
        held = self.base / "held-rotation"

        def write(path, *durations):
            _write_events(path, *(_event(duration) for duration in durations))

        write(log / "activity.jsonl.2", 1)
        write(middle, 2)
        write(active, 3)

        def hide_middle():
            middle.rename(held)
            write(active, 3, 4)
            # One-shot reads still show available files during a gap.
            code, output = self.run_cli("--json")
            self.assertEqual(code, 0)
            self.assertEqual(
                [json.loads(line)["duration_ms"] for line in output.splitlines()],
                [1, 3, 4],
            )

        actions = iter((hide_middle, lambda: held.rename(middle)))

        def next_poll(_):
            action = next(actions, None)
            if action is None:
                raise KeyboardInterrupt
            action()

        with patch.object(logs_cli.time, "sleep", side_effect=next_poll):
            code, output = self.run_cli("--follow", "--json", "--limit", "1")
        self.assertEqual(code, 130)
        self.assertEqual(
            [json.loads(line)["duration_ms"] for line in output.splitlines()], [3, 4]
        )

    def test_journald_follow_does_not_forward_private_snapshot_option(self):
        from oc_box import journal_output

        self.configure_logs(journald=False)
        first, second = _event(), _event(2)
        with (
            patch.object(logs_cli.activity_log, "read_events") as private,
            patch.object(journal_output, "emit") as mirror,
            patch.object(
                journal_output, "read_events", side_effect=[[first], [first, second]]
            ) as system,
            patch.object(logs_cli.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            code, output = self.run_cli("--journald", "--follow", "--json")
        self.assertEqual(code, 130)
        self.assertEqual(
            [json.loads(line)["duration_ms"] for line in output.splitlines()], [1, 2]
        )
        self.assertEqual(
            system.call_args_list, [call(limit=10000, level="info", workspace=None)] * 2
        )
        private.assert_not_called()
        mirror.assert_not_called()

    def test_broken_pipe_from_buffer_flush_ends_without_shutdown_error(self):
        for options in (["--follow"], ["--follow", "--table"]):
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            with (
                self.subTest(options=options),
                os.fdopen(write_fd, "w") as stdout,
                contextlib.redirect_stdout(stdout),
                patch.object(
                    logs_cli.activity_log, "read_events", return_value=[_event()]
                ),
                patch.object(logs_cli.time, "sleep") as sleep,
            ):
                self.assertEqual(logs_cli.cli(self.box, options), 0)
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
