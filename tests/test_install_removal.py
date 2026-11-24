"""Verify exact flat-install removal using only fresh synthetic homes."""

import contextlib
from dataclasses import replace
import fcntl
import io
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from oc_box import host_file_open_policy as layout
from oc_box import install_ownership as ownership, install_removal as removal
from oc_box import installation, updater

import test_install_ownership as fixtures


class FlatRemovalTests(unittest.TestCase):
    install = fixtures.FlatOwnershipTests.install
    review = fixtures.FlatOwnershipTests.review

    def setUp(self):
        fixtures.FlatOwnershipTests.setUp(self)
        env = patch.dict(
            os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, clear=True
        )
        env.start()
        self.addCleanup(env.stop)
        regular_host = patch.object(removal, "_guard_host")
        regular_host.start()
        self.addCleanup(regular_host.stop)

    def remove(self, plan):
        return removal.remove_flat_installation(
            plan, runtime=plan.release, home=self.home
        )

    def stages(self):
        return list(self.target.glob(".oc-box-uninstall-*"))

    def opt_into_updates(self):
        config = self.home / ".config/oc-box/config.json"
        config.parent.mkdir(parents=True)
        config.write_bytes(b'{"auto_update":true}\n')

    def test_installed_automatic_update_is_opt_in_and_does_not_wait_for_busy_installer(
        self,
    ):
        release = self.install()
        with (
            patch.object(updater, "_regular_user", return_value=True),
            patch.object(updater, "_github_upgrade") as fetch,
        ):
            self.assertIsNone(updater.automatic_upgrade(release))
            self.opt_into_updates()
            with patch.dict(os.environ, {"OC_BOX_UPDATE_CHECKED": "1"}):
                self.assertIsNone(updater.automatic_upgrade(release))
            with (self.target / layout.INSTALL_LOCK).open("r+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(updater.automatic_upgrade(release))
            fetch.assert_not_called()
        self.assertEqual(self.review(release).release, release)

    def test_retained_launcher_cannot_update_before_or_after_active_removal(self):
        older = self.install()
        active = self.install()
        self.opt_into_updates()
        with (
            patch.object(updater, "_regular_user", return_value=True),
            patch.object(updater, "_github_upgrade") as fetch,
            patch.object(updater.dependencies, "require"),
        ):
            with self.assertRaisesRegex(updater.UpdateError, "active installation"):
                updater.automatic_upgrade(older)
            self.remove(self.review(active))
            with self.assertRaisesRegex(updater.UpdateError, "active installation"):
                updater.automatic_upgrade(older)
            with self.assertRaisesRegex(updater.UpdateError, "not the active launcher"):
                updater.upgrade(older)
            fetch.assert_not_called()
        self.assertFalse(self.launcher.exists())
        self.assertFalse(os.path.lexists(self.target / "current"))
        self.assertEqual(list((self.target / "releases").iterdir()), [older])

    def test_installer_rechecks_source_when_current_changes_during_automatic_fetch(
        self,
    ):
        older = self.install()
        self.opt_into_updates()
        token = ownership.inspect_active_installation(self.home, older).token

        def fake_fetch(root, *, expected_active):
            self.assertEqual((root, expected_active), (older, token))
            current = self.install()  # a direct installer won during the fetch
            with self.assertRaisesRegex(ownership.OwnershipError, "changed before"):
                self.install(expected_active=expected_active)
            self.assertEqual((self.target / "current").resolve(), current)
            self.assertEqual(self.review(current).release, current)
            self.assertEqual(
                set((self.target / "releases").iterdir()), {older, current}
            )

        with (
            patch.object(updater, "_regular_user", return_value=True),
            patch.object(updater, "_github_upgrade", side_effect=fake_fetch) as fetch,
        ):
            self.assertIsNone(updater.automatic_upgrade(older))
        fetch.assert_called_once()

    def test_real_exec_handoff_keeps_updated_release_leased_and_original_arguments(
        self,
    ):
        older = self.install()
        self.opt_into_updates()
        package_parent = str(Path(ownership.__file__).resolve().parents[1])
        (self.source / "oc_box.py").write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {package_parent!r})\n"
            "from oc_box import install_ownership as owner\n"
            "with owner.installed_runtime_use(Path(__file__).parent):\n"
            " assert sys.argv[1:] == ['--new', '--project', 'synthetic project']\n"
            " assert os.environ.get('OC_BOX_UPDATE_CHECKED') == '1'\n"
            " assert owner.HANDOFF_FD not in os.environ\n"
            " assert not os.get_inheritable(owner._runtime_lease.fd)\n"
            " print('ready', flush=True); sys.stdin.buffer.read()\n"
        )
        script = (
            "import contextlib, io, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from oc_box import install_ownership as owner, installation, updater\n"
            "source, older = map(Path, sys.argv[2:])\n"
            "updater._regular_user = lambda: True\n"
            "installation.shutil.which = lambda *a, **k: None\n"
            "def local_upgrade(root, *, expected_active):\n"
            " with contextlib.redirect_stdout(io.StringIO()):\n"
            "  return installation.install(source, expected_active=expected_active)\n"
            "updater._github_upgrade = local_upgrade\n"
            "with owner.installed_runtime_use(older):\n"
            " updater.automatic_upgrade(older, launch_args=['--new', '--project', 'synthetic project'])\n"
            "raise SystemExit('the verified exec did not happen')\n"
        )
        with subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                package_parent,
                str(self.source),
                str(older),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                current = (self.target / "current").resolve(strict=True)
                self.assertNotEqual(current, older)
                plan = self.review(current)
                with self.assertRaisesRegex(removal.InstallRemovalError, "in use"):
                    self.remove(plan)
            finally:
                _, error = child.communicate(input=b"", timeout=5)
                self.assertEqual((child.returncode, error), (0, b""))
        self.assertEqual(self.review(current), plan)

    def test_exact_removal_preserves_older_release_and_never_opens_data(self):
        old = self.install()
        active = self.install()
        kept = {
            self.target / "update-features.json",
            self.target / "credentials/vault.json",
            self.target / "legacy-docs/user-note",
            self.target / "web-tls/user-certificate",
            self.target / "unknown",
            self.home / ".config/oc-box/config.json",
            self.home / ".local/state/oc-box/workspaces/state",
            self.home / ".local/state/oc-box/log/activity.jsonl",
            self.home / ".local/share/opencode/opencode.db",
            self.home / ".local/share/info/dir",
            self.home / "project/.oc-box-id",
            self.home / "external.ocbox",
        }
        for path in kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic preserve")
        unowned_receipt = self.target / layout.RECEIPT
        unowned_receipt.symlink_to(self.home / ".local/share/opencode/opencode.db")
        plan = self.review(active)
        real_open, real_scan = os.open, os.scandir
        protected = {
            old,
            *(
                path.parent
                for path in kept
                if path.parent
                not in {
                    self.home,
                    self.target,
                    self.home / ".local/share/info",
                }
            ),
        }

        def guarded(path, parent=None):
            path = Path(path)
            if parent is not None and not path.is_absolute():
                path = Path(os.readlink(f"/proc/self/fd/{parent}")) / path
            if path in kept or any(
                path.is_relative_to(private) for private in protected
            ):
                raise AssertionError("opened excluded data")

        def open_at(path, flags, *args, dir_fd=None):
            guarded(path, dir_fd)
            return real_open(path, flags, *args, dir_fd=dir_fd)

        def scan(path):
            guarded(
                os.readlink(f"/proc/self/fd/{path}") if isinstance(path, int) else path
            )
            return real_scan(path)

        with (
            patch.object(os, "open", side_effect=open_at),
            patch.object(os, "scandir", side_effect=scan),
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("unexpected child")
            ),
        ):
            self.assertEqual(self.remove(plan), len(plan.artifacts))
        self.assertTrue(all(not os.path.lexists(item.path) for item in plan.artifacts))
        self.assertTrue((old / "oc_box.py").is_file())
        self.assertTrue(unowned_receipt.is_symlink())
        self.assertEqual(self.stages(), [])
        self.assertTrue((self.target / layout.INSTALL_LOCK).is_file())
        self.assertTrue((self.target / ownership.UPGRADE_LOCK).is_file())
        for path in kept:
            self.assertEqual(path.read_bytes(), b"synthetic preserve")

    def test_enrollment_cannot_revive_but_its_marker_and_lock_inode_are_kept(self):
        release = self.install(verified_release=True)
        registry = self.home / ".local/state/oc-box"
        self.assertTrue(layout.enabled(release, home=self.home, registry=registry))
        receipt = self.target / layout.RECEIPT
        old_receipt = receipt.read_bytes()
        lock = self.target / layout.INSTALL_LOCK
        before = lock.stat()
        with ownership.installed_runtime_use(release):
            self.remove(self.review(release))
        self.assertEqual(receipt.read_bytes(), old_receipt)
        self.assertEqual(lock.stat().st_ino, before.st_ino)
        self.assertEqual(lock.stat().st_mode, before.st_mode)
        self.assertNotEqual(lock.stat().st_ctime_ns, before.st_ctime_ns)
        next_release = self.install(verified_release=True)
        self.assertFalse(
            layout.enabled(next_release, home=self.home, registry=registry)
        )

    def test_replaced_or_forged_review_refuses_without_moving_anything(self):
        release = self.install()
        plan = self.review(release)
        with self.assertRaises(removal.InstallRemovalError) as forged:
            self.remove(replace(plan, artifacts=plan.artifacts[1:]))
        self.assertFalse(forged.exception.changed)
        auxiliary = self.home / layout._AUXILIARY[0]
        replacement = auxiliary.with_name("user-replacement")
        replacement.write_bytes(auxiliary.read_bytes())
        os.replace(replacement, auxiliary)
        with self.assertRaises(removal.InstallRemovalError) as changed:
            self.remove(plan)
        self.assertFalse(changed.exception.changed)
        self.assertTrue(self.launcher.is_file())
        self.assertTrue(release.is_dir())
        self.assertEqual(self.stages(), [])

    def test_busy_installer_and_updater_fail_closed_and_keep_lock_inodes(self):
        release = self.install()
        plan = self.review(release)
        for name in (layout.INSTALL_LOCK, ownership.UPGRADE_LOCK):
            with self.subTest(name=name), (self.target / name).open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(
                    removal.InstallRemovalError, "in use"
                ) as busy:
                    self.remove(plan)
                self.assertFalse(busy.exception.changed)
                self.assertEqual(
                    os.fstat(lock.fileno()).st_ino, (self.target / name).stat().st_ino
                )
                self.assertEqual(self.review(release), plan)
                self.assertEqual(self.stages(), [])

    def test_live_other_pid_refuses_and_does_not_drop_the_callers_shared_lease(self):
        release = self.install()
        plan = self.review(release)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from pathlib import Path; from oc_box.install_ownership import installed_runtime_use\n"
            "with installed_runtime_use(Path(sys.argv[2])):\n"
            " print('ready', flush=True); sys.stdin.buffer.read()\n"
        )
        pkg_parent = Path(ownership.__file__).resolve().parents[1]
        with (
            ownership.installed_runtime_use(release),
            subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(pkg_parent), str(release)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as child,
        ):
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                with self.assertRaisesRegex(removal.InstallRemovalError, "in use"):
                    self.remove(plan)
            finally:
                _, error = child.communicate(input=b"", timeout=5)
                self.assertEqual((child.returncode, error), (0, b""))
            # The child's exit leaves our original shared lease, not an
            # unlocked window in which another process can remove the release.
            with (release / ownership.USE_LOCK).open("rb") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.remove(plan), len(plan.artifacts))

    def test_late_swap_is_atomically_recovered_without_unlinking_the_replacement(self):
        release = self.install()
        plan = self.review(release)
        actual_move = removal._rename_method()
        saved = self.launcher.with_name("original-reviewed-wrapper")
        swapped = False

        def move(parent, name, dst, slot):
            nonlocal swapped
            if not swapped:
                swapped = True
                self.assertEqual(name, self.launcher.name)
                self.launcher.rename(saved)
                self.launcher.write_bytes(b"user replacement")
            actual_move(parent, name, dst, slot)

        with patch.object(removal, "_rename_method", return_value=move):
            with self.assertRaises(removal.InstallRemovalError) as race:
                self.remove(plan)
        self.assertTrue(race.exception.changed)
        self.assertIsNone(race.exception.retained)
        self.assertEqual(self.launcher.read_bytes(), b"user replacement")
        self.assertEqual(saved.read_bytes(), layout.launcher_bytes(release))
        self.assertTrue(release.is_dir())
        self.assertEqual(self.stages(), [])

    def test_occupied_rollback_keeps_staging_and_does_not_overwrite_new_launcher(self):
        release = self.install()
        plan = self.review(release)
        actual_move = removal._rename_method()
        swapped = False

        def move(parent, name, dst, slot):
            nonlocal swapped
            actual_move(parent, name, dst, slot)
            if not swapped:
                swapped = True
                self.launcher.write_bytes(b"concurrent replacement")

        with patch.object(removal, "_rename_method", return_value=move):
            with self.assertRaises(removal.InstallRemovalError) as race:
                self.remove(plan)
        self.assertTrue(race.exception.changed)
        self.assertEqual(self.stages(), [race.exception.retained])
        self.assertEqual(
            (race.exception.retained / "0-oc-box").read_bytes(),
            layout.launcher_bytes(release),
        )
        self.assertEqual(self.launcher.read_bytes(), b"concurrent replacement")
        self.assertTrue(release.is_dir())

    def test_second_move_failure_restores_launcher_but_not_its_inventory_authority(
        self,
    ):
        release = self.install()
        plan = self.review(release)
        original_receipt = (release / ownership.INVENTORY).read_bytes()
        actual_move = removal._rename_method()
        calls = 0

        def move(parent, name, dst, slot):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic second move failure")
            actual_move(parent, name, dst, slot)

        with patch.object(removal, "_rename_method", return_value=move):
            with self.assertRaises(removal.InstallRemovalError) as stopped:
                self.remove(plan)
        self.assertEqual(calls, 3)
        self.assertTrue(stopped.exception.launcher_restored)
        self.assertTrue(stopped.exception.inventory_invalidated)
        self.assertIsNone(stopped.exception.retained)
        self.assertIn("launcher was restored", str(stopped.exception))
        self.assertIn("inventory no longer verifies", str(stopped.exception))
        self.assertIn("Reinstall with the verified installer", str(stopped.exception))
        self.assertEqual(self.launcher.read_bytes(), layout.launcher_bytes(release))
        self.assertEqual((release / ownership.INVENTORY).read_bytes(), original_receipt)
        with self.assertRaises(ownership.OwnershipError):
            self.review(release)

    def test_install_lock_sync_failure_reports_visible_rollback_and_invalid_inventory(
        self,
    ):
        release = self.install()
        plan = self.review(release)
        lock = self.target / layout.INSTALL_LOCK
        before = lock.stat()
        original_receipt = (release / ownership.INVENTORY).read_bytes()
        fsync = os.fsync

        def fail(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(lock):
                raise OSError("synthetic installation lock synchronization failure")
            return fsync(fd)

        with patch.object(os, "fsync", side_effect=fail):
            with self.assertRaises(removal.InstallRemovalError) as stopped:
                self.remove(plan)
        self.assertTrue(stopped.exception.launcher_restored)
        self.assertTrue(stopped.exception.inventory_invalidated)
        self.assertIsNone(stopped.exception.retained)
        self.assertIn(
            "Synchronization of the installation lock was not confirmed",
            str(stopped.exception),
        )
        self.assertEqual(self.launcher.read_bytes(), layout.launcher_bytes(release))
        self.assertEqual(lock.stat().st_mode, before.st_mode)
        self.assertEqual(lock.stat().st_ino, before.st_ino)
        self.assertNotEqual(lock.stat().st_ctime_ns, before.st_ctime_ns)
        self.assertEqual((release / ownership.INVENTORY).read_bytes(), original_receipt)
        with self.assertRaises(ownership.OwnershipError):
            self.review(release)

    def test_partial_unlink_reports_and_keeps_staging_instead_of_retrying(self):
        release = self.install()
        plan = self.review(release)
        real_unlink = os.unlink
        count = 0

        def unlink(name, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic private failure")
            return real_unlink(name, *args, **kwargs)

        with patch.object(os, "unlink", side_effect=unlink):
            with self.assertRaises(removal.InstallRemovalError) as partial:
                self.remove(plan)
        self.assertEqual(count, 2)
        self.assertTrue(partial.exception.changed)
        self.assertEqual(self.stages(), [partial.exception.retained])
        self.assertNotIn("synthetic private failure", str(partial.exception))
        self.assertFalse(self.launcher.exists())
        self.assertFalse(release.exists())
        with self.assertRaises(removal.InstallRemovalError):
            self.remove(plan)

    def test_late_leaf_swap_inside_detached_release_preserves_both_files(self):
        release = self.install()
        plan = self.review(release)
        actual_move = removal._rename_method()
        captured_slot = None

        def move(parent, name, dst, slot):
            nonlocal captured_slot
            if name == "bootstrap.py" and slot.startswith(".leaf-"):
                os.rename(
                    name, "synthetic-original", src_dir_fd=parent, dst_dir_fd=parent
                )
                fd = os.open(
                    name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent
                )
                try:
                    os.write(fd, b"unowned late replacement")
                finally:
                    os.close(fd)
                captured_slot = slot
            actual_move(parent, name, dst, slot)

        with patch.object(removal, "_rename_method", return_value=move):
            with self.assertRaises(removal.InstallRemovalError) as race:
                self.remove(plan)
        self.assertTrue(race.exception.changed)
        self.assertIsNotNone(captured_slot)
        staged = race.exception.retained
        self.assertEqual(
            (staged / captured_slot).read_bytes(), b"unowned late replacement"
        )
        released = next(
            path for path in staged.iterdir() if path.name.endswith(release.name)
        )
        self.assertEqual(
            (released / "synthetic-original").read_bytes(),
            b"synthetic launcher artifact\n",
        )

    def test_unconfirmed_metadata_sync_does_not_report_success(self):
        release = self.install()
        plan = self.review(release)
        fsync = os.fsync

        def fail_final_directory(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(self.target):
                raise OSError("synthetic durability failure")
            return fsync(fd)

        with patch.object(os, "fsync", side_effect=fail_final_directory):
            with self.assertRaises(removal.InstallRemovalError) as incomplete:
                self.remove(plan)
        self.assertTrue(incomplete.exception.changed)
        self.assertIsNone(incomplete.exception.retained)
        self.assertFalse(self.launcher.exists())
        self.assertFalse(release.exists())

    def test_prepared_action_expires_without_side_effects_and_rechecks_at_remove(self):
        release = self.install()
        plan = self.review(release)
        with removal.locked_flat_removal(
            plan, runtime=release, home=self.home
        ) as expired:
            self.assertEqual(self.review(release), plan)
        with self.assertRaisesRegex(removal.InstallRemovalError, "no longer active"):
            expired.remove()
        with removal.locked_flat_removal(
            plan, runtime=release, home=self.home
        ) as action:
            self.launcher.write_bytes(b"changed after lock and before remove")
            with self.assertRaises(removal.InstallRemovalError) as changed:
                action.remove()
            self.assertFalse(changed.exception.changed)
        self.assertEqual(self.stages(), [])

    def test_real_installed_script_enforces_lease_without_locking_help(self):
        source = Path(installation.__file__).resolve().parents[2]
        with contextlib.redirect_stdout(io.StringIO()):
            release = installation.install(source, self.home)
        plan = self.review(release)

        def direct(*options):
            # Model the legacy auto-updater's direct re-exec without -B.
            return subprocess.run(
                [sys.executable, str(release / "oc_box.py"), *options],
                capture_output=True,
                timeout=10,
            )

        self.assertEqual(direct("--version").returncode, 0)
        self.assertFalse((release / "__pycache__").exists())
        self.assertEqual(self.review(release), plan)
        with (release / ownership.USE_LOCK).open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(direct("--help").returncode, 0)
            refused = direct("--version")
            self.assertEqual(refused.returncode, 1)
            self.assertIn(b"being removed", refused.stderr)
            orphanable = subprocess.run(
                [sys.executable, "-I", "-B", str(release / "tui_operations.py")],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(orphanable.returncode, 1)
            self.assertIn(b"being removed", orphanable.stderr)

    def test_package_review_cannot_perform_removal(self):
        review = ownership.PackageInstallReview(
            "uv", self.home / "synthetic-tool", "1.9.9.dev0"
        )
        with patch.object(
            subprocess, "Popen", side_effect=AssertionError("unexpected child")
        ):
            with self.assertRaisesRegex(
                removal.InstallRemovalError, "No matching verified flat"
            ):
                removal.remove_flat_installation(
                    review, runtime=self.source, home=self.home
                )


class HostGuardTests(unittest.TestCase):
    def test_host_gate_refuses_root_or_container_before_looking_up_installation(self):
        for uid, env, markers in (
            (0, {}, False),
            (1000, {"OC_BOX_CONTAINER": "1"}, False),
            (1000, {}, True),
        ):
            with (
                self.subTest(uid=uid, env=env, markers=markers),
                patch.object(os, "getuid", return_value=uid),
                patch.object(os, "geteuid", return_value=uid),
                patch.dict(os.environ, env, clear=True),
                patch.object(removal, "_exists", return_value=markers),
                patch.object(
                    ownership,
                    "_home",
                    side_effect=AssertionError("unexpected home access"),
                ),
                self.assertRaisesRegex(removal.InstallRemovalError, "Linux host"),
            ):
                removal.remove_flat_installation(None, runtime=Path("/not-an-install"))
