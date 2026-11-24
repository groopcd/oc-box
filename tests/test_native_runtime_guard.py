from dataclasses import FrozenInstanceError, replace
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from oc_box import native_runtime_guard as guard


_NAME = "native-runtime-guard.json"
_REJECTION_NAME = "native-runtime-rejection.json"
_PRIVATE = "private-error-sentinel"
_PENDING = guard.Record("a" * 64, "oc-box-home-fixture", "2.3.9", "b" * 64, False)
_READY = replace(_PENDING, ready=True)
_NEXT = replace(_PENDING, version="2.3.10", digest="c" * 64)
_REJECTED = guard.Rejection("2.4.0", "d" * 64, "e" * 64, "build-plugin-failed")


def _crash_before_rename(path):
    with patch.object(guard.os, "replace", side_effect=lambda *a, **k: os._exit(37)):
        guard.publish(path, _READY, _NEXT)


class NativeRuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="oc-box-native-guard-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.count = 0
        self.path = self.workspace()
        self.directory = self.path.parent
        self.sidecar = self.directory / _NAME
        self.receipt = self.directory / _REJECTION_NAME

    def workspace(self):
        self.count += 1
        ident = f"{self.count:032x}"
        folder = self.root / ident
        folder.mkdir(mode=0o700)
        path = folder / "workspace.json"
        self.write(path, json.dumps({"id": ident, "project": "/example/workspace"}))
        return path

    @staticmethod
    def write(path, content, mode=0o600):
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(mode)

    def assert_refusal(self, category, action, *args):
        with self.assertRaises(guard.GuardError) as raised:
            action(*args)
        self.assertEqual(raised.exception.category, category)
        self.assertNotIn(_PRIVATE, str(raised.exception))
        self.assertNotIn(str(self.root), str(raised.exception))

    def assert_storage_refused(self, path, category):
        self.assert_refusal(category, guard.load, path)
        self.assert_refusal(category, guard.publish, path, None, _PENDING)

    def test_absence_legacy_modes_and_durable_private_record(self):
        self.path.chmod(0o644)
        self.directory.chmod(0o755)
        abandoned = self.directory / ".native-runtime-guard-abandoned.tmp"
        self.write(abandoned, b"")
        original = self.path.stat()
        self.assertIsNone(guard.load(self.path))
        self.assertEqual(set(self.directory.iterdir()), {self.path, abandoned})
        umask = os.umask(0o777)
        try:
            guard.publish(self.path, None, _PENDING)
        finally:
            os.umask(umask)
        self.assertEqual(guard.load(self.path), _PENDING)
        self.assertEqual(
            set(self.directory.iterdir()), {self.path, abandoned, self.sidecar}
        )
        info = self.sidecar.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(
            (info.st_uid, info.st_nlink, stat.S_IMODE(info.st_mode)),
            (os.geteuid(), 1, 0o600),
        )
        self.assertEqual(self.path.stat().st_ino, original.st_ino)
        self.assertEqual(self.path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        record = guard.load(self.path)
        with self.assertRaises(FrozenInstanceError):
            record.ready = True

    def test_registered_path_and_registry_json_are_required(self):
        self.assert_storage_refused(self.root / "workspace.json", "invalid-path")
        self.assert_storage_refused(
            self.path.with_name("elsewhere.json"), "invalid-path"
        )
        self.assert_storage_refused(
            self.directory / ".." / self.directory.name / self.path.name, "invalid-path"
        )
        self.assert_storage_refused(
            self.root / ("f" * 32) / "workspace.json", "unsafe-storage"
        )
        self.path.unlink()
        self.assert_storage_refused(self.path, "invalid-registration")
        for raw in (
            b"",
            b"[]",
            b'{"id":"other"}',
            f'{{"id":"other","id":"{self.directory.name}"}}'.encode(),
            b"\xff",
            b" " * (1024 * 1024 + 1),
        ):
            with self.subTest(case=raw[:64]):
                self.write(self.path, raw)
                self.assert_storage_refused(self.path, "invalid-registration")
        self.assertEqual(set(self.directory.iterdir()), {self.path})

    def test_parent_permissions_and_symlinked_ancestor_are_refused(self):
        for mode in (0o720, 0o702):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                self.assert_storage_refused(self.path, "unsafe-storage")
        self.directory.chmod(0o700)
        with patch.object(guard.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_storage_refused(self.path, "unsafe-storage")
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_storage_refused(
            alias / self.directory.name / self.path.name, "unsafe-storage"
        )
        final_alias = self.root / ("f" * 32)
        final_alias.symlink_to(self.directory, target_is_directory=True)
        self.assert_storage_refused(final_alias / self.path.name, "unsafe-storage")
        self.assertFalse(self.sidecar.exists())

    def test_unsafe_registry_and_sidecar_entries_are_never_opened(self):
        for name in ("workspace.json", _NAME):
            for kind in ("symlink", "dangling", "hardlink", "fifo", "directory"):
                with self.subTest(name=name, kind=kind):
                    path = self.workspace()
                    entry = path.with_name(name)
                    target = entry.with_name("target")
                    if name == "workspace.json":
                        path.rename(target)
                    else:
                        self.write(target, b"unchanged")
                    before = target.read_bytes()
                    if kind == "symlink":
                        entry.symlink_to(target)
                    elif kind == "dangling":
                        entry.symlink_to(target.with_name("missing"))
                    elif kind == "hardlink":
                        os.link(target, entry)
                    elif kind == "fifo":
                        os.mkfifo(entry, mode=0o600)
                    else:
                        entry.mkdir(mode=0o700)
                    inode = entry.lstat().st_ino
                    real_open = os.open

                    def check_open(name, *args, **kwargs):
                        self.assertNotEqual(name, entry.name)
                        return real_open(name, *args, **kwargs)

                    with patch.object(guard.os, "open", side_effect=check_open):
                        self.assert_storage_refused(path, "unsafe-storage")
                    self.assertEqual(entry.lstat().st_ino, inode)
                    self.assertEqual(target.read_bytes(), before)

    def test_unsafe_file_permissions_and_foreign_owner_are_refused(self):
        for mode in (0o620, 0o602):
            with self.subTest(registry_mode=oct(mode)):
                self.path.chmod(mode)
                self.assert_storage_refused(self.path, "unsafe-storage")
        self.path.chmod(0o600)
        guard.publish(self.path, None, _PENDING)
        for mode in (0o640, 0o644, 0o700, 0o666):
            with self.subTest(sidecar_mode=oct(mode)):
                self.sidecar.chmod(mode)
                self.assert_storage_refused(self.path, "unsafe-storage")
        self.sidecar.chmod(0o600)
        real_stat = os.stat

        for name in (self.path.name, _NAME):
            with self.subTest(foreign_owner=name):

                def foreign(entry, *args, **kwargs):
                    info = real_stat(entry, *args, **kwargs)
                    if entry == name and kwargs.get("dir_fd") is not None:
                        fields = list(info)
                        fields[stat.ST_UID] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return info

                with patch.object(guard.os, "stat", side_effect=foreign):
                    self.assert_storage_refused(self.path, "unsafe-storage")
        self.assertEqual(guard.load(self.path), _PENDING)

    def test_schema_refuses_malformed_duplicate_oversized_and_foreign_records(self):
        guard.publish(self.path, None, _PENDING)
        good = self.sidecar.read_bytes()
        for label, raw in (
            ("empty", b""),
            ("duplicate", good.replace(b'"schema":1', b'"schema":0,"schema":1')),
            ("unknown", good.replace(b'"schema":1', b'"schema":1,"private":"secret"')),
            ("wrong schema", good.replace(b'"schema":1', b'"schema":true')),
            ("future schema", good.replace(b'"schema":1', b'"schema":2')),
            ("foreign", good.replace(self.directory.name.encode(), b"f" * 32)),
            ("truncated", good[:-3]),
            ("invalid utf8", b"\xff" + good),
            ("not json", b"not-json-private-error-sentinel"),
            ("oversized", good + b" " * 1024),
            ("non-finite", good.replace(b'"ready":false', b'"ready":NaN')),
            ("nonbool readiness", good.replace(b'"ready":false', b'"ready":0')),
            ("invalid release", good.replace(b"2.3.9", b"2.3.9-rc.1")),
        ):
            with self.subTest(case=label):
                self.write(self.sidecar, raw)
                self.assert_storage_refused(self.path, "invalid-record")
                self.assertEqual(self.sidecar.read_bytes(), raw)
        foreign = self.workspace()
        self.write(foreign.with_name(_NAME), good)
        self.assert_storage_refused(foreign, "invalid-record")

    def test_record_field_validation_is_bounded_and_canonical(self):
        for field, value in (
            ("container", "a" * 12),
            ("container", "A" * 64),
            ("volume", "../volume"),
            ("volume", "v" * 129),
            ("digest", "sha256:" + _PENDING.digest),
            ("digest", "x" * 64),
            ("version", "1.3.9"),
            ("version", "3.3.9"),
            ("version", "2.03.9"),
            ("version", "2.3.09"),
            ("version", "2.3.9\n"),
            ("version", "v2.3.9"),
            ("version", "2.3.9+build"),
            ("version", "2.3." + "9" * 15),
            ("ready", 0),
        ):
            with self.subTest(field=field, value=value):
                record = replace(_PENDING, **{field: value})
                self.assert_refusal(
                    "invalid-record", guard.publish, self.path, None, record
                )
                self.assertFalse(self.sidecar.exists())

    def test_pending_redo_readiness_and_forward_fence(self):
        self.assert_refusal(
            "invalid-transition", guard.publish, self.path, None, _READY
        )
        guard.publish(self.path, None, _PENDING)
        first_inode = self.sidecar.stat().st_ino
        guard.publish(self.path, guard.load(self.path), _PENDING)
        self.assertNotEqual(self.sidecar.stat().st_ino, first_inode)
        ready_next = replace(_NEXT, ready=True)
        self.assert_refusal(
            "invalid-transition", guard.publish, self.path, _PENDING, ready_next
        )
        guard.publish(self.path, _PENDING, _READY)
        self.assertEqual(guard.load(self.path), _READY)
        guard.publish(self.path, _READY, _PENDING)
        self.assertEqual(guard.load(self.path), _PENDING)
        guard.publish(self.path, _PENDING, _NEXT)
        self.assertEqual(guard.load(self.path), _NEXT)
        guard.publish(self.path, _NEXT, ready_next)
        self.assertEqual(guard.load(self.path), ready_next)
        # Numeric comparison must also work across minor versions.
        minor = replace(_NEXT, version="2.10.0", digest="d" * 64)
        guard.publish(self.path, ready_next, minor)
        self.assertEqual(guard.load(self.path), minor)

    def test_regression_rebinding_and_same_version_digest_change_refuse(self):
        guard.publish(self.path, None, _NEXT)
        for previous in (_NEXT, replace(_NEXT, ready=True)):
            self.assertEqual(guard.load(self.path), previous)
            before = self.sidecar.read_bytes()
            for record in (
                _PENDING,
                _READY,
                replace(_NEXT, digest=_PENDING.digest),
                replace(_NEXT, digest=_PENDING.digest, ready=True),
                replace(_NEXT, version="2.4.0", container="0" * 64),
                replace(_NEXT, version="2.4.0", volume="other-volume"),
                replace(_NEXT, version="2.4.0", ready=True),
            ):
                with self.subTest(previous=previous, record=record):
                    self.assert_refusal(
                        "invalid-transition", guard.publish, self.path, previous, record
                    )
                    self.assertEqual(self.sidecar.read_bytes(), before)
            if not previous.ready:
                guard.publish(self.path, _NEXT, replace(_NEXT, ready=True))

    def test_cas_rejects_stale_value_and_missing_expected_record(self):
        self.assert_refusal("conflict", guard.publish, self.path, _PENDING, _READY)
        guard.publish(self.path, None, _PENDING)
        self.assert_refusal("conflict", guard.publish, self.path, None, _NEXT)
        guard.publish(self.path, _PENDING, _READY)
        self.assert_refusal("conflict", guard.publish, self.path, _PENDING, _NEXT)
        self.assertEqual(guard.load(self.path), _READY)

    def test_existing_fence_lost_during_read_is_not_reported_absent(self):
        guard.publish(self.path, None, _PENDING)
        inode = self.sidecar.stat().st_ino
        real_read = os.read

        def remove_after_read(fd, limit):
            data = real_read(fd, limit)
            if data and os.fstat(fd).st_ino == inode:
                self.sidecar.unlink()
            return data

        with patch.object(guard.os, "read", side_effect=remove_after_read):
            self.assert_refusal("unsafe-storage", guard.load, self.path)

    def test_rechecks_registry_directory_and_cas_after_staging(self):
        real_fsync = os.fsync
        for target in ("registry", "directory", "sidecar", "absent-sidecar"):
            with self.subTest(target=target):
                path = self.workspace()
                sidecar = path.with_name(_NAME)
                previous = None if target == "absent-sidecar" else _PENDING
                if previous is not None:
                    guard.publish(path, None, previous)
                retained = self.root / (path.parent.name + "-retained")

                def substitute(fd):
                    real_fsync(fd)
                    if target == "directory":
                        raw = path.read_bytes()
                        path.parent.rename(retained)
                        path.parent.mkdir(mode=0o700)
                        self.write(path, raw)
                    elif target == "absent-sidecar":
                        self.write(sidecar, b"unexpected")
                    else:
                        entry = path if target == "registry" else sidecar
                        other = entry.with_name("replacement")
                        self.write(other, entry.read_bytes())
                        other.replace(entry)

                with patch.object(guard.os, "fsync", side_effect=substitute):
                    self.assert_refusal(
                        "conflict" if target.endswith("sidecar") else "unsafe-storage",
                        guard.publish,
                        path,
                        previous,
                        _NEXT,
                    )
                if target == "directory":
                    self.assertEqual(
                        {p.name for p in path.parent.iterdir()}, {path.name}
                    )
                    self.assertEqual(
                        {p.name for p in retained.iterdir()}, {path.name, _NAME}
                    )
                elif target == "absent-sidecar":
                    self.assertEqual(sidecar.read_bytes(), b"unexpected")
                else:
                    self.assertEqual(guard.load(path), _PENDING)
                    self.assertEqual(
                        {p.name for p in path.parent.iterdir()}, {path.name, _NAME}
                    )

    def test_durable_publish_order_and_fd_relative_atomic_replace(self):
        events = []
        real_fchmod = os.fchmod
        real_fsync = os.fsync
        real_replace = os.replace

        def chmod(fd, mode):
            events.append("chmod")
            self.assertEqual(mode, 0o600)
            return real_fchmod(fd, mode)

        def sync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                events.append("sync-directory")
                self.assertEqual(guard.load(self.path), _PENDING)
                self.assertEqual(info.st_ino, self.directory.stat().st_ino)
            else:
                events.append("sync-bytes")
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertGreater(info.st_size, 0)
                self.assertFalse(self.sidecar.exists())
            return real_fsync(fd)

        def rename(source, dest, *, src_dir_fd, dst_dir_fd):
            events.append("rename")
            self.assertFalse(Path(source).is_absolute())
            self.assertEqual(dest, _NAME)
            self.assertEqual(src_dir_fd, dst_dir_fd)
            self.assertIsNone(guard.load(self.path))
            return real_replace(
                source, dest, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
            )

        with (
            patch.object(guard.os, "fchmod", side_effect=chmod),
            patch.object(guard.os, "fsync", side_effect=sync),
            patch.object(guard.os, "replace", side_effect=rename),
        ):
            guard.publish(self.path, None, _PENDING)
        self.assertEqual(events, ["chmod", "sync-bytes", "rename", "sync-directory"])

    def test_failure_before_rename_preserves_prior_fence_and_cleans_only_own_temp(self):
        guard.publish(self.path, None, _PENDING)
        abandoned = self.directory / ".native-runtime-guard-abandoned.tmp"
        self.write(abandoned, _PRIVATE)
        before = self.sidecar.read_bytes()
        for operation in ("fchmod", "write", "fsync", "replace"):
            with self.subTest(operation=operation):
                with patch.object(guard.os, operation, side_effect=OSError(_PRIVATE)):
                    self.assert_refusal(
                        "publish-unverified", guard.publish, self.path, _PENDING, _READY
                    )
                self.assertEqual(self.sidecar.read_bytes(), before)
                self.assertEqual(abandoned.read_text(), _PRIVATE)
                self.assertEqual(
                    set(self.directory.iterdir()), {self.path, self.sidecar, abandoned}
                )

    def test_directory_fsync_failure_leaves_visible_but_unconfirmed_pending(self):
        guard.publish(self.path, None, _PENDING)
        guard.publish(self.path, _PENDING, _READY)
        real_fsync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(_PRIVATE)
            return real_fsync(fd)

        with patch.object(guard.os, "fsync", side_effect=fail_directory):
            self.assert_refusal(
                "publish-unverified", guard.publish, self.path, _READY, _NEXT
            )
        self.assertEqual(guard.load(self.path), _NEXT)
        inode = self.sidecar.stat().st_ino
        guard.publish(self.path, _NEXT, _NEXT)
        self.assertNotEqual(self.sidecar.stat().st_ino, inode)
        self.assertEqual(guard.load(self.path), _NEXT)

    def test_interrupted_writer_temp_does_not_replace_or_reinitialize_fence(self):
        guard.publish(self.path, None, _PENDING)
        guard.publish(self.path, _PENDING, _READY)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_before_rename, args=(self.path,)
        )
        process.start()
        try:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 37)
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()
        leftovers = set(self.directory.iterdir()) - {self.path, self.sidecar}
        self.assertEqual(len(leftovers), 1)
        temp = leftovers.pop()
        self.assertEqual(stat.S_IMODE(temp.stat().st_mode), 0o600)
        self.assertEqual(guard.load(self.path), _READY)
        guard.publish(self.path, _READY, _NEXT)
        self.assertEqual(guard.load(self.path), _NEXT)
        self.assertTrue(temp.exists())

    def test_receipt_is_private_and_never_changes_or_requires_positive_fence(self):
        self.assertIsNone(guard.rejected(self.path))
        self.assertEqual(set(self.directory.iterdir()), {self.path})
        guard.publish_rejection(self.path, None, _REJECTED)
        self.assertEqual(guard.rejected(self.path), _REJECTED)
        self.assertIsNone(guard.load(self.path))
        info = self.receipt.lstat()
        self.assertEqual((info.st_uid, info.st_nlink), (os.geteuid(), 1))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(set(self.directory.iterdir()), {self.path, self.receipt})
        receipt = guard.rejected(self.path)
        with self.assertRaises(FrozenInstanceError):
            receipt.category = "readiness-contract"

        guard.publish(self.path, None, _PENDING)
        before = (self.sidecar.stat().st_ino, self.sidecar.read_bytes())
        # A receipt is a single cached result, not a version floor.
        other = replace(_REJECTED, version="2.3.8", category="readiness-contract")
        self.assert_refusal("conflict", guard.publish_rejection, self.path, None, other)
        guard.publish_rejection(self.path, _REJECTED, other)
        self.assert_refusal(
            "conflict", guard.publish_rejection, self.path, _REJECTED, other
        )
        self.assertEqual(guard.rejected(self.path), other)
        self.assertEqual(
            (self.sidecar.stat().st_ino, self.sidecar.read_bytes()), before
        )
        guard.publish(self.path, _PENDING, _READY)
        self.assertEqual(guard.rejected(self.path), other)

    def test_receipt_validation_is_closed_bounded_and_workspace_bound(self):
        for field, invalid in (
            ("version", "1.4.0"),
            ("version", "2.4.0+build"),
            ("host_digest", "x" * 64),
            ("probe_digest", ""),
            ("probe_digest", None),
            ("category", "probe-timeout"),
            ("category", _PRIVATE),
            ("category", ["build-plugin-failed"]),
        ):
            with self.subTest(field=field, value=invalid):
                self.assert_refusal(
                    "invalid-record",
                    guard.publish_rejection,
                    self.path,
                    None,
                    replace(_REJECTED, **{field: invalid}),
                )
                self.assertFalse(self.receipt.exists())

        guard.publish_rejection(self.path, None, _REJECTED)
        before = self.receipt.read_bytes()
        self.assert_refusal(
            "invalid-record", guard.publish_rejection, self.path, _REJECTED, None
        )
        self.assertEqual(self.receipt.read_bytes(), before)
        for raw in (
            before + b" " * 1024,
            before.replace(b'"schema":1', b'"schema":1,"schema":1'),
            before.replace(b"build-plugin-failed", b"probe-timeout"),
            before.replace(self.directory.name.encode(), b"f" * 32),
        ):
            with self.subTest(raw=raw[:64]):
                self.write(self.receipt, raw)
                self.assert_refusal("invalid-record", guard.rejected, self.path)
                self.assert_refusal(
                    "invalid-record",
                    guard.publish_rejection,
                    self.path,
                    None,
                    _REJECTED,
                )
                self.assertEqual(self.receipt.read_bytes(), raw)
                self.assertIsNone(guard.load(self.path))

    def test_receipt_unsafe_storage_is_not_a_cache_miss_or_permission_to_replace(self):
        self.receipt.symlink_to(self.directory / "missing")
        self.assert_refusal("unsafe-storage", guard.rejected, self.path)
        self.assert_refusal(
            "unsafe-storage", guard.publish_rejection, self.path, None, _REJECTED
        )
        self.assertTrue(self.receipt.is_symlink())
        self.assertIsNone(guard.load(self.path))

    def test_receipt_save_failure_cannot_modify_positive_fence(self):
        guard.publish(self.path, None, _PENDING)
        real_fsync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(_PRIVATE)
            return real_fsync(fd)

        with patch.object(guard.os, "fsync", side_effect=fail_directory):
            self.assert_refusal(
                "publish-unverified",
                guard.publish_rejection,
                self.path,
                None,
                _REJECTED,
            )
        self.assertEqual(guard.rejected(self.path), _REJECTED)
        self.assertEqual(guard.load(self.path), _PENDING)
        self.assertEqual(
            set(self.directory.iterdir()), {self.path, self.sidecar, self.receipt}
        )
