import contextlib
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from oc_box import workspace_preferences as preferences


WORKSPACE = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
PRIVATE = "synthetic-private-future-value"


def _setter(
    path,
    workspace,
    value,
    start,
    result,
    locking=None,
    inherited_lock=None,
    field="host_drop",
):
    try:
        if inherited_lock is not None:
            os.close(inherited_lock)
        if not start.wait(10):
            result.put(("error", "fixture-timeout"))
            return
        if locking is None:
            for _ in range(8):
                preferences.set_preferences(path, workspace, **{field: value})
                value = not value
            result.put(("ok", None))
        else:
            flock = preferences.fcntl.flock

            def observed(fd, operation):
                locking.set()
                return flock(fd, operation)

            with patch.object(preferences.fcntl, "flock", side_effect=observed):
                result.put(("ok", preferences.set_host_drop(path, workspace, value)))
    except Exception:
        result.put(("error", "preference-setter"))


def _snapshot_worker(path, workspace, target, operation, ready, resume, result, phase):
    opening = preferences.os.open
    reading = preferences.os.read
    statting = preferences.os.stat
    paused = False
    target_fd = None
    pathname_stats = 0

    def pause():
        nonlocal paused
        paused = True
        ready.set()
        if not resume.wait(10):
            raise RuntimeError("fixture-timeout")

    def pause_after_stat(name, flags, *args, **kwargs):
        nonlocal target_fd
        selected = name == target and flags == preferences._READ_FLAGS
        if not paused and selected and phase == "open":
            # _read_file_once has already observed the old canonical inode.
            pause()
        fd = opening(name, flags, *args, **kwargs)
        if selected:
            target_fd = fd
        return fd

    def pause_after_read(fd, maximum):
        data = reading(fd, maximum)
        if not paused and fd == target_fd and phase == "after_read":
            pause()
        return data

    def retire_final_pathname_stat(name, *args, **kwargs):
        nonlocal pathname_stats
        if name == target and kwargs.get("follow_symlinks") is False:
            pathname_stats += 1
            if not paused and phase == "final_stat" and pathname_stats == 2:
                # Model pathname stat resolving the still-open old inode, then
                # returning its real metadata after a cooperating replacement.
                pause()
                retired = os.fstat(target_fd)
                if retired.st_nlink != 0:
                    raise RuntimeError("fixture-inode-not-retired")
                return retired
        return statting(name, *args, **kwargs)

    try:
        with (
            patch.object(preferences.os, "open", side_effect=pause_after_stat),
            patch.object(preferences.os, "read", side_effect=pause_after_read),
            patch.object(
                preferences.os, "stat", side_effect=retire_final_pathname_stat
            ),
        ):
            if operation == "set":
                value = preferences.set_host_drop(path, workspace, False)
            elif operation == "enabled":
                value = preferences.enabled(path, workspace)
            else:
                value = preferences.read(path, workspace)
        result.put(("ok", value))
    except preferences.PreferenceError as error:
        result.put(("refused", str(error)))
    except Exception:
        result.put(("error", "snapshot-worker"))


class BoxCounter:
    class BoxError(Exception):
        pass

    def __init__(self, path, data):
        self.path = path
        self.data = data
        self.calls = []
        self.lookup_error = None

    def find_workspace(self, project, workspace):
        self.calls.append(("find_workspace", project, workspace))
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.path, self.data

    def __getattr__(self, name):
        self.calls.append((name,))
        raise AssertionError("Unexpected root operation")


class _WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-ui-preferences-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / WORKSPACE
        self.parent.mkdir(mode=0o700)
        self.path = self.parent / "workspace.json"
        self.registration = {
            "version": 2,
            "id": WORKSPACE,
            "project": "/synthetic/project",
        }
        self.path.write_text(json.dumps(self.registration))
        self.path.chmod(0o600)
        self.file = self.parent / preferences.FILE_NAME
        self.lock = self.parent / preferences.LOCK_NAME

    def seed(self, value=None, *, raw=None):
        if raw is None:
            value = (
                {"version": 1, "workspace": WORKSPACE, "host_drop": False}
                if value is None
                else value
            )
            raw = json.dumps(value).encode()
        self.file.write_bytes(raw)
        self.file.chmod(0o600)

    def refuse(self, operation, *args, message=None):
        with self.assertRaises(preferences.PreferenceError) as raised:
            operation(*args)
        self.assertIn(
            str(raised.exception),
            {
                preferences.INVALID_WORKSPACE,
                preferences.INVALID_DATA,
                preferences.INVALID_VALUE,
                preferences.INVALID_CLIPBOARD_VALUE,
                preferences.UNSAFE_STORAGE,
                preferences.BUSY,
                preferences.SAVE_UNVERIFIED,
            },
        )
        if message is not None:
            self.assertEqual(str(raised.exception), message)
        self.assertNotIn(PRIVATE, str(raised.exception))


