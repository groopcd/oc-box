import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
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
        self.images = {}
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
                                "Id": created["id"] * 2,
                                "Name": created["container"],
                                "Image": self.snapshot_image
                                if created["image"].removeprefix("sha256:")
                                == self.snapshot_image
                                else self.base_image,
                                "Config": {"Labels": {box.LABEL: created["id"]}},
                                "State": {
                                    "Running": False,
                                    "Paused": False,
                                    "Status": "created",
                                },
                                "Mounts": [
                                    {
                                        "Type": "bind",
                                        "Source": created["project"],
                                        "Destination": box.container_project(created),
                                    },
                                    {
                                        "Type": "volume",
                                        "Source": str(
                                            self.volumes[created["home_volume"]]
                                        ),
                                        "Destination": created["home"],
                                    },
                                ],
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
            labels = {box.LABEL: self.data["id"]}
            if "--change" in args:
                key, value = (
                    args[args.index("--change") + 1]
                    .removeprefix("LABEL ")
                    .split("=", 1)
                )
                labels[key] = value
            self.images[args[-1]] = {
                "Id": "sha256:" + self.snapshot_image,
                "Config": {"Labels": labels},
            }
            return subprocess.CompletedProcess(args, 0, self.snapshot_image, "")
        elif args[:2] == ("volume", "create"):
            name = args[-1]
            target = self.root / name
            target.mkdir()
            self.volumes[name] = target
            self.labels[name] = args[3].split("=", 1)[1]
        elif args[:2] == ("image", "exists"):
            return subprocess.CompletedProcess(
                args, 0 if args[2] in self.images else 1, "", ""
            )
        elif args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        self.images.get(
                            args[2],
                            {
                                "Id": "sha256:"
                                + (
                                    self.snapshot_image
                                    if args[2].removeprefix("sha256:")
                                    == self.snapshot_image
                                    else self.base_image
                                )
                            },
                        )
                    ]
                ),
                "",
            )
        elif args[:2] == ("image", "rm"):
            self.images.pop(args[2], None)
        elif args[:2] == ("volume", "rm"):
            shutil.rmtree(self.volumes.pop(args[2]))
            self.labels.pop(args[2])
        elif args[:2] == ("container", "rm"):
            self.created[:] = [
                data for data in self.created if data["container"] != args[-1]
            ]
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
        return subprocess.CompletedProcess([], 0, data["id"] * 2 + "\n", "")

    @contextlib.contextmanager
    def real_creation_dispatch(self, *, output=None, failure=None):
        """Use the real constructor/run capture path, replacing only native IO."""
        original_run = subprocess.run
        calls = []
        sentinel = "PRIVATE_SYNTHETIC_NATIVE_OUTPUT"

        def native(arguments, **kwargs):
            if arguments[0] != "podman":
                return original_run(arguments, **kwargs)
            args = tuple(arguments[1:])
            calls.append(
                all(
                    hasattr(kwargs.get(name), "fileno")
                    and "memfd:"
                    in os.readlink(f"/proc/self/fd/{kwargs[name].fileno()}")
                    for name in ("stdout", "stderr")
                )
            )
            if args[0] == "create":
                name = args[args.index("--name") + 1]
                record = next(
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["container"] == name
                )
                self.created.append(json.loads(json.dumps(record)))
                result = subprocess.CompletedProcess(
                    arguments,
                    0,
                    record["id"] * 2 + "\n" if output is None else output,
                    sentinel,
                )
            else:
                result = self.podman(box, *args)
            if (failure == "volume-inspect" and args[:2] == ("volume", "inspect")) or (
                failure == "create" and args[0] == "create"
            ):
                result = subprocess.CompletedProcess(arguments, 125, "", sentinel)
            for name in ("stdout", "stderr"):
                value = getattr(result, name, "") or ""
                target = kwargs.get(name)
                if hasattr(target, "write"):
                    target.write(value.encode())
                    target.flush()
                elif value:
                    print(
                        value,
                        end="",
                        file=sys.stdout if name == "stdout" else sys.stderr,
                    )
            if failure == "timeout" and args[0] == "create":
                raise subprocess.TimeoutExpired(arguments, 1, stderr=sentinel.encode())
            return subprocess.CompletedProcess(arguments, result.returncode)

        with (
            patch.object(box, "create_container", wraps=self.real_create_container),
            patch.object(subprocess, "run", side_effect=native),
        ):
            yield calls, sentinel

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

    def test_readonly_marker_full_and_portable_restore_preserves_source(self):
        marker = self.project / box.MARKER
        original = marker.read_bytes()
        for mode in (0o400, 0o444):
            marker.chmod(mode)
            for portable in (False, True):
                with self.subTest(mode=oct(mode), portable=portable):
                    self.archive = self.root / f"readonly-{mode}-{portable}.ocbox"
                    self.backup(portable=portable)
                    destination = self.root / f"readonly-{mode}-{portable}"
                    _, data = self.restore(destination)
                    self.assertNotEqual(data["id"], self.data["id"])
                    self.assertEqual(
                        (destination / box.MARKER).read_text(), data["id"] + "\n"
                    )
                    self.assertEqual(
                        (destination / box.MARKER).stat().st_mode & 0o777, 0o600
                    )
                    self.assertEqual(marker.read_bytes(), original)
                    self.assertEqual(marker.stat().st_mode & 0o777, mode)

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

    def test_required_mounts_must_match_before_stop_or_capture(self):
        def source_snapshot():
            return {
                path: (path.read_bytes(), path.stat().st_mode)
                for path in (
                    self.path,
                    box.session_file(self.path),
                    *self.project.rglob("*"),
                    *self.home.rglob("*"),
                )
                if path.is_file()
            }

        before = source_snapshot()
        for index, role in enumerate(("project", "private-home")):
            expected = self.info()["Mounts"][index]
            cases = {
                "missing": [],
                "wrong-source": [
                    {**expected, "Source": str(self.root / "other-source")}
                ],
                "wrong-destination": [
                    {**expected, "Destination": expected["Destination"] + "-other"}
                ],
                "wrong-type": [
                    {**expected, "Type": "volume" if role == "project" else "bind"}
                ],
                "tmpfs-instead-of-managed-mount": [
                    {**expected, "Type": "tmpfs", "Source": ""}
                ],
                "missing-type": [
                    {key: value for key, value in expected.items() if key != "Type"}
                ],
                "duplicate-expected": [expected, dict(expected)],
                "overridden-by-other-source": [
                    expected,
                    {**expected, "Source": str(self.root / "other-source")},
                ],
                "overridden-by-tmpfs": [
                    expected,
                    {**expected, "Type": "tmpfs", "Source": ""},
                ],
            }
            for portable in (False, True):
                for name, mounts in cases.items():
                    info = self.info()
                    info["Mounts"][index : index + 1] = mounts
                    self.running = True
                    self.operations.clear()
                    with self.subTest(mount=role, case=name, portable=portable):
                        with (
                            patch.object(
                                self,
                                "info",
                                side_effect=lambda: {
                                    **info,
                                    "State": {"Running": self.running},
                                },
                            ),
                            patch.object(
                                crypto,
                                "generate_key",
                                side_effect=AssertionError(
                                    "capture started before mount refusal"
                                ),
                            ) as generate,
                            patch.object(crypto, "encrypt_stream") as encrypt,
                        ):
                            with self.assertRaises(crypto.BackupError):
                                self.backup(portable=portable)
                            generate.assert_not_called()
                            encrypt.assert_not_called()
                        self.assertEqual(
                            self.operations,
                            [
                                ("container", "inspect", self.data["container"]),
                                ("volume", "inspect", self.data["home_volume"]),
                            ],
                        )
                        self.assertTrue(self.running)
                        self.assertEqual(self.created, [])
                        self.assertEqual(
                            self.volumes, {self.data["home_volume"]: self.home}
                        )
                        self.assertEqual(
                            self.labels, {self.data["home_volume"]: self.data["id"]}
                        )
                        self.assertEqual(source_snapshot(), before)
                        self.assertFalse(self.archive.exists())
                        self.assertFalse(
                            list(self.root.glob("oc-box-backup-ciphertext-*"))
                        )
                        self.assertFalse(list(self.root.glob(".oc-box-backup-*")))

    def test_backup_accepts_home_mount_with_restored_internal_project_path(self):
        self.data["container_project"] = str(self.root / "original-project")
        box.atomic_json(self.path, self.data)
        for portable in (False, True):
            with self.subTest(portable=portable):
                self.archive = self.root / f"internal-path-{portable}.ocbox"
                self.backup(portable=portable)
                with crypto.opened(self.archive, [self.identity]) as (manifest, _, _):
                    self.assertEqual(manifest["project"], str(self.project))
                    self.assertEqual(
                        manifest["container_project"], self.data["container_project"]
                    )
                    self.assertEqual(
                        manifest["configuration"]["home"], self.data["home"]
                    )

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

    def test_uncertain_volume_creation_cleans_owned_or_retains_attempt_journal(self):
        self.backup(portable=True)
        for outcome in (
            "timeout",
            "nonzero",
            "exists-error",
            "inspect-error",
            "malformed-inspect",
            "ambiguous-inspect",
            "foreign-label",
        ):
            with self.subTest(outcome=outcome):
                destination = self.root / ("volume-" + outcome)
                primary = crypto.BackupError("Synthetic volume creation failed.")
                attempted = []
                created = []

                def commands(box_arg, *args, **kwargs):
                    if args[:2] == ("volume", "create"):
                        journal = next(
                            path
                            for path in box.workspace_paths()
                            if box.read_json(path)["project"] == str(destination)
                        )
                        attempted.append(
                            box.read_json(journal).get(
                                "restore_attempted_resources", []
                            )
                        )
                        result = self.podman(box_arg, *args, **kwargs)
                        created.append(args[-1])
                        if outcome == "foreign-label":
                            self.labels[args[-1]] = "f" * 32
                        raise primary
                    if (
                        created
                        and args[:2] == ("volume", "exists")
                        and outcome == "exists-error"
                    ):
                        return subprocess.CompletedProcess(
                            args, 125, "", "PRIVATE_QUERY"
                        )
                    if created and args[:2] == ("volume", "inspect"):
                        if outcome == "inspect-error":
                            raise OSError("PRIVATE_QUERY")
                        if outcome == "malformed-inspect":
                            return subprocess.CompletedProcess(
                                args, 0, "PRIVATE_JSON", ""
                            )
                        if outcome == "ambiguous-inspect":
                            result = self.podman(box_arg, *args, **kwargs)
                            return subprocess.CompletedProcess(
                                args,
                                0,
                                json.dumps([*json.loads(result.stdout), {}]),
                                "",
                            )
                    return self.podman(box_arg, *args, **kwargs)

                self.operations.clear()
                with (
                    patch.object(backups, "command", side_effect=commands),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertIs(error.exception, primary)
                self.assertEqual(attempted, [["volume"]])
                journals = [
                    path
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                ]
                if outcome in ("timeout", "nonzero"):
                    self.assertNotIn(created[0], self.volumes)
                    self.assertEqual(journals, [])
                else:
                    self.assertIn(created[0], self.volumes)
                    self.assertEqual(len(journals), 1)
                    record = box.read_json(journals[0])
                    self.assertTrue(record["restore_incomplete"])
                    self.assertEqual(record["home_volume"], created[0])
                    self.assertFalse(
                        any(args[:2] == ("volume", "rm") for args in self.operations)
                    )
                self.assertEqual(
                    (self.home / ".local/share/opencode/opencode.db").read_bytes(),
                    b"synthetic-private-database",
                )

    def test_uncertain_image_commit_cleans_exact_owned_tag_or_retains_capture_journal(
        self,
    ):
        for outcome in (
            "timeout",
            "nonzero",
            "malformed-id",
            "exists-error",
            "inspect-error",
            "malformed-inspect",
            "ambiguous-inspect",
            "foreign-label",
            "remove-error",
        ):
            with self.subTest(outcome=outcome):
                self.archive = self.root / ("commit-" + outcome + ".ocbox")
                primary = crypto.BackupError("Synthetic image creation failed.")
                attempted = []
                tags = []

                def commands(box_arg, *args, **kwargs):
                    if args[0] == "commit":
                        tags.append(args[-1])
                        journals = list(
                            (self.path.parent / "backup-captures").glob("*.json")
                        )
                        attempted.append(
                            any(
                                box.read_json(path).get("tag") == args[-1]
                                for path in journals
                            )
                        )
                        self.podman(box_arg, *args, **kwargs)
                        if outcome == "foreign-label":
                            self.images[args[-1]]["Config"]["Labels"] = {}
                        if outcome == "malformed-id":
                            return subprocess.CompletedProcess(
                                args, 0, "PRIVATE_INVALID_ID", ""
                            )
                        raise primary
                    if (
                        tags
                        and args[:2] == ("image", "exists")
                        and outcome == "exists-error"
                    ):
                        return subprocess.CompletedProcess(
                            args, 125, "", "PRIVATE_QUERY"
                        )
                    if (
                        tags
                        and args[:2] == ("image", "inspect")
                        and outcome == "inspect-error"
                    ):
                        raise OSError("PRIVATE_QUERY")
                    if (
                        tags
                        and args[:2] == ("image", "inspect")
                        and outcome == "malformed-inspect"
                    ):
                        return subprocess.CompletedProcess(args, 0, "PRIVATE_JSON", "")
                    if (
                        tags
                        and args[:2] == ("image", "inspect")
                        and outcome == "ambiguous-inspect"
                    ):
                        result = self.podman(box_arg, *args, **kwargs)
                        return subprocess.CompletedProcess(
                            args, 0, json.dumps([*json.loads(result.stdout), {}]), ""
                        )
                    if args[:2] == ("image", "rm") and outcome == "remove-error":
                        raise OSError("PRIVATE_REMOVE")
                    return self.podman(box_arg, *args, **kwargs)

                self.operations.clear()
                with (
                    patch.object(backups, "command", side_effect=commands),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.backup()
                if outcome != "malformed-id":
                    self.assertIs(error.exception, primary)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(attempted, [True])
                journals = [
                    path
                    for path in (self.path.parent / "backup-captures").glob("*.json")
                    if box.read_json(path).get("tag") == tags[0]
                ]
                if outcome in ("timeout", "nonzero", "malformed-id"):
                    self.assertNotIn(tags[0], self.images)
                    self.assertEqual(journals, [])
                else:
                    self.assertIn(tags[0], self.images)
                    self.assertEqual(len(journals), 1)
                    self.assertEqual(
                        box.read_json(journals[0])["workspace_id"], self.data["id"]
                    )
                self.assertFalse(self.archive.exists())
                self.assertTrue(self.project.exists())

    def test_forward_volume_inspection_requires_one_valid_owned_record(self):
        self.backup(portable=True)
        original_decrypt = self.decrypt
        for shape in (
            "null",
            "empty",
            "multiple",
            "object",
            "scalar",
            "invalid-labels",
            "missing-mountpoint",
            "invalid-mountpoint",
        ):
            with self.subTest(shape=shape):
                destination = self.root / ("forward-volume-" + shape)
                created, inspected, extracted = [], [], []

                def commands(box_arg, *args, **kwargs):
                    result = self.podman(box_arg, *args, **kwargs)
                    if args[:2] == ("volume", "create"):
                        created.append(args[-1])
                    if (
                        args[:2] == ("volume", "inspect")
                        and args[2] in created
                        and not inspected
                    ):
                        inspected.append(True)
                        owned = json.loads(result.stdout)[0]
                        bodies = {
                            "null": None,
                            "empty": [],
                            "multiple": [owned, {}],
                            "object": owned,
                            "scalar": ["PRIVATE_INVALID_RECORD"],
                            "invalid-labels": [
                                {**owned, "Labels": ["PRIVATE_INVALID_LABELS"]}
                            ],
                            "missing-mountpoint": [{"Labels": owned["Labels"]}],
                            "invalid-mountpoint": [
                                {**owned, "Mountpoint": {"PRIVATE": "INVALID_PATH"}}
                            ],
                        }
                        return subprocess.CompletedProcess(
                            args, 0, json.dumps(bodies[shape]), ""
                        )
                    return result

                def decrypt(path, private, consumer):
                    extracted.append(path.name)
                    return original_decrypt(path, private, consumer)

                with (
                    patch.object(backups, "command", side_effect=commands),
                    patch.object(crypto, "decrypt_stream", side_effect=decrypt),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(inspected, [True])
                self.assertEqual(extracted, ["project.age"])
                self.assertFalse(destination.exists())
                self.assertFalse(
                    any(data["project"] == str(destination) for data in self.created)
                )
                self.assertEqual(
                    (self.home / ".local/share/opencode/opencode.db").read_bytes(),
                    b"synthetic-private-database",
                )

    def test_loaded_image_inspection_requires_one_valid_identity_before_activation(
        self,
    ):
        self.backup()
        for shape in (
            "null",
            "empty",
            "multiple",
            "object",
            "scalar",
            "missing-id",
            "invalid-id-type",
            "invalid-id",
        ):
            with self.subTest(shape=shape):
                destination = self.root / ("forward-image-" + shape)
                queried = []

                def commands(box_arg, *args, **kwargs):
                    if (
                        args[:2] == ("image", "inspect")
                        and args[2] == self.snapshot_image
                    ):
                        queried.append(True)
                        valid = {"Id": "sha256:" + self.snapshot_image}
                        bodies = {
                            "null": None,
                            "empty": [],
                            "multiple": [valid, {}],
                            "object": valid,
                            "scalar": ["PRIVATE_INVALID_RECORD"],
                            "missing-id": [{}],
                            "invalid-id-type": [{"Id": ["PRIVATE_INVALID_ID"]}],
                            "invalid-id": [{"Id": "PRIVATE_INVALID_ID"}],
                        }
                        return subprocess.CompletedProcess(
                            args, 0, json.dumps(bodies[shape]), ""
                        )
                    return self.podman(box_arg, *args, **kwargs)

                with (
                    patch.object(backups, "command", side_effect=commands),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(queried, [True])
                self.assertFalse(destination.exists())
                self.assertFalse(
                    any(data["project"] == str(destination) for data in self.created)
                )
                record = next(
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                )
                self.assertTrue(record["restore_incomplete"])
                self.assertEqual(
                    record["restore_attempted_resources"], ["volume", "image"]
                )

    def test_real_restore_creation_failures_keep_native_output_private(self):
        self.backup(portable=True)
        for failure in ("volume-inspect", "create", "timeout"):
            with self.subTest(failure=failure):
                output = io.StringIO()
                destination = self.root / ("private-create-" + failure)
                args = argparse.Namespace(
                    project=destination, file=self.archive, identity=[self.identity]
                )
                with (
                    self.real_creation_dispatch(failure=failure) as (calls, sentinel),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(output),
                    self.assertRaises((box.BoxError, crypto.BackupError)) as error,
                ):
                    backups.restore_backup(box, args)
                self.assertFalse(
                    sentinel in output.getvalue(),
                    "Native private output reached the console",
                )
                self.assertFalse(
                    sentinel in str(error.exception),
                    "Native private output reached the public error",
                )
                self.assertTrue(
                    calls and all(calls),
                    "Native constructor IO was not captured in private memory",
                )
                self.assertFalse(destination.exists())
                self.assertEqual(self.created, [])

    def test_real_restore_creation_validates_returned_id_before_completion(self):
        for portable in (False, True):
            self.archive = self.root / f"real-create-{portable}.ocbox"
            self.backup(portable=portable)
            for name, response in (
                ("valid", None),
                ("empty", ""),
                ("malformed", "PRIVATE_BAD_CID"),
                ("mismatched", "f" * 64),
            ):
                with self.subTest(portable=portable, response=name):
                    destination = self.root / f"real-create-{portable}-{name}"
                    output = io.StringIO()
                    args = argparse.Namespace(
                        project=destination, file=self.archive, identity=[self.identity]
                    )
                    with (
                        self.real_creation_dispatch(output=response) as (
                            calls,
                            sentinel,
                        ),
                        contextlib.redirect_stdout(output),
                        contextlib.redirect_stderr(output),
                    ):
                        if name == "valid":
                            backups.restore_backup(box, args)
                        else:
                            with self.assertRaises(crypto.BackupError) as error:
                                backups.restore_backup(box, args)
                            self.assertFalse(
                                "PRIVATE" in str(error.exception),
                                "Native response reached the public error",
                            )
                    self.assertFalse(
                        sentinel in output.getvalue(),
                        "Native warning reached the console",
                    )
                    self.assertTrue(
                        calls and all(calls),
                        "Native constructor IO was not captured in private memory",
                    )
                    if name == "valid":
                        _, record = box.find_workspace(destination)
                        self.assertNotIn("restore_incomplete", record)
                        self.assertEqual(
                            (destination / "unfinished.txt").read_text(),
                            "uncommitted source edits",
                        )
                    else:
                        records = [
                            box.read_json(path)
                            for path in box.workspace_paths()
                            if box.read_json(path)["project"] == str(destination)
                        ]
                        self.assertTrue(
                            not records
                            or all(
                                record.get("restore_incomplete") for record in records
                            )
                        )

    def test_real_restore_creation_independently_verifies_named_container(self):
        self.backup(portable=True)
        for shape in (
            "null",
            "empty",
            "multiple",
            "missing-id",
            "wrong-id",
            "wrong-name",
            "foreign-label",
            "running",
            "paused",
            "invalid-state",
            "wrong-project-source",
            "wrong-project-type",
            "wrong-home-source",
            "wrong-home-type",
            "duplicate-home",
            "wrong-image",
        ):
            with self.subTest(shape=shape):
                destination = self.root / ("verify-created-" + shape)
                injected = []

                def commands(box_arg, *args, **kwargs):
                    result = self.podman(box_arg, *args, **kwargs)
                    if (
                        args[:2] == ("container", "inspect")
                        and args[2] != self.data["container"]
                        and not injected
                    ):
                        injected.append(True)
                        record = json.loads(result.stdout)[0]
                        if shape == "missing-id":
                            record.pop("Id")
                        elif shape == "wrong-id":
                            record["Id"] = "d" * 64
                        elif shape == "wrong-name":
                            record["Name"] = "foreign-container"
                        elif shape == "foreign-label":
                            record["Config"]["Labels"] = {box.LABEL: "f" * 32}
                        elif shape in ("running", "paused"):
                            record["State"][shape.title()] = True
                        elif shape == "invalid-state":
                            record["State"]["Status"] = "running"
                        elif shape.startswith("wrong-project"):
                            record["Mounts"][0][
                                "Source" if shape.endswith("source") else "Type"
                            ] = "/foreign" if shape.endswith("source") else "volume"
                        elif shape.startswith("wrong-home"):
                            record["Mounts"][1][
                                "Source" if shape.endswith("source") else "Type"
                            ] = "/foreign" if shape.endswith("source") else "bind"
                        elif shape == "duplicate-home":
                            record["Mounts"].append(dict(record["Mounts"][1]))
                        elif shape == "wrong-image":
                            record["Image"] = "d" * 64
                        payload = (
                            None
                            if shape == "null"
                            else []
                            if shape == "empty"
                            else [record, {}]
                            if shape == "multiple"
                            else [record]
                        )
                        return subprocess.CompletedProcess(
                            args, 0, json.dumps(payload), ""
                        )
                    return result

                output = io.StringIO()
                args = argparse.Namespace(
                    project=destination, file=self.archive, identity=[self.identity]
                )
                with (
                    self.real_creation_dispatch() as (calls, sentinel),
                    patch.object(backups, "command", side_effect=commands),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(output),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    backups.restore_backup(box, args)
                self.assertEqual(injected, [True])
                self.assertFalse(
                    sentinel in output.getvalue(), "Native warning reached the console"
                )
                self.assertFalse(
                    "PRIVATE" in str(error.exception),
                    "Native response reached the public error",
                )
                self.assertTrue(calls and all(calls))
                records = [
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                ]
                self.assertTrue(
                    not records
                    or all(record.get("restore_incomplete") for record in records)
                )

    def test_cleanup_failed_inspection_is_not_inferred_absent(self):
        for kind in ("container", "volume"):
            with self.subTest(kind=kind):
                calls = []

                def commands(box_arg, *args, **kwargs):
                    calls.append(args)
                    if args[1] == "exists":
                        return subprocess.CompletedProcess(
                            args, 0 if len(calls) == 1 else 1, "", ""
                        )
                    return subprocess.CompletedProcess(args, 125, "PRIVATE_QUERY", "")

                with patch.object(backups, "command", side_effect=commands):
                    self.assertFalse(
                        backups.remove_restore_resource(box, kind, self.data)
                    )
                self.assertEqual([args[1] for args in calls], ["exists", "inspect"])

    def test_failed_image_load_retains_attempted_shared_image_identity(self):
        self.backup()
        primary = crypto.BackupError("Synthetic image load failed.")
        original = self.decrypt

        def decrypt(path, private, consumer):
            if path.name == "image.age":
                record = box.read_json(
                    next(path for path in box.workspace_paths() if path != self.path)
                )
                self.assertEqual(
                    record["restore_attempted_resources"], ["volume", "image"]
                )
                self.assertTrue(record["restore_incomplete"])
                raise primary
            return original(path, private, consumer)

        self.operations.clear()
        with (
            patch.object(crypto, "decrypt_stream", side_effect=decrypt),
            self.assertRaises(crypto.BackupError) as error,
        ):
            self.restore(self.root / "failed-image-load")
        self.assertIs(error.exception, primary)
        record = box.read_json(
            next(path for path in box.workspace_paths() if path != self.path)
        )
        self.assertEqual(record["image"], self.snapshot_image)
        self.assertEqual(record["restore_attempted_resources"], ["volume", "image"])
        self.assertTrue(record["restore_incomplete"])
        self.assertFalse(any(args[:2] == ("image", "rm") for args in self.operations))

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

    def test_initial_journal_barrier_failure_retains_blocked_recovery_before_resources(
        self,
    ):
        self.backup(portable=True)
        original = os.fsync
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                destination = self.root / ("journal-sync-" + kind)
                self.operations.clear()

                def sync(descriptor):
                    is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    if is_directory == (kind == "directory"):
                        raise OSError("PRIVATE_SYNC_ERROR")
                    original(descriptor)

                with (
                    patch.object(backups.os, "fsync", side_effect=sync),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertIn("durability", str(error.exception).lower())
                self.assertNotIn("PRIVATE", str(error.exception))
                records = [
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                ]
                self.assertEqual(len(records), 1)
                self.assertTrue(records[0]["restore_incomplete"])
                self.assertTrue(Path(records[0]["restore_staging_project"]).is_dir())
                self.assertFalse(destination.exists())
                self.assertFalse(
                    any(args[:2] == ("volume", "create") for args in self.operations)
                )

    def test_activation_and_completion_barrier_failures_keep_blocked_state(self):
        self.backup(portable=True)
        original = os.fsync
        for barrier in (
            "activation-parent",
            "session-file",
            "completion-file",
            "completion-parent",
        ):
            with self.subTest(barrier=barrier):
                destination = self.root / barrier
                failed = []

                def sync(descriptor):
                    info = os.fstat(descriptor)
                    location = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                    hit = (
                        barrier == "activation-parent"
                        and stat.S_ISDIR(info.st_mode)
                        and location == self.root
                        and destination.exists()
                    )
                    if barrier in ("session-file", "completion-file") and stat.S_ISREG(
                        info.st_mode
                    ):
                        try:
                            value = json.loads(
                                os.pread(descriptor, crypto.MAX_SMALL, 0)
                            )
                        except (ValueError, OSError):
                            value = {}
                        if not isinstance(value, dict):
                            value = {}
                        hit = (
                            set(value) == {"sessions", "last"}
                            if barrier == "session-file"
                            else value.get("project") == str(destination)
                            and "restore_incomplete" not in value
                        )
                    if barrier == "completion-parent" and stat.S_ISDIR(info.st_mode):
                        workspace = location / "workspace.json"
                        if workspace.is_file():
                            value = box.read_json(workspace)
                            hit = (
                                value.get("project") == str(destination)
                                and "restore_incomplete" not in value
                            )
                    if hit and not failed:
                        failed.append(True)
                        raise OSError("PRIVATE_SYNC_ERROR")
                    original(descriptor)

                with (
                    patch.object(backups.os, "fsync", side_effect=sync),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertEqual(failed, [True])
                self.assertIn("durability", str(error.exception).lower())
                self.assertNotIn("PRIVATE", str(error.exception))
                record = next(
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                )
                self.assertTrue(record["restore_incomplete"])
                self.assertTrue(destination.is_dir())
                self.assertEqual(
                    (destination / box.MARKER).read_text().strip(), record["id"]
                )
                self.assertEqual(record["restore_attempted_resources"][0], "volume")

    def test_completion_cancellation_retains_recovery_when_rollback_is_uncertain(self):
        self.backup(portable=True)
        replace = os.replace
        sync_path = backups.sync_path
        source_before = {
            path: path.read_bytes()
            for path in (
                self.path,
                box.session_file(self.path),
                self.project / box.MARKER,
            )
        }
        for rollback in ("verified", "rename-failure", "barrier-failure"):
            with self.subTest(rollback=rollback):
                destination = self.root / ("cancel-" + rollback)
                cancellation = KeyboardInterrupt("Synthetic completion cancellation")
                state = {
                    "cancelled": False,
                    "rolled_back": False,
                    "rollback_failed": False,
                }
                published = []

                def replacing(source, target, **kwargs):
                    source_path, target_path = Path(source), Path(target)
                    rolling_back = (
                        state["cancelled"]
                        and source_path.name == "restore-blocked.json"
                    )
                    if rolling_back and rollback == "rename-failure":
                        state["rollback_failed"] = True
                        raise OSError("PRIVATE_ROLLBACK_RENAME")
                    result = replace(source, target, **kwargs)
                    if rolling_back:
                        state["rolled_back"] = True
                    if (
                        target_path.name == "workspace.json"
                        and target_path.is_file()
                        and not state["cancelled"]
                    ):
                        record = box.read_json(target_path)
                        if (
                            record.get("project") == str(destination)
                            and "restore_incomplete" not in record
                        ):
                            state["cancelled"] = True
                            published.append((target_path, record))
                            raise cancellation
                    return result

                def syncing(path, *, directory=False):
                    if (
                        rollback == "barrier-failure"
                        and state["rolled_back"]
                        and not state["rollback_failed"]
                        and directory
                        and Path(path) == published[0][0].parent
                    ):
                        state["rollback_failed"] = True
                        raise backups.DurabilityError(
                            "Synthetic rollback durability refusal"
                        )
                    return sync_path(path, directory=directory)

                self.operations.clear()
                with (
                    patch.object(backups.os, "replace", side_effect=replacing),
                    patch.object(backups, "sync_path", side_effect=syncing),
                    self.assertRaises(KeyboardInterrupt) as error,
                ):
                    self.restore(destination)
                self.assertIs(error.exception, cancellation)
                self.assertEqual(len(published), 1)
                record_path, created = published[0]
                if rollback == "verified":
                    self.assertFalse(destination.exists())
                    self.assertFalse(record_path.exists())
                    self.assertNotIn(created["home_volume"], self.volumes)
                    self.assertFalse(
                        any(
                            data["container"] == created["container"]
                            for data in self.created
                        )
                    )
                else:
                    self.assertTrue(state["rollback_failed"])
                    self.assertFalse(
                        any(
                            args[:2] in (("container", "rm"), ("volume", "rm"))
                            for args in self.operations
                        )
                    )
                    self.assertTrue(destination.is_dir())
                    self.assertEqual(
                        (destination / "unfinished.txt").read_text(),
                        "uncommitted source edits",
                    )
                    self.assertEqual(
                        (destination / box.MARKER).read_text().strip(), created["id"]
                    )
                    home = self.volumes[created["home_volume"]]
                    self.assertEqual(
                        (home / ".local/share/opencode/opencode.db").read_bytes(),
                        b"synthetic-private-database",
                    )
                    self.assertTrue(
                        any(
                            data["container"] == created["container"]
                            for data in self.created
                        )
                    )
                    retained = box.read_json(record_path)
                    self.assertTrue(retained["restore_incomplete"])
                    self.assertEqual(
                        retained["restore_attempted_resources"], ["volume", "container"]
                    )
                for path, content in source_before.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_backup_cleanup_failure_is_not_hidden_by_callers_handled_exception(self):
        ambient = RuntimeError("Synthetic already-handled caller failure")
        with patch.object(backups, "remove_capture_image", return_value=False):
            try:
                raise ambient
            except RuntimeError as handled:
                with self.assertRaises(crypto.BackupError) as error:
                    self.backup()
                self.assertIs(handled, ambient)
        self.assertIn("cleanup incomplete", str(error.exception).lower())
        self.assertTrue(self.archive.is_file())
        self.assertTrue(list((self.path.parent / "backup-captures").glob("*.json")))

    def test_each_payload_barrier_failure_retains_its_blocked_journal(self):
        self.backup(portable=True)
        original = self.decrypt
        for component in ("project.age", "home.age", "registry.age"):
            with self.subTest(component=component):
                destination = self.root / ("payload-" + component)

                def decrypt(path, private, consumer):
                    if path.name == component:
                        status = Path(
                            consumer[consumer.index("--durability-status") + 1]
                        )
                        self.assertEqual(status.read_text(), "pending\n")
                        status.write_text("failed\n")
                        raise crypto.BackupError(
                            "Synthetic consumer durability refusal."
                        )
                    return original(path, private, consumer)

                with (
                    patch.object(crypto, "decrypt_stream", side_effect=decrypt),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    self.restore(destination)
                self.assertIn("durability", str(error.exception).lower())
                record = next(
                    box.read_json(path)
                    for path in box.workspace_paths()
                    if box.read_json(path)["project"] == str(destination)
                )
                self.assertTrue(record["restore_incomplete"])
                self.assertTrue(Path(record["restore_staging_project"]).is_dir())
                self.assertFalse(destination.exists())

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


class DurableBackupStateTests(unittest.TestCase):
    def test_durability_status_refuses_links_public_modes_and_oversized_input(self):
        for kind in ("symlink", "hardlink", "public", "oversized"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                outside = root / "outside"
                outside.write_bytes(b"PRIVATE_STATUS_VALUE")
                status = root / "status"
                if kind == "symlink":
                    status.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, status)
                else:
                    status.write_bytes(b"pending\n" if kind == "public" else b"x" * 32)
                    status.chmod(0o644 if kind == "public" else 0o600)
                with self.assertRaises(crypto.BackupError) as error:
                    backups.read_durability_status(status)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(outside.read_bytes(), b"PRIVATE_STATUS_VALUE")

    def test_durable_json_syncs_new_file_before_publication_then_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace.json"
            events = []
            replace = os.replace
            fsync = os.fsync

            def replacing(source, destination, **kwargs):
                if Path(destination) == target:
                    events.append("publish")
                return replace(source, destination, **kwargs)

            def syncing(descriptor):
                events.append(
                    "directory"
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    else "file"
                )
                fsync(descriptor)

            with (
                patch.object(backups.os, "replace", side_effect=replacing),
                patch.object(backups.os, "fsync", side_effect=syncing),
            ):
                backups.durable_json(box, target, {"restore_incomplete": True})
            self.assertLess(events.index("file"), events.index("publish"))
            self.assertEqual(events[-1], "directory")
            self.assertEqual(box.read_json(target), {"restore_incomplete": True})

    def test_failed_json_file_barrier_never_replaces_prior_blocked_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace.json"
            box.atomic_json(target, {"restore_incomplete": True})
            before = target.read_bytes()
            with (
                patch.object(
                    backups.os, "fsync", side_effect=OSError("PRIVATE_SYNC_ERROR")
                ),
                self.assertRaises(crypto.BackupError) as error,
            ):
                backups.durable_json(box, target, {"complete": True})
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertEqual(target.read_bytes(), before)


class StagedMarkerTests(unittest.TestCase):
    def test_readonly_marker_is_atomically_replaced_by_unprivileged_child(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-marker-") as temporary:
            root = Path(temporary)
            uid, gid = (
                (65534, 65534) if os.geteuid() == 0 else (os.getuid(), os.getgid())
            )
            source, fresh = "a" * 32, "b" * 32
            code = (
                "import os,sys; from pathlib import Path; from oc_box import backups; "
                "os.chdir(sys.argv[1]); "
                "uid,gid=map(int,sys.argv[2:4]); "
                "os.setgroups([]) if os.geteuid()==0 else None; "
                "os.setgid(gid) if os.geteuid()==0 else None; "
                "os.setuid(uid) if os.geteuid()==0 else None; "
                "assert os.geteuid()!=0; "
                "backups.replace_project_marker(Path('.'),'.oc-box-id',sys.argv[4],sys.argv[5])"
            )
            env = {
                **crypto.environment(),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            }
            for mode in (0o400, 0o444):
                with self.subTest(mode=oct(mode)):
                    stage = root / str(mode)
                    stage.mkdir(mode=0o700)
                    marker = stage / ".oc-box-id"
                    marker.write_text(source + "\n")
                    marker.chmod(mode)
                    before = marker.stat().st_ino
                    if os.geteuid() == 0:
                        os.chown(stage, uid, gid)
                        os.chown(marker, uid, gid)
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            code,
                            str(stage),
                            str(uid),
                            str(gid),
                            source,
                            fresh,
                        ],
                        env=env,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stderr.decode(errors="replace")
                    )
                    self.assertEqual(marker.read_text(), fresh + "\n")
                    self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                    self.assertNotEqual(marker.stat().st_ino, before)
                    self.assertEqual(list(stage.iterdir()), [marker])

    def test_staged_marker_refuses_links_and_invalid_source_without_writes(self):
        for kind in ("symlink", "hardlink", "wrong-id", "oversized"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / "stage"
                stage.mkdir(mode=0o700)
                outside = root / "outside"
                outside.write_text("a" * 32 + "\n")
                marker = stage / ".oc-box-id"
                if kind == "symlink":
                    marker.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, marker)
                else:
                    marker.write_text("wrong" if kind == "wrong-id" else "a" * 128)
                before = marker.read_bytes()
                with self.assertRaises(crypto.BackupError):
                    backups.replace_project_marker(
                        stage, ".oc-box-id", "a" * 32, "b" * 32
                    )
                self.assertEqual(marker.read_bytes(), before)
                self.assertEqual(outside.read_text(), "a" * 32 + "\n")
                self.assertEqual(list(stage.iterdir()), [marker])


class BackupCliTests(unittest.TestCase):
    def test_backup_command_timeout_and_nonzero_never_expose_private_output(self):
        for outcome in ("timeout", "nonzero"):
            with self.subTest(outcome=outcome):

                def run(arguments, **kwargs):
                    kwargs["stderr"].write(b"PRIVATE_TOOL_STDERR")
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(
                            arguments, 1, stderr=b"PRIVATE_TOOL_STDERR"
                        )
                    return subprocess.CompletedProcess(arguments, 125)

                with (
                    patch.object(backups.subprocess, "run", side_effect=run),
                    self.assertRaises(crypto.BackupError) as error,
                ):
                    backups.command(box, "volume", "create", "synthetic-only")
                self.assertNotIn("PRIVATE", str(error.exception))

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
