import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from oc_box import workspace_control as control


def _hold_admission(path, connection):
    try:
        connection.send(("waiting",))
        with control.admission(Path(path), timeout=3):
            connection.send(("admitted",))
            connection.recv()
        connection.send(("released",))
    except control.ControlError as exc:
        connection.send(("error", exc.category))
    finally:
        connection.close()


def _stop_then_admit(path, connection):
    try:
        token = control.request_stop(Path(path))
        connection.send(("published", token))
        with control.admission(Path(path), timeout=3):
            connection.send(("admitted", control.capture(Path(path))))
    except control.ControlError as exc:
        connection.send(("error", exc.category))
    finally:
        connection.close()


def _timed_admission(path, connection):
    started = time.monotonic()
    try:
        with control.admission(Path(path), timeout=0.1):
            connection.send(("admitted",))
    except control.ControlError as exc:
        connection.send(("error", exc.category, time.monotonic() - started))
    finally:
        connection.close()


def _publish_stop(path, connection):
    try:
        connection.send(("published", control.request_stop(Path(path))))
    except control.ControlError as exc:
        connection.send(("error", exc.category))
    finally:
        connection.close()


def _first_use(path, action, connection, pause):
    initialize = control._initialize

    def held_initialize(directory):
        connection.send(("directory-created",))
        connection.recv()
        return initialize(directory)

    try:
        connection.send(("started",))
        with (
            patch.object(control, "_initialize", held_initialize)
            if pause
            else contextlib.nullcontext()
        ):
            token = getattr(control, action)(Path(path))
        connection.send(("token", token))
    except control.ControlError as exc:
        connection.send(("error", exc.category))
    finally:
        connection.close()


class _ControlFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-control-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.count = 0
        self.path = self.workspace()

    def workspace(self):
        self.count += 1
        parent = self.root / f"workspace-{self.count}"
        parent.mkdir(mode=0o700)
        path = parent / "workspace.json"
        path.write_text('{"synthetic":true}\n')
        path.chmod(0o600)
        return path

    def invoke(self, action, path, baseline):
        if action == "unchanged":
            return control.unchanged(path, baseline)
        if action == "admission":
            with control.admission(path, timeout=0):
                return None
        return getattr(control, action)(path)

    def assert_refusal(self, function, *args, category=None):
        with self.assertRaises(control.ControlError) as raised:
            function(*args)
        error = raised.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), f"Workspace control failed ({error.category}).")
        self.assertNotIn(str(self.root), str(error))
        self.assertNotIn("private-sentinel", str(error))
        self.assertTrue(error.__suppress_context__)
        if category is not None:
            self.assertEqual(error.category, category)
        return error

    def all_refuse(self, path, baseline, *, category=None):
        for action in (
            "capture",
            "request_stop",
            "unchanged",
            "admission",
            "mark_uncertain",
            "finish_transition",
            "require_certain",
        ):
            with self.subTest(action=action):
                self.assert_refusal(
                    self.invoke, action, path, baseline, category=category
                )


