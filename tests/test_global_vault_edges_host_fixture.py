"""Bounded offline safety/process checks. NEVER execute a Podman lifecycle."""

import io
import importlib.util
import hashlib
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, session_terminal, vault
from tools import test_global_vault_edges_podman as edge

fixture = edge.fixture


class GlobalVaultEdgesHostFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gve-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / (fixture.PREFIX + "edges-unit")
        self.root.mkdir(mode=0o700)
        self.runtime = self.parent / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.binary = Path(sys.executable).resolve()
        self.boot = b"00000000-0000-0000-0000-000000000001\n"
        self.source = {
            "checkout": "/synthetic/checkout",
            "version": "1.6.45+g" + "a" * 7,
            "commit": "a" * 40,
            "commit_count": 74,
            "dirty": False,
        }
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "host-home"),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "OC_BOX_CONTAINER": "0",
            "PERSONAL_GITHUB_TOKEN": "synthetic-unrelated-secret",
            "CONTAINER_HOST": "ssh://foreign",
            "CONTAINERS_CONF_OVERRIDE": "/foreign/selector",
            "OPENCODE_AUTH_CONTENT": "synthetic-unrelated-auth",
        }
        self.enter(patch.dict(os.environ, self.environment, clear=True))
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        # Hard safety net: native/engine calls require an explicit unit boundary.
        self.engine = self.enter(
            patch.object(box, "run", side_effect=AssertionError("engine forbidden"))
        )
        self.enter(patch.object(fixture, "system_boot_id", return_value=self.boot))
        self.same_source = self.enter(
            patch.object(edge.interactive, "require_same_source")
        )
        self.environment.update(
            {
                "PATH": str(self.root / "bin") + ":" + os.defpath,
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "CONTAINERS_STORAGE_CONF": str(
                    self.root / "config/containers/storage.conf"
                ),
                "CONTAINERS_CONF": str(self.root / "config/containers/containers.conf"),
            }
        )
        self.environment.pop("CONTAINERS_CONF_OVERRIDE")
        self.environment.pop("CONTAINER_HOST")
        os.environ.clear()
        os.environ.update(self.environment)
        fixture.prepare_root(self.root, "tumbleweed")
        fixture.prepare_podman_cli(self.root, self.binary)  # File-only ELF checks.
        for name in ("config", "data", "state", "cache"):
            (self.root / ("host-" + name + "-2")).mkdir(mode=0o700)
        fixture.write_json_new(self.root / "executables.json", {"2": str(self.binary)})
        wrapper = self.root / "bin/opencode"
        wrapper.write_text(fixture.native_wrapper_source())
        wrapper.chmod(0o700)
        fixture.write_json_new(
            self.root / "edges.json",
            {
                "format": edge.FORMAT,
                "snapshot_schema": 2,
                "home_manifest_contract": "oc-box-home-topology-v2",
                "cases": list(edge.CASES),
                "source": self.source,
                "distro": "tumbleweed",
                "native": {
                    "path": str(self.binary),
                    "sha256": edge.interactive.file_hash(self.binary),
                    "version": "2.2.6",
                    "interpreter": sys.executable,
                },
            },
        )
        fixture.write_json_new(
            self.root / "config/opencode/opencode.json", edge.provider_free_config()
        )
        (self.root / "podman-tmp/alive").write_bytes(self.boot)

    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def record(self, name="source-a"):
        with patch.dict(os.environ, fixture.isolated_env(self.root), clear=True):
            path, data = box.create_workspace(self.root / name, "tumbleweed")
            data.update(opencode_version="2.2.6", ready=True)
            box.atomic_json(path, data)
        return path, data

    def args(self, **overrides):
        result = {"work-dir": self.parent, "opencode-v2": self.binary}
        result.update(overrides)
        return [
            item for name, value in result.items() for item in ("--" + name, str(value))
        ]

    def report(self, case):
        return fixture.private_json(self.root, "edges-" + case + "-report.json")

    def cipher_evidence(self):
        target = vault.vault_path()
        vault.private_directory(target.parent)
        target.write_bytes(b"synthetic-private-ciphertext-unit-evidence\n")
        return target.read_bytes()

    def snapshot(self, **fields):
        return {
            "snapshot_schema": 2,
            "home_manifest_contract": "oc-box-home-topology-v2",
            **fields,
        }

    def home_digest(self, home, *, metadata=None):
        # Execute only the read-only scanner against private synthetic files.
        # Metadata overrides let unprivileged runs exercise numeric ownership.
        prelude = ""
        if metadata is not None:
            prelude = (
                "import pathlib,os\n_original_lstat=pathlib.Path.lstat\n"
                + "_overrides="
                + repr(metadata)
                + "\n"
                + "def _lstat(path,*args,**kwargs):\n"
                + " info=_original_lstat(path,*args,**kwargs)\n"
                + " values=list(info)\n"
                + " for index,value in _overrides.get(str(path),{}).items(): values[index]=value\n"
                + " return os.stat_result(values)\n"
                + "pathlib.Path.lstat=_lstat\n"
            )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", prelude + edge.HOME_MANIFEST, str(home)],
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, "private scanner failed")
        self.assertRegex(result.stdout.decode().strip(), r"^[0-9a-f]{64}$")
        return result.stdout.decode().strip()

    def test_home_topology_root_and_link_metadata_independent_mutations(self):
        home = self.root / "topology-home"
        home.mkdir(mode=0o700)
        target = home / "target"
        target.write_bytes(b"SYNTHETIC_TOPOLOGY_BYTES\n")
        link = home / "link"
        link.symlink_to("target")
        initial = self.home_digest(home)
        # stat_result's UID/GID/mode/nlink slots are independent expectations,
        # not derived from the scanner's serialization.
        for name, path, index, value in (
            ("root-uid", home, 4, home.lstat().st_uid + 1),
            ("root-gid", home, 5, home.lstat().st_gid + 1),
            ("root-mode", home, 0, home.lstat().st_mode ^ 0o010),
            ("root-nlink", home, 3, home.lstat().st_nlink + 1),
            ("link-uid", link, 4, link.lstat().st_uid + 1),
            ("link-gid", link, 5, link.lstat().st_gid + 1),
            ("link-mode", link, 0, link.lstat().st_mode ^ 0o010),
            ("link-nlink", link, 3, link.lstat().st_nlink + 1),
            ("file-nlink", target, 3, target.lstat().st_nlink + 1),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(
                    self.home_digest(home, metadata={str(path): {index: value}}),
                    initial,
                )
                self.assertEqual(self.home_digest(home), initial)

    def test_home_topology_digest_has_independent_exact_root_and_entry_contract(self):
        home = self.root / "exact-home"
        home.mkdir(mode=0o700)
        content = b"FIXED_HOME_BYTES\n"
        (home / "a").write_bytes(content)
        (home / "a").chmod(0o600)
        os.link(home / "a", home / "b")
        (home / "link").symlink_to("a")
        root_info, link_info = home.lstat(), (home / "link").lstat()
        fixed_file = [
            "file",
            0o600,
            os.getuid(),
            os.getgid(),
            2,
            len(content),
            hashlib.sha256(content).hexdigest(),
            "a",
        ]
        expected = {
            "contract": "oc-box-home-topology-v2",
            "entries": {
                ".": ["directory", 0o700, os.getuid(), os.getgid(), root_info.st_nlink],
                "a": fixed_file,
                "b": fixed_file,
                "link": [
                    "link",
                    0o777,
                    os.getuid(),
                    os.getgid(),
                    link_info.st_nlink,
                    "a",
                    "link",
                ],
            },
        }
        expected_digest = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(self.home_digest(home), expected_digest)

    def test_home_topology_hardlinked_symlinks_keep_lstat_heads_and_owners(self):
        home = self.root / "symlink-hardlinks"
        home.mkdir(mode=0o700)
        (home / "a").symlink_to("absent")
        (home / "c").symlink_to("absent")
        os.link(home / "a", home / "b", follow_symlinks=False)
        os.link(home / "c", home / "d", follow_symlinks=False)
        before = self.home_digest(home)
        (home / "b").unlink()
        (home / "d").unlink()
        os.link(home / "a", home / "d", follow_symlinks=False)
        os.link(home / "c", home / "b", follow_symlinks=False)
        self.assertTrue(all((home / name).is_symlink() for name in "abcd"))
        self.assertTrue(all((home / name).lstat().st_nlink == 2 for name in "abcd"))
        self.assertNotEqual(self.home_digest(home), before)

    def test_home_topology_hardlink_heads_detect_equal_content_and_count_regrouping(
        self,
    ):
        home = self.root / "hardlink-home"
        home.mkdir(mode=0o700)
        for name in ("a", "c"):
            (home / name).write_bytes(b"SAME_SYNTHETIC_BYTES\n")
        os.link(home / "a", home / "b")
        os.link(home / "c", home / "d")
        before = self.home_digest(home)
        # All paths still have two links and identical bytes/owners/modes.
        # Only the internal equivalence classes change: ab/cd -> ad/bc.
        (home / "b").unlink()
        (home / "d").unlink()
        os.link(home / "a", home / "d")
        os.link(home / "c", home / "b")
        self.assertTrue(all((home / name).lstat().st_nlink == 2 for name in "abcd"))
        self.assertNotEqual(self.home_digest(home), before)

    def test_home_topology_link_targets_are_not_followed_and_external_links_count(self):
        home = self.root / "link-home"
        home.mkdir(mode=0o700)
        outside = self.parent / "outside"
        outside.mkdir(mode=0o700)
        (outside / "data").write_bytes(b"OUTSIDE_SYNTHETIC_BYTES\n")
        (home / "external").symlink_to(outside, target_is_directory=True)
        (home / "dangling").symlink_to("absent")
        (home / "cycle").symlink_to(".", target_is_directory=True)
        target = home / "file"
        target.write_bytes(b"INSIDE_SYNTHETIC_BYTES\n")
        before = self.home_digest(home)
        (outside / "data").write_bytes(b"CHANGED_OUTSIDE_BYTES\n")
        self.assertEqual(self.home_digest(home), before)
        os.link(target, outside / "alias")
        self.assertNotEqual(self.home_digest(home), before)
        (outside / "alias").unlink()
        self.assertEqual(self.home_digest(home), before)

    def test_legacy_snapshot_inspection_is_explicit_and_never_extended_proof(self):
        legacy = {"homes": {"source-a": "a" * 64}, "ciphertext": "b" * 64}
        original = copy.deepcopy(legacy)
        with self.assertRaisesRegex(fixture.FixtureError, "snapshot-contract"):
            edge.protected_snapshot(legacy)
        inspected = edge.protected_snapshot(legacy, legacy_inspection=True)
        self.assertEqual(inspected, legacy)
        self.assertEqual(legacy, original)
        self.assertNotIn("snapshot_schema", inspected)
        self.assertNotIn("home_manifest_contract", inspected)
        for mutate in (
            lambda value: value.update(snapshot_schema=True),
            lambda value: value.update(snapshot_schema=1),
            lambda value: value.update(home_manifest_contract="legacy"),
        ):
            current = self.snapshot(**legacy)
            mutate(current)
            with (
                self.subTest(snapshot=current),
                self.assertRaisesRegex(fixture.FixtureError, "snapshot-contract"),
            ):
                edge.protected_snapshot(current)

    def test_legacy_fault_baseline_refuses_without_child_or_second_snapshot(self):
        self.record()
        self.cipher_evidence()
        legacy = {"ciphertext": "a" * 64, "homes": {"source-a": "b" * 64}}
        with (
            patch.object(edge, "checkpoint"),
            patch.object(edge, "install_cipher"),
            patch.object(edge, "stopped_snapshot", return_value=legacy) as snapshot,
            patch.object(edge, "drive_child") as child,
        ):
            with self.assertRaisesRegex(edge.CaseFailure, "snapshot-contract"):
                edge.failure_case(
                    self.root, "corrupt-vault", "start", "synthetic", sys.executable
                )
        snapshot.assert_called_once()
        child.assert_not_called()
        self.assertFalse(
            (self.root / "edges-corrupt-vault-start-baseline.json").exists()
        )
        self.assertEqual(self.report("corrupt-vault-start")["status"], "FAIL")
        self.assertEqual(
            legacy, {"ciphertext": "a" * 64, "homes": {"source-a": "b" * 64}}
        )

    def test_stopped_snapshot_binds_versioned_home_digest_without_weakening_source(
        self,
    ):
        _, record = self.record()
        self.cipher_evidence()
        with (
            patch.object(edge, "checkpoint"),
            patch.object(
                fixture, "snapshot_source", return_value={"db": "fixed-source"}
            ) as source,
            patch.object(
                fixture,
                "verify_owned_resources",
                return_value={
                    "Id": "fixed-container",
                    "State": {"StartedAt": "start", "FinishedAt": "stop"},
                },
            ),
            patch.object(
                box, "run", return_value=SimpleNamespace(stdout="c" * 64 + "\n")
            ) as engine,
        ):
            snapshot = edge.stopped_snapshot(self.root)
        self.assertEqual(snapshot["snapshot_schema"], 2)
        self.assertEqual(snapshot["home_manifest_contract"], "oc-box-home-topology-v2")
        self.assertEqual(snapshot["source"], {"db": "fixed-source"})
        self.assertEqual(snapshot["homes"], {"source-a": "c" * 64})
        source.assert_called_once()
        command = engine.call_args.args[0]
        self.assertEqual(command[:2], ["podman", "unshare"])
        self.assertEqual(command[-2], edge.HOME_MANIFEST)
        self.assertEqual(
            command[-1],
            str(
                self.root
                / "data/containers/storage/volumes"
                / record["home_volume"]
                / "_data"
            ),
        )
        self.assertIs(engine.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(engine.call_args.kwargs["private"])

    def test_host_guard_precedes_all_placement_probes_and_writes(self):
        with (
            patch.object(
                fixture, "host_guard", side_effect=fixture.FixtureError("host-only")
            ),
            patch.object(edge, "fresh_parent") as placement,
            patch.object(edge.interactive, "source_identity") as source,
            patch.object(edge.interactive, "native_binary") as native,
            patch.object(edge.tempfile, "mkdtemp") as create,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(edge.main(self.args()), 1)
        for operation in (placement, source, native, create):
            operation.assert_not_called()
        self.assertIn("host-only", output.getvalue())
        self.engine.assert_not_called()

    def test_fresh_parent_rejects_all_retained_families_and_renamed_markers(self):
        for prefix in (*edge.interactive.FIXTURE_PREFIXES,):
            with self.subTest(prefix=prefix):
                root = self.parent / (prefix + "retained")
                child = root / "nested"
                child.mkdir(parents=True)
                with self.assertRaisesRegex(
                    fixture.FixtureError, "fresh-parent-required"
                ):
                    edge.fresh_parent(child)
        for marker in (
            *edge.interactive.FIXTURE_MARKERS,
            "edges.json",
            "edges-result.json",
        ):
            with self.subTest(marker=marker):
                root = self.parent / ("renamed-" + marker)
                child = root / "nested"
                child.mkdir(parents=True)
                (root / marker).write_text("synthetic retained evidence")
                with self.assertRaisesRegex(
                    fixture.FixtureError, "fresh-parent-required"
                ):
                    edge.fresh_parent(child)
                self.assertEqual(
                    (root / marker).read_text(), "synthetic retained evidence"
                )
        alias = self.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-path"):
            edge.fresh_parent(alias)

    def test_both_parents_validated_before_source_or_native(self):
        for option in ("work-dir", "temp-dir"):
            with (
                self.subTest(option=option),
                patch.object(fixture, "host_guard"),
                patch.object(edge.interactive, "source_identity") as source,
                patch.object(edge.interactive, "native_binary") as native,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(edge.main(self.args(**{option: self.root})), 1)
                source.assert_not_called()
                native.assert_not_called()

    def test_all_family_renamed_report_only_parents_refuse_before_allocation(self):
        # Literal family evidence names, independent of production marker tables.
        markers = (
            "history-purpose.json",
            "history-result.json",
            "home-component.json",
            "home-ready.json",
            "home-mapping.json",
            "home-created-source.json",
            "home-created-destination.json",
            "home-run-original-report.json",
            "home-cleanup-original-report.json",
            "cleanup-purpose.json",
            "full-restore-result.json",
            "downgrade-result.json",
            "retry-result.json",
            "result.json",
            "seed-baseline.json",
            "interactive-start-original-report.json",
            "edges-corrupt-vault-start-baseline.json",
            "edges-populated-first-owner-report.json",
        )
        for marker in markers:
            retained = self.parent / ("renamed-" + marker)
            retained.mkdir()
            (retained / marker).write_bytes(b"UNCHANGED_SYNTHETIC_FAILED_EVIDENCE\n")
            child = retained / "nested"
            child.mkdir()
            for path in (retained, child):
                for option in ("work-dir", "temp-dir"):
                    with (
                        self.subTest(marker=marker, path=path.name, option=option),
                        patch.object(fixture, "host_guard"),
                        patch.object(edge.interactive, "source_identity") as source,
                        patch.object(edge.interactive, "native_binary") as native,
                        patch.object(edge.tempfile, "mkdtemp") as allocate,
                        patch("sys.stdout", new_callable=io.StringIO),
                    ):
                        self.assertEqual(edge.main(self.args(**{option: path})), 1)
                        source.assert_not_called()
                        native.assert_not_called()
                        allocate.assert_not_called()
                        self.assertEqual(
                            (retained / marker).read_bytes(),
                            b"UNCHANGED_SYNTHETIC_FAILED_EVIDENCE\n",
                        )
        self.engine.assert_not_called()

    def test_no_retained_selectors_and_explicit_native_required(self):
        for extra in (
            ("--resume",),
            ("--fixture", str(self.root)),
            ("--cleanup", str(self.root)),
            ("--diagnose", str(self.root)),
        ):
            with (
                self.subTest(extra=extra),
                patch("sys.stderr", new_callable=io.StringIO) as output,
            ):
                with self.assertRaises(SystemExit) as caught:
                    edge.parser().parse_args(self.args() + list(extra))
                self.assertEqual(caught.exception.code, 2)
                self.assertNotIn(str(self.root), output.getvalue())
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            edge.parser().parse_args(["--work-dir", str(self.parent)])

    def test_positive_environment_private_selectors_and_no_operator_credentials(self):
        with patch.dict(
            os.environ,
            {
                "CONTAINER_HOST": "ssh://foreign",
                "CONTAINERS_CONF_OVERRIDE": "/foreign/selector",
            },
        ):
            environment = fixture.isolated_env(self.root)
        for name in (
            "PERSONAL_GITHUB_TOKEN",
            "CONTAINER_HOST",
            "CONTAINERS_CONF_OVERRIDE",
            "OPENCODE_AUTH_CONTENT",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["HOME"], str(self.root / "host-home"))
        self.assertEqual(environment["OC_BOX_STATE_HOME"], str(self.root / "registry"))
        self.assertEqual(
            environment["CONTAINERS_CONF"],
            str(self.root / "config/containers/containers.conf"),
        )
        self.assertEqual(
            environment["CONTAINERS_STORAGE_CONF"],
            str(self.root / "config/containers/storage.conf"),
        )
        self.assertEqual(environment["PATH"].split(":")[0], str(self.root / "bin"))
        self.assertEqual(
            fixture.private_json(self.root, "config/oc-box/config.json")["pass_env"],
            [fixture.PASS_ENV],
        )

    def test_metadata_registry_and_source_guards_are_file_only(self):
        self.assertEqual(edge.validate_edges(self.root), self.root)
        path, data = self.record()
        fixture.record_ids(self.root)
        self.assertEqual(edge.validate_edges(self.root), self.root)
        for mutation, expected in (
            (
                {"credentials": {"mode": "env", "env": [], "vault": []}},
                "workspace-policy",
            ),
            ({"project": str(self.parent / "foreign")}, "workspace-allowlist"),
            ({"container": "foreign"}, "workspace-allowlist"),
            ({"home_volume": "foreign"}, "workspace-allowlist"),
        ):
            with self.subTest(mutation=mutation):
                box.atomic_json(path, {**data, **mutation})
                with self.assertRaisesRegex(fixture.FixtureError, expected):
                    edge.validate_edges(self.root)
        box.atomic_json(path, data)
        with patch.object(
            edge.interactive,
            "require_same_source",
            side_effect=fixture.FixtureError("source-changed"),
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                edge.checkpoint(self.root)
        self.engine.assert_not_called()

    def test_selectors_unchanged_and_old_results_refused_before_engine(self):
        selector = self.root / "bin/podman"
        original = selector.read_text()
        selector.write_text(original + "\n# altered\n")
        with self.assertRaisesRegex(fixture.FixtureError, "podman-selector"):
            edge.checkpoint(self.root)
        selector.write_text(original)
        fixture.write_json_new(self.root / "result.json", {"status": "retained"})
        with self.assertRaisesRegex(fixture.FixtureError, "fixture-only"):
            edge.checkpoint(self.root)
        self.engine.assert_not_called()

    def test_stale_missing_invalid_boot_refuses_before_retained_engine(self):
        self.record()
        fixture.record_ids(self.root)
        marker = self.root / "podman-tmp/alive"
        for content, expected in (
            (None, "runtime-boot-marker"),
            (b"", "runtime-boot-marker"),
            (b"invalid", "runtime-boot-marker"),
            (b"00000000-0000-0000-0000-000000000002\n", "runtime-boot-mismatch"),
        ):
            with (
                self.subTest(content=content),
                patch.object(fixture, "validate_store") as engine,
            ):
                if content is None:
                    marker.unlink()
                else:
                    marker.write_bytes(content)
                with self.assertRaisesRegex(fixture.FixtureError, expected):
                    edge.checkpoint(self.root)
                engine.assert_not_called()

    def test_store_refusal_aborts_before_native_vault_or_owner(self):
        with (
            patch.object(fixture, "host_guard"),
            patch.object(vault, "crypto"),
            patch.object(
                fixture,
                "validate_store",
                side_effect=fixture.FixtureError("private-store"),
            ),
            patch.object(fixture, "prepare_native_cli") as native,
            patch.object(edge, "prepare_ciphertexts") as cipher,
            patch.object(edge, "owner_case") as owner,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "private-store"):
                edge.run(self.root, self.binary)
        for operation in (native, cipher, owner):
            operation.assert_not_called()

    def test_resource_guards_preserve_full_helper_mount_label_volume_contract(self):
        _, data = self.record()
        volume = (
            self.root
            / "data/containers/storage/volumes"
            / data["home_volume"]
            / "_data"
        )
        volume.mkdir(parents=True)
        info = {
            "Name": data["container"],
            "State": {"Running": False},
            "HostConfig": {"PortBindings": {}},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": data["project"],
                    "Destination": data["project"],
                },
                {"Type": "volume", "Source": str(volume), "Destination": data["home"]},
            ],
        }
        with (
            patch.object(fixture.backups, "owned_container", return_value=info),
            patch.object(
                fixture.backups,
                "owned_volume",
                return_value={"Name": data["home_volume"], "Mountpoint": str(volume)},
            ),
        ):
            self.assertIs(
                fixture.verify_owned_resources(self.root, data, stopped=True), info
            )
            for mutation, expected in (
                ({"State": {"Running": True}}, "workspace-state"),
                ({"Name": "foreign"}, "resource-allowlist"),
                ({"HostConfig": {"PortBindings": {"80/tcp": [{}]}}}, "published-ports"),
                ({"Mounts": []}, "resource-mounts"),
            ):
                with (
                    self.subTest(expected=expected),
                    patch.object(
                        fixture.backups,
                        "owned_container",
                        return_value={**info, **mutation},
                    ),
                ):
                    with self.assertRaisesRegex(fixture.FixtureError, expected):
                        fixture.verify_owned_resources(self.root, data, stopped=True)

    def test_zero_entry_oracle_does_not_weaken_full_provider_contract(self):
        self.assertEqual(edge.hashes({}), dict.fromkeys(fixture.KNOWN))
        with self.assertRaisesRegex(fixture.FixtureError, "synthetic-contract"):
            fixture.expected_hashes({})
        with self.assertRaisesRegex(fixture.FixtureError, "oracle-contract"):
            edge.hashes({"FOREIGN_API_KEY": "synthetic"})
        self.assertNotIn("providers", edge.provider_free_config())
        self.assertNotIn("model", edge.provider_free_config())
        self.assertNotIn("mcp", edge.provider_free_config())

    def test_actual_global_filter_stays_intact_and_catalog_setting_is_supported(self):
        _, data = self.record()
        controls = (
            "OPENCODE_DISABLE_MODELS_FETCH",
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_DISABLE_CLAUDE_CODE",
            "OPENCODE_PURE",
        )
        scoped = {
            **edge.ambient_values(),
            **dict.fromkeys(controls, "1"),
            "LEMONADE_BASE_URL": "http://127.0.0.1:32123/api/v1",
        }
        with patch.dict(os.environ, scoped):
            actual = box.container_env(data)
        self.assertTrue(
            all(
                name not in actual
                for name in (*fixture.KNOWN, *controls, "OPENCODE_AUTH_CONTENT")
            )
        )
        self.assertEqual(
            actual["LEMONADE_BASE_URL"], "http://host.containers.internal:32123/api/v1"
        )
        self.assertEqual(actual["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        legacy = fixture.compat.legacy_credentials(scoped["OPENCODE_AUTH_CONTENT"])
        self.assertEqual(legacy[0]["integrationID"], "openai")
        self.assertEqual(legacy[0]["value"]["type"], "key")

    def test_owner_callback_removal_verifies_live_snapshot_twice_without_policy_edits(
        self,
    ):
        path, data = self.record()
        initial = edge.hashes(
            {fixture.KEY: "synthetic-vault-key", fixture.SIDE: "synthetic-vault-side"}
        )
        fixture.write_json_new(self.root / "oracle-hashes.json", {"populated": initial})
        calls = []

        def oracle(root, selected, connection, ident, expected):
            self.assertEqual(expected, initial)
            self.assertEqual(selected["id"], data["id"])
            self.assertEqual(ident, "ses_synthetic")
            self.assertNotEqual(os.environ[fixture.KEY], "synthetic-vault-key")
            calls.append("native-shell")
            return 314

        def request(method, target, value=None):
            if target == "/api/credential":
                return {"data": []}
            self.assertEqual(target, "/api/session/ses_synthetic")
            return {
                "data": {
                    "id": "ses_synthetic",
                    "location": {"directory": data["project"]},
                }
            }

        def main(arguments):
            self.assertEqual(arguments, ["start", "--project", data["project"]])
            return box.run_tui(["native", "--session", "ses_synthetic"])

        baseline = dict(os.environ)
        with (
            patch.object(fixture, "host_guard"),
            patch.object(edge, "checkpoint"),
            patch.object(fixture, "verify_owned_resources"),
            patch.object(
                edge.service,
                "live_connection",
                return_value={
                    "workspace": data["id"],
                    "password": "synthetic-private-control",
                },
            ),
            patch.object(fixture, "request_for", return_value=request),
            patch.object(edge, "terminal_oracle", side_effect=oracle),
            patch.object(
                edge,
                "install_cipher",
                side_effect=lambda *args: calls.append("encrypted-removal"),
            ) as install,
            patch.object(box, "main", side_effect=main),
            patch.object(box, "run", return_value=None),
        ):
            result = edge.owner_worker(self.root, edge.OWNERS[0])
        self.assertEqual(result["primary"], None)
        self.assertEqual(result["secondary"], [])
        self.assertTrue(result["checks"]["live_snapshot_after_removal"])
        self.assertEqual(calls, ["native-shell", "encrypted-removal", "native-shell"])
        install.assert_called_once_with(self.root, "removed")
        self.assertNotIn(
            "credentials", fixture.private_json(self.root, path.relative_to(self.root))
        )
        self.assertEqual(dict(os.environ), baseline)

    def test_individually_stable_terminal_pids_must_stay_same_across_removal(self):
        _, data = self.record()
        expected = edge.hashes(
            {fixture.KEY: "synthetic-key", fixture.SIDE: "synthetic-side"}
        )
        fixture.write_json_new(
            self.root / "oracle-hashes.json", {"populated": expected}
        )
        _, marker = edge.shell_oracle(expected, data, 314, "unit-salt")
        pids = iter((314, 314, 315, 315))
        observed = []

        def request(method, target, value=None):
            if target == "/api/info":
                pid = next(pids)
                observed.append(pid)
                return {"version": "2.2.6", "pid": pid}
            if target == "/api/credential" or target.endswith("/terminal"):
                return {"data": []}
            if target.endswith("/terminal/read"):
                return {"data": {"screen": {"text": marker.decode()}}}
            if target.endswith("/export"):
                return {"data": {"messages": []}}
            self.assertEqual(target, "/api/session/ses_synthetic")
            return {
                "data": {
                    "id": "ses_synthetic",
                    "location": {"directory": data["project"]},
                }
            }

        process = unittest.mock.Mock(returncode=0)
        process.poll.return_value = 0
        process.wait.return_value = 0
        with (
            patch.object(fixture, "host_guard"),
            patch.object(edge, "checkpoint"),
            patch.object(fixture, "verify_owned_resources"),
            patch.object(
                edge.service,
                "live_connection",
                return_value={
                    "workspace": data["id"],
                    "password": "synthetic-control",
                    "url": "http://127.0.0.1:12345",
                },
            ),
            patch.object(fixture, "request_for", return_value=request),
            patch.object(
                edge.service,
                "client_command",
                return_value=box.ContainerCommand(["synthetic-client"], {}),
            ),
            patch.object(edge, "install_cipher") as install,
            patch.object(edge.secrets, "token_hex", return_value="unit-salt"),
            patch.object(edge.subprocess, "Popen", return_value=process),
            patch.object(edge.select, "select", return_value=([0], [], [])),
            patch.object(edge.os, "read", return_value=b"\r\n" + marker + b"\r\n"),
            patch.object(edge.os, "write"),
            patch.object(
                box,
                "main",
                side_effect=lambda args: box.run_tui(
                    ["native", "--session", "ses_synthetic"]
                ),
            ),
        ):
            result = edge.owner_worker(self.root, edge.OWNERS[0])
        self.assertEqual(observed, [314, 314, 315, 315])
        self.assertEqual(result["primary"], "native-context")
        self.assertTrue(result["checks"]["native_shell"])
        self.assertNotIn("live_snapshot_after_removal", result["checks"])
        install.assert_called_once_with(self.root, "removed")
        self.engine.assert_not_called()

    def test_worker_detects_inserted_key_before_ambient_restoration(self):
        self.record()
        fixture.write_json_new(
            self.root / "oracle-hashes.json",
            {"removed": edge.hashes({fixture.SIDE: "synthetic-side"})},
        )

        def main(_):
            os.environ[fixture.SIDE] = "synthetic-accidentally-retained-vault-value"
            return 0

        baseline = dict(os.environ)
        with (
            patch.object(fixture, "host_guard"),
            patch.object(edge, "checkpoint"),
            patch.object(box, "main", side_effect=main),
        ):
            result = edge.owner_worker(self.root, edge.OWNERS[1])
        self.assertIn("host-environment-changed", result["secondary"])
        self.assertEqual(dict(os.environ), baseline)

    def test_production_terminal_creation_is_empty_env(self):
        requests = []

        def request(method, path, value=None):
            requests.append((method, path, value))
            return {"data": [] if method == "GET" else {"id": "pty_synthetic"}}

        with patch.object(session_terminal, "terminal_size", return_value=(80, 24)):
            result = session_terminal.choose_terminal(
                request, "ses_synthetic", "/synthetic/project"
            )
        self.assertEqual(result["id"], "pty_synthetic")
        self.assertEqual(
            requests[1][2],
            {
                "args": [],
                "cwd": "/synthetic/project",
                "title": "Terminal",
                "env": {},
                "size": {"cols": 80, "rows": 24},
            },
        )
        self.assertEqual(
            edge.terminal_source(), Path(session_terminal.__file__).read_text()
        )

    def test_oracle_requires_actual_output_and_server_context_not_command_echo(self):
        expected = edge.hashes({})
        data = {"project": str(self.root), "home": str(self.root / "host-home")}
        submitted, marker = edge.shell_oracle(
            expected, data, os.getpid(), "synthetic-command-only-salt"
        )
        self.assertNotIn(marker, submitted)
        self.assertFalse(edge.marker_seen(submitted, marker))
        self.assertFalse(edge.marker_seen(b"echoed-prefix" + marker + b"\r\n", marker))
        command = shlex.split(submitted.decode().strip())
        env = {"PATH": os.defpath, "HOME": data["home"]}
        result = subprocess.run(
            command, env=env, cwd=self.root, capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(edge.marker_seen(result.stdout, marker))
        result = subprocess.run(
            command,
            env={**env, fixture.KEY: "synthetic-wrong-client-value"},
            cwd=self.root,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(edge.marker_seen(result.stdout, marker))

    def test_scope_leak_guard_runs_before_restoration_can_hide_inserted_values(self):
        expected = dict(os.environ)
        with patch.object(
            credentials := edge.credentials, "has_unlocked_values", return_value=False
        ):
            edge.assert_host_scope(expected)
            os.environ[fixture.SIDE] = "synthetic-inserted-vault-value"
            with self.assertRaisesRegex(
                fixture.FixtureError, "host-environment-changed"
            ):
                edge.assert_host_scope(expected)
            os.environ.pop(fixture.SIDE)
        with patch.object(credentials, "has_unlocked_values", return_value=True):
            with self.assertRaisesRegex(fixture.FixtureError, "vault-cache-retained"):
                edge.assert_host_scope(expected)

    def test_actual_controlling_tty_masked_prompt_no_secret_in_command_or_result(self):
        password = "synthetic-typed-only-secret"
        digest = hashlib.sha256(password.encode()).hexdigest()
        bootstrap = (
            edge.TTY_BOOTSTRAP
            + "import hashlib,json,sys; sys.path.insert(0,sys.argv[1]+'/src'); from oc_box import vault; value=vault.hidden('Vault passphrase: '); print('OC_BOX_EDGES_CHILD:'+json.dumps({'match':hashlib.sha256(value.encode()).hexdigest()==sys.argv[2]}),flush=True)"
        )
        command = [sys.executable, "-I", "-B", "-c", bootstrap, str(edge.APP), digest]
        self.assertNotIn(password, repr(command))
        result = edge.drive_child(
            command,
            environment={"PATH": os.defpath, "HOME": str(self.root)},
            password=password,
            timeout=10,
        )
        self.assertEqual(result["value"], {"match": True})
        self.assertEqual(result["prompts"], 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn(password, repr(result))
        self.engine.assert_not_called()

    def test_pty_echo_enabled_unexpected_prompt_timeout_and_duplicate_prompt_fail_closed(
        self,
    ):
        script = "import time; print('Vault passphrase:  [0 chars, masked]',flush=True); time.sleep(20)"
        for password, expected in (
            ("synthetic-no-send", "prompt-echo-enabled"),
            (None, "unexpected-prompt"),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(fixture.FixtureError, expected),
            ):
                edge.drive_child(
                    [sys.executable, "-I", "-B", "-c", script],
                    environment={"PATH": os.defpath},
                    password=password,
                    timeout=3,
                )
        with self.assertRaisesRegex(fixture.FixtureError, "child-timeout"):
            edge.drive_child(
                [sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"],
                environment={"PATH": os.defpath},
                timeout=0.2,
            )
        with patch.object(edge.termios, "tcgetattr", return_value=[0, 0, 0, 0]):
            output = (
                b"Vault passphrase:  [0 chars, masked]\rVault passphrase: [accepted]\n"
            )
            self.assertFalse(edge.prompt_ready(0, output, True, "synthetic"))
            with self.assertRaisesRegex(fixture.FixtureError, "prompt-count"):
                edge.prompt_ready(
                    0,
                    output + b"Vault passphrase:  [0 chars, masked]",
                    True,
                    "synthetic",
                )

    def test_real_private_stdlib_venv_has_no_crypto_pip_or_system_site(self):
        python = edge.create_crypto_free_python(self.root)
        self.assertTrue(python.is_file())
        self.assertTrue(python.is_symlink())
        self.assertEqual(python.resolve(), Path(sys._base_executable).resolve())
        self.assertIn(
            "include-system-site-packages = false",
            (python.parent.parent / "pyvenv.cfg").read_text(),
        )
        self.assertTrue(python.is_relative_to(self.root))
        with self.assertRaisesRegex(fixture.FixtureError, "private-venv"):
            edge.create_crypto_free_python(self.root)
        self.engine.assert_not_called()

    def test_bundled_crypto_refuses_before_venv_creation(self):
        package = self.parent / "synthetic-package"
        (package / "vault-deps").mkdir(parents=True)
        with (
            patch.object(box, "PACKAGE", package),
            patch.object(edge.venv, "EnvBuilder") as create,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "bundled-crypto"):
                edge.create_crypto_free_python(self.root)
        create.assert_not_called()

    @unittest.skipUnless(
        importlib.util.find_spec("cryptography"),
        "parent needs optional crypto; run the documented uv test command",
    )
    def test_real_vault_refusal_matrix_uses_crypto_free_child_and_masked_wrong_password(
        self,
    ):
        no_crypto = edge.create_crypto_free_python(self.root)
        environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "host-home"),
            "XDG_DATA_HOME": str(self.root / "data"),
        }
        with patch.dict(os.environ, environment, clear=True):
            target = vault.vault_path()
            with vault.locked(target, create=True):
                vault.write_encrypted(
                    target, {}, "synthetic-correct-passphrase-only-in-memory"
                )
        valid = target.read_bytes()
        bootstrap = (
            edge.TTY_BOOTSTRAP
            + """import json,sys
sys.path.insert(0,sys.argv[1]+'/src')
from oc_box import vault
try:
    vault.select_all()
except vault.VaultError as error:
    text=str(error)
    kind='missing-crypto' if text.startswith('Vault support needs the optional cryptography dependency.') else 'corrupt-vault' if text.startswith('Unsupported or damaged vault format;') else 'wrong-passphrase' if text.startswith('Unable to unlock vault: incorrect passphrase') else 'unexpected'
    print('OC_BOX_EDGES_CHILD:'+json.dumps({'category':kind}),flush=True)
    raise SystemExit(1)
raise SystemExit(97)
"""
        )
        for fault in edge.FAULTS:
            with self.subTest(fault=fault):
                target.write_bytes(
                    b'{"invalid":"synthetic-corrupt-envelope"}'
                    if fault == "corrupt-vault"
                    else valid
                )
                before = target.read_bytes()
                python = (
                    sys.executable if fault == "wrong-passphrase" else str(no_crypto)
                )
                result = edge.drive_child(
                    [python, "-I", "-B", "-c", bootstrap, str(edge.APP)],
                    environment=environment,
                    password="synthetic-wrong-typed-passphrase"
                    if fault == "wrong-passphrase"
                    else None,
                    timeout=15,
                )
                self.assertEqual(result["exit_code"], 1)
                self.assertEqual(result["value"], {"category": fault})
                self.assertEqual(result["prompts"], int(fault == "wrong-passphrase"))
                self.assertEqual(target.read_bytes(), before)
        self.engine.assert_not_called()

    def test_populated_oracle_hashes_actual_shell_and_separate_server_environment(self):
        values = {
            fixture.KEY: "synthetic-test-owner-key",
            fixture.SIDE: "synthetic-test-owner-side",
        }
        environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "host-home"),
            **values,
        }
        server = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            data = {"project": str(self.root), "home": environment["HOME"]}
            submitted, marker = edge.shell_oracle(
                edge.hashes(values), data, server.pid, "synthetic-command-salt"
            )
            self.assertNotIn(marker, submitted)
            self.assertTrue(
                all(value.encode() not in submitted for value in values.values())
            )
            command = shlex.split(submitted.decode().strip())
            actual = subprocess.run(
                command, env=environment, cwd=self.root, capture_output=True, timeout=10
            )
            self.assertEqual(actual.returncode, 0)
            self.assertTrue(edge.marker_seen(actual.stdout, marker))
            wrong = subprocess.run(
                command,
                env={**environment, fixture.KEY: "synthetic-client-decoy"},
                cwd=self.root,
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertFalse(edge.marker_seen(wrong.stdout, marker))
        finally:
            edge.stop_child(server)

    def test_fault_matrix_actual_entrypoint_command_and_postfault_snapshot_order(self):
        _, data = self.record()
        cipher = self.cipher_evidence()
        for fault in edge.FAULTS:
            for action in ("start", "reset"):
                with self.subTest(fault=fault, action=action):
                    events = []
                    snapshot = self.snapshot(
                        ciphertext="a" * 64,
                        resources={"id": "synthetic"},
                        registry={"selection": "b" * 64},
                    )
                    result = {
                        "exit_code": 1,
                        "value": {
                            "exit_code": 1,
                            "genuine_missing_crypto": fault == "missing-crypto",
                        },
                        "prompts": int(fault == "wrong-passphrase"),
                        "refusals": [fault],
                    }

                    def drive(command, **kwargs):
                        events.append("entrypoint")
                        self.assertEqual(command[1:4], ["-I", "-B", "-c"])
                        self.assertIn("code=box.entrypoint()", command[4])
                        self.assertNotIn("unittest.mock", command[4])
                        self.assertNotIn("test_global_vault", command[4])
                        self.assertEqual(
                            command[-4:], ["oc-box", action, "--workspace", data["id"]]
                        )
                        self.assertNotIn("synthetic-wrong-passphrase", repr(command))
                        self.assertNotIn(
                            "synthetic-wrong-passphrase", repr(kwargs["environment"])
                        )
                        self.assertEqual(
                            kwargs["password"],
                            "synthetic-wrong-passphrase"
                            if fault == "wrong-passphrase"
                            else None,
                        )
                        return result

                    with (
                        patch.object(edge, "checkpoint"),
                        patch.object(
                            edge,
                            "install_cipher",
                            side_effect=lambda *args: events.append("fault"),
                        ),
                        patch.object(
                            edge,
                            "stopped_snapshot",
                            side_effect=lambda *args, **kwargs: (
                                events.append("snapshot") or snapshot
                            ),
                        ),
                        patch.object(edge, "drive_child", side_effect=drive),
                    ):
                        edge.failure_case(
                            self.root,
                            fault,
                            action,
                            "synthetic-wrong-passphrase",
                            Path("/synthetic/venv/bin/python"),
                        )
                    self.assertEqual(
                        events, ["fault", "snapshot", "entrypoint", "snapshot"]
                    )
                    report = self.report(fault + "-" + action)
                    self.assertEqual(report["status"], "PASS")
                    self.assertTrue(report["checks"]["state_preserved"])
                    self.assertEqual(
                        (
                            self.root
                            / ("edges-" + fault + "-" + action + "-ciphertext.json")
                        ).read_bytes(),
                        cipher,
                    )

    def test_any_nonzero_is_not_refusal_and_secondary_snapshot_failure_preserves_primary(
        self,
    ):
        self.record()
        self.cipher_evidence()
        before = self.snapshot(ciphertext="a" * 64)
        result = {
            "exit_code": 2,
            "value": {"exit_code": 2},
            "prompts": 0,
            "refusals": [],
        }
        with (
            patch.object(edge, "checkpoint"),
            patch.object(edge, "install_cipher") as install,
            patch.object(
                edge,
                "stopped_snapshot",
                side_effect=[before, fixture.FixtureError("workspace-state")],
            ),
            patch.object(edge, "drive_child", return_value=result),
        ):
            with self.assertRaisesRegex(
                edge.CaseFailure, "unexpected-child-result"
            ) as caught:
                edge.failure_case(
                    self.root, "corrupt-vault", "reset", "synthetic", sys.executable
                )
        install.assert_called_once()
        self.assertEqual(caught.exception.secondary, ["workspace-state"])
        report = self.report("corrupt-vault-reset")
        self.assertEqual(report["primary_category"], "unexpected-child-result")
        self.assertEqual(report["secondary_categories"], ["workspace-state"])

    def test_unexpected_lifecycle_mutation_retains_after_evidence_without_rollback(
        self,
    ):
        self.record()
        self.cipher_evidence()
        before = self.snapshot(ciphertext="a" * 64, resources={"container_id": "old"})
        after = {**before, "resources": {"container_id": "changed"}}
        result = {
            "exit_code": 1,
            "value": {"exit_code": 1, "genuine_missing_crypto": False},
            "prompts": 0,
            "refusals": ["corrupt-vault"],
        }
        with (
            patch.object(edge, "checkpoint"),
            patch.object(edge, "install_cipher") as install,
            patch.object(edge, "stopped_snapshot", side_effect=[before, after]),
            patch.object(edge, "drive_child", return_value=result),
        ):
            with self.assertRaisesRegex(edge.CaseFailure, "state-changed"):
                edge.failure_case(
                    self.root, "corrupt-vault", "start", "synthetic", sys.executable
                )
        install.assert_called_once()
        self.assertEqual(
            fixture.private_json(self.root, "edges-corrupt-vault-start-after.json"),
            after,
        )
        self.assertEqual(self.report("corrupt-vault-start")["status"], "FAIL")

    def test_snapshot_guard_precedes_all_resources_and_data_reads(self):
        with (
            patch.object(
                edge,
                "checkpoint",
                side_effect=fixture.FixtureError("runtime-boot-mismatch"),
            ),
            patch.object(fixture, "workspace_records") as registry,
            patch.object(fixture, "snapshot_source") as snapshot,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "runtime-boot-mismatch"):
                edge.stopped_snapshot(self.root)
        registry.assert_not_called()
        snapshot.assert_not_called()
        self.engine.assert_not_called()

    def test_exact_native_version_log_append_is_diagnostic_not_protected_state(self):
        self.record()
        self.cipher_evidence()
        log = self.root / "host-data-2" / edge.NATIVE_LOG
        log.parent.mkdir(parents=True)
        log.write_bytes(b"synthetic native startup metadata\n")
        before = self.snapshot(
            ciphertext="a" * 64,
            native={
                name: fixture.tree_digest(self.root / ("host-" + name + "-2"))
                for name in ("config", "data", "state", "cache")
            },
        )
        original = copy.deepcopy(before)
        with log.open("ab") as stream:
            stream.write(b"synthetic --version metadata\n")
        after = copy.deepcopy(before)
        after["native"]["data"] = fixture.tree_digest(self.root / "host-data-2")
        self.assertNotEqual(
            edge.native_log_digest(before), edge.native_log_digest(after)
        )
        self.assertEqual(
            edge.protected_snapshot(before), edge.protected_snapshot(after)
        )
        for kind in ("config", "state", "cache"):
            same_name_elsewhere = copy.deepcopy(after)
            same_name_elsewhere["native"][kind][edge.NATIVE_LOG] = "f" * 64
            self.assertNotEqual(
                edge.protected_snapshot(before),
                edge.protected_snapshot(same_name_elsewhere),
            )
        self.assertEqual(before, original)  # Projection never mutates evidence.
        edge.assert_native_log_clean(self.root, ("synthetic-forbidden-passphrase",))
        result = {
            "exit_code": 1,
            "value": {"exit_code": 1, "genuine_missing_crypto": False},
            "prompts": 0,
            "refusals": ["corrupt-vault"],
        }
        with (
            patch.object(edge, "checkpoint"),
            patch.object(edge, "install_cipher"),
            patch.object(edge, "stopped_snapshot", side_effect=[before, after]),
            patch.object(edge, "drive_child", return_value=result),
        ):
            edge.failure_case(
                self.root, "corrupt-vault", "start", "synthetic", sys.executable
            )
        self.assertEqual(self.report("corrupt-vault-start")["status"], "PASS")
        self.assertEqual(
            fixture.private_json(
                self.root, "edges-corrupt-vault-start-native-log.json"
            ),
            {
                "before_sha256": edge.native_log_digest(before),
                "after_sha256": edge.native_log_digest(after),
            },
        )

    def test_native_log_exception_never_waives_other_db_config_auth_cache_or_log_files(
        self,
    ):
        self.record()
        self.cipher_evidence()
        protected = (
            ("data", "opencode/oc-box-v2.db"),
            ("config", "opencode/opencode.json"),
            ("data", "opencode/auth.json"),
            ("cache", "cache.bin"),
            ("state", "state.bin"),
            ("data", edge.NATIVE_LOG + ".1"),
        )
        for case, (kind, relative) in zip(edge.CASES[len(edge.OWNERS) :], protected):
            fault, action = case.rsplit("-", 1)
            with self.subTest(kind=kind, relative=relative):
                before = self.snapshot(
                    ciphertext="a" * 64,
                    native={name: {} for name in ("config", "data", "state", "cache")},
                )
                before["native"]["data"][edge.NATIVE_LOG] = "b" * 64
                before["native"][kind][relative] = "c" * 64
                after = copy.deepcopy(before)
                after["native"]["data"][edge.NATIVE_LOG] = "d" * 64
                after["native"][kind][relative] = "e" * 64
                result = {
                    "exit_code": 1,
                    "value": {
                        "exit_code": 1,
                        "genuine_missing_crypto": fault == "missing-crypto",
                    },
                    "prompts": int(fault == "wrong-passphrase"),
                    "refusals": [fault],
                }
                with (
                    patch.object(edge, "checkpoint"),
                    patch.object(edge, "install_cipher"),
                    patch.object(edge, "stopped_snapshot", side_effect=[before, after]),
                    patch.object(edge, "drive_child", return_value=result),
                ):
                    with self.assertRaisesRegex(edge.CaseFailure, "state-changed"):
                        edge.failure_case(
                            self.root, fault, action, "synthetic", sys.executable
                        )
                self.assertEqual(self.report(case)["primary_category"], "state-changed")

    def test_diagnostic_native_log_is_read_only_scanned_for_memory_only_secrets(self):
        log = self.root / "host-data-2" / edge.NATIVE_LOG
        log.parent.mkdir(parents=True)
        forbidden = (
            "synthetic-vault-value-never-in-report",
            "synthetic-passphrase-never-in-report",
        )
        for secret in forbidden:
            with self.subTest(
                secret_kind="vault" if secret == forbidden[0] else "passphrase"
            ):
                content = b"x" * (1048576 - 7) + secret.encode() + b"\n"
                log.write_bytes(content)
                before = edge.interactive.file_hash(log)
                with (
                    patch.object(edge, "checkpoint"),
                    patch.object(fixture, "workspace_records") as resources,
                ):
                    with self.assertRaisesRegex(
                        fixture.FixtureError, "native-log-secret"
                    ) as caught:
                        edge.stopped_snapshot(self.root, forbidden=forbidden)
                self.assertNotIn(secret, str(caught.exception))
                resources.assert_not_called()
                self.assertEqual(edge.interactive.file_hash(log), before)
        log.write_bytes(b"synthetic harmless native metadata\n")
        before = edge.interactive.file_hash(log)
        edge.assert_native_log_clean(self.root, forbidden)
        self.assertEqual(edge.interactive.file_hash(log), before)
        log.unlink()
        log.symlink_to(self.parent / "foreign-log")
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-path"):
            edge.assert_native_log_clean(self.root, forbidden)

    def test_namespace_refusal_precedes_fault_install_and_all_engine_queries(self):
        cipher_writer = edge.install_cipher
        for key, value in (
            ("HOME", "/synthetic/foreign-home"),
            ("OC_BOX_STATE_HOME", "/synthetic/foreign-registry"),
            ("CONTAINER_HOST", "ssh://foreign"),
            ("CONTAINERS_CONF_OVERRIDE", "/synthetic/foreign-selector"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {key: value}),
                patch.object(edge, "install_cipher") as install,
                patch.object(fixture, "validate_store") as engine,
            ):
                # Direct checkpoint and cipher writes both refuse a wrong namespace.
                with self.assertRaisesRegex(fixture.FixtureError, "fixture-only"):
                    edge.checkpoint(self.root)
                with self.assertRaisesRegex(fixture.FixtureError, "fixture-only"):
                    cipher_writer(self.root, "empty")
                engine.assert_not_called()
                with self.assertRaisesRegex(edge.CaseFailure, "fixture-only"):
                    edge.failure_case(
                        self.root,
                        "missing-crypto",
                        "reset",
                        "synthetic",
                        sys.executable,
                    )
                install.assert_not_called()
            # Each independent matrix iteration needs an unclaimed report slot.
            (self.root / "edges-missing-crypto-reset-report.json").unlink()

    def test_private_home_manifest_detects_db_config_auth_sentinels_not_mtime_only(
        self,
    ):
        home = self.root / "manifest-home"
        files = (
            ".local/share/opencode/oc-box-v2.db",
            ".local/share/opencode/auth.json",
            ".config/opencode/opencode.json",
            "acceptance-home.txt",
        )
        for name in files:
            target = home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"SYNTHETIC_BYTE_SENTINEL\n")

        def digest():
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", edge.HOME_MANIFEST, str(home)],
                env={"PATH": os.defpath},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertRegex(result.stdout.decode().strip(), r"^[0-9a-f]{64}$")
            return result.stdout

        initial = digest()
        for name in files:
            with self.subTest(name=name):
                target = home / name
                target.write_bytes(b"CHANGED_SYNTHETIC_BYTES\n")
                self.assertNotEqual(digest(), initial)
                target.write_bytes(b"SYNTHETIC_BYTE_SENTINEL\n")
                os.utime(target, (1000000000, 1000000000))
                self.assertEqual(digest(), initial)

    def test_run_keeps_all_ten_cases_in_fresh_only_order(self):
        active = self.root / "synthetic-active-cipher"
        events = []

        def install(_, name):
            active.write_text(name)
            events.append("cipher-" + name)

        def owner(_, case, password, python):
            events.append(case)
            expected = dict(zip(edge.OWNERS, ("populated", "removed", "empty", None)))[
                case
            ]
            self.assertEqual(active.read_text() if active.exists() else None, expected)
            if case == edge.OWNERS[0]:
                active.write_text("removed")
            self.assertEqual(password is None, case == edge.OWNERS[3])

        def failure(root, fault, action, password, python, *, forbidden):
            events.append(fault + "-" + action)
            self.assertEqual(
                forbidden[:2], ("synthetic-vault-key", "synthetic-vault-side")
            )
            self.assertEqual(len(forbidden), 4)
            self.assertTrue(password in forbidden)
            self.assertTrue(
                all(value not in os.environ.values() for value in forbidden)
            )

        with (
            patch.object(fixture, "host_guard"),
            patch.object(vault, "crypto"),
            patch.object(fixture, "validate_store"),
            patch.object(fixture, "prepare_native_cli"),
            patch.object(edge, "checkpoint"),
            patch.object(
                edge,
                "create_crypto_free_python",
                return_value=Path("/synthetic/crypto-free/python"),
            ),
            patch.object(
                edge,
                "prepare_ciphertexts",
                return_value=("synthetic-vault-key", "synthetic-vault-side"),
            ),
            patch.object(edge, "install_cipher", side_effect=install),
            patch.object(edge, "owner_case", side_effect=owner),
            patch.object(vault, "vault_path", return_value=active),
            patch.object(
                edge,
                "failure_case",
                side_effect=failure,
            ),
        ):
            edge.run(self.root, self.binary)
        self.assertEqual(
            [name for name in events if name in edge.CASES], list(edge.CASES)
        )
        self.assertEqual(events.count("cipher-populated"), 1)
        self.assertEqual(events.count("cipher-empty"), 1)

    def test_missing_parent_crypto_is_configuration_refusal_before_engine(self):
        with (
            patch.object(fixture, "host_guard"),
            patch.object(
                vault,
                "crypto",
                side_effect=vault.VaultError("synthetic-private-diagnostic"),
            ),
            patch.object(fixture, "validate_store") as engine,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "parent-crypto-required"):
                edge.run(self.root, self.binary)
        engine.assert_not_called()

    def test_catalog_verdict_uses_one_post_teardown_snapshot_and_keeps_primary(self):
        for earlier in (None, "native-context"):
            with self.subTest(earlier=earlier):
                state = {"requests": 0, "unexpected": 0}
                server = unittest.mock.Mock(server_port=32123)
                worker = unittest.mock.Mock()
                worker.is_alive.return_value = False

                def pending_completes():
                    state.update(requests=1, unexpected=1)

                server.shutdown.side_effect = pending_completes
                before = set(self.parent.glob(fixture.PREFIX + "edges-*"))
                with (
                    patch.object(fixture, "host_guard"),
                    patch.object(
                        edge.interactive, "source_identity", return_value=self.source
                    ),
                    patch.object(
                        edge.interactive, "native_binary", return_value=self.binary
                    ),
                    patch.object(edge.shutil, "which", return_value=str(self.binary)),
                    patch.object(
                        edge, "empty_catalog", return_value=(server, worker, state)
                    ),
                    patch.object(
                        edge,
                        "run",
                        side_effect=fixture.FixtureError(earlier) if earlier else None,
                    ),
                    patch.object(
                        edge.interactive,
                        "require_same_source",
                        side_effect=lambda source: state.update(
                            requests=2, unexpected=2
                        ),
                    ),
                    patch("sys.stdout", new_callable=io.StringIO),
                ):
                    self.assertEqual(edge.main(self.args()), 1)
                created = set(self.parent.glob(fixture.PREFIX + "edges-*")) - before
                self.assertEqual(len(created), 1)
                report = fixture.private_json(created.pop(), "edges-result.json")
                self.assertEqual(report["status"], "FAIL")
                self.assertEqual(report["catalog"], {"requests": 1, "unexpected": 1})
                self.assertEqual(state, {"requests": 2, "unexpected": 2})
                self.assertEqual(
                    report["primary_category"], earlier or "catalog-request"
                )
                self.assertEqual(
                    report["secondary_categories"],
                    ["catalog-request"] if earlier else [],
                )
                server.shutdown.assert_called_once()
                server.server_close.assert_called_once()
                worker.join.assert_called_once_with(timeout=3)
        self.engine.assert_not_called()

    def test_catalog_close_with_idle_and_partial_header_clients_is_bounded_child(self):
        bootstrap = """import os,socket,sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/src']
from tools import test_global_vault_edges_podman as edge
server,worker,state=edge.empty_catalog()
idle=socket.create_connection(server.server_address,timeout=2)
partial=socket.create_connection(server.server_address,timeout=2)
partial.sendall(b'GET /api/v1/models HTTP/1.1\\r\\n')
server.shutdown()
server.server_close()
worker.join(timeout=3)
assert not worker.is_alive()
print('CATALOG_CLOSE_RETURNED',flush=True)
# This proves bounded close, not daemon-handler draining. Exit owns both clients.
os._exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(edge.APP)],
            env={
                "PATH": os.defpath,
                "HOME": str(self.root / "host-home"),
                "TMPDIR": str(self.root / "temporary"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=8,
            start_new_session=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"CATALOG_CLOSE_RETURNED")

    def test_owner_matrix_requires_every_oracle_check_and_stopped_resource(self):
        self.record()
        self.record("source-b")
        for case in edge.OWNERS:
            with self.subTest(case=case):
                checks = dict.fromkeys(
                    (
                        "launcher_zero",
                        "native_shell",
                        "global_default",
                        "host_environment_exact",
                        "vault_cache_empty",
                    ),
                    True,
                )
                if case == edge.OWNERS[0]:
                    checks["live_snapshot_after_removal"] = True
                if case == edge.OWNERS[3]:
                    checks.update(crypto_not_loaded=True, vault_absent=True)
                result = {
                    "exit_code": 0,
                    "value": {"checks": checks, "primary": None, "secondary": []},
                    "prompts": int(case != edge.OWNERS[3]),
                    "empty": case == edge.OWNERS[2],
                    "absent": case == edge.OWNERS[3],
                }
                with (
                    patch.object(edge, "checkpoint"),
                    patch.object(edge, "drive_child", return_value=result),
                    patch.object(fixture, "verify_owned_resources") as owned,
                ):
                    edge.owner_case(
                        self.root,
                        case,
                        "synthetic" if case != edge.OWNERS[3] else None,
                        sys.executable,
                    )
                self.assertTrue(owned.call_args.kwargs["stopped"])
                self.assertEqual(self.report(case)["status"], "PASS")

    def test_incomplete_owner_proof_does_not_pass(self):
        self.record()
        result = {
            "exit_code": 0,
            "value": {"checks": {}, "primary": None, "secondary": []},
        }
        with (
            patch.object(edge, "checkpoint"),
            patch.object(edge, "drive_child", return_value=result),
        ):
            with self.assertRaisesRegex(edge.CaseFailure, "child-protocol"):
                edge.owner_case(self.root, edge.OWNERS[0], "synthetic", sys.executable)
        self.assertEqual(self.report(edge.OWNERS[0])["status"], "FAIL")

    def test_unknown_cases_refuse_before_child_engine_or_fault_changes(self):
        with (
            patch.object(edge, "checkpoint") as checkpoint,
            patch.object(edge, "drive_child") as child,
            patch.object(edge, "install_cipher") as cipher,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "fixture-only"):
                edge.owner_case(self.root, "../foreign", None, sys.executable)
            for fault, action in (
                ("foreign", "start"),
                ("corrupt-vault", "../foreign"),
            ):
                with (
                    self.subTest(fault=fault, action=action),
                    self.assertRaisesRegex(fixture.FixtureError, "fixture-only"),
                ):
                    edge.failure_case(
                        self.root, fault, action, "synthetic", sys.executable
                    )
        checkpoint.assert_not_called()
        child.assert_not_called()
        cipher.assert_not_called()

    def test_reports_immutable_redacted_fixed_schema_and_separate_reporting_error(self):
        case = edge.OWNERS[0]
        edge.write_report(self.root, case, {"native_shell": True}, "terminal-oracle")
        original = (self.root / ("edges-" + case + "-report.json")).read_bytes()
        with self.assertRaises(edge.CaseFailure) as caught:
            edge.finish_case(self.root, case, {}, "owner-refused")
        self.assertEqual(str(caught.exception), "owner-refused")
        self.assertEqual(caught.exception.secondary, ["io-error"])
        self.assertEqual(
            (self.root / ("edges-" + case + "-report.json")).read_bytes(), original
        )
        for error in (
            RuntimeError("synthetic-private-output"),
            fixture.FixtureError("synthetic-private-output"),
            OSError("synthetic-private-output"),
            subprocess.CalledProcessError(1, ["synthetic-private-argv"]),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIn(edge.category(error), edge.CATEGORIES)
                self.assertNotIn("synthetic-private", edge.category(error))
        with self.assertRaisesRegex(fixture.FixtureError, "report-schema"):
            edge.write_report(
                self.root, edge.OWNERS[1], {"native_shell": "synthetic-secret"}
            )
        self.assertFalse((self.root / "result.json").exists())

    def test_child_reap_error_does_not_replace_primary_failure(self):
        primary = fixture.FixtureError("child-timeout")
        with patch.object(
            edge, "stop_child", side_effect=OSError("synthetic-private-cleanup-details")
        ):
            with self.assertRaises(edge.CaseFailure) as caught:
                edge.reap_child(object(), primary)
        self.assertEqual(str(caught.exception), "child-timeout")
        self.assertEqual(caught.exception.secondary, ["io-error"])


if __name__ == "__main__":
    unittest.main()