class WorkspacePreferenceTests(_WorkspaceFixture):
    def test_absence_defaults_enabled_for_new_and_existing_registrations_without_writes(
        self,
    ):
        for extra in (
            {},
            {"opencode_version": "1.19.5", "session": "synthetic-old-session"},
        ):
            self.path.write_text(json.dumps({**self.registration, **extra}))
            before = self.path.read_bytes()
            with (
                patch.object(
                    preferences.fcntl,
                    "flock",
                    side_effect=AssertionError("read locked"),
                ),
                patch.object(
                    preferences.os, "mkdir", side_effect=AssertionError("read created")
                ),
                patch.object(
                    preferences.os, "write", side_effect=AssertionError("read wrote")
                ),
            ):
                self.assertEqual(
                    preferences.read(self.path, WORKSPACE), {"host_drop": True}
                )
                self.assertIs(preferences.enabled(self.path, WORKSPACE), True)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.parent.iterdir()), [self.path])

    def test_absent_preference_read_does_not_inspect_or_create_a_lock(self):
        self.lock.symlink_to(self.root / "unrelated-missing-file")
        self.assertEqual(preferences.read(self.path, WORKSPACE), {"host_drop": True})
        self.assertTrue(self.lock.is_symlink())
        self.assertFalse(self.file.exists())
        self.assertFalse((self.root / "unrelated-missing-file").exists())

    def test_roundtrip_private_atomic_file_preserves_registration_and_other_host_files(
        self,
    ):
        marker = self.root / ".oc-box-id"
        settings = self.root / "host-settings.json"
        marker.write_text(WORKSPACE + "\n")
        settings.write_bytes(b"synthetic-host-settings")
        original = self.path.read_bytes()
        first = preferences.set_host_drop(self.path, WORKSPACE, False)
        self.assertEqual(
            first, {"host_drop": False, "version": 1, "workspace": WORKSPACE}
        )
        self.assertIs(preferences.enabled(self.path, WORKSPACE), False)
        first_inode = self.file.stat().st_ino
        second = preferences.set_host_drop(self.path, WORKSPACE, True)
        self.assertTrue(second["host_drop"])
        self.assertNotEqual(self.file.stat().st_ino, first_inode)
        for path in (self.file, self.lock):
            info = path.stat()
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_nlink, 1)
        self.assertEqual(
            {path.name for path in self.parent.iterdir()},
            {"workspace.json", preferences.FILE_NAME, preferences.LOCK_NAME},
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(marker.read_text(), WORKSPACE + "\n")
        self.assertEqual(settings.read_bytes(), b"synthetic-host-settings")

    def test_owned_0755_parent_is_legitimate_without_permission_repair(self):
        self.parent.chmod(0o755)
        self.assertTrue(preferences.enabled(self.path, WORKSPACE))
        preferences.set_host_drop(self.path, WORKSPACE, False)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o755)
        self.assertFalse(preferences.enabled(self.path, WORKSPACE))

    def test_unknown_bounded_fields_survive_updates_without_entering_known_schema(self):
        future = {
            "version": 1,
            "workspace": WORKSPACE,
            "host_drop": False,
            "source": PRIVATE,
            "future": {"names": ["α", PRIVATE], "number": 7, "unset": None},
        }
        self.seed(future)
        self.assertEqual(preferences.read(self.path, WORKSPACE), future)
        updated = preferences.set_host_drop(self.path, WORKSPACE, True)
        self.assertEqual(updated, {**future, "host_drop": True})
        self.assertEqual(json.loads(self.file.read_bytes()), updated)

    def test_clipboard_fields_default_off_and_update_independently_of_host_drop(self):
        self.seed()
        legacy = preferences.read(self.path, WORKSPACE)
        self.assertNotIn("host_clipboard", legacy)
        saved = preferences.set_preferences(
            self.path, WORKSPACE, host_clipboard=True, host_clipboard_symlinks=False
        )
        self.assertFalse(saved["host_drop"])
        self.assertTrue(saved["host_clipboard"])
        self.assertFalse(saved["host_clipboard_symlinks"])
        updated = preferences.set_host_drop(self.path, WORKSPACE, True)
        self.assertTrue(updated["host_clipboard"])
        self.assertFalse(updated["host_clipboard_symlinks"])
        disabled = preferences.set_preferences(
            self.path, WORKSPACE, host_clipboard=False
        )
        self.assertTrue(disabled["host_drop"])
        self.assertFalse(disabled["host_clipboard"])
        self.assertFalse(disabled["host_clipboard_symlinks"])
        self.assertEqual(self.path.read_text(), json.dumps(self.registration))

    def test_invalid_clipboard_fields_and_setter_types_refuse_without_replacement(self):
        for name in ("host_clipboard", "host_clipboard_symlinks"):
            for invalid in (None, 0, 1, "on", "off", [], {}):
                with self.subTest(name=name, value=invalid):
                    self.seed(
                        {
                            "version": 1,
                            "workspace": WORKSPACE,
                            "host_drop": False,
                            name: invalid,
                        }
                    )
                    before = self.file.read_bytes()
                    self.refuse(preferences.read, self.path, WORKSPACE)
                    self.refuse(preferences.set_preferences, self.path, WORKSPACE)
                    self.assertEqual(self.file.read_bytes(), before)
                    self.assertFalse(self.lock.exists())
            self.file.unlink()
            for invalid in (0, 1, "on", [], {}):
                with self.subTest(name=name, setter_value=invalid):
                    with self.assertRaisesRegex(
                        preferences.PreferenceError, preferences.INVALID_CLIPBOARD_VALUE
                    ):
                        preferences.set_preferences(
                            self.path, WORKSPACE, **{name: invalid}
                        )
                    self.assertFalse(self.file.exists())
                    self.assertFalse(self.lock.exists())

    def test_boolean_and_schema_validation_never_falls_back_to_enabled(self):
        valid = {"version": 1, "workspace": WORKSPACE, "host_drop": False}
        values = [None, [], {}, True, "on"]
        for key, invalids in (
            ("version", (None, True, 0, 2, "1")),
            ("workspace", (None, OTHER, WORKSPACE.upper(), 1, True)),
            ("host_drop", (None, 0, 1, "true", "false", "on", [], {})),
        ):
            values.extend({**valid, key: value} for value in invalids)
        for key in valid:
            values.append({name: value for name, value in valid.items() if name != key})
        for index, value in enumerate(values):
            with self.subTest(case=index):
                self.seed(value, raw=json.dumps(value).encode())
                original = self.file.read_bytes()
                self.refuse(preferences.read, self.path, WORKSPACE)
                self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
                self.assertEqual(self.file.read_bytes(), original)
                self.assertFalse(self.lock.exists())

    def test_corruption_duplicates_encoding_and_nonfinite_unknown_values_refuse(self):
        valid = '{"version":1,"workspace":"' + WORKSPACE + '","host_drop":false'
        cases = (
            b"",
            b"\xff",
            b"not-json " + PRIVATE.encode(),
            b"{",
            (valid + ',"host_drop":true}').encode(),
            (valid + ',"future":{"field":1,"field":2}}').encode(),
            (valid + ',"future":NaN}').encode(),
            (valid + ',"future":Infinity}').encode(),
            (valid + ',"future":1e9999}').encode(),
            (valid + ',"future":"\\ud800"}').encode(),
            (valid + "}").encode("utf-16"),
        )
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                self.seed(raw=raw)
                self.refuse(preferences.read, self.path, WORKSPACE)
                self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
                self.assertEqual(self.file.read_bytes(), raw)
                self.assertFalse(self.lock.exists())

    def test_input_and_future_structure_budgets_are_bounded(self):
        self.seed(raw=b" " * (preferences.MAX_BYTES + 1))
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
        self.assertFalse(self.lock.exists())
        nested = PRIVATE
        for _ in range(preferences.MAX_DEPTH + 2):
            nested = [nested]
        for future in ([None] * preferences.MAX_NODES, nested):
            self.seed(
                {
                    "version": 1,
                    "workspace": WORKSPACE,
                    "host_drop": False,
                    "future": future,
                }
            )
            self.refuse(preferences.read, self.path, WORKSPACE)
        raw = json.dumps(
            {"version": 1, "workspace": WORKSPACE, "host_drop": False}
        ).encode()
        self.seed(raw=raw + b" " * (preferences.MAX_BYTES - len(raw)))
        self.assertFalse(preferences.enabled(self.path, WORKSPACE))

    def test_invalid_setter_values_refuse_before_any_file_or_lock_creation(self):
        for value in (None, 0, 1, "on", "off", [], {}):
            with self.subTest(value_type=type(value).__name__):
                self.refuse(
                    preferences.set_host_drop,
                    self.path,
                    WORKSPACE,
                    value,
                    message=preferences.INVALID_VALUE,
                )
                self.assertEqual(list(self.parent.iterdir()), [self.path])

    def test_symlink_hardlink_special_file_and_nonprivate_preferences_are_not_repaired(
        self,
    ):
        outside = self.root / "outside-private"
        outside.write_bytes(PRIVATE.encode())
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.file.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, self.file)
                elif kind == "fifo":
                    os.mkfifo(self.file, 0o600)
                else:
                    self.file.mkdir(mode=0o700)
                self.refuse(preferences.read, self.path, WORKSPACE)
                self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
                self.assertFalse(self.lock.exists())
                self.file.rmdir() if kind == "directory" else self.file.unlink()
                self.assertEqual(outside.read_bytes(), PRIVATE.encode())
        for mode in (0o000, 0o400, 0o640, 0o644, 0o660, 0o666, 0o1600):
            with self.subTest(mode=oct(mode)):
                self.seed()
                self.file.chmod(mode)
                try:
                    self.refuse(preferences.read, self.path, WORKSPACE)
                    self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
                    self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), mode)
                    self.assertFalse(self.lock.exists())
                finally:
                    self.file.chmod(0o600)

    def test_lock_symlink_hardlink_fifo_and_wrong_mode_refuse_without_preference_write(
        self,
    ):
        outside = self.root / "unrelated-lock"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "mode", "nonempty"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.lock.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, self.lock)
                elif kind == "fifo":
                    os.mkfifo(self.lock, 0o600)
                else:
                    self.lock.write_bytes(b"content" if kind == "nonempty" else b"")
                    self.lock.chmod(0o644 if kind == "mode" else 0o600)
                self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
                self.assertFalse(self.file.exists())
                self.lock.unlink()
                self.assertEqual(outside.read_bytes(), b"")

    @unittest.skipUnless(
        os.geteuid() == 0, "changing file owners requires container root"
    )
    def test_foreign_parent_preference_and_lock_owners_refuse_without_repair(self):
        self.seed()
        for target in (self.parent, self.file, self.path):
            with self.subTest(target=target.name):
                os.chown(target, 1, -1)
                try:
                    self.refuse(preferences.read, self.path, WORKSPACE)
                    self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
                    self.assertFalse(self.lock.exists())
                    self.assertEqual(target.stat().st_uid, 1)
                finally:
                    os.chown(target, 0, -1)
        self.lock.write_bytes(b"")
        self.lock.chmod(0o600)
        os.chown(self.lock, 1, -1)
        try:
            self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
            self.assertEqual(self.lock.stat().st_uid, 1)
        finally:
            os.chown(self.lock, 0, -1)

    def test_unsafe_parent_and_ancestor_symlink_refuse_before_writes(self):
        for mode in (0o775, 0o777, 0o702):
            self.parent.chmod(mode)
            try:
                self.refuse(preferences.read, self.path, WORKSPACE)
                self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
                self.assertEqual(list(self.parent.iterdir()), [self.path])
            finally:
                self.parent.chmod(0o700)
        linked = self.root / "linked-state"
        linked.symlink_to(self.root, target_is_directory=True)
        self.refuse(preferences.read, linked / WORKSPACE / "workspace.json", WORKSPACE)
        self.refuse(
            preferences.set_host_drop,
            linked / WORKSPACE / "workspace.json",
            WORKSPACE,
            False,
        )
        self.assertFalse(self.lock.exists())

    def test_invalid_registration_ids_paths_and_copied_preference_binding_refuse(self):
        for workspace in (
            "",
            "a" * 31,
            "a" * 33,
            WORKSPACE.upper(),
            "../" + WORKSPACE,
            None,
            1,
        ):
            self.refuse(preferences.read, self.path, workspace)
            self.refuse(preferences.set_host_drop, self.path, workspace, True)
        for path in (
            Path(WORKSPACE) / "workspace.json",
            self.parent / "other.json",
            self.parent / ".." / WORKSPACE / "workspace.json",
            self.path,
        ):
            requested = OTHER if path == self.path else WORKSPACE
            self.refuse(preferences.read, path, requested)
            self.refuse(preferences.set_host_drop, path, requested, True)
        self.path.write_text(json.dumps({**self.registration, "id": OTHER}))
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.path.write_text(json.dumps(self.registration))
        self.seed({"version": 1, "workspace": OTHER, "host_drop": True})
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.assertFalse(self.lock.exists())

    def test_missing_and_linked_registry_never_create_registration_or_preferences(self):
        raw = self.path.read_bytes()
        self.path.unlink()
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
        self.assertEqual(list(self.parent.iterdir()), [])
        outside = self.root / "copied-workspace.json"
        outside.write_bytes(raw)
        outside.chmod(0o600)
        self.path.symlink_to(outside)
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.assertEqual(outside.read_bytes(), raw)
        self.assertFalse(self.lock.exists())

    def test_missing_parent_hardlinked_unsafe_or_oversized_registry_refuses_without_writes(
        self,
    ):
        missing = self.root / OTHER / "workspace.json"
        self.refuse(preferences.read, missing, OTHER)
        self.refuse(preferences.set_host_drop, missing, OTHER, True)
        self.assertFalse(missing.parent.exists())
        original = self.path.read_bytes()
        outside = self.root / "other-registry.json"
        outside.write_bytes(original)
        outside.chmod(0o600)
        self.path.unlink()
        os.link(outside, self.path)
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.path.unlink()
        self.path.write_bytes(original)
        self.path.chmod(0o666)
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.chmod(0o600)
        self.path.write_bytes(b" " * (preferences.MAX_REGISTRY_BYTES + 1))
        self.refuse(preferences.read, self.path, WORKSPACE)
        self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.assertEqual(outside.read_bytes(), original)
        self.assertFalse(self.file.exists())
        self.assertFalse(self.lock.exists())

    def test_changed_during_read_is_refused_not_used_as_a_default(self):
        self.seed()
        original = preferences.os.read
        inode = self.file.stat().st_ino
        changed = False

        def mutate(fd, maximum):
            nonlocal changed
            data = original(fd, maximum)
            if not changed and os.fstat(fd).st_ino == inode:
                changed = True
                with self.file.open("ab") as stream:
                    stream.write(b" ")
            return data

        with patch.object(preferences.os, "read", side_effect=mutate):
            self.refuse(preferences.read, self.path, WORKSPACE)
        self.assertTrue(changed)
        self.assertFalse(self.lock.exists())

    def test_parent_substitution_before_lock_does_not_write_to_either_directory(self):
        retained = self.root / "retained-registration"
        original = preferences._locked

        @contextlib.contextmanager
        def replace_parent(directory):
            self.parent.rename(retained)
            self.parent.mkdir(mode=0o700)
            (self.parent / "workspace.json").write_text(json.dumps(self.registration))
            with original(directory) as lock:
                yield lock

        with patch.object(preferences, "_locked", replace_parent):
            self.refuse(preferences.set_host_drop, self.path, WORKSPACE, False)
        self.assertEqual({path.name for path in retained.iterdir()}, {"workspace.json"})
        self.assertEqual(
            {path.name for path in self.parent.iterdir()}, {"workspace.json"}
        )

    def test_partial_write_failure_keeps_prior_mapping_and_removes_only_own_temp_file(
        self,
    ):
        self.seed()
        before = self.file.read_bytes()
        unrelated = self.parent / ".ui-preferences-unrelated.tmp"
        unrelated.write_bytes(PRIVATE.encode())
        write = preferences.os.write
        count = 0

        def fail(fd, data):
            nonlocal count
            count += 1
            if count == 1:
                return write(fd, data[:3])
            raise OSError(PRIVATE)

        with patch.object(preferences.os, "write", side_effect=fail):
            self.refuse(preferences.set_host_drop, self.path, WORKSPACE, True)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(unrelated.read_bytes(), PRIVATE.encode())
        self.assertEqual(
            {path.name for path in self.parent.iterdir()},
            {
                "workspace.json",
                preferences.FILE_NAME,
                preferences.LOCK_NAME,
                unrelated.name,
            },
        )

    def test_owner_lifecycle_lock_can_remain_held_while_preference_is_saved(self):
        owner_lock = self.parent / "lock"
        fd = os.open(owner_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(preferences, "LOCK_TIMEOUT", 0.05):
                preferences.set_host_drop(self.path, WORKSPACE, False)
            self.assertFalse(preferences.enabled(self.path, WORKSPACE))
            other = os.open(owner_lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        finally:
            os.close(fd)

    def test_post_replace_fsync_failure_reports_uncertainty_without_claiming_rollback(
        self,
    ):
        self.seed()
        original = self.path.read_bytes()
        fsync = preferences.os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(PRIVATE)
            return fsync(fd)

        with patch.object(preferences.os, "fsync", side_effect=fail_directory):
            self.refuse(
                preferences.set_host_drop,
                self.path,
                WORKSPACE,
                True,
                message=preferences.SAVE_UNVERIFIED,
            )
        self.assertTrue(preferences.enabled(self.path, WORKSPACE))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(
            {path.name for path in self.parent.iterdir()},
            {"workspace.json", preferences.FILE_NAME, preferences.LOCK_NAME},
        )

    def test_dedicated_lock_timeout_is_bounded_and_does_not_overwrite_preferences(self):
        self.seed()
        before = self.file.read_bytes()
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(preferences, "LOCK_TIMEOUT", 0):
                self.refuse(
                    preferences.set_host_drop,
                    self.path,
                    WORKSPACE,
                    True,
                    message=preferences.BUSY,
                )
            self.assertEqual(self.file.read_bytes(), before)
        finally:
            os.close(fd)

    def workers(self, count=2):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_setter,
                args=(self.path, WORKSPACE, bool(index % 2), start, results),
            )
            for index in range(count)
        ]
        return workers, start, results

    def stop_worker(self, worker):
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)

    def start_snapshot(self, target, operation, phase="open"):
        context = multiprocessing.get_context("fork")
        ready, resume, result = context.Event(), context.Event(), context.Queue()
        worker = context.Process(
            target=_snapshot_worker,
            args=(
                self.path,
                WORKSPACE,
                target,
                operation,
                ready,
                resume,
                result,
                phase,
            ),
        )
        worker.start()
        self.addCleanup(self.stop_worker, worker)
        self.assertTrue(ready.wait(5), "reader did not reach the stat/open boundary")
        return worker, resume, result

    def snapshot_result(self, worker, resume, result):
        resume.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 0)
        return result.get(timeout=5)

    def replace_fixture(self, target, *, value=None, raw=None, mode=0o600):
        replacement = self.parent / "fixture-next.json"
        replacement.write_bytes(json.dumps(value).encode() if raw is None else raw)
        replacement.chmod(mode)
        os.replace(replacement, target)

    def test_precheck_stat_open_race_allows_both_cooperating_setters_to_succeed(self):
        self.seed(
            {
                "version": 1,
                "workspace": WORKSPACE,
                "host_drop": False,
                "future": {"value": PRIVATE},
            }
        )
        worker, resume, result = self.start_snapshot(preferences.FILE_NAME, "set")
        # This writer commits under the real dedicated preference lock while
        # the other writer is paused after its old-inode precheck stat.
        first = preferences.set_host_drop(self.path, WORKSPACE, True)
        self.assertTrue(first["host_drop"])
        kind, second = self.snapshot_result(worker, resume, result)
        self.assertEqual(kind, "ok")
        self.assertFalse(second["host_drop"])
        self.assertEqual(second["future"], {"value": PRIVATE})
        self.assertEqual(preferences.read(self.path, WORKSPACE), second)

    def test_enabled_read_retries_an_explicit_setters_safe_atomic_replacement(self):
        self.seed()
        worker, resume, result = self.start_snapshot(
            preferences.FILE_NAME, "enabled", phase="after_read"
        )
        preferences.set_host_drop(self.path, WORKSPACE, True)
        self.assertEqual(self.snapshot_result(worker, resume, result), ("ok", True))

    def test_read_retries_owner_registry_atomic_replacement_outside_preference_lock(
        self,
    ):
        worker, resume, result = self.start_snapshot(
            "workspace.json", "read", phase="after_read"
        )
        updated = {**self.registration, "unrelated_owner_state": PRIVATE}
        self.replace_fixture(self.path, value=updated)
        self.assertEqual(
            self.snapshot_result(worker, resume, result),
            ("ok", {"host_drop": True}),
        )
        self.assertEqual(json.loads(self.path.read_bytes()), updated)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.file.exists())

    def test_final_pathname_stat_retirement_discards_old_preference_bytes(self):
        self.seed()
        worker, resume, result = self.start_snapshot(
            preferences.FILE_NAME, "enabled", phase="final_stat"
        )
        preferences.set_host_drop(self.path, WORKSPACE, True)
        # Returning the old, retired bytes would incorrectly report False.
        self.assertEqual(self.snapshot_result(worker, resume, result), ("ok", True))

    def test_final_pathname_stat_retirement_retries_owner_registry_snapshot(self):
        worker, resume, result = self.start_snapshot(
            "workspace.json", "read", phase="final_stat"
        )
        updated = {**self.registration, "unrelated_owner_state": PRIVATE}
        self.replace_fixture(self.path, value=updated)
        self.assertEqual(
            self.snapshot_result(worker, resume, result),
            ("ok", {"host_drop": True}),
        )
        self.assertEqual(json.loads(self.path.read_bytes()), updated)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.file.exists())

    def test_registry_snapshot_retry_never_accepts_changed_id_corruption_links_or_mode(
        self,
    ):
        for phase, kind, expected in (
            (phase, kind, expected)
            for phase in ("after_read", "final_stat")
            for kind, expected in (
                ("changed-id", preferences.INVALID_WORKSPACE),
                ("corrupt", preferences.INVALID_DATA),
                ("hardlink", preferences.UNSAFE_STORAGE),
                ("symlink", preferences.UNSAFE_STORAGE),
                ("mode", preferences.UNSAFE_STORAGE),
            )
        ):
            with self.subTest(phase=phase, kind=kind):
                self.path.unlink()
                self.path.write_text(json.dumps(self.registration))
                self.path.chmod(0o600)
                worker, resume, result = self.start_snapshot(
                    "workspace.json", "read", phase=phase
                )
                if kind == "changed-id":
                    self.replace_fixture(
                        self.path, value={**self.registration, "id": OTHER}
                    )
                elif kind == "corrupt":
                    self.replace_fixture(self.path, raw=PRIVATE.encode())
                elif kind in ("hardlink", "symlink"):
                    outside = self.root / ("linked-registry-" + kind)
                    original = json.dumps(self.registration).encode()
                    outside.write_bytes(original)
                    outside.chmod(0o600)
                    replacement = self.parent / "fixture-next.json"
                    if kind == "hardlink":
                        os.link(outside, replacement)
                    else:
                        replacement.symlink_to(outside)
                    os.replace(replacement, self.path)
                else:
                    self.replace_fixture(self.path, value=self.registration, mode=0o666)
                self.assertEqual(
                    self.snapshot_result(worker, resume, result), ("refused", expected)
                )
                if kind in ("hardlink", "symlink"):
                    self.assertEqual(outside.read_bytes(), original)
                self.assertFalse(self.lock.exists())
                self.assertFalse(self.file.exists())

    def test_retry_rejects_corrupt_wrong_workspace_or_nonprivate_preference_successor(
        self,
    ):
        opening = preferences.os.open
        valid = {"version": 1, "workspace": WORKSPACE, "host_drop": False}
        for raw, mode, expected in (
            (PRIVATE.encode(), 0o600, preferences.INVALID_DATA),
            (
                json.dumps({**valid, "workspace": OTHER}).encode(),
                0o600,
                preferences.INVALID_DATA,
            ),
            (json.dumps(valid).encode(), 0o644, preferences.UNSAFE_STORAGE),
        ):
            with self.subTest(mode=oct(mode), expected=expected):
                self.seed()
                changed = False

                def replace_before_open(name, flags, *args, **kwargs):
                    nonlocal changed
                    if (
                        not changed
                        and name == preferences.FILE_NAME
                        and flags == preferences._READ_FLAGS
                    ):
                        changed = True
                        self.replace_fixture(self.file, raw=raw, mode=mode)
                    return opening(name, flags, *args, **kwargs)

                with patch.object(
                    preferences.os, "open", side_effect=replace_before_open
                ):
                    self.refuse(
                        preferences.set_host_drop,
                        self.path,
                        WORKSPACE,
                        True,
                        message=expected,
                    )
                self.assertTrue(changed)
                self.assertEqual(self.file.read_bytes(), raw)
                self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), mode)
                self.assertFalse(self.lock.exists())

    def test_continuously_replaced_safe_snapshots_have_a_bounded_retry_budget(self):
        self.seed()
        reading = preferences.os.read
        replaced = 0

        def replace_after_every_snapshot_read(fd, maximum):
            nonlocal replaced
            data = reading(fd, maximum)
            current, opened = self.file.stat(), os.fstat(fd)
            if data and (current.st_dev, current.st_ino) == (
                opened.st_dev,
                opened.st_ino,
            ):
                replaced += 1
                self.replace_fixture(
                    self.file,
                    value={
                        "version": 1,
                        "workspace": WORKSPACE,
                        "host_drop": False,
                        "future_generation": replaced,
                    },
                )
            return data

        with (
            patch.object(preferences, "MAX_SNAPSHOT_ATTEMPTS", 2),
            patch.object(
                preferences.os, "read", side_effect=replace_after_every_snapshot_read
            ),
        ):
            self.refuse(
                preferences.set_host_drop,
                self.path,
                WORKSPACE,
                True,
                message=preferences.BUSY,
            )
        self.assertEqual(replaced, 2)
        self.assertFalse(self.lock.exists())
        self.assertFalse(preferences.enabled(self.path, WORKSPACE))

    def test_concurrent_setters_preserve_future_fields_and_atomic_complete_json(self):
        future = {
            "version": 1,
            "workspace": WORKSPACE,
            "host_drop": False,
            "future": {"value": PRIVATE},
        }
        self.seed(future)
        workers, start, results = self.workers()
        for worker in workers:
            worker.start()
            self.addCleanup(self.stop_worker, worker)
        start.set()
        deadline = time.monotonic() + 10
        while True:
            value = json.loads(self.file.read_bytes())
            self.assertIs(type(value["host_drop"]), bool)
            self.assertEqual(value["future"], future["future"])
            self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
            if (
                not any(worker.is_alive() for worker in workers)
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.001)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(
            [results.get(timeout=5)[0] for _ in workers], ["ok"] * len(workers)
        )
        self.assertEqual(
            preferences.read(self.path, WORKSPACE)["future"], future["future"]
        )

    def test_concurrent_clipboard_and_drop_updates_preserve_each_others_fields(self):
        self.seed(
            {
                "version": 1,
                "workspace": WORKSPACE,
                "host_drop": False,
                "host_clipboard": False,
                "host_clipboard_symlinks": False,
                "future": PRIVATE,
            }
        )
        context = multiprocessing.get_context("fork")
        start, results = context.Event(), context.Queue()
        workers = [
            context.Process(
                target=_setter,
                args=(self.path, WORKSPACE, False, start, results, None, None, field),
            )
            for field in ("host_drop", "host_clipboard")
        ]
        for worker in workers:
            worker.start()
            self.addCleanup(self.stop_worker, worker)
        start.set()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual([results.get(timeout=5)[0] for _ in workers], ["ok", "ok"])
        saved = preferences.read(self.path, WORKSPACE)
        self.assertTrue(saved["host_drop"])
        self.assertTrue(saved["host_clipboard"])
        self.assertFalse(saved["host_clipboard_symlinks"])
        self.assertEqual(saved["future"], PRIVATE)

    def test_setter_rereads_future_fields_after_waiting_for_its_lock(self):
        self.seed(
            {"version": 1, "workspace": WORKSPACE, "host_drop": False, "future": "old"}
        )
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        context = multiprocessing.get_context("fork")
        start, locking, results = context.Event(), context.Event(), context.Queue()
        worker = context.Process(
            target=_setter,
            args=(self.path, WORKSPACE, True, start, results, locking, fd),
        )
        worker.start()
        self.addCleanup(self.stop_worker, worker)
        try:
            start.set()
            self.assertTrue(locking.wait(5))
            replacement = self.parent / "fixture-next.json"
            replacement.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "workspace": WORKSPACE,
                        "host_drop": False,
                        "future": PRIVATE,
                    }
                )
            )
            replacement.chmod(0o600)
            os.replace(replacement, self.file)
        finally:
            os.close(fd)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 0)
        kind, saved = results.get(timeout=5)
        self.assertEqual(kind, "ok")
        self.assertEqual(saved["future"], PRIVATE)
        self.assertTrue(saved["host_drop"])


