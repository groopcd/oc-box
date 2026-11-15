"""Bounded stdlib checks on synthetic files only; no host/native/engine acceptance."""

import copy
import json
import os
from pathlib import Path
import tempfile
import signal
import builtins
import unittest
from unittest.mock import patch

from tools import diagnose_v2_stopped_restore as reader


class FileReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="file-reader-fixture-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "checked-source"
        self.source_id, self.full_id = "a" * 32, "b" * 32
        self.seed = "ses_filereaderseed"
        source_hashes = {"src/oc_box/workspace_identity.py": reader.IDENTITY}
        original = {
            "source": {
                "build": {"commit": reader.COMMIT, "commit_count": 123, "dirty": False},
                "sha256": source_hashes,
            },
            "native": {
                "version": "2.3.0",
                "path": "/synthetic/unmodified-opencode",
                "sha256": "2e0e4656521ca56e5ba16a00d48429958b8105b3fa6466710a4350e68759381f",
            },
        }
        raw_original = json.dumps(original).encode()
        original_hash = reader.hashlib.sha256(raw_original).hexdigest()
        companion = {
            "original_report_sha256": original_hash,
            "primary_failure": {"category": "stopped-restore"},
            "provenance": {"application": {"sha256": source_hashes}},
        }
        raw_companion = json.dumps(companion).encode()
        companion_hash = reader.hashlib.sha256(raw_companion).hexdigest()
        self.write("result.json", raw_original)
        self.write(".private-stage", raw_companion)
        os.link(
            self.root / ".private-stage", self.root / "external-lifecycle-result.json"
        )
        self.doc(
            "fixture.json",
            {
                "format": "oc-box-v2-only-lifecycle-host-acceptance",
                "version": 1,
                "projects": ["source-a", "restored-full", "restored-portable"],
                "distro": "tumbleweed",
            },
        )
        self.doc("native-profile.json", original["native"])
        self.doc("executables.json", {"2": original["native"]["path"]})
        self.write(
            "podman-tmp/alive", Path("/proc/sys/kernel/random/boot_id").read_bytes()
        )
        self.write(
            "config/containers/storage.conf",
            (
                '[storage]\ndriver = "overlay"\nrunroot = '
                + json.dumps(str(self.root / "podman-run"))
                + "\ngraphroot = "
                + json.dumps(str(self.root / "data/containers/storage"))
                + "\n"
            ).encode(),
        )
        self.write(
            "config/containers/containers.conf",
            (
                "[engine]\nremote = false\ntmp_dir = "
                + json.dumps(str(self.root / "podman-tmp"))
                + "\nstatic_dir = "
                + json.dumps(str(self.root / "data/libpod"))
                + "\nvolume_path = "
                + json.dumps(str(self.root / "data/containers/storage/volumes"))
                + '\nhooks_dir = []\n[containers]\nnetns = "private"\nenv = []\nmounts = []\ndevices = []\n'
            ).encode(),
        )
        self.doc(
            "fixture-ids.json",
            {"source-a": self.source_id, "restored-full": self.full_id},
        )
        self.records = {}
        for name, ident in (
            ("source-a", self.source_id),
            ("restored-full", self.full_id),
        ):
            (self.root / name).mkdir(mode=0o700)
            info = (self.root / name).stat()
            data = {
                "id": ident,
                "version": 2,
                "project": str(self.root / name),
                "home": str(self.root / "host-home"),
                "container": "oc-box-" + ident,
                "home_volume": "oc-box-home-" + ident,
                "distro": "tumbleweed",
                "opencode_version": "2.3.0",
                "container_project": str(self.root / "source-a"),
                "credentials": {
                    "mode": "selected",
                    "env": [],
                    "vault": ["ACCEPTANCE_API_KEY"],
                },
                "project_identity": [info.st_dev, info.st_ino],
            }
            if name == "restored-full":
                data["restored_from"] = {"workspace_id": self.source_id, "kind": "full"}
            self.records[name] = data
            self.doc(f"registry/workspaces/{ident}/workspace.json", data)
            self.doc(
                f"registry/workspaces/{ident}/sessions-v2.json",
                {"sessions": [], "last": self.seed},
            )
            self.write(f"{name}/.oc-box-id", ident.encode())
            (self.root / name / ".oc-box-id").chmod(0o644)
        self.doc(
            "seed-baseline.json",
            {
                "info": {
                    "id": self.seed,
                    "location": {"directory": str(self.root / "source-a")},
                }
            },
        )
        self.doc(
            f"registry/workspaces/{self.full_id}/recovered-registry/workspace.json",
            self.records["source-a"],
        )
        self.doc(
            f"registry/workspaces/{self.full_id}/recovered-registry/sessions-v2.json",
            {"sessions": [], "last": self.seed},
        )
        identity = self.source / "src/oc_box/workspace_identity.py"
        identity.parent.mkdir(parents=True, mode=0o700)
        identity.write_bytes(
            (
                Path(__file__).resolve().parents[1] / "src/oc_box/workspace_identity.py"
            ).read_bytes()
        )
        identity.chmod(0o600)
        exists = Path.exists
        self.patches = [
            patch.object(reader, "ROOT", self.root),
            patch.object(reader, "SOURCE", self.source),
            patch.object(reader, "ORIGINAL", original_hash),
            patch.object(reader, "COMPANION", companion_hash),
            patch.object(reader.os, "geteuid", return_value=1000),
            patch.dict(reader.os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(
                Path,
                "exists",
                lambda path: (
                    False
                    if str(path) in {"/.dockerenv", "/run/.containerenv"}
                    else exists(path)
                ),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def write(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)

    def doc(self, relative, value):
        self.write(relative, json.dumps(value).encode())

    def test_current_consistency_only_with_six_metadata_matches(self):
        result = reader.audit()
        for name in (
            "restored_version_matches",
            "fresh_workspace_id",
            "fresh_container_name",
            "fresh_home_volume_name",
            "internal_project_path_preserved",
            "restored_last_matches_seed",
        ):
            self.assertIs(result[name], True)
        self.assertIs(result["home_tree_comparison_recomputed"], False)
        self.assertIs(result["engine_or_native_cli_executed"], False)
        self.assertIs(result["volume_or_archive_read"], False)
        self.assertIn("not historical", result["scope"])

    def test_stale_boot_refuses_before_workspace_metadata(self):
        self.write("podman-tmp/alive", b"11111111-1111-1111-1111-111111111111\n")
        with self.assertRaisesRegex(reader.Refusal, "boot-marker-stale"):
            reader.audit()

    def test_changed_report_and_source_are_refused(self):
        for relative in (
            "result.json",
            "checked-source/src/oc_box/workspace_identity.py",
        ):
            path = self.root / relative
            old = path.read_bytes()
            self.write(relative, old + b"\n")
            with self.assertRaisesRegex(reader.Refusal, "report-or-source-hash"):
                reader.audit()
            self.write(relative, old)

    def test_symlink_and_nonprivate_workspace_metadata_are_refused(self):
        relative = f"registry/workspaces/{self.full_id}/workspace.json"
        path = self.root / relative
        raw = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaisesRegex(reader.Refusal, "file-mode"):
            reader.audit()
        path.unlink()
        target = self.root / "not-selected.json"
        target.write_bytes(raw)
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaisesRegex(reader.Refusal, "file-owner"):
            reader.audit()

    def test_missing_restored_selection_is_reported_without_repair(self):
        path = self.root / f"registry/workspaces/{self.full_id}/sessions-v2.json"
        path.unlink()
        self.assertIs(reader.audit()["restored_last_matches_seed"], False)
        self.assertFalse(path.exists())

    def test_wrong_physical_identity_and_unsafe_journal_refuse(self):
        bad = copy.deepcopy(self.records["restored-full"])
        bad["project_identity"][1] += 1
        self.doc(f"registry/workspaces/{self.full_id}/workspace.json", bad)
        with self.assertRaisesRegex(reader.Refusal, "physical-project-identity"):
            reader.audit()
        self.doc(
            "fixture-ids.json",
            {"source-a": self.source_id, "restored-full": "../foreign"},
        )
        with self.assertRaisesRegex(reader.Refusal, "journal-binding"):
            reader.audit()

    def test_disposable_selector_path_is_not_probed_or_repaired(self):
        self.doc("selector.json", {"interpreter": "/missing-disposable-interpreter"})
        self.assertIs(reader.audit()["restored_last_matches_seed"], True)
        self.assertEqual(
            json.loads((self.root / "selector.json").read_text()),
            {"interpreter": "/missing-disposable-interpreter"},
        )

    def test_fifo_is_refused_without_blocking_data_open(self):
        path = self.root / f"registry/workspaces/{self.full_id}/sessions-v2.json"
        path.unlink()
        os.mkfifo(path, 0o600)
        previous = signal.getsignal(signal.SIGALRM)

        def timeout(*args):
            raise TimeoutError("synthetic FIFO read must not block")

        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(1)
        try:
            with self.assertRaisesRegex(reader.Refusal, "file-owner"):
                reader.audit()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_physical_probe_uses_pinned_fd_not_reopened_absolute_path(self):
        def checked_exec(code, namespace):
            builtins.exec(code, namespace)
            actual = namespace["file_handle"]

            def forbidden(*args):
                raise AssertionError("absolute project probe must not run")

            namespace["same_project"] = forbidden

            def pinned_handle(fd):
                target = Path(os.readlink("/proc/self/fd/" + str(fd)))
                self.assertIn(
                    target, {self.root / "source-a", self.root / "restored-full"}
                )
                if target.name == "restored-full":
                    target.rename(self.root / "held-original-project")
                    target.mkdir(mode=0o700)
                    self.assertNotEqual(os.fstat(fd).st_ino, target.stat().st_ino)
                return actual(fd)

            namespace["file_handle"] = pinned_handle

        with patch.object(reader, "exec", checked_exec, create=True):
            self.assertIs(reader.audit()["restored_last_matches_seed"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
