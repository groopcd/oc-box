"""Focused offline sharing-oracle regressions, not host acceptance.

The production journal writer touches only fresh synthetic state. Native API,
container, and refresh boundaries use local stubs; the sole real child is a tiny
Python EOF peer. No test calls a host guard, OpenCode, Podman, or FUSE.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from oc_box import activity_log
from tools import test_sharing_podman as runner


PRIVATE = "SYNTHETIC_PRIVATE_ORACLE_PAYLOAD"
NAMES = ["oc_box_share_" + digit * 32 for digit in "abcde"]
REFRESH_NAMES = (
    "prepare_runtime",
    "launch_credentials",
    "config_archive",
    "ensure_opencode_version",
    "start_container",
    "create_container",
    "sync_sessions",
)


class LocalCase(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="sharing-oracle-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)

    def refused(self, category):
        return self.assertRaisesRegex(runner.Refusal, "^" + category + "$")


class JournalOracleTests(LocalCase):
    def journal(self, name="state"):
        root = self.parent / name
        root.mkdir(mode=0o700)
        self.assertTrue(
            activity_log.record_event(
                "command.started", command="start", duration_ms=0, root=root
            )
        )
        return root / "log"

    def reject(self, directory):
        output, errors = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            self.refused("journal"),
        ):
            runner.validate_journal(directory)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_real_writer_fresh_journal_and_lock_are_accepted(self):
        directory = self.journal()
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            {"activity.jsonl", "activity.lock"},
        )
        self.assertEqual(runner.validate_journal(directory), 1)
        self.assertTrue(
            activity_log.record_event(
                "command.finished",
                command="start",
                workspace="a" * 32,
                outcome="success",
                exit_code=0,
                duration_ms=10,
                root=directory.parent,
            )
        )
        self.assertEqual(runner.validate_journal(directory), 2)

    def test_real_writer_four_rotated_files_plus_lock_are_accepted(self):
        directory = self.journal()
        # Exercise real rotation cheaply, without constructing fake log files.
        with patch.object(activity_log, "_MAX_FILE_BYTES", 384):
            for index in range(20):
                self.assertTrue(
                    activity_log.record_event(
                        "command.finished",
                        command="start",
                        outcome="success",
                        exit_code=0,
                        duration_ms=index,
                        root=directory.parent,
                    )
                )
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            {
                "activity.jsonl",
                "activity.jsonl.1",
                "activity.jsonl.2",
                "activity.jsonl.3",
                "activity.lock",
            },
        )
        records = sum(
            len(path.read_bytes().splitlines())
            for path in directory.glob("activity.jsonl*")
        )
        self.assertGreater(records, 1)
        self.assertEqual(runner.validate_journal(directory), records)

    def test_empty_directory_and_empty_journals_are_not_evidence(self):
        empty = self.parent / "empty"
        empty.mkdir(mode=0o700)
        self.reject(empty)
        directory = self.journal()
        (directory / "activity.jsonl").write_bytes(b"")
        self.reject(directory)

    def test_malformed_records_are_fixed_refusals(self):
        valid = {
            "event": "command.started",
            "timestamp": "2026-01-02T03:04:05.000Z",
            "command": "start",
        }
        malformed = [
            b"{\n",
            b"[]\n",
            b"\xff\n",
            json.dumps(valid).encode(),  # Missing final newline.
            b'{"event":"command.started","event":"command.finished",'
            b'"timestamp":"2026-01-02T03:04:05.000Z"}\n',
        ]
        malformed.extend(
            (json.dumps({**valid, key: value}) + "\n").encode()
            for key, value in (
                ("event", []),
                ("command", {}),
                ("outcome", []),
                ("timestamp", "2026-99-02T03:04:05.000Z"),
                ("exit_code", True),
                ("duration_ms", -1),
                ("workspace", "not-a-workspace"),
            )
        )
        for index, raw in enumerate(malformed):
            with self.subTest(case=index):
                directory = self.journal("malformed-" + str(index))
                (directory / "activity.jsonl").write_bytes(raw)
                self.reject(directory)

    def test_private_fields_and_raw_contents_never_pass_or_echo(self):
        for field in ("argv", "env", "stderr", "prompt", "content", "password"):
            with self.subTest(field=field):
                directory = self.journal(field)
                path = directory / "activity.jsonl"
                record = json.loads(path.read_text())
                record[field] = PRIVATE
                path.write_text(json.dumps(record) + "\n")
                self.reject(directory)
        for index, marker in enumerate(
            (b"synthetic-live", b"SHARING_NEVER_READ_CONTENT_", PRIVATE.encode())
        ):
            with self.subTest(raw=index):
                directory = self.journal("raw-" + str(index))
                (directory / "activity.jsonl").write_bytes(marker + b"\n")
                self.reject(directory)

    def test_foreign_entry_is_not_ignored(self):
        for index, name in enumerate(("foreign-private-entry", "activity.jsonl.4")):
            with self.subTest(name=name):
                directory = self.journal("foreign-" + str(index))
                (directory / name).write_text(PRIVATE)
                self.reject(directory)

    def test_foreign_directory_journal_and_lock_ownership_is_refused(self):
        original_lstat = Path.lstat
        for index, name in enumerate((None, "activity.jsonl", "activity.lock")):
            with self.subTest(name=name):
                directory = self.journal("owner-" + str(index))
                target = directory if name is None else directory / name

                def lstat(path, *args, **kwargs):
                    info = original_lstat(path, *args, **kwargs)
                    if path == target:
                        fields = list(info)
                        fields[stat.ST_UID] = info.st_uid + 1
                        return os.stat_result(fields)
                    return info

                with patch.object(Path, "lstat", lstat):
                    self.reject(directory)

    def test_directory_journal_and_lock_symlinks_are_refused(self):
        directory = self.journal("real")
        alias = self.parent / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        self.reject(alias)
        for name in ("activity.jsonl", "activity.lock"):
            with self.subTest(name=name):
                directory = self.journal("link-" + name)
                path = directory / name
                target = self.parent / ("target-" + name)
                path.rename(target)
                path.symlink_to(target)
                self.reject(directory)

    def test_hardlinked_journal_and_lock_are_refused(self):
        for name in ("activity.jsonl", "activity.lock"):
            with self.subTest(name=name):
                directory = self.journal("hard-" + name)
                os.link(directory / name, self.parent / ("alias-" + name))
                self.reject(directory)

    def test_directory_log_and_lock_modes_and_nonempty_lock_are_refused(self):
        for index, name in enumerate((None, "activity.jsonl", "activity.lock")):
            with self.subTest(name=name):
                directory = self.journal("mode-" + str(index))
                path = directory if name is None else directory / name
                path.chmod(0o755 if name is None else 0o640)
                self.reject(directory)
        directory = self.journal("lock-content")
        (directory / "activity.lock").write_text(PRIVATE)
        self.reject(directory)


class HarnessCase(LocalCase):
    def setUp(self):
        super().setUp()
        self.root = self.parent / "fixture"
        self.root.mkdir(mode=0o700)
        for name in sorted(runner.INPUTS):
            directory = self.root / name
            directory.mkdir(mode=0o700)
            (directory / "text.txt").write_text("synthetic-live-before 中")
            (directory / "unread.txt").write_text("synthetic-unread-original")
        binary = self.parent / "native-not-executed"
        binary.write_bytes(b"SYNTHETIC_BYTES_FOR_HASH_ONLY")
        self.workspace = self.root / "workspace.json"
        self.workspace.write_text("{}\n")
        self.data = {"project": "/synthetic/project", "home": "/synthetic/home"}
        fixture = SimpleNamespace(
            workspace_records=Mock(
                return_value={"source-a": (self.workspace, self.data)}
            ),
            verify_owned_resources=Mock(
                return_value={"Mounts": [], "HostConfig": {"PortBindings": {}}}
            ),
        )
        box = SimpleNamespace(container_project=lambda data: data["project"])
        provider = SimpleNamespace(
            errors=0,
            plan=Mock(return_value="SYNTHETIC_CASE"),
            take=Mock(),
        )
        loaded = SimpleNamespace(fixture=fixture, box=box, service=SimpleNamespace())
        self.harness = runner.Harness(
            SimpleNamespace(opencode_v2=binary),
            SimpleNamespace(),
            loaded,
            provider,
            self.root,
        )
        self.harness.output = io.StringIO()


class InputAndRegrantOracleTests(HarnessCase):
    def test_capture_is_once_and_unchanged_trees_verify(self):
        self.assertEqual(self.harness.expected_inputs, {})
        self.harness.capture_inputs()
        self.assertEqual(set(self.harness.expected_inputs), runner.INPUTS)
        self.harness.verify_inputs()
        with self.refused("oracle"):
            self.harness.capture_inputs()

    def reject_registration_mutation(self, mutate):
        self.harness.capture_inputs()
        expected = copy.deepcopy(self.harness.expected_inputs)
        directory = self.root / "share-input-a"
        mutate(directory)
        with (
            patch.object(self.harness, "validate") as validate,
            patch.object(runner, "ShareChild") as launch,
            self.refused("source-mutated"),
        ):
            self.harness.share()
        validate.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.harness.expected_inputs, expected)

    def test_pre_registration_root_mode_mutation_refuses_before_launch(self):
        self.reject_registration_mutation(lambda path: path.chmod(0o750))

    def test_pre_registration_unread_mutation_refuses_before_launch(self):
        self.reject_registration_mutation(
            lambda path: (path / "unread.txt").write_text(PRIVATE)
        )

    def test_registration_mutation_is_not_adopted_as_baseline(self):
        self.harness.capture_inputs()
        expected = copy.deepcopy(self.harness.expected_inputs)
        child = SimpleNamespace(until=Mock())

        def ready(**kwargs):
            (self.root / "share-input-a/unread.txt").write_text(PRIVATE)
            return NAMES[0]

        child.until.side_effect = ready
        with (
            patch.object(self.harness, "share", return_value=child),
            self.refused("source-mutated"),
        ):
            self.harness.accepted_share(ack=True)
        self.assertEqual(self.harness.expected_inputs, expected)
        self.assertEqual(self.harness.names, [])

    def test_source_b_teardown_mutation_fails_final_verification_before_engine(self):
        self.harness.capture_inputs()
        self.harness.core = Mock(
            side_effect=lambda: (self.root / "share-input-b/unread.txt").write_text(
                PRIVATE
            )
        )
        self.harness.backup = Mock()
        self.harness.journal = Mock()
        self.harness.validate = Mock()
        with self.refused("source-mutated"):
            self.harness.run()
        self.harness.validate.assert_not_called()
        self.harness.f.verify_owned_resources.assert_not_called()

    def test_source_b_mutation_during_final_child_close_is_a_secondary_failure(self):
        self.harness.capture_inputs()
        expected = copy.deepcopy(self.harness.expected_inputs)
        child = SimpleNamespace(
            forced=False,
            close=Mock(
                side_effect=lambda: (self.root / "share-input-b/unread.txt").write_text(
                    PRIVATE
                )
            ),
        )
        self.harness.owners = [child]
        self.harness.close()
        child.close.assert_called_once_with()
        self.assertEqual(self.harness.secondary, ["source-mutated"])
        self.assertEqual(self.harness.expected_inputs, expected)

    def test_only_declared_new_file_and_atomic_replacement_advance_expectations(self):
        self.harness.capture_inputs()
        before = copy.deepcopy(self.harness.expected_inputs)
        self.harness.change_input("share-input-a", "new.txt", "synthetic-new 中")
        after_new = copy.deepcopy(self.harness.expected_inputs)
        self.assertEqual(after_new["share-input-b"], before["share-input-b"])
        self.assertEqual(
            set(after_new["share-input-a"]), set(before["share-input-a"]) | {"new.txt"}
        )
        self.assertEqual(
            after_new["share-input-a"]["unread.txt"],
            before["share-input-a"]["unread.txt"],
        )
        self.harness.change_input(
            "share-input-a", "text.txt", "synthetic-live-after 🙂", replace=True
        )
        self.assertEqual(
            self.harness.expected_inputs["share-input-b"], before["share-input-b"]
        )
        self.assertEqual(
            self.harness.expected_inputs["share-input-a"]["new.txt"],
            after_new["share-input-a"]["new.txt"],
        )
        self.assertNotEqual(
            self.harness.expected_inputs["share-input-a"]["text.txt"],
            before["share-input-a"]["text.txt"],
        )
        self.assertFalse((self.root / "replacement.txt").exists())
        self.harness.verify_inputs()

    def reject_unrelated_edit(self, *, replace):
        original_write = Path.write_text
        self.harness.capture_inputs()
        expected = copy.deepcopy(self.harness.expected_inputs)
        filename = "text.txt" if replace else "new.txt"
        trigger = (
            self.root / "replacement.txt"
            if replace
            else self.root / "share-input-a/new.txt"
        )

        def write(path, *args, **kwargs):
            result = original_write(path, *args, **kwargs)
            if path == trigger:
                original_write(self.root / "share-input-a/unread.txt", PRIVATE)
            return result

        with (
            patch.object(Path, "write_text", write),
            self.refused("source-mutated"),
        ):
            self.harness.change_input(
                "share-input-a", filename, "synthetic-declared", replace=replace
            )
        self.assertEqual(self.harness.expected_inputs, expected)

    def test_unrelated_mutation_during_new_file_is_never_adopted(self):
        self.reject_unrelated_edit(replace=False)

    def test_unrelated_mutation_during_atomic_replacement_is_never_adopted(self):
        self.reject_unrelated_edit(replace=True)

    def test_no_regrant_checks_every_historical_name(self):
        self.harness.names = list(NAMES)
        self.harness.unknown = Mock()
        self.harness.no_regrant("restored-full")
        self.assertEqual(
            self.harness.unknown.call_args_list,
            [call(name, project="restored-full") for name in NAMES],
        )

    def test_early_namespace_reappearance_stops_acceptance(self):
        self.harness.names = list(NAMES)

        def absent(name, **kwargs):
            if name == NAMES[0]:
                raise runner.Refusal("oracle")

        self.harness.unknown = Mock(side_effect=absent)
        with self.refused("oracle"):
            self.harness.no_regrant("source-a")
        self.harness.unknown.assert_called_once_with(NAMES[0], project="source-a")

    def test_container_config_and_credentials_change_stable_snapshot(self):
        directory = self.root / "config"
        directory.mkdir(mode=0o700)
        (directory / "opencode.json").write_text("{}\n")
        self.harness.container_python = Mock(return_value="container-config-before")
        self.harness.request = Mock(return_value={"data": []})
        with patch.object(runner, "listeners", return_value=frozenset()):
            initial = self.harness.stable_config()
            self.harness.container_python.return_value = "container-config-after"
            config_changed = self.harness.stable_config()
            self.assertNotEqual(initial, config_changed)
            self.harness.container_python.return_value = "container-config-before"
            self.harness.request.return_value = {"data": [{"value": PRIVATE}]}
            credentials_changed = self.harness.stable_config()
        self.assertNotEqual(initial, credentials_changed)
        self.assertEqual(initial[0], config_changed[0])
        self.assertEqual(initial[0], credentials_changed[0])
        self.assertEqual(
            self.harness.container_python.call_args.args[1],
            "/synthetic/home/.config/opencode",
        )
        self.harness.request.assert_called_with(
            "GET", "/api/credential", project="source-a"
        )
        self.assertIn(
            hashlib.sha256(
                json.dumps(self.harness.request.return_value, sort_keys=True).encode()
            ).hexdigest(),
            credentials_changed,
        )
        self.assertNotIn(PRIVATE, repr(credentials_changed))


class RefreshObservationTests(LocalCase):
    def box(self):
        return SimpleNamespace(**{name: Mock() for name in REFRESH_NAMES})

    def test_no_refresh_calls_pass_without_output(self):
        box = self.box()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), runner.observe_refresh(box) as calls:
            self.assertEqual(calls, [])
        self.assertEqual(output.getvalue(), "")
        for name in REFRESH_NAMES:
            getattr(box, name).assert_not_called()

    def test_original_return_and_side_effect_precede_fixed_refusal(self):
        for name in ("prepare_runtime", "config_archive", "launch_credentials"):
            with self.subTest(name=name):
                box = self.box()
                effects, result = [], object()

                def original(*args, **kwargs):
                    effects.append((args, kwargs))
                    return result

                boundary = Mock(side_effect=original)
                setattr(box, name, boundary)
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.refused("refresh-called"),
                ):
                    with runner.observe_refresh(box) as calls:
                        self.assertIs(
                            getattr(box, name)(PRIVATE, secret=PRIVATE), result
                        )
                        self.assertEqual(effects, [((PRIVATE,), {"secret": PRIVATE})])
                        self.assertEqual(calls, [name])
                self.assertIs(getattr(box, name), boundary)
                boundary.assert_called_once_with(PRIVATE, secret=PRIVATE)
                self.assertEqual(
                    output.getvalue(), "SHARING_FORBIDDEN_REFRESH_CALLED\n"
                )
                self.assertEqual(errors.getvalue(), "")
                self.assertNotIn(PRIVATE, output.getvalue())

    def test_original_context_manager_enter_exit_are_not_replaced(self):
        box, effects, value = self.box(), [], object()

        @contextlib.contextmanager
        def credentials(*args, **kwargs):
            effects.append("enter")
            try:
                yield value
            finally:
                effects.append("exit")

        box.launch_credentials = credentials
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.refused("refresh-called"),
        ):
            with runner.observe_refresh(box):
                with box.launch_credentials(PRIVATE) as actual:
                    self.assertIs(actual, value)
                    self.assertEqual(effects, ["enter"])
                self.assertEqual(effects, ["enter", "exit"])

    def test_original_exception_identity_and_side_effect_are_retained(self):
        box, effects = self.box(), []
        failure = RuntimeError(PRIVATE)

        def original(*args, **kwargs):
            effects.append("called")
            raise failure

        box.config_archive = original
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(RuntimeError) as raised,
        ):
            with runner.observe_refresh(box):
                box.config_archive(PRIVATE)
        self.assertIs(raised.exception, failure)
        self.assertEqual(effects, ["called"])
        self.assertEqual(output.getvalue(), "SHARING_FORBIDDEN_REFRESH_CALLED\n")


class NativeSettlingOracleTests(HarnessCase):
    def test_delayed_provider_publication_is_awaited_after_native_quiet(self):
        self.harness.capture_inputs()
        native_quiet, first_take, published = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        messages = [{"type": "assistant", "time": {"completed": 1}}]
        captured = {"text": "synthetic-result", "images": [], "tool_results": []}

        def request(method, route, value=None, **kwargs):
            if (method, route) == ("POST", "/api/session"):
                return {"data": {"id": "synthetic-session"}}
            if (method, route) == ("POST", "/api/session/synthetic-session/prompt"):
                return None
            if route.startswith("/api/session/synthetic-session/message?"):
                return {"data": messages}
            if (method, route) == ("GET", "/api/session/active"):
                native_quiet.set()
                return {"data": {}}
            raise AssertionError("Unexpected synthetic request")

        def take(token):
            self.assertTrue(native_quiet.is_set())
            if published.is_set():
                return captured
            first_take.set()
            return None

        def publish():
            if first_take.wait(2):
                published.set()

        self.harness.request = Mock(side_effect=request)
        self.harness.provider.take.side_effect = take
        worker = threading.Thread(target=publish, daemon=True)
        worker.start()
        try:
            result, actual_messages = self.harness.invoke(
                "execute", {"code": "return 1"}
            )
            self.assertIs(result, captured)
            self.assertIs(actual_messages, messages)
            self.assertGreaterEqual(self.harness.provider.take.call_count, 2)
            created = next(
                call.args[2]
                for call in self.harness.request.call_args_list
                if call.args[:2] == ("POST", "/api/session")
            )
            self.assertEqual(created["title"], "Synthetic host-sharing check")
            self.assertNotIn("SHARING_CASE_", created["title"])
        finally:
            first_take.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_stale_catalog_observation_then_namespace_specific_absence_passes(self):
        name = NAMES[0]
        self.harness.call = Mock(
            side_effect=[
                ({"text": "synthetic-live-before"}, []),
                ({"text": "Unknown tool '" + name + ".read_file'"}, []),
            ]
        )
        with patch.object(runner.time, "sleep") as sleep:
            self.harness.unknown(name, project="source-b")
        self.assertEqual(
            self.harness.call.call_args_list,
            [call(name, "read_file", "text.txt", project="source-b")] * 2,
        )
        sleep.assert_called_once_with(0.1)

    def test_persistent_presence_or_wrong_namespace_refuses_after_three_calls(self):
        for text in (
            "synthetic-live-before",
            "Unknown tool",
            "Unknown tool '" + NAMES[1] + ".read_file'",
        ):
            with self.subTest(text=text):
                self.harness.call = Mock(return_value=({"text": text}, []))
                with (
                    patch.object(runner.time, "sleep") as sleep,
                    self.refused("oracle"),
                ):
                    self.harness.unknown(NAMES[0])
                self.assertEqual(self.harness.call.call_count, 3)
                self.assertEqual(sleep.call_count, 2)


class OwnerShutdownOracleTests(HarnessCase):
    def test_real_local_eof_cleanup_finishes_zero_without_forced_termination(self):
        marker = self.parent / "eof-cleanup-marker"
        script = (
            "import os,pathlib,sys\n"
            "control=os.fdopen(int(os.environ.pop('OC_BOX_SHARING_CONTROL_FD')),'w',buffering=1)\n"
            "print('{\"ready\":true}', file=control, flush=True)\n"
            "sys.stdin.buffer.read()\n"
            "pathlib.Path(sys.argv[1]).write_text('EOF_CLEANUP_COMPLETE')\n"
        )
        child = runner.PipeChild(
            [sys.executable, "-I", "-B", "-u", "-c", script, str(marker)],
            {"PATH": os.defpath, "LANG": "C.UTF-8"},
            self.parent,
        )
        try:
            child.ready(timeout=5)
            self.harness.owners = [child]
            self.harness.close()
            self.assertEqual(child.process.returncode, 0)
            self.assertEqual(marker.read_text(), "EOF_CLEANUP_COMPLETE")
            self.assertFalse(getattr(child, "forced", False))
            self.assertEqual(self.harness.secondary, [])
            self.assertTrue(child.process.stdin.closed)
            self.assertTrue(child.control.closed)
            self.assertTrue(child.errors.closed)
        finally:
            # Only this test's known, synthetic Python process can be killed.
            if child.process.poll() is None:
                child.process.kill()
                child.process.wait(timeout=3)
            child.process.stdin.close()
            child.control.close()
            child.errors.close()

    def test_hung_mock_owner_waits_before_signals_and_records_forced_termination(self):
        for needs_kill in (False, True):
            with self.subTest(needs_kill=needs_kill):
                process = SimpleNamespace(
                    pid=42424242,
                    stdin=io.BytesIO(),
                    stdout=io.BytesIO(),
                    poll=Mock(return_value=None),
                )
                order = []
                waits = 0

                def wait(timeout):
                    nonlocal waits
                    self.assertTrue(process.stdin.closed)
                    waits += 1
                    order.append(("wait", timeout))
                    if waits == 1 or (needs_kill and waits == 2):
                        raise subprocess.TimeoutExpired("synthetic-owner", timeout)
                    return 0

                process.wait = Mock(side_effect=wait)
                with (
                    patch.object(runner.subprocess, "Popen", return_value=process),
                    patch.object(
                        runner.os,
                        "killpg",
                        side_effect=lambda pid, sig: order.append(("signal", sig)),
                    ) as kill,
                ):
                    child = runner.PipeChild(
                        ["synthetic-not-executed"], {}, self.parent
                    )
                    try:
                        self.harness.secondary = []
                        self.harness.owners = [child]
                        self.harness.close()
                        expected = [
                            ("wait", 15),
                            ("signal", signal.SIGTERM),
                            ("wait", 5),
                        ]
                        if needs_kill:
                            expected.extend([("signal", signal.SIGKILL), ("wait", 5)])
                        self.assertEqual(order, expected)
                        self.assertTrue(child.forced)
                        self.assertEqual(
                            self.harness.secondary, ["forced-owner-termination"]
                        )
                        self.assertEqual(
                            kill.call_args_list,
                            [call(process.pid, signal.SIGTERM)]
                            + (
                                [call(process.pid, signal.SIGKILL)]
                                if needs_kill
                                else []
                            ),
                        )
                    finally:
                        process.stdin.close()
                        process.stdout.close()
                        child.errors.close()


if __name__ == "__main__":
    unittest.main()
