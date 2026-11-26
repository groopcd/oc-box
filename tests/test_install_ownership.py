"""Installation-only ownership evidence, using isolated synthetic directories."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import host_file_open_policy as layout, install_ownership as ownership
from oc_box import installation


def synthetic_vfs(fd, *, fsid="abc123"):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino], {
        "method": "linux-file-handle-v1",
        "fsid": fsid,
        "handle": {"type": 1, "bytes": f"{info.st_ino:016x}"},
    }


def write_inventory(receipt, inventory):
    # Keep the original receipt inode while modeling old persisted evidence.
    receipt.write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n"
    )


def simulate_saved_device_before_reboot(release):
    receipt = release / ownership.INVENTORY
    inventory = json.loads(receipt.read_bytes())
    old_device = inventory["anchors"][0][0] + 1
    for vector in (
        *inventory["anchors"],
        *(inventory[key] for key in ("lock", "upgrade", "receipt")),
    ):
        vector[0] = old_device
    for parent, stamp, _ in inventory["artifacts"].values():
        parent[0] = stamp[0] = old_device
    write_inventory(receipt, inventory)
    return receipt


class FlatOwnershipTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.home = root / "synthetic user's home"
        self.home.mkdir()
        self.source = root / "source"
        self.target = self.home / layout.INSTALL_PATH
        self.launcher = self.home / layout.LAUNCHER
        for name in (
            *installation.PROGRAM_FILES,
            *(f"docs/{name}" for name in installation.MANUAL_FILES),
            installation.COMPLETION_FILE,
        ):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("synthetic launcher artifact\n")
        stack.enter_context(
            patch.object(installation.shutil, "which", return_value=None)
        )
        stack.enter_context(
            patch.object(
                installation,
                "get_version_info",
                return_value={
                    "version": "1.9.200.dev0+gaaaaaaa",
                    "commit": "a" * 40,
                    "dirty": False,
                },
            )
        )

    def install(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return installation.install(self.source, self.home, **kwargs)

    def review(self, release):
        return ownership.review_installation(release, home=self.home)

    def test_inventory_records_persistent_handles_for_pinned_activation_directories(
        self,
    ):
        observed = []

        def capture(fd):
            proof = synthetic_vfs(fd)
            observed.append(
                (Path(os.readlink(f"/proc/self/fd/{fd}")), proof[1]["handle"])
            )
            return proof

        with patch.object(
            ownership.workspace_identity, "snapshot_fd", side_effect=capture
        ):
            release = self.install()
            receipt = release / ownership.INVENTORY
            inventory = json.loads(receipt.read_bytes())
            self.assertEqual(inventory["version"], 2)
            self.assertEqual(
                [path for path, _ in observed],
                [self.home, self.target, release.parent, release, self.launcher.parent],
            )
            self.assertEqual(
                inventory["vfs_identity"],
                {
                    "method": "linux-file-handle-v1",
                    "fsid": "abc123",
                    "handles": [handle for _, handle in observed],
                },
            )
            self.assertEqual(self.review(release).release, release)
            self.assertEqual(
                ownership.inspect_active_installation(self.home, release).build[
                    "commit"
                ],
                "a" * 40,
            )

    def test_no_joint_proof_records_legacy_v2_and_never_adopts_a_mismatched_device(
        self,
    ):
        for failure in ("unsupported", "different-filesystem"):
            with self.subTest(failure=failure):

                def partial(fd):
                    identity, proof = synthetic_vfs(fd)
                    if Path(os.readlink(f"/proc/self/fd/{fd}")) == self.launcher.parent:
                        if failure == "unsupported":
                            return identity, None
                        proof["fsid"] = "def456"
                    return identity, proof

                with patch.object(
                    ownership.workspace_identity, "snapshot_fd", side_effect=partial
                ):
                    release = self.install()
                receipt = release / ownership.INVENTORY
                original = receipt.read_bytes()
                self.assertNotIn("vfs_identity", json.loads(original))
                with patch.object(
                    ownership.workspace_identity,
                    "snapshot_fd",
                    side_effect=synthetic_vfs,
                ) as vfs:
                    self.assertEqual(self.review(release).release, release)
                    ownership.inspect_active_installation(self.home, release)
                    self.assertEqual(receipt.read_bytes(), original)
                    simulate_saved_device_before_reboot(release)
                    stale = receipt.read_bytes()
                    with self.assertRaises(ownership.OwnershipError):
                        ownership.inspect_active_installation(self.home, release)
                    vfs.assert_not_called()
                    self.assertEqual(receipt.read_bytes(), stale)

    def test_recorded_vfs_must_match_even_at_the_original_device(self):
        with patch.object(
            ownership.workspace_identity, "snapshot_fd", side_effect=synthetic_vfs
        ):
            release = self.install()
        receipt = release / ownership.INVENTORY
        original = receipt.read_bytes()
        for failure in ("unsupported", "filesystem", "handle", "split", "inode"):
            with self.subTest(failure=failure):

                def changed(fd):
                    identity, proof = synthetic_vfs(fd)
                    if failure == "unsupported":
                        return identity, None
                    if failure == "filesystem":
                        proof["fsid"] = "def456"
                    if Path(os.readlink(f"/proc/self/fd/{fd}")) == self.launcher.parent:
                        if failure == "handle":
                            proof["handle"]["bytes"] = "ab"
                        elif failure == "split":
                            proof["fsid"] = "def456"
                        elif failure == "inode":
                            identity[1] += 1
                    return identity, proof

                with patch.object(
                    ownership.workspace_identity, "snapshot_fd", side_effect=changed
                ):
                    with self.assertRaises(ownership.OwnershipError):
                        ownership.inspect_active_installation(self.home, release)
                    with self.assertRaises(ownership.OwnershipError):
                        self.review(release)
        self.assertEqual(receipt.read_bytes(), original)

    def test_activation_rebases_only_coherent_device_numbers_and_uses_live_lock_stamps(
        self,
    ):
        with patch.object(
            ownership.workspace_identity, "snapshot_fd", side_effect=synthetic_vfs
        ):
            release = self.install()
            receipt = simulate_saved_device_before_reboot(release)
            stale = receipt.read_bytes()
            original_mount = ownership._mount
            with patch.object(
                ownership, "_mount", side_effect=lambda fd: original_mount(fd) + 1
            ):
                active = ownership.inspect_active_installation(self.home, release)
            for value, path in (
                (active.install_lock, self.target / layout.INSTALL_LOCK),
                (active.upgrade_lock, self.target / ownership.UPGRADE_LOCK),
                (active.use_lock, release / ownership.USE_LOCK),
            ):
                self.assertEqual(value, ownership._stamp(path.lstat()))
            with self.assertRaises(ownership.OwnershipError):
                self.review(release)
            self.assertEqual(receipt.read_bytes(), stale)
            for field, column in (
                (("anchors", 3), 0),
                (("anchors", 3), 1),
                (("lock",), 0),
                (("upgrade",), 0),
                (("receipt",), 0),
                (("artifacts", str(layout.LAUNCHER), 0), 0),
                (("artifacts", str(layout.LAUNCHER), 1), 0),
                (
                    (
                        "artifacts",
                        str((release / "build-info.json").relative_to(self.home)),
                        1,
                    ),
                    0,
                ),
            ):
                with self.subTest(field=field, column=column):
                    altered = json.loads(stale)
                    vector = altered
                    for part in field:
                        vector = vector[part]
                    vector[column] -= 1
                    write_inventory(receipt, altered)
                    with self.assertRaises(ownership.OwnershipError):
                        ownership.inspect_active_installation(self.home, release)

    def test_renumbered_activation_refuses_replaced_files_and_same_device_mount(self):
        with patch.object(
            ownership.workspace_identity, "snapshot_fd", side_effect=synthetic_vfs
        ):
            for kind in ("wrapper", "manifest", "lock", "mount"):
                with self.subTest(kind=kind):
                    release = self.install()
                    simulate_saved_device_before_reboot(release)
                    if kind == "mount":
                        original_mount = ownership._mount

                        def mount(fd):
                            return original_mount(fd) + (
                                Path(os.readlink(f"/proc/self/fd/{fd}"))
                                == self.launcher.parent
                            )

                        with patch.object(ownership, "_mount", side_effect=mount):
                            with self.assertRaises(ownership.OwnershipError):
                                ownership.inspect_active_installation(
                                    self.home, release
                                )
                    else:
                        path = {
                            "wrapper": self.launcher,
                            "manifest": release / "build-info.json",
                            "lock": self.target / layout.INSTALL_LOCK,
                        }[kind]
                        replacement = path.with_name("synthetic-replacement")
                        replacement.write_bytes(path.read_bytes())
                        replacement.chmod(path.stat().st_mode & 0o777)
                        os.replace(replacement, path)
                        with self.assertRaises(ownership.OwnershipError):
                            ownership.inspect_active_installation(self.home, release)

    def test_inventory_leaves_older_releases_and_all_state_outside_the_plan(self):
        old = self.install()
        excluded = (
            old / ownership.INVENTORY,
            self.target / "legacy-docs/saved",
            self.target / "update-features.json",
            self.target / "credentials/vault.json",
            self.target / "web-tls/localhost/server-key.pem",
            self.target / "my-records/custom",
            self.target / layout.INSTALL_LOCK,
            self.home / ".config/oc-box/config.json",
            self.home / ".local/state/oc-box/workspaces/state",
            self.home / ".local/state/oc-box/log/activity.jsonl",
            self.home / ".local/share/opencode/opencode.db",
            self.home / ".local/share/info/dir",
            self.home / "project/.oc-box-id",
        )
        for path in excluded[1:]:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic protected data")
        active = self.install()
        before = {path: path.lstat() for path in self.home.rglob("*")}
        real_open = os.open
        real_scan = os.scandir
        protected = {
            *[
                path.parent
                for path in excluded
                if path.name not in (layout.INSTALL_LOCK, "dir", "update-features.json")
            ]
        }

        def guard(value, fd=None):
            path = Path(value)
            if fd is not None and not path.is_absolute():
                path = Path(os.readlink(f"/proc/self/fd/{fd}")) / path
            if (
                any(path.is_relative_to(private) for private in protected)
                or path in excluded[1:-1]
            ):
                raise AssertionError("accessed retained state")

        def checked_open(path, flags, *args, dir_fd=None):
            # The existing install lock is checked with O_PATH, not opened for writing.
            if path != layout.INSTALL_LOCK:
                guard(path, dir_fd)
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR))
            return real_open(path, flags, *args, dir_fd=dir_fd)

        def checked_scan(path):
            guard(
                os.readlink(f"/proc/self/fd/{path}") if isinstance(path, int) else path
            )
            return real_scan(path)

        with (
            patch.object(ownership.os, "open", side_effect=checked_open),
            patch.object(ownership.os, "scandir", side_effect=checked_scan),
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("unexpected child")
            ),
            patch.dict(
                os.environ,
                {
                    "OC_BOX_STATE_HOME": str(self.home / ".local/share/opencode"),
                    "XDG_DATA_HOME": str(self.home / "project"),
                    "XDG_CONFIG_HOME": str(self.home / "project"),
                },
            ),
        ):
            plan = self.review(active)
            self.assertEqual(self.review(active), plan)
        self.assertEqual(plan.release, active)
        paths = {entry.path for entry in plan.artifacts}
        self.assertIn(self.launcher, paths)
        self.assertIn(active / "oc_box.py", paths)
        self.assertIn(active / ownership.INVENTORY, paths)
        self.assertIn(self.target / "current", paths)
        self.assertIn(self.home / ".local/share/man/man1/oc-box.1", paths)
        self.assertTrue(paths.isdisjoint(excluded))
        self.assertFalse(any(path.is_relative_to(old) for path in paths))
        self.assertNotIn(self.target, paths)
        self.assertNotIn(self.target / "releases", paths)
        after = {path: path.lstat() for path in self.home.rglob("*")}
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual(
            {
                path: (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                for path, info in before.items()
            },
            {
                path: (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                for path, info in after.items()
            },
        )

    def test_same_bytes_replacement_of_wrapper_auxiliary_or_receipt_refuses(self):
        for item in ("wrapper", "manual", "inventory"):
            with self.subTest(item=item):
                active = self.install()
                path = {
                    "wrapper": self.launcher,
                    "manual": self.home / layout._AUXILIARY[0],
                    "inventory": active / ownership.INVENTORY,
                }[item]
                temporary = path.with_name("synthetic-replacement")
                temporary.write_bytes(path.read_bytes())
                temporary.chmod(path.stat().st_mode & 0o777)
                os.replace(temporary, path)
                with self.assertRaisesRegex(ownership.OwnershipError, "inventory"):
                    self.review(active)

    def test_receipt_swapped_after_metadata_precheck_is_not_opened_for_read(self):
        active = self.install()
        receipt = active / ownership.INVENTORY
        open_at = os.open
        calls = 0

        def late_fifo(path, flags, *args, dir_fd=None):
            nonlocal calls
            if path == ownership.INVENTORY:
                # Only the inode used by the initial metadata precheck is allowed
                # to be opened for data. A late FIFO must fail before reopening.
                self.assertTrue(flags & os.O_PATH)
                calls += 1
                if calls == 2:
                    receipt.rename(receipt.with_suffix(".original"))
                    os.mkfifo(receipt)
            return open_at(path, flags, *args, dir_fd=dir_fd)

        with patch.object(ownership.os, "open", side_effect=late_fifo):
            with self.assertRaisesRegex(ownership.OwnershipError, "changed"):
                self.review(active)
        self.assertEqual(calls, 2)

    def test_legacy_and_checkout_are_never_adopted(self):
        with self.assertRaisesRegex(ownership.OwnershipError, "Source, editable"):
            self.review(self.source)
        with patch.object(ownership, "record_inventory", return_value=False):
            legacy = self.install()
        self.assertEqual(self.launcher.read_bytes(), layout.launcher_bytes(legacy))
        with self.assertRaisesRegex(
            ownership.OwnershipError, "no verified uninstall inventory"
        ):
            self.review(legacy)
        with self.assertRaises(ownership.OwnershipError):
            self.review(self.target)

    def test_stale_runtime_refuses_even_while_its_release_still_exists(self):
        old = self.install()
        active = self.install()
        with self.assertRaisesRegex(ownership.OwnershipError, "pinned"):
            self.review(old)
        self.assertEqual(self.review(active).release, active)

    def test_pinned_wrapper_does_not_add_bytecode_to_inventory(self):
        (self.source / "oc_box.py").write_text("import dependencies\n")
        (self.source / "dependencies.py").write_text("# synthetic Python module\n")
        active = self.install()
        plan = self.review(active)
        subprocess.run(
            [self.launcher],
            env={"HOME": str(self.home), "PATH": str(Path(sys.executable).parent)},
            check=True,
            timeout=10,
        )
        self.assertFalse((active / "__pycache__").exists())
        self.assertEqual(self.review(active), plan)

    def test_redirect_hard_link_and_same_device_mount_refuse_without_reading_target(
        self,
    ):
        foreign = self.home / ".local/share/opencode/opencode.db"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"synthetic OpenCode history")
        active = self.install()
        mount = ownership._mount
        with patch.object(
            ownership,
            "_mount",
            side_effect=lambda fd: (
                mount(fd) + (os.readlink(f"/proc/self/fd/{fd}") == str(self.launcher))
            ),
        ):
            with self.assertRaisesRegex(ownership.OwnershipError, "mount boundary"):
                self.review(active)
        self.launcher.unlink()
        self.launcher.symlink_to(foreign)
        with self.assertRaises(ownership.OwnershipError):
            self.review(active)
        self.launcher.unlink()
        os.link(foreign, self.launcher)
        with self.assertRaisesRegex(ownership.OwnershipError, "unsafe"):
            self.review(active)
        self.assertEqual(foreign.read_bytes(), b"synthetic OpenCode history")

    def test_optional_private_dependencies_are_recorded_but_unknown_child_refuses(self):
        def dependencies(release):
            path = release / "tui-deps/example/thing.py"
            path.parent.mkdir(parents=True)
            path.write_text("synthetic optional package")

        with patch.object(
            installation, "optional_tui_runtime", side_effect=dependencies
        ):
            active = self.install(with_tui=True)
        self.assertIn(
            active / "tui-deps/example/thing.py",
            {entry.path for entry in self.review(active).artifacts},
        )
        unknown = active / "tui-deps/example/unknown"
        unknown.mkdir()
        (unknown / "private").write_text("do not adopt")
        with self.assertRaises(ownership.OwnershipError):
            self.review(active)

    def test_artifacts_changed_during_info_indexing_are_not_adopted(self):
        for change in ("foreign", "replace"):
            with self.subTest(change=change):

                def dependencies(release):
                    package = release / "tui-deps/example"
                    package.mkdir(parents=True)
                    (package / "provided.py").write_bytes(b"synthetic installed bytes")

                def index(*args, **kwargs):
                    active = (self.target / "current").resolve(strict=True)
                    self.assertTrue(self.launcher.is_file())
                    self.assertFalse((active / ownership.INVENTORY).exists())
                    package = active / "tui-deps/example"
                    if change == "foreign":
                        (package / "unowned").write_text("late foreign data")
                    else:
                        expected = package / "provided.py"
                        other = package / "late-replacement"
                        other.write_bytes(expected.read_bytes())
                        other.chmod(expected.stat().st_mode & 0o777)
                        os.replace(other, expected)
                    return subprocess.CompletedProcess(args, 0)

                with (
                    patch.object(
                        installation, "optional_tui_runtime", side_effect=dependencies
                    ),
                    patch.object(
                        installation.shutil,
                        "which",
                        return_value="/synthetic/install-info",
                    ),
                    patch.object(installation.subprocess, "run", side_effect=index),
                ):
                    active = self.install(with_tui=True)
                self.assertFalse((active / ownership.INVENTORY).exists())
                with self.assertRaisesRegex(
                    ownership.OwnershipError, "no verified uninstall inventory"
                ):
                    self.review(active)
                if change == "foreign":
                    self.assertEqual(
                        (active / "tui-deps/example/unowned").read_text(),
                        "late foreign data",
                    )


class PackageOwnershipTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.prefix = self.home / ".local/share/pipx/venvs/oc-box"
        lib = (
            self.prefix
            / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
        self.runtime = lib / "oc_box"
        self.runtime.mkdir(parents=True)
        self.dist = lib / "oc_box-1.9.200.dev0.dist-info"
        self.dist.mkdir()
        self.version = "1.9.200.dev0"
        (self.runtime / "distribution.json").write_text(
            json.dumps(
                {
                    "installer": "python-package",
                    "name": "oc-box",
                    "version": self.version,
                }
            )
        )
        (self.dist / "METADATA").write_text(f"Name: oc-box\nVersion: {self.version}\n")
        (self.prefix / "pyvenv.cfg").write_text(
            "include-system-site-packages = false\n"
        )
        self.program = self.prefix / "bin/oc-box"
        self.program.parent.mkdir()
        self.program.write_text("# synthetic package entry point\n")
        self.program.chmod(0o755)
        self.launcher = self.home / layout.LAUNCHER
        self.launcher.parent.mkdir()
        self.launcher.symlink_to(self.program)
        stack.enter_context(patch.object(sys, "prefix", str(self.prefix)))
        stack.enter_context(patch.object(sys, "base_prefix", "/usr"))
        stack.enter_context(
            patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("unexpected package manager"),
            )
        )

    def pipx(self):
        (self.prefix / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "pipx_metadata_version": "0.5",
                    "source_interpreter": "/usr/bin/python3",
                    "main_package": {
                        "package": "oc-box",
                        "package_version": self.version,
                        "package_or_url": "oc-box[tui]",
                        "include_apps": True,
                        "include_dependencies": False,
                        "apps": ["oc-box"],
                    },
                }
            )
        )

    def review(self):
        return ownership.review_installation(self.runtime, home=self.home)

    def test_pipx_receipt_only_returns_manual_guidance(self):
        self.pipx()
        review = self.review()
        self.assertEqual(
            review, ownership.PackageInstallReview("pipx", self.prefix, self.version)
        )
        self.assertNotIsInstance(review, ownership.FlatInstallPlan)
        self.launcher.unlink()
        self.launcher.write_text("a different installed launcher")
        with self.assertRaisesRegex(ownership.OwnershipError, "does not select"):
            self.review()

    @unittest.skipIf(sys.version_info < (3, 11), "uv receipts require tomllib")
    def test_uv_receipt_only_returns_manual_guidance_and_dual_claim_refuses(self):
        (self.prefix / "uv-receipt.toml").write_text(
            "[tool]\n"
            'requirements = [{name="oc-box", extras=["vault"]}]\n'
            f'entrypoints = [{{name="oc-box", install-path="{self.launcher}"}}]\n'
        )
        self.assertEqual(
            self.review(),
            ownership.PackageInstallReview("uv", self.prefix, self.version),
        )
        self.pipx()
        with self.assertRaisesRegex(
            ownership.OwnershipError, "Multiple package managers"
        ):
            self.review()

    def test_unverified_pip_transient_direct_source_and_system_refuse(self):
        with self.assertRaisesRegex(ownership.OwnershipError, "No verified persistent"):
            self.review()
        self.pipx()
        (self.dist / "direct_url.json").write_text('{"dir_info":{"editable":true}}')
        with self.assertRaisesRegex(
            ownership.OwnershipError, "Editable and direct-source"
        ):
            self.review()
        with patch.object(sys, "base_prefix", str(self.prefix)):
            with self.assertRaisesRegex(
                ownership.OwnershipError, "not a verified private"
            ):
                self.review()