class WorkspaceControlTests(_ControlFixture):
    def test_only_mutators_initialize_fixed_private_artifacts(self):
        for action in ("capture", "request_stop"):
            with self.subTest(action=action):
                path = self.workspace()
                token = getattr(control, action)(path)
                self.assertRegex(token, r"\A[0-9a-f]{64}\Z")
                directory = path.parent / "control"
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(
                    {item.name for item in directory.iterdir()},
                    {"history-stop.json", "admission.lock", "marker.lock"},
                )
                for artifact in directory.iterdir():
                    info = artifact.lstat()
                    self.assertTrue(stat.S_ISREG(info.st_mode))
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    self.assertEqual(info.st_uid, os.getuid())
                    self.assertEqual(info.st_nlink, 1)
                self.assertEqual(
                    json.loads((directory / "history-stop.json").read_bytes()),
                    {"version": 1, "token": token, "uncertain": False},
                )
                self.assertEqual((directory / "marker.lock").read_bytes(), b"1\n")
                self.assertEqual((directory / "admission.lock").read_bytes(), b"")

    def test_capture_preserves_existing_marker_and_all_stable_inodes(self):
        token = control.request_stop(self.path)
        directory = self.path.parent / "control"
        before = {
            item.name: (item.read_bytes(), item.stat().st_ino)
            for item in directory.iterdir()
        }
        self.assertEqual(control.capture(self.path), token)
        self.assertTrue(control.unchanged(self.path, token))
        with control.admission(self.path, timeout=0):
            self.assertEqual(control.capture(self.path), token)
            self.assertTrue(control.unchanged(self.path, token))
        self.assertEqual(
            before,
            {
                item.name: (item.read_bytes(), item.stat().st_ino)
                for item in directory.iterdir()
            },
        )

    def test_stop_rotates_only_marker_and_can_publish_inside_admission(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        locks = {
            name: (directory / name).stat().st_ino
            for name in ("admission.lock", "marker.lock")
        }
        previous_inode = (directory / "history-stop.json").stat().st_ino
        with control.admission(self.path, timeout=0):
            token = control.request_stop(self.path)
            self.assertNotEqual(token, baseline)
            self.assertFalse(control.unchanged(self.path, baseline))
            self.assertTrue(control.unchanged(self.path, token))
            self.assertEqual(control.capture(self.path), token)
        self.assertNotEqual(
            previous_inode, (directory / "history-stop.json").stat().st_ino
        )
        for name, inode in locks.items():
            self.assertEqual((directory / name).stat().st_ino, inode)

    def test_readers_do_not_initialize_or_repair(self):
        baseline = "0" * 64
        self.assert_refusal(
            control.unchanged, self.path, baseline, category="missing-control"
        )
        self.assert_refusal(
            self.invoke, "admission", self.path, baseline, category="missing-control"
        )
        for action in ("mark_uncertain", "finish_transition", "require_certain"):
            self.assert_refusal(
                getattr(control, action), self.path, category="missing-control"
            )
        self.assertEqual(
            {item.name for item in self.path.parent.iterdir()}, {"workspace.json"}
        )
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        (directory / "history-stop.json").unlink()
        before = {item.name: item.stat().st_ino for item in directory.iterdir()}
        self.all_refuse(self.path, baseline, category="missing-control")
        self.assertEqual(
            before, {item.name: item.stat().st_ino for item in directory.iterdir()}
        )

    def test_missing_locks_and_abandoned_initialization_are_not_recreated(self):
        for missing in ("admission.lock", "marker.lock", "all"):
            with self.subTest(missing=missing):
                path = self.workspace()
                baseline = control.capture(path)
                directory = path.parent / "control"
                names = [item.name for item in directory.iterdir()]
                for name in names if missing == "all" else (missing,):
                    (directory / name).unlink()
                before = {item.name: item.read_bytes() for item in directory.iterdir()}
                with patch.object(control, "_WRITER_TIMEOUT", 0.02):
                    self.all_refuse(path, baseline)
                self.assertEqual(
                    before,
                    {item.name: item.read_bytes() for item in directory.iterdir()},
                )

    def test_owned_legacy_parent_modes_are_preserved(self):
        for mode in (0o700, 0o750, 0o755, 0o711):
            with self.subTest(mode=oct(mode)):
                path = self.workspace()
                path.parent.chmod(mode)
                path.chmod(0o644)
                token = control.capture(path)
                self.assertTrue(control.unchanged(path, token))
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), mode)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_mode_refusal_matrix_does_not_change_original_files(self):
        for subject, mode in (
            ("parent", 0o777),
            ("registry", 0o666),
            ("control", 0o755),
            ("history-stop.json", 0o644),
            ("admission.lock", 0o640),
            ("marker.lock", 0o660),
        ):
            with self.subTest(subject=subject):
                path = self.workspace()
                baseline = control.capture(path)
                directory = path.parent / "control"
                artifact = {
                    "parent": path.parent,
                    "registry": path,
                    "control": directory,
                }.get(subject, directory / subject)
                artifact.chmod(mode)
                marker = directory / "history-stop.json"
                before = marker.read_bytes(), marker.stat().st_ino, artifact.lstat()
                self.all_refuse(path, baseline, category="unsafe-control")
                self.assertEqual(
                    before,
                    (marker.read_bytes(), marker.stat().st_ino, artifact.lstat()),
                )

    def test_symlink_refusal_matrix_never_touches_targets(self):
        for subject in (
            "parent",
            "registry",
            "control",
            "history-stop.json",
            "admission.lock",
            "marker.lock",
        ):
            with self.subTest(subject=subject):
                path = self.workspace()
                baseline = control.capture(path)
                directory = path.parent / "control"
                artifact = {
                    "parent": path.parent,
                    "registry": path,
                    "control": directory,
                }.get(subject, directory / subject)
                target = artifact.with_name(artifact.name + ".original")
                artifact.rename(target)
                artifact.symlink_to(target.name)
                before = artifact.lstat(), target.stat()
                self.all_refuse(path, baseline)
                self.assertTrue(artifact.is_symlink())
                self.assertEqual(before, (artifact.lstat(), target.stat()))

    def test_hardlink_refusal_matrix_preserves_links_and_contents(self):
        for subject in (
            "registry",
            "history-stop.json",
            "admission.lock",
            "marker.lock",
        ):
            with self.subTest(subject=subject):
                path = self.workspace()
                baseline = control.capture(path)
                artifact = (
                    path if subject == "registry" else path.parent / "control" / subject
                )
                alias = self.root / ("alias-" + str(self.count))
                os.link(artifact, alias)
                before = artifact.read_bytes(), artifact.stat()
                self.all_refuse(path, baseline, category="unsafe-control")
                self.assertEqual(before, (artifact.read_bytes(), artifact.stat()))
                self.assertEqual(alias.read_bytes(), before[0])
                self.assertEqual(alias.stat().st_nlink, 2)

    def test_wrong_owner_refused_for_each_relevant_directory_and_file(self):
        for subject in (
            "parent",
            "registry",
            "control",
            "history-stop.json",
            "admission.lock",
            "marker.lock",
        ):
            with self.subTest(subject=subject):
                path = self.workspace()
                baseline = control.capture(path)
                directory = path.parent / "control"
                artifact = {
                    "parent": path.parent,
                    "registry": path,
                    "control": directory,
                }.get(subject, directory / subject)
                target = artifact.stat()
                original_fstat = os.fstat

                def wrong_owner(fd, original_fstat=original_fstat, target=target):
                    info = original_fstat(fd)
                    if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                        fields = list(info)
                        fields[4] = os.getuid() + 1
                        return os.stat_result(fields)
                    return info

                before = (directory / "history-stop.json").read_bytes()
                with patch.object(control.os, "fstat", side_effect=wrong_owner):
                    self.all_refuse(path, baseline, category="unsafe-control")
                self.assertEqual((directory / "history-stop.json").read_bytes(), before)

    def test_nonregular_marker_and_locks_refuse_without_blocking(self):
        for name in ("history-stop.json", "admission.lock", "marker.lock"):
            for kind in ("fifo", "directory"):
                with self.subTest(name=name, kind=kind):
                    path = self.workspace()
                    baseline = control.capture(path)
                    artifact = path.parent / "control" / name
                    artifact.unlink()
                    if kind == "fifo":
                        os.mkfifo(artifact, 0o600)
                    else:
                        artifact.mkdir(mode=0o700)
                    before = artifact.lstat()
                    self.all_refuse(path, baseline)
                    self.assertEqual(before, artifact.lstat())

    def test_bounded_strict_marker_schema_matrix_never_repairs(self):
        baseline = control.capture(self.path)
        marker = self.path.parent / "control" / "history-stop.json"
        valid = {"version": 1, "token": baseline, "uncertain": False}
        objects = (
            {"version": 1, "token": baseline},
            {**valid, "version": 2},
            {**valid, "version": True},
            {**valid, "version": 1.0},
            {**valid, "private-sentinel": 1},
            *({**valid, "uncertain": value} for value in (None, 0, 1, "false", [])),
            *(
                {**valid, "token": value}
                for value in (None, "a" * 63, "a" * 65, "A" * 64, "a" * 63 + "\n")
            ),
        )
        cases = (
            b"",
            b"private-sentinel",
            b"\xff",
            b"[1,2]",
            b"null",
            b"{}",
            b"x" * (control._MAX_MARKER_BYTES + 1),
            *(json.dumps(value).encode() for value in objects),
            b'{"version":1,"version":1,"token":"'
            + baseline.encode()
            + b'","uncertain":false}',
        )
        for index, content in enumerate(cases):
            with self.subTest(case=index):
                marker.write_bytes(content)
                before = marker.stat().st_ino
                self.all_refuse(self.path, baseline, category="invalid-marker")
                self.assertEqual(marker.read_bytes(), content)
                self.assertEqual(marker.stat().st_ino, before)

    def test_uncertainty_is_atomic_persistent_and_never_implicitly_cleared(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        marker = directory / "history-stop.json"
        locks = {
            name: (directory / name).stat().st_ino
            for name in ("admission.lock", "marker.lock")
        }
        self.assertIsNone(control.require_certain(self.path))
        with control.admission(self.path, timeout=0):
            self.assertIsNone(control.mark_uncertain(self.path))
            self.assertEqual(
                json.loads(marker.read_bytes()),
                {"version": 1, "token": baseline, "uncertain": True},
            )
            self.assert_refusal(
                control.require_certain, self.path, category="uncertain-control"
            )
        before = marker.read_bytes(), marker.stat().st_ino
        self.assertIsNone(control.mark_uncertain(self.path))
        for action in ("capture", "unchanged", "require_certain"):
            self.assert_refusal(
                self.invoke, action, self.path, baseline, category="uncertain-control"
            )
        self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))
        stopped = control.request_stop(self.path)
        self.assertNotEqual(stopped, baseline)
        self.assertEqual(
            json.loads(marker.read_bytes()),
            {"version": 1, "token": stopped, "uncertain": True},
        )
        # Admission remains available for an explicit user's lifecycle work.
        with control.admission(self.path, timeout=0):
            for token in (baseline, stopped):
                self.assert_refusal(
                    control.unchanged, self.path, token, category="uncertain-control"
                )
            self.assert_refusal(
                control.require_certain, self.path, category="uncertain-control"
            )
            self.assertIsNone(control.mark_uncertain(self.path))
        for name, inode in locks.items():
            self.assertEqual((directory / name).stat().st_ino, inode)
        self.assertTrue(json.loads(marker.read_bytes())["uncertain"])

    def test_interrupted_uncertainty_publish_preserves_previous_valid_flag(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        marker = directory / "history-stop.json"
        before = marker.read_bytes(), marker.stat().st_ino
        with patch.object(control.os, "replace", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                control.mark_uncertain(self.path)
        self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))
        self.assertTrue(control.unchanged(self.path, baseline))
        self.assertFalse(list(directory.glob(".history-stop.*.tmp")))

    def test_explicit_transition_finish_changes_only_flag_and_is_idempotent(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        marker = directory / "history-stop.json"
        with control.admission(self.path, timeout=0):
            control.mark_uncertain(self.path)
            latest = control.request_stop(self.path)
            self.assertNotEqual(latest, baseline)
            self.assertIsNone(control.finish_transition(self.path))
            self.assertEqual(
                json.loads(marker.read_bytes()),
                {"version": 1, "token": latest, "uncertain": False},
            )
            self.assertIsNone(control.require_certain(self.path))
            self.assertTrue(control.unchanged(self.path, latest))
            self.assertFalse(control.unchanged(self.path, baseline))
            before = marker.read_bytes(), marker.stat().st_ino
            self.assertIsNone(control.finish_transition(self.path))
            self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))

    def test_interrupted_finish_does_not_clear_a_published_uncertainty_fence(self):
        control.capture(self.path)
        with control.admission(self.path, timeout=0):
            control.mark_uncertain(self.path)
            marker = self.path.parent / "control" / "history-stop.json"
            before = marker.read_bytes(), marker.stat().st_ino
            with patch.object(control.os, "replace", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    control.finish_transition(self.path)
            self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))
            self.assert_refusal(
                control.require_certain, self.path, category="uncertain-control"
            )

    def test_invalid_inputs_are_fixed_errors_and_do_not_initialize(self):
        for baseline in (None, {}, "private-sentinel", "a" * 65, b"a" * 64):
            self.assert_refusal(
                control.unchanged, self.path, baseline, category="invalid-baseline"
            )
        for timeout in (
            None,
            True,
            "private-sentinel",
            -1,
            float("nan"),
            float("inf"),
            control.MAX_TIMEOUT + 1,
            10**1000,
        ):
            self.assert_refusal(
                lambda timeout=timeout: self.invoke_with_timeout(timeout),
                category="invalid-timeout",
            )
        for path in (
            self.path.with_name("private-sentinel"),
            self.path.parent / ".." / self.path.parent.name / "workspace.json",
        ):
            self.assert_refusal(control.capture, path, category="invalid-path")
        self.assertFalse((self.path.parent / "control").exists())
        self.assertEqual(
            control.ControlError("private-sentinel").category, "control-unavailable"
        )

    def invoke_with_timeout(self, timeout):
        with control.admission(self.path, timeout=timeout):
            self.fail("Invalid timeout admitted")

    def test_all_open_descriptors_are_close_on_exec_and_no_tree_is_scanned(self):
        original_open = os.open
        observed = []

        def checked_open(*args, **kwargs):
            self.assertTrue(args[1] & os.O_CLOEXEC)
            fd = original_open(*args, **kwargs)
            self.assertFalse(os.get_inheritable(fd))
            observed.append(fd)
            return fd

        with (
            patch.object(control.os, "open", side_effect=checked_open),
            patch.object(os, "scandir", side_effect=AssertionError("No scanning")),
            patch.object(os, "listdir", side_effect=AssertionError("No scanning")),
        ):
            baseline = control.capture(self.path)
            with control.admission(self.path, timeout=0):
                self.assertTrue(control.unchanged(self.path, baseline))
                control.request_stop(self.path)
        self.assertGreater(len(observed), 10)

    def test_interrupted_writer_cleans_only_own_temp_and_preserves_marker(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        abandoned = directory / ".history-stop.abandoned.tmp"
        abandoned.write_bytes(b"private-sentinel")
        abandoned.chmod(0o600)
        marker = directory / "history-stop.json"
        before = marker.read_bytes(), marker.stat().st_ino
        for operation in ("fchmod", "write", "fsync", "replace"):
            for error in (KeyboardInterrupt(), OSError("private-sentinel")):
                with self.subTest(operation=operation, error=type(error).__name__):
                    with patch.object(control.os, operation, side_effect=error):
                        if isinstance(error, KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                control.request_stop(self.path)
                        else:
                            self.assert_refusal(
                                control.request_stop,
                                self.path,
                                category="control-unavailable",
                            )
                    self.assertEqual(
                        before, (marker.read_bytes(), marker.stat().st_ino)
                    )
                    self.assertEqual(abandoned.read_bytes(), b"private-sentinel")
                    self.assertEqual(
                        {item.name for item in directory.iterdir()},
                        {
                            "admission.lock",
                            "marker.lock",
                            "history-stop.json",
                            abandoned.name,
                        },
                    )
                    self.assertEqual(control.capture(self.path), baseline)
                    with control.admission(self.path, timeout=0):
                        pass

    def test_temp_cleanup_failure_does_not_mask_keyboard_interrupt(self):
        baseline = control.capture(self.path)
        with (
            patch.object(control.os, "replace", side_effect=KeyboardInterrupt()),
            patch.object(control.os, "unlink", side_effect=OSError("private-sentinel")),
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.request_stop(self.path)
        self.assertTrue(control.unchanged(self.path, baseline))
        directory = self.path.parent / "control"
        leftovers = list(directory.glob(".history-stop.*.tmp"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(stat.S_IMODE(leftovers[0].stat().st_mode), 0o600)

    def test_writer_never_unlinks_a_substituted_temp_pathname(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        saved = directory / "saved-temp"
        target = self.root / "private-sentinel"
        target.write_bytes(b"private-sentinel")
        substituted = []

        def substitute_temp(source, destination, **kwargs):
            temp = directory / source
            temp.rename(saved)
            temp.symlink_to(target)
            substituted.append(temp)
            raise OSError("private-sentinel")

        with patch.object(control.os, "replace", side_effect=substitute_temp):
            self.assert_refusal(control.request_stop, self.path)
        self.assertTrue(control.unchanged(self.path, baseline))
        self.assertEqual(len(substituted), 1)
        self.assertTrue(substituted[0].is_symlink())
        self.assertEqual(target.read_bytes(), b"private-sentinel")
        self.assertTrue(saved.is_file())

    def test_initialization_interruption_is_not_silently_repaired(self):
        with patch.object(control.os, "replace", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                control.capture(self.path)
        directory = self.path.parent / "control"
        before = {item.name: item.read_bytes() for item in directory.iterdir()}
        self.assertEqual(before, {"marker.lock": b"", "admission.lock": b""})
        with patch.object(control, "_WRITER_TIMEOUT", 0.02):
            self.all_refuse(self.path, "0" * 64)
        self.assertEqual(
            before, {item.name: item.read_bytes() for item in directory.iterdir()}
        )

    def test_destination_substitution_is_not_overwritten(self):
        baseline = control.capture(self.path)
        directory = self.path.parent / "control"
        marker = directory / "history-stop.json"
        saved = directory / "saved-marker"
        target = self.root / "private-sentinel"
        target.write_bytes(b"private-sentinel")
        original_write = control._write_all

        def substitute(fd, content):
            original_write(fd, content)
            marker.rename(saved)
            marker.symlink_to(target)

        with patch.object(control, "_write_all", side_effect=substitute):
            self.assert_refusal(control.request_stop, self.path)
        self.assertTrue(marker.is_symlink())
        self.assertEqual(target.read_bytes(), b"private-sentinel")
        self.assertEqual(json.loads(saved.read_bytes())["token"], baseline)
        self.assertFalse(list(directory.glob(".history-stop.*.tmp")))

    def test_caller_errors_propagate_unchanged_and_release_admission(self):
        control.capture(self.path)
        for error in (
            ValueError("private-sentinel"),
            OSError("private-sentinel"),
            KeyboardInterrupt(),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    with control.admission(self.path, timeout=0):
                        raise error
                self.assertIs(raised.exception, error)
                with control.admission(self.path, timeout=0):
                    pass


class WorkspaceControlProcessTests(_ControlFixture):
    def setUp(self):
        super().setUp()
        self.context = multiprocessing.get_context("spawn")

    def process(self, target, *args):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=target, args=(*args, child))
        process.start()
        child.close()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            parent.close()

        self.addCleanup(cleanup)
        return process, parent

    def receive(self, connection):
        self.assertTrue(connection.poll(4), "Private control child did not respond")
        return connection.recv()

    def finish(self, process):
        process.join(timeout=4)
        self.assertFalse(process.is_alive(), "Private control child did not exit")
        self.assertEqual(process.exitcode, 0)

    def test_independent_process_admission_is_serialized_on_stable_inode(self):
        control.capture(self.path)
        lock = self.path.parent / "control" / "admission.lock"
        inode = lock.stat().st_ino
        first, one = self.process(_hold_admission, str(self.path))
        self.assertEqual(self.receive(one), ("waiting",))
        self.assertEqual(self.receive(one), ("admitted",))
        second, two = self.process(_hold_admission, str(self.path))
        self.assertEqual(self.receive(two), ("waiting",))
        self.assertFalse(two.poll(0.1))
        one.send("release")
        self.assertEqual(self.receive(one), ("released",))
        self.assertEqual(self.receive(two), ("admitted",))
        two.send("release")
        self.assertEqual(self.receive(two), ("released",))
        self.finish(first)
        self.finish(second)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_stop_is_published_before_waiting_for_held_admission(self):
        baseline = control.capture(self.path)
        with control.admission(self.path, timeout=0):
            process, connection = self.process(_stop_then_admit, str(self.path))
            message = self.receive(connection)
            self.assertEqual(message[0], "published")
            token = message[1]
            self.assertNotEqual(token, baseline)
            self.assertFalse(control.unchanged(self.path, baseline))
            self.assertEqual(control.capture(self.path), token)
            self.assertFalse(connection.poll(0.1))
        self.assertEqual(self.receive(connection), ("admitted", token))
        self.finish(process)

    def test_finish_preserves_stop_published_by_an_independent_process(self):
        baseline = control.capture(self.path)
        with control.admission(self.path, timeout=0):
            control.mark_uncertain(self.path)
            process, connection = self.process(_publish_stop, str(self.path))
            published = self.receive(connection)
            self.assertEqual(published[0], "published")
            token = published[1]
            self.assertNotEqual(token, baseline)
            self.assert_refusal(
                control.require_certain, self.path, category="uncertain-control"
            )
            control.finish_transition(self.path)
            self.assertIsNone(control.require_certain(self.path))
            self.assertTrue(control.unchanged(self.path, token))
            self.assertFalse(control.unchanged(self.path, baseline))
            self.assertEqual(control.capture(self.path), token)
        self.finish(process)

    def test_independent_process_timeout_is_bounded_and_nonmutating(self):
        baseline = control.capture(self.path)
        marker = self.path.parent / "control" / "history-stop.json"
        before = marker.read_bytes(), marker.stat().st_ino
        with control.admission(self.path, timeout=0):
            process, connection = self.process(_timed_admission, str(self.path))
            message = self.receive(connection)
            self.assertEqual(message[:2], ("error", "control-timeout"))
            self.assertGreaterEqual(message[2], 0.08)
            self.assertLess(message[2], 2)
            self.assert_refusal(
                self.invoke,
                "admission",
                self.path,
                baseline,
                category="control-timeout",
            )
        self.finish(process)
        self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))
        self.assertTrue(control.unchanged(self.path, baseline))

    def test_process_interruption_releases_without_recreating_lock(self):
        control.capture(self.path)
        lock = self.path.parent / "control" / "admission.lock"
        before = lock.stat().st_ino
        process, connection = self.process(_hold_admission, str(self.path))
        self.assertEqual(self.receive(connection), ("waiting",))
        self.assertEqual(self.receive(connection), ("admitted",))
        process.terminate()
        process.join(timeout=3)
        self.assertFalse(process.is_alive())
        with control.admission(self.path, timeout=0.2):
            self.assertEqual(lock.stat().st_ino, before)

    def test_first_use_races_preserve_stop_intent_in_both_creator_orders(self):
        for first_action, second_action in (
            ("capture", "request_stop"),
            ("request_stop", "capture"),
        ):
            with self.subTest(creator=first_action):
                path = self.workspace()
                # This helper has one extra argument after its pipe, unlike the
                # other workers; use a small explicit process setup here.
                connections = []
                processes = []
                for action, pause in ((first_action, True), (second_action, False)):
                    parent, child = self.context.Pipe()
                    process = self.context.Process(
                        target=_first_use,
                        args=(str(path), action, child, pause),
                    )
                    process.start()
                    child.close()
                    connections.append(parent)
                    processes.append(process)
                    self.addCleanup(parent.close)
                    self.addCleanup(self.stop_process, process)
                    self.assertEqual(self.receive(parent), ("started",))
                    if pause:
                        self.assertEqual(self.receive(parent), ("directory-created",))
                connections[0].send("initialize")
                first = self.receive(connections[0])
                second = self.receive(connections[1])
                self.assertEqual(first[0], "token")
                self.assertEqual(second[0], "token")
                if first_action == "request_stop":
                    self.assertEqual(first[1], second[1])
                else:
                    self.assertNotEqual(first[1], second[1])
                self.assertEqual(control.capture(path), second[1])
                for process in processes:
                    self.finish(process)

    def stop_process(self, process):
        if process.is_alive():
            process.terminate()
        process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