class WorkspacePreferenceCliTests(_WorkspaceFixture):
    def invoke(self, box, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = preferences.cli(box, arguments)
        self.assertEqual(result, 0)
        return output.getvalue()

    def box(self):
        return BoxCounter(
            self.path, {**self.registration, "opencode_version": "1.19.5"}
        )

    def test_host_guard_precedes_lookup_and_help_remains_available(self):
        box = self.box()
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            for arguments in (
                (),
                ("--json",),
                ("--table",),
                ("--host-drop", "off"),
                ("--host-clipboard", "on"),
            ):
                with self.assertRaises(box.BoxError) as raised:
                    self.invoke(box, *arguments)
                self.assertEqual(str(raised.exception), preferences.HOST_ONLY)
            with contextlib.redirect_stdout(io.StringIO()) as help_text:
                with self.assertRaises(SystemExit) as raised:
                    preferences.cli(box, ["--help"])
                self.assertEqual(raised.exception.code, 0)
            for token in (
                "--project PATH",
                "--workspace ID",
                "--host-drop on|off",
                "--host-clipboard on|off",
                "--host-clipboard-symlinks on|off",
                "--table",
                "--json",
            ):
                self.assertIn(token, help_text.getvalue())
        self.assertEqual(box.calls, [])
        self.assertFalse(self.file.exists())
        self.assertFalse(self.lock.exists())

    def test_read_only_default_and_saved_status_do_not_start_runtime_or_create_state(
        self,
    ):
        box = self.box()
        with patch.dict(os.environ, {}, clear=True):
            text = self.invoke(box, "--workspace", WORKSPACE)
            self.assertIn("enabled (default)", text)
            self.assertEqual(list(self.parent.iterdir()), [self.path])
            self.seed()
            text = self.invoke(box, "--project", "/synthetic/project")
            self.assertIn("disabled (saved)", text)
        self.assertEqual(
            [call[0] for call in box.calls], ["find_workspace", "find_workspace"]
        )
        self.assertFalse(self.lock.exists())

    def test_save_does_not_require_version_tty_or_client_and_reports_future_only(self):
        box = self.box()
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "synthetic",
                    "STY": "synthetic",
                    "TERM": "dumb",
                    "SSH_TTY": "synthetic",
                },
                clear=True,
            ),
            patch(
                "sys.stdin.isatty", side_effect=AssertionError("preference queried TTY")
            ),
        ):
            text = self.invoke(box, "--host-drop", "off")
        self.assertIn("Saved host drop preference: disabled.", text)
        self.assertIn("future TUI clients", text)
        self.assertIn("current clients keep their setting", text)
        self.assertEqual([call[0] for call in box.calls], ["find_workspace"])
        self.assertFalse(preferences.enabled(self.path, WORKSPACE))

    def test_json_projects_only_boolean_status_and_fixed_source_not_unknown_fields(
        self,
    ):
        box = self.box()
        with patch.dict(os.environ, {}, clear=True):
            default = self.invoke(box, "--json")
        self.assertEqual(
            json.loads(default),
            {
                "host_drop": True,
                "host_clipboard": False,
                "host_clipboard_symlinks": True,
                "source": "default",
            },
        )
        self.assertFalse(self.file.exists())
        self.assertFalse(self.lock.exists())
        self.seed(
            {
                "version": 1,
                "workspace": WORKSPACE,
                "host_drop": False,
                "future": PRIVATE,
                "source": PRIVATE,
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            output = self.invoke(box, "--json")
            self.assertEqual(
                json.loads(output),
                {
                    "host_drop": False,
                    "host_clipboard": False,
                    "host_clipboard_symlinks": True,
                    "source": "saved",
                },
            )
            updated = self.invoke(box, "--host-drop", "on", "--json")
        self.assertEqual(
            json.loads(updated),
            {
                "host_drop": True,
                "host_clipboard": False,
                "host_clipboard_symlinks": True,
                "source": "saved",
                "applies_to_future_clients": True,
                "current_clients_unchanged": True,
            },
        )
        for text in (default, output, updated):
            self.assertNotIn(PRIVATE, text)
            self.assertNotIn(str(self.path), text)
            self.assertNotIn(WORKSPACE, text)
        self.assertEqual(preferences.read(self.path, WORKSPACE)["future"], PRIVATE)

    def test_table_projects_values_and_scope_and_saves_explicit_changes_only(self):
        box = self.box()
        with patch.dict(os.environ, {}, clear=True):
            default = self.invoke(box, "--table")
            self.assertEqual(list(self.parent.iterdir()), [self.path])
            self.seed(
                {
                    "version": 1,
                    "workspace": WORKSPACE,
                    "host_drop": False,
                    "future": PRIVATE,
                }
            )
            updated = self.invoke(box, "--host-clipboard-symlinks", "off", "--table")
        for pattern in (
            r"Host drop[^\n]*enabled[^\n]*default",
            r"Host clipboard[^\n]*disabled[^\n]*default",
            r"Host clipboard symlinks[^\n]*enabled[^\n]*default",
        ):
            self.assertRegex(default, pattern)
        for pattern in (
            r"Host drop[^\n]*disabled[^\n]*saved",
            r"Host clipboard[^\n]*disabled[^\n]*default",
            r"Host clipboard symlinks[^\n]*disabled[^\n]*saved",
        ):
            self.assertRegex(updated, pattern)
        for text in (default, updated):
            self.assertIn("Future workspace TUI clients (host only)", text)
            self.assertRegex(text, r"Current clients[^\n]*unchanged")
            self.assertNotIn(PRIVATE, text)
            self.assertNotIn(str(self.path), text)
            self.assertNotIn(WORKSPACE, text)
        self.assertNotIn("Explicit preferences saved", default)
        self.assertIn("Explicit preferences saved", updated)
        self.assertEqual(
            preferences.read(self.path, WORKSPACE),
            {
                "version": 1,
                "workspace": WORKSPACE,
                "host_drop": False,
                "host_clipboard_symlinks": False,
                "future": PRIVATE,
            },
        )

    def test_table_and_json_conflict_before_lookup_or_preference_save(self):
        box = self.box()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            preferences.cli(box, ["--host-drop", "off", "--table", "--json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(box.calls, [])
        self.assertEqual(list(self.parent.iterdir()), [self.path])

    def test_clipboard_preferences_save_without_discovery_or_runtime_and_project_safely(
        self,
    ):
        box = self.box()
        with (
            patch.dict(
                os.environ, {"TERM": "dumb", "SSH_TTY": "synthetic"}, clear=True
            ),
            patch("sys.stdin.isatty", side_effect=AssertionError("queried TTY")),
        ):
            output = self.invoke(
                box,
                "--host-clipboard",
                "on",
                "--host-clipboard-symlinks",
                "off",
                "--host-drop",
                "off",
                "--json",
            )
            status = self.invoke(box)
        self.assertEqual(
            json.loads(output),
            {
                "host_drop": False,
                "host_clipboard": True,
                "host_clipboard_symlinks": False,
                "source": "saved",
                "applies_to_future_clients": True,
                "current_clients_unchanged": True,
            },
        )
        self.assertIn("Host clipboard preference: enabled (saved)", status)
        self.assertIn("Host clipboard symlink preference: disabled (saved)", status)
        self.assertEqual(
            [call[0] for call in box.calls], ["find_workspace", "find_workspace"]
        )

    def test_corrupt_storage_returns_only_a_fixed_root_error_without_output_or_repair(
        self,
    ):
        self.seed(raw=PRIVATE.encode())
        box = self.box()
        output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(box.BoxError) as raised:
                preferences.cli(box, ["--host-drop", "on", "--json"])
        self.assertEqual(str(raised.exception), preferences.INVALID_DATA)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.file.read_bytes(), PRIVATE.encode())
        self.assertFalse(self.lock.exists())

    def test_missing_lookup_error_and_invalid_registry_identity_are_fixed_and_noncreating(
        self,
    ):
        for data in (None, {}, {"id": "../" + PRIVATE}, {"id": 1}):
            box = BoxCounter(None if data is None else self.path, data)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(box.BoxError) as raised:
                    self.invoke(box, "--host-drop", "on")
            self.assertEqual(str(raised.exception), preferences.INVALID_WORKSPACE)
            self.assertEqual([call[0] for call in box.calls], ["find_workspace"])
        box = self.box()
        box.lookup_error = RuntimeError(PRIVATE)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(box.BoxError) as raised:
                self.invoke(box)
        self.assertEqual(str(raised.exception), preferences.INVALID_WORKSPACE)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.file.exists())

    def test_invalid_selector_id_is_rejected_before_root_lookup(self):
        box = self.box()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(box.BoxError) as raised:
                self.invoke(box, "--workspace", "../" + PRIVATE)
        self.assertEqual(str(raised.exception), preferences.INVALID_WORKSPACE)
        self.assertEqual(box.calls, [])

    def test_parser_has_child_on_off_choices_and_mutually_exclusive_selectors(self):
        parsed = preferences.parser().parse_args(
            ["--workspace", WORKSPACE, "--host-drop", "off", "--json"]
        )
        self.assertEqual(parsed.host_drop, "off")
        self.assertTrue(parsed.json)
        for arguments in (
            ["--no-host-drop"],
            ["--host-drop", "true"],
            ["--project", "/synthetic/project", "--workspace", WORKSPACE],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    preferences.parser().parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
