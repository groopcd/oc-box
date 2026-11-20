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


class LogsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-logs-cli-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "oc-box"
        self.box = SimpleNamespace(BoxError=_BoxError, MARKER=".fixture-id")
        env = patch.dict(
            os.environ,
            {
                "OC_BOX_CONTAINER": "0",
                "OC_BOX_JOURNALD": "0",
                "OC_BOX_STATE_HOME": str(self.root),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = logs_cli.cli(self.box, args)
        return code, stdout.getvalue()

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
        root.assert_not_called()
        read.assert_not_called()

    def test_bad_arguments_refuse_before_io(self):
        with (
            patch.object(logs_cli.activity_log, "_root") as root,
            patch.object(logs_cli.activity_log, "read_events") as read,
        ):
            for args in (
                ("--project", ".", "--workspace", _ID),
                ("--limit", "0"),
                ("--limit", "10001"),
                ("--limit", "1.5"),
                ("--level", "warning"),
            ):
                with (
                    self.subTest(args=args),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    self.run_cli(*args)
                self.assertEqual(result.exception.code, 2)
        root.assert_not_called()
        read.assert_not_called()

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

    def test_missing_journal_is_empty_and_never_initialized(self):
        self.assertEqual(self.run_cli("--json"), (0, ""))
        self.assertFalse(self.root.exists())

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
                patch.dict(os.environ, {state_variable: str(path)}, clear=True),
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
        with (
            patch.object(
                logs_cli.activity_log, "read_events", side_effect=snapshots
            ) as read,
            patch.object(
                logs_cli.time, "sleep", side_effect=[None] * 7 + [KeyboardInterrupt]
            ) as sleep,
        ):
            code, output = self.run_cli(
                "--json", "--follow", "--level", "warn", "--limit", "1"
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

    def test_private_follow_ignores_gap_and_resumes_without_replay(self):
        log = self.root / "log"
        self.root.mkdir(mode=0o700)
        log.mkdir(mode=0o700)
        (log / "activity.lock").touch(mode=0o600)
        active = log / "activity.jsonl"
        middle = log / "activity.jsonl.1"
        held = self.base / "held-rotation"

        def write(path, *durations):
            path.write_text(
                "".join(json.dumps(_event(duration)) + "\n" for duration in durations)
            )
            path.chmod(0o600)

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

        first, second = _event(), _event(2)
        with (
            patch.object(logs_cli.activity_log, "read_events") as private,
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

    def test_broken_pipe_from_buffer_flush_ends_without_shutdown_error(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with (
            os.fdopen(write_fd, "w") as stdout,
            contextlib.redirect_stdout(stdout),
            patch.object(logs_cli.activity_log, "read_events", return_value=[_event()]),
            patch.object(logs_cli.time, "sleep") as sleep,
        ):
            self.assertEqual(logs_cli.cli(self.box, ["--follow"]), 0)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
