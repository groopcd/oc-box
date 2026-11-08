import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import backups, backup_crypto as crypto, oc_box as box, vault


@unittest.skipUnless(
    shutil.which("age") and shutil.which("age-keygen"),
    "optional age host tools unavailable",
)
class WorkspaceBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-backups-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environ = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "OC_BOX_CONTAINER": "0",
                "PERSONAL_GITHUB_TOKEN": "synthetic-unrelated-host-token",
            },
        )
        environ.start()
        self.addCleanup(environ.stop)
        self.project = self.root / "source"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data["ready"] = True
        self.data["devices"] = ["/dev/accel/accel0"]
        self.data["podman_args"] = [
            "--env=SECRET=synthetic-saved-value",
            "--privileged",
        ]
        self.data["credentials"] = {
            "mode": "selected",
            "env": [],
            "vault": ["EXAMPLE_TOKEN"],
        }
        box.atomic_json(self.path, self.data)
        self.sessions = {
            "sessions": ["ses_original", "ses_subagent"],
            "last": "ses_original",
            "host_sessions": {"ses_original": "ses_archive"},
            "synced": {"ses_original": "digest"},
        }
        box.atomic_json(box.session_file(self.path), self.sessions)
        (self.project / "unfinished.txt").write_text("uncommitted source edits")
        (self.project / ".git").mkdir()
        (self.project / ".git/HEAD").write_text("ref: refs/heads/master\n")
        self.home = self.root / "source-volume"
        (self.home / ".local/share/opencode").mkdir(parents=True)
        (self.home / ".local/share/opencode/opencode.db").write_bytes(
            b"synthetic-private-database"
        )
        (self.home / ".config").mkdir()
        (self.home / ".config/auth.json").write_text("synthetic-existing-auth")
        self.volumes = {self.data["home_volume"]: self.home}
        self.labels = {self.data["home_volume"]: self.data["id"]}
        self.created = []
        self.operations = []
        self.running = False
        self.base_image = "b" * 64
        self.snapshot_image = "c" * 64
        self.identity = self.root / "backup-identity.txt"
        self.identity.write_bytes(crypto.small(["age-keygen"]))
        self.identity.chmod(0o600)
        self.recipient = (
            crypto.small(["age-keygen", "-y", str(self.identity)]).decode().strip()
        )
        self.archive = self.root / "workspace.ocbox"
        self.real_encrypt_stream = crypto.encrypt_stream
        self.real_decrypt_stream = crypto.decrypt_stream
        self.real_archive_command = backups.archive_command
        self.real_create_container = box.create_container
        self.environment = contextlib.ExitStack()
        self.addCleanup(self.environment.close)
        self.environment.enter_context(patch.object(box, "check_backend"))
        self.environment.enter_context(
            patch.object(backups, "command", side_effect=self.podman)
        )
        self.environment.enter_context(
            patch.object(backups, "archive_command", side_effect=self.archive_command)
        )
        self.environment.enter_context(
            patch.object(crypto, "encrypt_stream", side_effect=self.encrypt)
        )
        self.environment.enter_context(
            patch.object(crypto, "decrypt_stream", side_effect=self.decrypt)
        )
        self.environment.enter_context(
            patch.object(box, "create_container", side_effect=self.create_container)
        )

    def info(self):
        return {
            "Config": {"Labels": {box.LABEL: self.data["id"]}},
            "State": {"Running": self.running},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.project),
                    "Destination": box.container_project(self.data),
                },
                {
                    "Type": "volume",
                    "Source": str(self.home),
                    "Destination": self.data["home"],
                },
            ],
        }

    def podman(self, box_arg, *args, **kwargs):
        self.operations.append(args)
        if args[:2] == ("volume", "exists"):
            return subprocess.CompletedProcess(
                args, 0 if args[2] in self.volumes else 1, "", ""
            )
        if args[:2] == ("container", "exists"):
            exists = args[2] == self.data["container"] or any(
                data["container"] == args[2] for data in self.created
            )
            return subprocess.CompletedProcess(args, 0 if exists else 1, "", "")
        if args[:2] == ("container", "inspect"):
            if args[2] != self.data["container"]:
                created = next(
                    data for data in self.created if data["container"] == args[2]
                )
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps(
                        [
                            {
                                "Config": {"Labels": {box.LABEL: created["id"]}},
                                "State": {"Running": False},
                            }
                        ]
                    ),
                    "",
                )
            return subprocess.CompletedProcess(args, 0, json.dumps([self.info()]), "")
        if args[:2] == ("volume", "inspect"):
            name = args[2]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Labels": {box.LABEL: self.labels[name]},
                            "Mountpoint": str(self.volumes[name]),
                        }
                    ]
                ),
                "",
            )
        if args[0] == "stop":
            self.running = False
        elif args[0] == "commit":
            return subprocess.CompletedProcess(args, 0, self.snapshot_image, "")
        elif args[:2] == ("volume", "create"):
            name = args[-1]
            target = self.root / name
            target.mkdir()
            self.volumes[name] = target
            self.labels[name] = args[3].split("=", 1)[1]
        elif args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"Id": "sha256:" + self.snapshot_image}]), ""
            )
        elif args[:2] == ("volume", "rm"):
            shutil.rmtree(self.volumes[args[2]])
        return subprocess.CompletedProcess(args, 0, "", "")

    def archive_command(self, box_arg, action, root, **kwargs):
        # Substitute user-namespace transport only, not the actual archive or
        # encryption/extraction functions. Ownership mapping is a live-host test.
        kwargs["private"] = False
        return self.real_archive_command(box_arg, action, root, **kwargs)

    def encrypt(self, producer, recipient, path):
        if producer[:2] == ["podman", "save"]:
            producer = [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'synthetic-image-layer')",
            ]
        return self.real_encrypt_stream(producer, recipient, path)

    def decrypt(self, path, private, consumer):
        if consumer[:2] == ["podman", "load"]:
            consumer = [
                sys.executable,
                "-c",
                "import sys; assert sys.stdin.buffer.read()==b'synthetic-image-layer'",
            ]
        return self.real_decrypt_stream(path, private, consumer)

    def create_container(self, path, data, **kwargs):
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", kwargs["environment"])
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["podman_args"], [])
        self.assertTrue(data["restore_incomplete"])
        self.created.append(json.loads(json.dumps(data)))
        data["ready"] = False

    def backup(self, portable=False):
        args = argparse.Namespace(
            project=self.project,
            workspace=None,
            output=self.archive,
            portable=portable,
            recipient=[self.recipient],
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            backups.make_backup(box, args)

    def restore(self, destination):
        args = argparse.Namespace(
            project=destination, file=self.archive, identity=[self.identity]
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            backups.restore_backup(box, args)
        path, data = box.find_workspace(destination)
        assert path is not None and data is not None
        return path, data

    def test_v2_backup_restore_retains_major_selection_and_both_databases(self):
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        box.atomic_json(
            box.session_file(self.path),
            {"sessions": ["ses_native"], "last": "ses_native"},
        )
        (self.home / ".local/share/opencode/oc-box-v2.db").write_bytes(
            b"synthetic-v2-db-bytes"
        )
        self.backup()
        path, data = self.restore(self.root / "v2-restored")
        self.assertEqual(data["opencode_version"], "2.2.6")
        self.assertEqual(box.session_state(path)["last"], "ses_native")
        home = self.volumes[data["home_volume"]]
        self.assertEqual(
            (home / ".local/share/opencode/oc-box-v2.db").read_bytes(),
            b"synthetic-v2-db-bytes",
        )
        self.assertTrue((home / ".local/share/opencode/opencode.db").exists())

    def test_credential_modes_roundtrip_without_vault_probe_or_unlock(self):
        policies = (
            ("2.2.6", {"mode": "env", "env": [], "vault": []}),
            ("2.2.6", {"mode": "selected", "env": [], "vault": ["EXAMPLE_TOKEN"]}),
            ("2.2.6", {"mode": "vault", "env": [], "vault": []}),
            ("2.2.6", None),
            ("1.19.5", {"mode": "vault", "env": [], "vault": []}),
        )
        with (
            patch.object(
                vault,
                "is_present",
                side_effect=AssertionError("Backups must not probe the host vault"),
            ),
            patch.object(backups.os, "geteuid", return_value=1000),
            patch.object(
                vault,
                "select_all",
                side_effect=AssertionError("Backups must not unlock the host vault"),
            ),
            patch.object(
                vault,
                "select",
                side_effect=AssertionError(
                    "Backups must not select host vault entries"
                ),
            ),
            patch.object(
                box,
                "detect_opencode_release",
                side_effect=AssertionError("Backup mode selection is read-only"),
            ),
        ):
            for index, (release, policy) in enumerate(policies):
                for portable in (False, True):
                    with self.subTest(
                        release=release, policy=policy, portable=portable
                    ):
                        self.data["opencode_version"] = release
                        if policy is None:
                            self.data.pop("credentials", None)
                            expected = {"mode": "vault", "env": [], "vault": []}
                        else:
                            self.data["credentials"] = policy
                            expected = policy
                        box.atomic_json(self.path, self.data)
                        self.archive = self.root / f"policy-{index}-{portable}.ocbox"
                        self.backup(portable=portable)
                        with contextlib.redirect_stdout(io.StringIO()):
                            backups.cli(
                                box,
                                [
                                    "backup-info",
                                    str(self.archive),
                                    "--identity",
                                    str(self.identity),
                                ],
                            )
                        _, restored = self.restore(
                            self.root / f"policy-restored-{index}-{portable}"
                        )
                        self.assertEqual(restored["credentials"], expected)
                        self.assertEqual(restored["opencode_version"], release)

    def test_full_real_encrypted_roundtrip_preserves_project_home_and_session_ids(self):
        before = {
            path: path.read_bytes()
            for path in (
                self.path,
                box.session_file(self.path),
                self.project / box.MARKER,
            )
        }
        self.running = True
        self.backup()
        self.assertFalse(self.running)
        self.assertTrue(any(command[0] == "stop" for command in self.operations))
        self.assertTrue(any(command[0] == "commit" for command in self.operations))
        self.assertNotIn(b"uncommitted source edits", self.archive.read_bytes())
        self.assertNotIn(b"synthetic-private-database", self.archive.read_bytes())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        destination = self.root / "restored"
        path, data = self.restore(destination)
        self.assertNotEqual(data["id"], self.data["id"])
        self.assertEqual(data["container_project"], str(self.project))
        self.assertEqual(box.container_project(data), str(self.project))
        self.assertEqual(data["home"], self.data["home"])
        self.assertTrue(data["ready"])
        self.assertNotIn("restore_incomplete", data)
        self.assertEqual(data["image"], self.snapshot_image)
        self.assertEqual(
            (destination / "unfinished.txt").read_text(), "uncommitted source edits"
        )
        self.assertEqual(
            (destination / ".git/HEAD").read_text(), "ref: refs/heads/master\n"
        )
        restored_home = self.volumes[data["home_volume"]]
        self.assertEqual(
            (restored_home / ".local/share/opencode/opencode.db").read_bytes(),
            b"synthetic-private-database",
        )
        self.assertEqual(
            (restored_home / ".config/auth.json").read_text(), "synthetic-existing-auth"
        )
        self.assertEqual(
            box.session_state(path),
            {"sessions": self.sessions["sessions"], "last": self.sessions["last"]},
        )
        self.assertTrue((path.parent / "recovered-registry/sessions.json").exists())
        self.assertEqual(data["credentials"], self.data["credentials"])
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["podman_args"], [])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(
            any(command[0] in ("start", "exec") for command in self.operations)
        )

    def test_portable_restore_uses_distro_image_and_requires_baseline_setup(self):
        self.backup(portable=True)
        self.assertFalse(any(command[0] == "commit" for command in self.operations))
        with crypto.opened(self.archive, [self.identity]) as (manifest, _, _):
            self.assertEqual(manifest["kind"], "portable")
            self.assertNotIn("image.age", manifest["components"])
        _, data = self.restore(self.root / "portable")
        self.assertEqual(data["image"], box.DISTROS["tumbleweed"][0])
        self.assertFalse(data["ready"])

    def test_backup_info_omits_raw_extra_option_values_and_never_uses_podman(self):
        self.backup(portable=True)
        self.operations.clear()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            backups.inspect_backup(
                box, argparse.Namespace(file=self.archive, identity=[self.identity])
            )
        info = json.loads(output.getvalue())
        self.assertEqual(info["kind"], "portable")
        self.assertNotIn("synthetic-saved-value", output.getvalue())
        self.assertEqual(self.operations, [])

    def test_existing_destination_or_backup_file_is_never_overwritten(self):
        self.backup(portable=True)
        existing = self.archive.read_bytes()
        with self.assertRaisesRegex(crypto.BackupError, "already exists"):
            self.backup(portable=True)
        self.assertEqual(self.archive.read_bytes(), existing)
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "keep").write_text("keep")
        with self.assertRaisesRegex(crypto.BackupError, "never overwrites"):
            self.restore(destination)
        self.assertEqual((destination / "keep").read_text(), "keep")

    def test_archive_destination_inside_project_or_registry_is_refused(self):
        for parent in (self.project, self.path.parent):
            self.archive = parent / "bad.ocbox"
            with (
                self.subTest(parent=parent),
                self.assertRaisesRegex(crypto.BackupError, "outside"),
            ):
                self.backup()
        self.assertFalse(any(command[0] == "stop" for command in self.operations))

    def test_active_workspace_lock_and_external_mount_are_refused(self):
        with (
            box.workspace_lock(self.path),
            self.assertRaisesRegex(box.BoxError, "active oc-box"),
        ):
            self.backup()
        original = self.info()
        original["Mounts"].append(
            {"Type": "bind", "Source": "/host/extra", "Destination": "/extra"}
        )
        with (
            patch.object(self, "info", return_value=original),
            self.assertRaisesRegex(crypto.BackupError, "additional bind mounts"),
        ):
            self.backup()
        self.assertFalse(self.archive.exists())

    def test_wrong_identity_or_corrupt_archive_never_creates_workspace_resources(self):
        self.backup(portable=True)
        wrong = self.root / "wrong.txt"
        wrong.write_bytes(crypto.small(["age-keygen"]))
        wrong.chmod(0o600)
        self.operations.clear()
        args = argparse.Namespace(
            project=self.root / "wrong-destination", file=self.archive, identity=[wrong]
        )
        with self.assertRaises(crypto.BackupError):
            backups.restore_backup(box, args)
        self.assertFalse(args.project.exists())
        self.assertEqual(self.operations, [])
        self.assertEqual(box.workspace_paths(), [self.path])

    def test_create_failure_rolls_back_only_new_resources_and_preserves_original(self):
        self.backup(portable=True)
        destination = self.root / "failed-restore"
        with patch.object(
            box, "create_container", side_effect=box.BoxError("create failed")
        ):
            with self.assertRaisesRegex(box.BoxError, "create failed"):
                self.restore(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(box.workspace_paths(), [self.path])
        self.assertTrue(self.project.exists())
        self.assertEqual(
            (self.project / "unfinished.txt").read_text(), "uncommitted source edits"
        )
        self.assertFalse(list(self.root.glob(".oc-box-restore-*")))

    def test_incomplete_authenticated_component_never_activates_destination(self):
        self.backup(portable=True)
        original = self.decrypt

        def broken(path, private, consumer):
            if path.name == "home.age":
                raise crypto.BackupError("authentication failed")
            return original(path, private, consumer)

        with (
            patch.object(crypto, "decrypt_stream", side_effect=broken),
            self.assertRaisesRegex(crypto.BackupError, "authentication failed"),
        ):
            self.restore(self.root / "incomplete")
        self.assertEqual(box.workspace_paths(), [self.path])
        self.assertFalse((self.root / "incomplete").exists())

    def test_atomic_no_replace_activation_cannot_overwrite_empty_racing_directory(self):
        first = self.root / "staged"
        first.mkdir()
        (first / "new").write_text("new")
        target = self.root / "race"
        target.mkdir()
        with self.assertRaisesRegex(crypto.BackupError, "without overwrite"):
            backups.activate_project(first, target)
        self.assertTrue(first.exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_restore_cannot_write_into_host_opencode_or_vault_directories(self):
        locations = (
            box.state_root(),
            box.APP,
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "opencode",
            Path(os.environ["XDG_DATA_HOME"]) / "opencode",
            box.vault.vault_path().parent,
        )
        for parent in locations:
            parent.mkdir(parents=True, exist_ok=True)
            with (
                self.subTest(parent=parent),
                self.assertRaisesRegex(crypto.BackupError, "outside trusted"),
            ):
                self.restore(parent / "new-restore")
        self.assertEqual(box.workspace_paths(), [self.path])

    def test_rollback_removes_restrictive_staging_without_following_symlinks(self):
        outside = self.root / "unrelated"
        outside.mkdir(mode=0o755)
        (outside / "keep").write_text("keep")
        stage = self.root / "restrictive-staging"
        (stage / "locked/nested").mkdir(parents=True)
        (stage / "locked/nested/file").write_text("staged")
        (stage / "locked/external").symlink_to(outside, target_is_directory=True)
        (stage / "locked/nested").chmod(0)
        (stage / "locked").chmod(0)
        backups.remove_staged_tree(stage)
        self.assertFalse(stage.exists())
        self.assertEqual((outside / "keep").read_text(), "keep")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_restore_writes_blocked_journal_before_staging_sensitive_payload(self):
        self.backup(portable=True)
        original = self.decrypt

        def verify(path, private, consumer):
            if path.name == "project.age":
                pending = [
                    record for record in box.workspace_paths() if record != self.path
                ]
                self.assertEqual(len(pending), 1)
                journal = box.read_json(pending[0])
                self.assertTrue(journal["restore_incomplete"])
                self.assertIn("restore_staging_project", journal)
                self.assertEqual(journal["devices"], [])
                self.assertEqual(journal["podman_args"], [])
            return original(path, private, consumer)

        with patch.object(crypto, "decrypt_stream", side_effect=verify):
            restored_path, restored = self.restore(self.root / "journal-restore")
        self.assertNotIn("restore_incomplete", restored)
        self.assertNotIn("restore_staging_project", restored)
        self.assertTrue(restored_path.exists())

    def test_invalid_mount_paths_limits_and_architecture_fail_before_restore(self):
        self.backup(portable=True)
        with crypto.opened(self.archive, [self.identity]) as (manifest, _, _):
            for change in (
                lambda value: value.update(container_project="/etc/private"),
                lambda value: value["configuration"].update(home="/"),
                lambda value: value["configuration"].update(pids=1),
                lambda value: value.update(container_project="/home/../etc"),
            ):
                changed = json.loads(json.dumps(manifest))
                change(changed)
                with self.assertRaises(crypto.BackupError):
                    backups.validate_manifest(box, changed)

    def test_restored_internal_paths_are_used_by_tools_session_selection_and_archives(
        self,
    ):
        self.backup(portable=True)
        path, data = self.restore(self.root / "moved")
        self.assertIn("PWD=" + str(self.project), box.maintenance_command(data))
        command = box.exec_command(data, ["opencode"])
        self.assertEqual(command[command.index("--workdir") + 1], str(self.project))
        # Do not rewrite the saved live DB or its paths to relocate host data.
        listing = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [{"id": "ses_original", "directory": str(self.project), "updated": 1}]
            ),
            "",
        )
        with patch.object(box, "run", return_value=listing):
            self.assertEqual(box.refresh_sessions(path, data)["last"], "ses_original")
        exported = {
            "info": {
                "id": "ses_original",
                "directory": str(self.project),
                "title": "Work",
            },
            "messages": [],
        }
        host = box.archive_session(exported, data)
        self.assertEqual(host["info"]["directory"], data["project"])

    def test_failed_image_capture_removes_only_the_unique_temporary_image_tag(self):
        real = self.encrypt

        def fail_image(producer, recipient, path):
            if path.name == "image.age":
                raise crypto.BackupError("image save failed")
            return real(producer, recipient, path)

        with patch.object(crypto, "encrypt_stream", side_effect=fail_image):
            with self.assertRaisesRegex(crypto.BackupError, "image save failed"):
                self.backup()
        captures = [command for command in self.operations if command[0] == "commit"]
        removed = [
            command for command in self.operations if command[:2] == ("image", "rm")
        ]
        self.assertEqual(len(captures), 1)
        self.assertEqual(removed, [("image", "rm", captures[0][-1])])
        self.assertTrue(captures[0][-1].startswith("localhost/oc-box-backup-"))
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.root.glob("oc-box-backup-ciphertext-*")))
        self.assertTrue(self.path.exists())

    def test_reset_restored_workspace_rebuilds_catalog_os_and_keeps_paths_home_and_sessions(
        self,
    ):
        self.backup()
        path, data = self.restore(self.root / "reset-restored")
        sessions = box.session_file(path).read_bytes()
        with (
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "check_backend"),
            patch.object(
                box.credentials, "unlocked", return_value=contextlib.nullcontext({})
            ),
            patch.object(box, "check_resource_controllers"),
            patch.object(box, "remove_resources") as remove,
            patch.object(box, "launch", return_value=0) as launch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["reset", "--workspace", data["id"]]), 0)
        remove.assert_called_once()
        self.assertTrue(remove.call_args.kwargs["keep_home"])
        restored = box.read_json(path)
        self.assertEqual(restored["image"], box.DISTROS[data["distro"]][0])
        self.assertEqual(restored["container_project"], str(self.project))
        self.assertEqual(restored["home_volume"], data["home_volume"])
        self.assertEqual(box.session_file(path).read_bytes(), sessions)
        launch.assert_called_once()

    def test_restored_container_create_clears_image_env_and_ignores_archived_devices(
        self,
    ):
        self.backup(portable=True)
        _, data = self.restore(self.root / "create-check")
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            self.assertNotIn("PERSONAL_GITHUB_TOKEN", kwargs.get("env", {}))
            if args[:2] == ("volume", "inspect"):
                return subprocess.CompletedProcess(
                    [], 0, json.dumps([{"Labels": {box.LABEL: data["id"]}}]), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch.object(box, "podman", side_effect=fake),
            patch.object(box, "warn_device_access"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.real_create_container(
                self.path, data, environment=crypto.environment()
            )
        created = next(call for call in calls if call[0] == "create")
        self.assertIn("--unsetenv-all", created)
        self.assertIn("--no-healthcheck", created)
        self.assertIn("--image-volume=ignore", created)
        self.assertNotIn("--privileged", created)
        self.assertFalse(any(arg.startswith("--device=") for arg in created))
        self.assertIn(
            f"{data['project']}:{self.project}:rw,z,rprivate,nosuid,nodev", created
        )

    def test_failed_cleanup_retains_incomplete_registration_for_explicit_purge(self):
        self.backup(portable=True)
        destination = self.root / "cleanup-problem"
        original_command = self.podman

        def commands(box_arg, *args, **kwargs):
            if args[:2] in (("container", "rm"), ("volume", "rm")):
                return subprocess.CompletedProcess(args, 125, "", "")
            return original_command(box_arg, *args, **kwargs)

        with (
            patch.object(
                box, "create_container", side_effect=box.BoxError("create failed")
            ),
            patch.object(backups, "command", side_effect=commands),
        ):
            with self.assertRaisesRegex(box.BoxError, "create failed"):
                self.restore(destination)
        path, data = box.find_workspace(destination)
        assert path is not None and data is not None
        self.assertTrue(data["restore_incomplete"])
        self.assertTrue(destination.exists())
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "start_container") as start,
        ):
            with self.assertRaisesRegex(box.BoxError, "restore did not finish"):
                box.main(["--workspace", data["id"]])
        start.assert_not_called()

    def test_incomplete_restore_purge_never_starts_or_syncs_it(self):
        self.data["restore_incomplete"] = True
        args = argparse.Namespace(yes=True, delete_sessions=False, all=False)
        with (
            patch.object(box, "inspect_owned", return_value=None),
            patch.object(box, "podman") as podman,
            patch.object(box, "sync_sessions") as sync,
            patch.object(box, "stop_container"),
            patch.object(box, "remove_resources"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            box.purge(self.path, self.data, args)
        podman.assert_not_called()
        sync.assert_not_called()
        self.assertTrue(self.project.exists())

    def test_rollback_refuses_deleting_same_named_unowned_resources(self):
        data = {**self.data, "id": "f" * 32}
        with patch.object(backups, "command", side_effect=self.podman):
            with self.assertRaisesRegex(crypto.BackupError, "ownership mismatch"):
                backups.remove_restore_resource(box, "container", data)
            with self.assertRaisesRegex(
                crypto.BackupError, "ownership/inspection mismatch"
            ):
                backups.remove_restore_resource(box, "volume", data)
        self.assertFalse(
            any(
                command[:2] in (("container", "rm"), ("volume", "rm"))
                for command in self.operations
            )
        )


class BackupCliTests(unittest.TestCase):
    def test_no_container_side_backup_or_silent_create(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(crypto, "tools"),
        ):
            for arguments in (
                ["backup", "--output", "/tmp/synthetic.ocbox"],
                ["restore", "/tmp/synthetic.ocbox", "--project", "/tmp/new"],
                ["backup-info", "/tmp/synthetic.ocbox"],
            ):
                with (
                    self.subTest(arguments=arguments),
                    self.assertRaisesRegex(box.BoxError, "on the host"),
                ):
                    box.main(arguments)
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(backups.os, "geteuid", return_value=0),
        ):
            with self.assertRaisesRegex(box.BoxError, "without sudo"):
                box.main(["backup-info", "/tmp/synthetic.ocbox"])

    def test_backups_do_not_unlock_vault_or_refresh_configuration(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(backups.os, "geteuid", return_value=1000),
            patch.object(crypto, "tools"),
            patch.object(backups, "make_backup") as backup,
            patch.object(
                box.credentials,
                "unlocked",
                side_effect=AssertionError("no vault unlock"),
            ),
            patch.object(
                box, "prepare_runtime", side_effect=AssertionError("no config refresh")
            ),
        ):
            self.assertEqual(
                box.main(["backup", "--output", "/tmp/synthetic.ocbox"]), 0
            )
        backup.assert_called_once()

    def test_termination_uses_cleanup_path_and_restores_original_signal_handlers(self):
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

        def terminate(*_):
            current = signal.getsignal(signal.SIGTERM)
            assert callable(current)
            current(signal.SIGTERM, None)

        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(backups.os, "geteuid", return_value=1000),
            patch.object(crypto, "tools"),
            patch.object(backups, "make_backup", side_effect=terminate),
        ):
            with self.assertRaises(KeyboardInterrupt):
                box.main(["backup", "--output", "/tmp/synthetic.ocbox"])
        for sig, previous in old.items():
            self.assertEqual(signal.getsignal(sig), previous)


if __name__ == "__main__":
    unittest.main()
