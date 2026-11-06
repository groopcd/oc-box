import argparse
import contextlib
import ctypes
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import status_report
import workspace_identity as identity

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_identity", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


def stable(fsid="filesystem-a", handle="01020304", kind=77):
    return {
        "method": "linux-file-handle-v1",
        "fsid": fsid,
        "handle": {"type": kind, "bytes": handle},
    }


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        prerequisites = patch.object(box.dependencies, "require")
        prerequisites.start()
        self.addCleanup(prerequisites.stop)
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.before = [47, 17530529]
        self.after = [48, 17530529]
        identity.record(self.data, self.before, stable())
        box.atomic_json(self.path, self.data)
        box.atomic_json(
            box.session_file(self.path), {"last": "ses_keep", "sessions": ["ses_keep"]}
        )

    def test_device_number_changes_are_allowed_only_with_same_filesystem_handle_and_inode(
        self,
    ):
        self.assertTrue(identity.matches(self.data, self.after, stable()))
        for observed, fingerprint in (
            ([48, 123], stable()),
            (self.after, stable(fsid="different-filesystem")),
            (self.after, stable(handle="new-subvolume-or-generation")),
            (self.after, stable(kind=1)),
            (self.after, None),
            (self.before, stable(handle="reused-inode-generation")),
        ):
            with self.subTest(observed=observed, fingerprint=fingerprint):
                self.assertFalse(identity.matches(self.data, observed, fingerprint))

    def test_btrfs_inode_256_in_another_subvolume_is_rejected(self):
        self.data["project_identity"][1] = 256
        for fsid, handle in (
            ("filesystem-a", "different-root-id"),
            ("other-fs", "01020304"),
        ):
            self.assertFalse(
                identity.matches(self.data, [48, 256], stable(fsid, handle))
            )

    def test_driver_handle_formats_are_opaque_and_not_filesystem_allowlisted(self):
        # Driver capability fixtures, not a claim of live filesystem coverage.
        for driver, kind, length in (
            ("ext4", 2, 8),
            ("xfs", 129, 16),
            ("btrfs", 77, 20),
            ("bcachefs", 128, 24),
            ("zfs", 1, 32),
            ("unknown-future-driver", 9999, 64),
        ):
            with self.subTest(driver=driver):
                fingerprint = stable("opaque-fsid", "01" * length, kind)
                data = {
                    "project_identity": [47, 123],
                    identity.STABLE_IDENTITY_KEY: fingerprint,
                }
                self.assertTrue(identity.matches(data, [48, 123], fingerprint))
                self.assertFalse(identity.matches(data, [48, 123], None))

    def test_recorded_stable_identity_never_falls_back_to_matching_device_inode(self):
        self.assertFalse(identity.matches(self.data, self.before, None))
        self.data[identity.STABLE_IDENTITY_KEY] = None
        self.assertFalse(identity.matches(self.data, self.before, stable()))
        self.data[identity.STABLE_IDENTITY_KEY] = {"method": "unknown"}
        self.assertFalse(identity.matches(self.data, self.before, stable()))

    def test_legacy_registration_uses_strict_device_inode_until_verified_or_repaired(
        self,
    ):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        self.assertTrue(identity.matches(self.data, self.before, stable()))
        self.assertFalse(identity.matches(self.data, self.after, stable()))
        box.atomic_json(self.path, self.data)
        with patch.object(identity, "snapshot", return_value=(self.after, stable())):
            with self.assertRaisesRegex(box.BoxError, "one explicit repair"):
                box.find_workspace(self.project)

    def test_verified_lookup_and_status_are_read_only_across_reboot(self):
        before = self.path.read_bytes()
        with patch.object(identity, "snapshot", return_value=(self.after, stable())):
            self.assertEqual(box.find_workspace(self.project), (self.path, self.data))
            self.assertEqual(
                status_report.project_condition(self.data), ("present", self.data["id"])
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_updates_transient_diagnostics_under_lock_without_touching_sessions(
        self,
    ):
        sessions = box.session_file(self.path).read_bytes()
        marker = (self.project / box.MARKER).read_bytes()
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            box.workspace_lock(self.path),
        ):
            box.refresh_workspace_identity(self.path, self.data)
        self.assertEqual(box.read_json(self.path)["project_identity"], self.after)
        self.assertEqual(
            box.read_json(self.path)[identity.STABLE_IDENTITY_KEY], stable()
        )
        self.assertEqual(box.session_file(self.path).read_bytes(), sessions)
        self.assertEqual((self.project / box.MARKER).read_bytes(), marker)

    def test_matching_legacy_registration_is_upgraded_on_launch(self):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        box.atomic_json(self.path, self.data)
        with (
            patch.object(identity, "snapshot", return_value=(self.before, stable())),
            box.workspace_lock(self.path),
        ):
            box.refresh_workspace_identity(self.path, self.data)
        self.assertEqual(
            box.read_json(self.path)[identity.STABLE_IDENTITY_KEY], stable()
        )

    def test_unsupported_filesystem_retains_conservative_fallback(self):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        box.atomic_json(self.path, self.data)
        with (
            patch.object(identity, "snapshot", return_value=(self.before, None)),
            box.workspace_lock(self.path),
            patch.object(box, "atomic_json") as write,
        ):
            box.refresh_workspace_identity(self.path, self.data)
        write.assert_not_called()
        self.assertTrue(identity.matches(self.data, self.before, None))
        self.assertFalse(identity.matches(self.data, self.after, None))

    def test_explicit_repair_records_new_baseline_and_preserves_sessions(self):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        box.atomic_json(self.path, self.data)
        before = box.session_file(self.path).read_bytes()
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            patch.object(box, "inspect_owned", return_value=None),
            box.workspace_lock(self.path),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.repair_workspace(self.path, self.data)
        self.assertEqual(
            box.read_json(self.path)[identity.STABLE_IDENTITY_KEY], stable()
        )
        self.assertEqual(box.read_json(self.path)["project_identity"], self.after)
        self.assertEqual(box.session_file(self.path).read_bytes(), before)

    def test_refresh_refuses_another_marker_and_does_not_write(self):
        (self.project / box.MARKER).unlink()
        with patch.object(identity, "snapshot", return_value=(self.after, stable())):
            other_path, _ = box.create_workspace(self.project, "ubuntu")
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            patch.object(box, "atomic_json") as write,
        ):
            with self.assertRaisesRegex(box.BoxError, "marker does not match"):
                box.refresh_workspace_identity(self.path, self.data)
        write.assert_not_called()
        self.assertTrue(other_path.exists())

    def test_new_container_creation_uses_stable_identity_after_device_change(self):
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            patch.object(box, "warn_device_access"),
            patch.object(box, "podman") as podman,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            podman.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(
                [],
                0,
                json.dumps([{"Labels": {box.LABEL: self.data["id"]}}])
                if args[:2] == ("volume", "inspect")
                else "",
                "",
            )
            box.create_container(self.path, self.data)
        self.assertTrue(any(call.args[0] == "create" for call in podman.call_args_list))

    def test_existing_container_start_and_maintenance_accept_same_stable_object(self):
        info = {
            "State": {"Running": True},
            "Mounts": [{"Source": str(self.project), "Destination": str(self.project)}],
        }
        self.data["ready"] = True
        box.atomic_json(self.path, self.data)
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            patch.object(box, "inspect_owned", return_value=info),
            patch.object(box, "check_resource_controllers"),
            patch.object(
                box, "podman", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as podman,
            patch("terminal_ui.run_tui", return_value=0) as shell,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.start_container(self.path, self.data)
            self.assertEqual(box.maintenance_terminal(self.path, self.data), 0)
        self.assertFalse(
            any(call.args[0] == "create" for call in podman.call_args_list)
        )
        shell.assert_called_once()

    def test_copied_marker_at_original_path_does_not_override_strong_identity(self):
        self.project.rename(self.root / "original")
        self.project.mkdir()
        (self.project / box.MARKER).write_text(self.data["id"])
        # Model a driver that reuses the inode number but changes generation.
        with patch.object(
            identity,
            "snapshot",
            return_value=(self.before, stable(handle="replacement-generation")),
        ):
            with self.assertRaisesRegex(box.BoxError, "identity changed"):
                box.find_workspace(self.project)

    def test_legacy_registration_is_not_upgraded_on_read_only_listing(self):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        box.atomic_json(self.path, self.data)
        before = self.path.read_bytes()
        with patch.object(identity, "snapshot", return_value=(self.before, stable())):
            self.assertEqual(
                status_report.project_condition(self.data), ("present", self.data["id"])
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(box.main(["list"]), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_purge_accepts_same_stable_directory_but_not_changed_handle(self):
        args = argparse.Namespace(yes=True, delete_sessions=False, all=False)
        with (
            patch.object(identity, "snapshot", return_value=(self.after, stable())),
            patch.object(box, "inspect_owned", return_value=None),
            patch.object(box, "stop_container"),
            patch.object(box, "remove_resources"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.purge(self.path, self.data, args)
        self.assertTrue(self.project.exists())
        self.assertFalse((self.project / box.MARKER).exists())

    def test_all_refuses_same_inode_but_changed_filesystem_before_deletion(self):
        args = argparse.Namespace(yes=True, delete_sessions=False, all=True)
        with (
            patch.object(
                identity,
                "snapshot",
                return_value=(self.before, stable(fsid="unrelated")),
            ),
            patch.object(box, "remove_resources") as remove,
        ):
            with self.assertRaisesRegex(box.BoxError, "refusing --all"):
                box.purge(self.path, self.data, args)
        remove.assert_not_called()
        self.assertTrue((self.project / box.MARKER).exists())

    def test_legacy_upgrade_occurs_inside_main_workspace_lock(self):
        self.data.pop(identity.STABLE_IDENTITY_KEY)
        box.atomic_json(self.path, self.data)

        def verify(path, data, args):
            with self.assertRaisesRegex(box.BoxError, "active oc-box process"):
                with box.workspace_lock(path):
                    self.fail("lock must remain held")
            self.assertEqual(data[identity.STABLE_IDENTITY_KEY], stable())
            return 0

        with (
            patch.object(identity, "snapshot", return_value=(self.before, stable())),
            patch.object(box, "check_backend"),
            patch.object(box, "launch", side_effect=verify),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["--project", str(self.project)]), 0)

    def test_snapshot_uses_one_fd_and_closes_it_when_handle_is_unavailable(self):
        with (
            patch.object(identity, "file_handle", return_value=None),
            patch.object(identity.os, "close", wraps=os.close) as close,
        ):
            observed, fingerprint = identity.snapshot(self.project)
        self.assertEqual(
            observed, [self.project.stat().st_dev, self.project.stat().st_ino]
        )
        self.assertIsNone(fingerprint)
        close.assert_called_once()

    def test_native_project_identity_matches_repeated_queries_and_device_change(self):
        # Exercise the actual filesystem when the checkout supports handles.
        before, fingerprint = identity.snapshot(APP)
        if fingerprint is None:
            self.skipTest("Checkout filesystem does not expose persistent handles")
        self.assertEqual(fingerprint["method"], "linux-file-handle-v1")
        after, repeated = identity.snapshot(APP)
        self.assertEqual(before, after)
        self.assertEqual(fingerprint, repeated)
        record = {
            "project_identity": [before[0] + 1, before[1]],
            identity.STABLE_IDENTITY_KEY: fingerprint,
        }
        self.assertTrue(identity.same_project(record, APP))

    def test_filesystem_id_failure_cannot_create_a_partial_identity(self):
        for value in (0, OSError("unavailable")):
            with (
                self.subTest(value=value),
                patch.object(
                    identity, "file_handle", return_value={"type": 77, "bytes": "abcd"}
                ),
            ):
                with patch.object(identity.os, "fstatvfs") as statvfs:
                    if isinstance(value, OSError):
                        statvfs.side_effect = value
                    else:
                        statvfs.return_value.f_fsid = value
                    observed, fingerprint = identity.snapshot(self.project)
                self.assertEqual(
                    observed, [self.project.stat().st_dev, self.project.stat().st_ino]
                )
                self.assertIsNone(fingerprint)

    def test_snapshot_refuses_symlink_root(self):
        link = self.root / "symlink"
        link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(OSError):
            identity.snapshot(link)

    def test_native_handle_ignores_transient_mount_id(self):
        class Lookup:
            def __call__(self, fd, path, buffer, mount, flags):
                self.args = (fd, path, flags)
                struct.pack_into("=Ii4s", buffer, 0, 4, 77, b"test")
                ctypes.cast(mount, ctypes.POINTER(ctypes.c_int))[0] = 999
                return 0

        lookup = Lookup()
        library = type("Libc", (), {"name_to_handle_at": lookup})()
        with patch.object(identity.ctypes, "CDLL", return_value=library):
            self.assertEqual(
                identity.file_handle(12), {"type": 77, "bytes": b"test".hex()}
            )
        self.assertEqual(lookup.args, (12, b"", 0x1000))

    def test_unsupported_or_oversized_native_handle_is_rejected(self):
        class Lookup:
            def __init__(self, result, length):
                self.result, self.length = result, length

            def __call__(self, fd, path, buffer, mount, flags):
                struct.pack_into("=I", buffer, 0, self.length)
                return self.result

        for result, length in ((-1, 128), (0, 0), (0, 4096)):
            library = type("Libc", (), {"name_to_handle_at": Lookup(result, length)})()
            with (
                self.subTest(result=result, length=length),
                patch.object(identity.ctypes, "CDLL", return_value=library),
            ):
                self.assertIsNone(identity.file_handle(12))

    def test_native_handle_uses_driver_requested_size_with_bounded_retry(self):
        class Lookup:
            calls = 0

            def __call__(self, fd, path, buffer, mount, flags):
                self.calls += 1
                if self.calls == 1:
                    struct.pack_into("=I", buffer, 0, 256)
                    ctypes.set_errno(errno.EOVERFLOW)
                    return -1
                self.capacity = struct.unpack_from("=I", buffer)[0]
                struct.pack_into("=Ii", buffer, 0, 256, 9999)
                ctypes.memmove(ctypes.addressof(buffer) + 8, b"x" * 256, 256)
                return 0

        lookup = Lookup()
        with patch.object(
            identity.ctypes,
            "CDLL",
            return_value=type("Libc", (), {"name_to_handle_at": lookup})(),
        ):
            value = identity.file_handle(12)
        self.assertEqual(value, {"type": 9999, "bytes": (b"x" * 256).hex()})
        self.assertEqual(lookup.capacity, 256)
        self.assertEqual(lookup.calls, 2)

    def test_native_handle_missing_libc_support_is_a_safe_fallback(self):
        with patch.object(identity.ctypes, "CDLL", return_value=object()):
            self.assertIsNone(identity.file_handle(12))
        with patch.object(identity.sys, "platform", "unknown"):
            self.assertIsNone(identity.file_handle(12))


if __name__ == "__main__":
    unittest.main()
