"""Offline contracts for retained sharing-backup diagnosis, not host acceptance.

Only fresh synthetic files and mocked source/donor/crypto boundaries are used.
No test starts Podman, native OpenCode, age, a worker, or a retained fixture.
Actual host guards are exercised only on refusing inputs, never bypassed to
perform a host operation. The production modules use normal ``tools`` imports.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from tools import diagnose_sharing_backup as diagnostic
from tools import test_global_vault_podman as donor
from tools import test_session_sync_podman as history_donor


COMMIT = "b" * 40
HARNESS_COMMIT = "c" * 40
SENSITIVE = "SYNTHETIC_PRIVATE_ERROR_ENV_ARGV_AND_PAYLOAD"
NAMESPACE = "oc_box_share_" + "d" * 32
HOST_SCOPE = (
    "Synthetic external-host production sharing CLI/PTY, real rootless "
    "workspace/native tools; no real providers/projects or security certification."
)
PROBE_FIELDS = {
    "authenticated",
    "authentication_exit",
    "scanner_producer_exit",
    "scanner_exit",
    "scanner_category",
    "timed_out",
}


def probe_result():
    return {
        "authenticated": True,
        "authentication_exit": 0,
        "scanner_producer_exit": 0,
        "scanner_exit": 0,
        "scanner_category": "ok",
        "timed_out": False,
    }


class BackupError(Exception):
    pass


class OfflineCase(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="sharing-diagnostic-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.processes = self.enter(
            patch.object(
                subprocess, "run", side_effect=AssertionError("process forbidden")
            )
        )
        self.children = self.enter(
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("child forbidden")
            )
        )
        self.workers = self.enter(
            patch.object(
                threading, "Thread", side_effect=AssertionError("worker forbidden")
            )
        )

    def tearDown(self):
        self.processes.assert_not_called()
        self.children.assert_not_called()
        self.workers.assert_not_called()

    def enter(self, manager):
        return self.stack.enter_context(manager)

    def refused(self, category):
        return self.assertRaisesRegex(diagnostic.Refusal, "^" + category + "$")

    def arguments(self, *extra):
        return [
            "--source",
            "/synthetic/source",
            "--expect-commit",
            COMMIT,
            "--fixture",
            "/synthetic/fixture",
            "--recorded-harness-commit",
            HARNESS_COMMIT,
            "--temp-dir",
            "/synthetic/encrypted-temporary",
            *extra,
        ]

    def capture_main(
        self,
        arguments,
        *,
        history,
        probe=None,
        launch_isolated=True,
        launch_no_bytecode=True,
    ):
        output = io.StringIO()
        errors = io.StringIO()

        def helper(name, path):
            self.assertTrue(diagnostic.sys.dont_write_bytecode)
            self.assertRegex(
                diagnostic.sys.pycache_prefix or "",
                r"^/nonexistent-oc-box-diagnostic-bytecode-[0-9a-f]{32}$",
            )
            if Path(path).name == diagnostic.HISTORY:
                return history
            if Path(path).name == "sharing_backup_probe.py" and probe is not None:
                return probe
            raise AssertionError("unexpected helper import")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(output, "close"))
            stack.enter_context(patch.object(diagnostic.os, "dup", return_value=91))
            stack.enter_context(
                patch.object(diagnostic.os, "fdopen", return_value=output)
            )
            stack.enter_context(
                patch.object(diagnostic, "load_file", side_effect=helper)
            )
            flags = SimpleNamespace(
                **{
                    name: getattr(diagnostic.sys.flags, name)
                    for name in dir(diagnostic.sys.flags)
                    if not name.startswith("_")
                }
            )
            flags.isolated = launch_isolated
            flags.dont_write_bytecode = launch_no_bytecode
            stack.enter_context(patch.object(diagnostic.sys, "flags", flags))
            stack.enter_context(patch.object(diagnostic.sys, "pycache_prefix", None))
            stack.enter_context(
                patch.object(diagnostic.sys, "dont_write_bytecode", True)
            )
            stack.enter_context(patch.object(diagnostic.resource, "setrlimit"))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = diagnostic.main(arguments)
        raw = output.getvalue()
        self.assertNotIn(SENSITIVE, raw + errors.getvalue())
        self.assertNotIn(NAMESPACE, raw + errors.getvalue())
        result = json.loads(raw)
        self.assertEqual(result["format"], diagnostic.FORMAT)
        self.assertIn(result["status"], ("diagnostic-only", "diagnostic-refused"))
        self.assertIs(result["acceptance"], False)
        self.assertEqual(result["engine_commands"], 0)
        self.assertNotIn('"PASS"', raw)
        return code, result


class OptionsAndHostGuardTests(OfflineCase):
    def test_parser_errors_do_not_reflect_private_argv(self):
        for arguments in ([], self.arguments("--" + SENSITIVE), ["--source"]):
            errors = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(errors),
                self.assertRaises(SystemExit) as raised,
            ):
                diagnostic.parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(
                errors.getvalue(),
                "sharing backup diagnostic: category=invalid-options\n",
            )

    def test_main_host_guard_is_first_even_before_invalid_argument_parsing(self):
        history = SimpleNamespace(
            host_guard=Mock(side_effect=history_donor.Refusal("host-only")),
            fresh_parent=Mock(),
            load_source=Mock(),
            source_snapshot=Mock(),
        )
        with contextlib.ExitStack() as stack:
            boundaries = [
                stack.enter_context(patch.object(diagnostic, name))
                for name in (
                    "parser",
                    "validated_evidence",
                    "inspect_archive",
                    "private_file",
                    "digest",
                )
            ]
            creation = stack.enter_context(patch.object(tempfile, "mkdtemp"))
            for arguments in ([], ["--" + SENSITIVE], self.arguments()):
                with self.subTest(arguments=arguments):
                    code, result = self.capture_main(arguments, history=history)
                    self.assertEqual(code, 1)
                    self.assertEqual(result["category"], "host-only")
                    self.assertEqual(result["status"], "diagnostic-refused")
        self.assertEqual(history.host_guard.call_count, 3)
        history.fresh_parent.assert_not_called()
        history.load_source.assert_not_called()
        history.source_snapshot.assert_not_called()
        creation.assert_not_called()
        for boundary in boundaries:
            boundary.assert_not_called()

    def test_actual_donor_host_guards_refuse_root_and_each_container_marker(self):
        # Every row is negative. No real loader/host operation runs afterwards.
        for module in (history_donor, donor):
            for uid, container, marker in (
                (0, "0", None),
                (1000, "1", None),
                (1000, "0", "/.dockerenv"),
                (1000, "0", "/run/.containerenv"),
            ):
                with (
                    self.subTest(module=module.__name__, uid=uid, marker=marker),
                    patch.object(module.os, "geteuid", return_value=uid),
                    patch.dict(os.environ, {"OC_BOX_CONTAINER": container}, clear=True),
                    patch.object(
                        Path,
                        "exists",
                        autospec=True,
                        side_effect=lambda path: str(path) == marker,
                    ),
                    self.assertRaisesRegex(
                        (history_donor.Refusal, donor.FixtureError), "^host-only$"
                    ),
                ):
                    module.host_guard()

    def test_bad_launch_flags_refuse_without_source_access_and_never_enable_cache_writes(
        self,
    ):
        history = SimpleNamespace(
            host_guard=Mock(), fresh_parent=Mock(), load_source=Mock()
        )
        for isolated, no_bytecode in ((False, True), (True, False), (False, False)):
            with self.subTest(isolated=isolated, no_bytecode=no_bytecode):
                code, result = self.capture_main(
                    self.arguments(),
                    history=history,
                    launch_isolated=isolated,
                    launch_no_bytecode=no_bytecode,
                )
                self.assertEqual(code, 1)
                self.assertEqual(result["category"], "invalid-options")
        history.fresh_parent.assert_not_called()
        history.load_source.assert_not_called()

    def test_temporary_parent_refuses_any_git_checkout_ancestor_without_git_execution(
        self,
    ):
        for marker in ("directory", "worktree-file", "dangling-link"):
            checkout = self.parent / marker
            nested = checkout / "nested" / "temporary"
            nested.mkdir(parents=True)
            git = checkout / ".git"
            if marker == "directory":
                git.mkdir()
            elif marker == "worktree-file":
                git.write_text("gitdir: /synthetic/other-worktree\n")
            else:
                git.symlink_to(checkout / "absent")
            with self.subTest(marker=marker), self.refused("placement"):
                diagnostic.outside_checkouts(nested)
        diagnostic.outside_checkouts(self.parent)

    def test_category_has_only_fixed_public_values(self):
        errors = (
            (diagnostic.Refusal("archive-envelope"), "archive-envelope"),
            (donor.FixtureError("runtime-boot-mismatch"), "runtime-boot-stale"),
            (diagnostic.Refusal(SENSITIVE), "metadata"),
            (donor.FixtureError(SENSITIVE), "metadata"),
            (RuntimeError(SENSITIVE), "diagnostic-failed"),
            (OSError(SENSITIVE), "io-error"),
            (
                subprocess.CalledProcessError(1, [SENSITIVE], stderr=SENSITIVE),
                "diagnostic-failed",
            ),
            (KeyboardInterrupt(SENSITIVE), "interrupted"),
            (SystemExit(SENSITIVE), "interrupted"),
        )
        for error, expected in errors:
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(diagnostic.category(error), expected)
                self.assertIn(diagnostic.category(error), diagnostic.CATEGORIES)


class EvidenceCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = self.parent / "retained-synthetic"
        self.root.mkdir(mode=0o700)
        self.temporary = self.parent / "encrypted-temporary"
        self.temporary.mkdir(mode=0o700)
        self.source = {
            "id": "a" * 32,
            "project": str(self.root / "source-a"),
            "container_project": "/synthetic/original-project",
        }
        self.identity = {
            "commit": COMMIT,
            "commit_count": 100,
            "version": "1.7.71+g" + COMMIT[:7],
            "dirty": False,
            "sha256": {"src/oc_box/sharing.py": "d" * 64},
        }
        self.scanner = (
            Path(diagnostic.__file__).resolve().with_name("sharing_archive_scan.py")
        )
        self.blobs = {
            name: self.scanner.with_name(name).read_bytes()
            if name in (self.scanner.name, diagnostic.HISTORY)
            else ("synthetic original Git blob " + name).encode()
            for name in diagnostic.RECORDED_FILES
        }
        self.hashes = {
            name: hashlib.sha256(blob).hexdigest() for name, blob in self.blobs.items()
        }
        self.native = {
            "path": "/synthetic/native-v2",
            "version": "2.2.6",
            "sha256": "e" * 64,
        }
        self.purpose = {
            "format": "oc-box-sharing-host-v1",
            "root": str(self.root),
            "uid": os.getuid(),
            "source_identity": copy.deepcopy(self.identity),
            "harness_sha256": copy.deepcopy(self.hashes),
            "native": copy.deepcopy(self.native),
            "config_sha256": "f" * 64,
        }
        self.report = {
            "format": "oc-box-sharing-host-v1",
            "status": "FAIL",
            "stage": "backup-restore",
            "source_identity": copy.deepcopy(self.identity),
            "harness_sha256": copy.deepcopy(self.hashes),
            "native": {key: self.native[key] for key in ("version", "sha256")},
            "checks": {"reset_no_regrant": True},
            "primary_category": "archive-scan",
            "scope": HOST_SCOPE,
            "private_error_never_reported": SENSITIVE,
            "namespace_never_reported": NAMESPACE,
        }
        self.write_documents()
        for name in ("sharing-full.ocbox", "sharing-age-identity.txt"):
            path = self.root / name
            path.write_bytes(b"synthetic non-crypto bytes " + name.encode())
            path.chmod(0o600)
        self.records = {
            "source-a": (self.root / "metadata-a", self.source),
            "source-b": (self.root / "metadata-b", {"id": "b" * 32}),
        }
        self.f = SimpleNamespace(
            no_symlinks=donor.no_symlinks,
            safe_path=donor.safe_path,
            private_json=donor.private_json,
            validate_fixture=Mock(return_value=self.root),
            require_runtime_boot=Mock(),
            workspace_records=Mock(return_value=self.records),
            host_guard=Mock(),
            private_output=contextlib.nullcontext,
            isolated_env=Mock(return_value={"PATH": "/synthetic/no-executables"}),
            temporary_location=Mock(side_effect=self.temporary_location),
            crypto=SimpleNamespace(
                MAX_BACKUP=1024 * 1024,
                MAX_SMALL=65536,
                BackupError=BackupError,
                opened=Mock(side_effect=AssertionError("age forbidden")),
            ),
            backups=SimpleNamespace(validate_manifest=Mock()),
        )
        self.forbidden = []
        for name in (
            "validate_store",
            "validate_store_info",
            "verify_owned_resources",
            "inspect_resources",
            "check_native_version",
            "launcher",
            "restore",
            "unlock_vault",
            "prepare_podman_cli",
        ):
            boundary = Mock(side_effect=AssertionError(name + " forbidden"))
            setattr(self.f, name, boundary)
            self.forbidden.append(boundary)
        self.loaded = SimpleNamespace(
            source=self.parent / "source-checkout",
            identity=self.identity,
            fixture=self.f,
            box=SimpleNamespace(
                container_project=lambda source: source["container_project"],
                run=Mock(side_effect=AssertionError("engine forbidden")),
            ),
        )
        self.history = SimpleNamespace(
            host_guard=Mock(),
            fresh_parent=Mock(return_value=self.temporary),
            load_source=Mock(return_value=self.loaded),
            source_snapshot=Mock(return_value=copy.deepcopy(self.identity)),
            git=Mock(side_effect=self.git),
        )
        self.args = SimpleNamespace(
            fixture=self.root,
            source=self.loaded.source,
            expect_commit=COMMIT,
            recorded_harness_commit=HARNESS_COMMIT,
            temp_dir=self.temporary,
        )
        self.probe = SimpleNamespace(
            probe_component=Mock(side_effect=AssertionError("probe forbidden"))
        )

    def tearDown(self):
        super().tearDown()
        for boundary in self.forbidden:
            boundary.assert_not_called()
        self.loaded.box.run.assert_not_called()

    def write_documents(self):
        for name, value in (
            ("sharing-purpose.json", self.purpose),
            ("sharing-result.json", self.report),
        ):
            path = self.root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def git(self, root, *arguments):
        self.assertEqual(root, Path(diagnostic.__file__).resolve().parents[1])
        if arguments == ("rev-parse", "--is-shallow-repository"):
            return b"false\n"
        self.assertEqual(arguments[0], "show")
        prefix = HARNESS_COMMIT + ":tools/"
        self.assertTrue(arguments[1].startswith(prefix))
        return self.blobs[arguments[1][len(prefix) :]]

    @contextlib.contextmanager
    def temporary_location(self, root, parent):
        self.assertEqual(root, self.root)
        self.assertFalse(parent.is_relative_to(root))
        self.assertFalse(parent.is_relative_to(self.loaded.source))
        self.assertFalse(
            parent.is_relative_to(Path(diagnostic.__file__).resolve().parents[1])
        )
        yield

    def validate(self):
        return diagnostic.validated_evidence(self.args, self.history, self.loaded)

    def main_arguments(self):
        return [
            "--source",
            str(self.args.source),
            "--expect-commit",
            self.args.expect_commit,
            "--fixture",
            str(self.args.fixture),
            "--recorded-harness-commit",
            self.args.recorded_harness_commit,
            "--temp-dir",
            str(self.args.temp_dir),
        ]


class EvidenceValidationTests(EvidenceCase):
    def test_original_failed_report_is_file_only_and_pins_four_git_blobs(self):
        root, source, archive, identity, scanner, watched = self.validate()
        self.assertEqual(
            (root, source, scanner), (self.root, self.source, self.scanner)
        )
        self.assertEqual(archive, self.root / "sharing-full.ocbox")
        self.assertEqual(identity, self.root / "sharing-age-identity.txt")
        self.assertEqual(
            set(watched),
            {
                self.root / name
                for name in (
                    "sharing-full.ocbox",
                    "sharing-age-identity.txt",
                    "sharing-purpose.json",
                    "sharing-result.json",
                )
            },
        )
        self.f.validate_fixture.assert_called_once_with(
            self.root, allow_unrecorded=True
        )
        self.f.require_runtime_boot.assert_called_once_with(self.root)
        self.assertEqual(
            self.history.git.call_args_list[1:],
            [
                call(
                    Path(diagnostic.__file__).resolve().parents[1],
                    "show",
                    HARNESS_COMMIT + ":tools/" + name,
                )
                for name in diagnostic.RECORDED_FILES
            ],
        )
        self.assertEqual(len(diagnostic.RECORDED_FILES), 4)
        self.f.crypto.opened.assert_not_called()
        self.probe.probe_component.assert_not_called()

    def test_report_scope_status_stage_reset_and_later_evidence_refuse(self):
        mutations = (
            ("format", "foreign"),
            ("status", "PASS"),
            ("status", "INCOMPLETE"),
            ("stage", "complete"),
            ("stage", "sharing"),
            ("scope", "offline native rehearsal"),
            ("scope", None),
            ("source_identity", {**self.identity, "commit": "a" * 40}),
            ("checks", {}),
            ("checks", {"reset_no_regrant": 1}),
            ("checks", {"reset_no_regrant": False}),
            (
                "checks",
                {"reset_no_regrant": True, "backup_external_tree_excluded": False},
            ),
        )
        original = copy.deepcopy(self.report)
        for name, value in mutations:
            with self.subTest(name=name, value=value):
                self.report = {**copy.deepcopy(original), name: value}
                self.write_documents()
                with self.refused("purpose"):
                    self.validate()
        self.f.crypto.opened.assert_not_called()

    def test_purpose_pins_root_uid_and_source_identity(self):
        original = copy.deepcopy(self.purpose)
        for name, value in (
            ("format", "foreign"),
            ("root", str(self.parent / "other-root")),
            ("uid", os.getuid() + 1),
            ("source_identity", {**self.identity, "dirty": True}),
        ):
            with self.subTest(name=name):
                self.purpose = {**copy.deepcopy(original), name: value}
                self.write_documents()
                with self.refused("purpose"):
                    self.validate()

    def test_native_version_hash_and_identity_match_without_native_probe(self):
        originals = (copy.deepcopy(self.purpose), copy.deepcopy(self.report))
        for document, name, value in (
            ("purpose", "version", "1.19.5"),
            ("purpose", "sha256", "not-a-hash"),
            ("report", "version", "2.2.7"),
            ("report", "sha256", "a" * 64),
        ):
            with self.subTest(document=document, name=name):
                self.purpose, self.report = copy.deepcopy(originals)
                getattr(self, document)["native"][name] = value
                self.write_documents()
                with self.refused("purpose"):
                    self.validate()

    def test_each_recorded_harness_hash_is_required_and_no_extra_hashes(self):
        originals = (copy.deepcopy(self.purpose), copy.deepcopy(self.report))
        for document in ("purpose", "report"):
            for name in (*diagnostic.RECORDED_FILES, "extra.py"):
                with self.subTest(document=document, name=name):
                    self.purpose, self.report = copy.deepcopy(originals)
                    getattr(self, document)["harness_sha256"][name] = "0" * 64
                    self.write_documents()
                    with self.refused("purpose"):
                        self.validate()

    def test_changed_current_scanner_refuses_instead_of_using_new_filters(self):
        changed = dict(self.blobs)
        changed[self.scanner.name] = b"old scanner with different exact filters"
        self.blobs = changed
        hashes = {
            name: hashlib.sha256(blob).hexdigest() for name, blob in changed.items()
        }
        self.purpose["harness_sha256"] = hashes
        self.report["harness_sha256"] = hashes
        self.write_documents()
        with self.refused("scanner-changed"):
            self.validate()
        self.f.crypto.opened.assert_not_called()

    def test_changed_current_history_helper_refuses_before_key_or_age_reads(self):
        self.blobs[diagnostic.HISTORY] = b"different original history helper bytes"
        hashes = {
            name: hashlib.sha256(blob).hexdigest() for name, blob in self.blobs.items()
        }
        self.purpose["harness_sha256"] = hashes
        self.report["harness_sha256"] = hashes
        self.write_documents()
        with patch.object(diagnostic, "private_file") as keys:
            with self.refused("source-changed"):
                self.validate()
        keys.assert_not_called()
        self.f.crypto.opened.assert_not_called()

    def test_shallow_or_missing_recorded_git_history_never_opens_archive(self):
        for failure in (b"true\n", subprocess.CalledProcessError(128, [SENSITIVE])):
            with self.subTest(kind=type(failure).__name__):
                if isinstance(failure, bytes):
                    self.history.git.side_effect = None
                    self.history.git.return_value = failure
                    expected = diagnostic.Refusal
                else:
                    self.history.git.side_effect = failure
                    expected = subprocess.CalledProcessError
                with self.assertRaises(expected):
                    self.validate()
        self.f.crypto.opened.assert_not_called()

    def test_restore_directory_dangling_link_or_foreign_record_refuse(self):
        restore = self.root / "restored-full"
        for kind in ("directory", "dangling-link", "foreign-record"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    restore.mkdir()
                elif kind == "dangling-link":
                    restore.symlink_to(self.parent / "absent")
                else:
                    self.records["restored-full"] = (self.root / "foreign", {})
                try:
                    with self.refused("purpose"):
                        self.validate()
                finally:
                    if kind == "directory":
                        restore.rmdir()
                    elif kind == "dangling-link":
                        restore.unlink()
                    else:
                        self.records.pop("restored-full")

    def test_root_redirect_refuses_before_fixture_metadata_and_boot(self):
        link = self.parent / "redirect"
        link.symlink_to(self.root, target_is_directory=True)
        self.args.fixture = link
        with self.assertRaisesRegex(donor.FixtureError, "^unsafe-path$"):
            self.validate()
        self.f.validate_fixture.assert_not_called()
        self.f.require_runtime_boot.assert_not_called()

    def test_identity_privacy_or_link_refusal_never_constructs_age_or_probe(self):
        path = self.root / "sharing-age-identity.txt"
        original = path.read_bytes()
        for kind in ("public", "hardlink", "symlink"):
            second = self.root / "second-identity-name"
            with self.subTest(kind=kind):
                if kind == "public":
                    path.chmod(0o644)
                elif kind == "hardlink":
                    os.link(path, second)
                else:
                    path.rename(second)
                    path.symlink_to(second)
                try:
                    with self.assertRaises((diagnostic.Refusal, donor.FixtureError)):
                        self.validate()
                finally:
                    if kind == "hardlink":
                        second.unlink()
                    elif kind == "symlink":
                        path.unlink()
                        second.rename(path)
                    path.chmod(0o600)
                self.assertEqual(path.read_bytes(), original)
        self.f.crypto.opened.assert_not_called()
        self.probe.probe_component.assert_not_called()

    def test_fixture_inside_candidate_source_refuses_before_metadata(self):
        self.loaded.source = self.parent
        with self.refused("placement"):
            self.validate()
        self.f.validate_fixture.assert_not_called()

    def test_stale_boot_refuses_before_key_reads_probes_or_age_construction(self):
        events = []
        self.f.validate_fixture.side_effect = lambda *a, **k: events.append("metadata")

        def stale(root):
            events.append("boot")
            raise donor.FixtureError("runtime-boot-mismatch")

        self.f.require_runtime_boot.side_effect = stale
        with (
            patch.object(self.f, "private_json", wraps=self.f.private_json) as reads,
            patch.object(diagnostic, "private_file") as keys,
        ):
            code, result = self.capture_main(
                self.main_arguments(), history=self.history, probe=self.probe
            )
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "diagnostic-refused")
        self.assertEqual(result["category"], "runtime-boot-stale")
        self.assertEqual(events, ["metadata", "boot"])
        reads.assert_not_called()
        keys.assert_not_called()
        self.f.crypto.opened.assert_not_called()
        self.probe.probe_component.assert_not_called()
        self.f.temporary_location.assert_not_called()


class PrivateFileTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = self.parent / "private-root"
        self.root.mkdir(mode=0o700)
        self.path = self.root / "identity"
        self.path.write_bytes(b"synthetic bytes, not a key")
        self.path.chmod(0o600)
        self.fixture = SimpleNamespace(safe_path=donor.safe_path)

    def check(self, maximum=1024):
        return diagnostic.private_file(self.fixture, self.root, "identity", maximum)

    def test_exact_private_nonempty_owned_regular_single_link_file(self):
        self.assertEqual(self.check(), self.path)
        self.assertEqual(stat.S_IMODE(self.path.lstat().st_mode), 0o600)

    def test_permissions_empty_oversize_and_foreign_uid_refuse(self):
        for mode in (0o400, 0o640, 0o604, 0o660, 0o700, 0o4600, 0o2600):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.refused("metadata"):
                    self.check()
        self.path.chmod(0o600)
        with self.refused("metadata"):
            self.check(maximum=1)
        # Simulate another owner; never chown a real file or require host root.
        with patch.object(diagnostic.os, "getuid", return_value=os.getuid() + 1):
            with self.refused("metadata"):
                self.check()
        self.path.write_bytes(b"")
        with self.refused("metadata"):
            self.check()

    def test_hardlink_refuses(self):
        os.link(self.path, self.root / "second-name")
        self.assertEqual(self.path.lstat().st_nlink, 2)
        with self.refused("metadata"):
            self.check()

    def test_symlink_leaf_or_ancestor_never_reaches_following_stat(self):
        original = self.root / "original"
        self.path.rename(original)
        self.path.symlink_to(original)
        real_stat = Path.stat
        followed = []

        def track(path, *args, **kwargs):
            if kwargs.get("follow_symlinks", True):
                followed.append(path)
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", autospec=True, side_effect=track):
            with self.assertRaisesRegex(donor.FixtureError, "^unsafe-path$"):
                self.check()
        self.assertNotIn(self.path, followed)
        self.path.unlink()
        self.path = original
        redirect = self.parent / "root-redirect"
        redirect.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(donor.FixtureError, "^unsafe-path$"):
            diagnostic.private_file(self.fixture, redirect, "original", 1024)


class ArchiveInspectionCase(EvidenceCase):
    def setUp(self):
        super().setUp()
        self.directory = self.temporary / "ciphertext-components"
        self.directory.mkdir(mode=0o700)
        for name in diagnostic.COMPONENTS:
            path = self.directory / name
            path.write_bytes(b"synthetic ciphertext only: " + name.encode())
            path.chmod(0o600)
        self.manifest = {
            "kind": "full",
            "source_workspace_id": self.source["id"],
            "project": self.source["project"],
            "container_project": self.source["container_project"],
            "oc_box": {"commit": COMMIT, "dirty": False},
            "components": {
                name: donor.crypto.digest(self.directory / name)
                for name in diagnostic.COMPONENTS
            },
        }
        self.private = b"synthetic private tuple value never printed"
        self.events = []
        self.f.crypto.digest = Mock(wraps=donor.crypto.digest)

        @contextlib.contextmanager
        def opened(archive, identities):
            self.events.append("opened")
            self.assertEqual(archive, self.root / "sharing-full.ocbox")
            self.assertEqual(identities, [self.root / "sharing-age-identity.txt"])
            yield self.manifest, self.directory, self.private
            self.events.append("closed")

        self.f.crypto.opened = Mock(side_effect=opened)
        self.f.backups.validate_manifest.side_effect = lambda box, manifest: (
            self.events.append("validate-manifest")
        )

        def probe(*arguments):
            self.events.append(arguments[1].name)
            return probe_result()

        self.probe.probe_component.side_effect = probe

    def inspect(self):
        return diagnostic.inspect_archive(
            self.loaded,
            self.root / "sharing-full.ocbox",
            self.root / "sharing-age-identity.txt",
            self.scanner,
            self.source,
            self.probe,
        )


class ArchiveInspectionTests(ArchiveInspectionCase):
    def test_real_native_manifest_validation_remains_pure_metadata(self):
        self.loaded.box.DISTROS = {"synthetic": {}}
        self.manifest.update(
            configuration={
                "distro": "synthetic",
                "home": "/synthetic/private-home",
                "devices": [],
                "podman_args": [],
                "ready": True,
                "cpus": "1",
                "memory": "1g",
                "pids": 128,
                "credentials": {"mode": "vault", "env": [], "vault": []},
            },
            architecture="synthetic",
            created_at="synthetic-time",
            sessions={"sessions": [], "last": None},
            image_id="sha256:" + "a" * 64,
            expanded_byte_limit=1024,
        )
        self.f.backups.validate_manifest.side_effect = donor.backups.validate_manifest
        self.f.crypto.BackupError = donor.backups.BackupError
        self.assertEqual(set(self.inspect()), set(diagnostic.COMPONENTS))
        self.probe.probe_component.reset_mock()
        self.manifest["configuration"]["devices"] = SENSITIVE
        with self.refused("archive-envelope"):
            self.inspect()
        self.probe.probe_component.assert_not_called()

    def test_opened_tuple_order_pure_manifest_then_exactly_four_tar_oci_probes(self):
        result = self.inspect()
        self.assertEqual(list(result), list(diagnostic.COMPONENTS))
        self.f.backups.validate_manifest.assert_called_once_with(
            self.loaded.box, self.manifest
        )
        self.assertEqual(
            self.probe.probe_component.call_args_list,
            [
                call(
                    self.f.crypto,
                    self.directory / name,
                    self.private,
                    self.scanner,
                    "oci" if name == "image.age" else "tar",
                )
                for name in diagnostic.COMPONENTS
            ],
        )
        self.assertEqual(
            self.events,
            ["opened", "validate-manifest", *diagnostic.COMPONENTS, "closed"],
        )
        self.assertNotIn(self.private.decode(), json.dumps(result))
        self.assertEqual(
            self.f.crypto.digest.call_args_list,
            [
                call(self.directory / name)
                for name in diagnostic.COMPONENTS
                for unused in range(2)
            ],
        )
        for name, measured in result.items():
            self.assertEqual(
                set(measured), PROBE_FIELDS | {"ciphertext", "ciphertext_unchanged"}
            )
            self.assertEqual(
                measured["ciphertext"], donor.crypto.digest(self.directory / name)
            )
            self.assertIs(measured["ciphertext_unchanged"], True)
            self.assertEqual(
                {key: measured[key] for key in PROBE_FIELDS}, probe_result()
            )

    def test_ciphertext_metadata_mismatch_refuses_before_any_probe(self):
        metadata = self.manifest["components"]["project.age"]
        original = copy.deepcopy(metadata)
        for field, value in (("sha256", "0" * 64), ("bytes", original["bytes"] + 1)):
            with self.subTest(field=field):
                self.manifest["components"]["project.age"] = {**original, field: value}
                with self.assertRaises(diagnostic.Refusal):
                    self.inspect()
        self.probe.probe_component.assert_not_called()

    def test_ciphertext_change_during_probe_is_an_evidence_refusal(self):
        def mutate(crypto, path, private, scanner, mode):
            path.write_bytes(path.read_bytes() + b"mutation of synthetic ciphertext")
            return probe_result()

        self.probe.probe_component.side_effect = mutate
        with self.refused("evidence-changed"):
            self.inspect()
        self.assertEqual(self.probe.probe_component.call_count, 1)
        self.assertEqual(
            self.f.crypto.digest.call_args_list,
            [
                call(self.directory / "project.age"),
                call(self.directory / "project.age"),
            ],
        )

    def test_invalid_manifest_is_rejected_before_every_component_probe(self):
        original = copy.deepcopy(self.manifest)
        mutations = (
            ("kind", "portable"),
            ("source_workspace_id", "f" * 32),
            ("project", "/foreign"),
            ("container_project", "/foreign"),
            ("oc_box", {"commit": "f" * 40, "dirty": False}),
            ("oc_box", {"commit": COMMIT, "dirty": True}),
            ("components", {"project.age": {}}),
            ("components", {**original["components"], "extra.age": {}}),
        )
        for name, value in mutations:
            with self.subTest(name=name, value=value):
                self.manifest = {**copy.deepcopy(original), name: value}
                with self.refused("purpose"):
                    self.inspect()
        self.probe.probe_component.assert_not_called()

    def test_native_manifest_validator_failure_precedes_purpose_and_probes(self):
        self.f.backups.validate_manifest.side_effect = BackupError(SENSITIVE)
        with self.refused("archive-envelope") as raised:
            self.inspect()
        self.assertIsNone(raised.exception.__cause__)
        self.probe.probe_component.assert_not_called()

    def test_only_backup_errors_are_replaced_with_fixed_envelope_refusal(self):
        for failure in (BackupError(SENSITIVE), RuntimeError(SENSITIVE)):
            with self.subTest(kind=type(failure).__name__):
                self.f.crypto.opened.side_effect = failure
                expected = (
                    diagnostic.Refusal
                    if isinstance(failure, BackupError)
                    else RuntimeError
                )
                with self.assertRaises(expected) as raised:
                    self.inspect()
                self.assertEqual(
                    str(raised.exception),
                    "archive-envelope"
                    if isinstance(failure, BackupError)
                    else SENSITIVE,
                )
        self.probe.probe_component.assert_not_called()


class MainReportTests(ArchiveInspectionCase):
    def invoke(self):
        return self.capture_main(
            self.main_arguments(), history=self.history, probe=self.probe
        )

    def test_output_cleanup_failure_never_erases_primary_or_returns_completed_status(
        self,
    ):
        @contextlib.contextmanager
        def broken_cleanup():
            yield
            raise OSError(SENSITIVE)

        self.f.private_output = broken_cleanup
        code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "diagnostic-refused")
        self.assertEqual(result["category"], "io-error")
        self.f.crypto.opened.side_effect = BackupError(SENSITIVE)
        code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["category"], "archive-envelope")
        self.assertEqual(result["secondary_categories"], ["io-error"])

    def test_success_is_diagnostic_only_and_preserves_four_original_files(self):
        watched = [
            self.root / name
            for name in (
                "sharing-full.ocbox",
                "sharing-age-identity.txt",
                "sharing-purpose.json",
                "sharing-result.json",
            )
        ]
        before = {path: (path.read_bytes(), path.lstat()) for path in watched}
        code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "diagnostic-only")
        self.assertIs(result["original_evidence_unchanged"], True)
        self.assertEqual(set(result["components"]), set(diagnostic.COMPONENTS))
        self.assertEqual(
            result["source_identity"],
            {
                key: self.identity[key]
                for key in ("commit", "commit_count", "version", "dirty")
            },
        )
        self.assertNotIn("sha256", result["source_identity"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.f.temporary_location.assert_called_once_with(self.root, self.temporary)
        for path, (content, info) in before.items():
            self.assertEqual(path.read_bytes(), content)
            after = path.lstat()
            self.assertEqual(
                (after.st_ino, after.st_mode, after.st_nlink, after.st_mtime_ns),
                (info.st_ino, info.st_mode, info.st_nlink, info.st_mtime_ns),
            )

    def test_each_watched_file_change_refuses_without_rewriting_original_report(self):
        # Mutation is solely a fake probe; production code must detect it.
        for name in (
            "sharing-full.ocbox",
            "sharing-age-identity.txt",
            "sharing-purpose.json",
            "sharing-result.json",
        ):
            path = self.root / name
            original = path.read_bytes()
            report_original = (self.root / "sharing-result.json").read_bytes()

            def mutate(*args):
                if args[1].name == "project.age":
                    path.write_bytes(original + b"\n")
                return probe_result()

            self.probe.probe_component.side_effect = mutate
            try:
                with self.subTest(name=name):
                    code, result = self.invoke()
                    self.assertEqual(code, 1)
                    self.assertEqual(result["status"], "diagnostic-refused")
                    self.assertEqual(result["category"], "evidence-changed")
                    if name != "sharing-result.json":
                        self.assertEqual(
                            (self.root / "sharing-result.json").read_bytes(),
                            report_original,
                        )
            finally:
                path.write_bytes(original)

    def test_source_change_after_inspection_refuses(self):
        self.history.source_snapshot.return_value = {**self.identity, "dirty": True}
        code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["category"], "source-changed")
        self.assertEqual(result["status"], "diagnostic-refused")

    def test_loaded_source_host_refusal_precedes_root_keys_and_probe_execution(self):
        self.f.host_guard.side_effect = donor.FixtureError("host-only")
        code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["category"], "host-only")
        self.f.validate_fixture.assert_not_called()
        self.f.crypto.opened.assert_not_called()
        self.probe.probe_component.assert_not_called()
        self.f.temporary_location.assert_not_called()

    def test_temp_parent_in_fixture_or_either_checkout_refuses_before_age(self):
        for parent in (
            self.root,
            self.root / "temporary",
            self.loaded.source,
            Path(diagnostic.__file__).resolve().parents[1],
        ):
            with self.subTest(parent=parent):
                self.history.fresh_parent.return_value = parent
                code, result = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(result["category"], "placement")
        self.f.crypto.opened.assert_not_called()
        self.probe.probe_component.assert_not_called()

    def test_invalid_full_lowercase_commit_refuses_before_source_root_or_key(self):
        for name in ("expect_commit", "recorded_harness_commit"):
            original = getattr(self.args, name)
            for value in ("HEAD", "b" * 39, "B" * 40, "g" * 40, COMMIT + "\n"):
                with self.subTest(name=name, value=value):
                    setattr(self.args, name, value)
                    code, result = self.invoke()
                    self.assertEqual(code, 1)
                    self.assertEqual(result["category"], "invalid-options")
            setattr(self.args, name, original)
        self.history.fresh_parent.assert_not_called()
        self.history.load_source.assert_not_called()
        self.f.validate_fixture.assert_not_called()
        self.f.crypto.opened.assert_not_called()

    def test_private_errors_never_escape_json_as_messages_paths_argv_or_namespace(self):
        for error in (
            BackupError(SENSITIVE + NAMESPACE),
            OSError(SENSITIVE + NAMESPACE),
            RuntimeError(SENSITIVE + NAMESPACE),
            subprocess.CalledProcessError(2, [SENSITIVE], stderr=NAMESPACE),
            KeyboardInterrupt(SENSITIVE),
        ):
            with self.subTest(kind=type(error).__name__):
                self.f.crypto.opened.side_effect = error
                with patch.dict(os.environ, {"PROVIDER_API_KEY": SENSITIVE}):
                    code, result = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "diagnostic-refused")
                self.assertEqual(result["components"], {})
                self.assertIn(result["category"], diagnostic.CATEGORIES)
                self.assertNotIn("error", result)
                self.assertNotIn("argv", result)
                self.assertNotIn("env", result)

    def test_refused_inspection_still_hashes_every_watched_file_afterwards(self):
        self.f.crypto.opened.side_effect = BackupError(SENSITIVE)
        with patch.object(diagnostic, "digest", wraps=diagnostic.digest) as hashes:
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "diagnostic-refused")
        self.assertEqual(result["category"], "archive-envelope")
        for name in (
            "sharing-full.ocbox",
            "sharing-age-identity.txt",
            "sharing-purpose.json",
            "sharing-result.json",
        ):
            with self.subTest(name=name):
                self.assertEqual(hashes.call_args_list.count(call(self.root / name)), 2)
        self.history.source_snapshot.assert_called_once_with(
            self.args.source, self.args.expect_commit
        )

    def test_archive_primary_is_retained_when_report_later_changes(self):
        report = self.root / "sharing-result.json"
        changed = report.read_bytes() + b"\n"

        def fail_after_tampering(*args):
            report.write_bytes(changed)
            raise BackupError(SENSITIVE)

        self.f.crypto.opened.side_effect = fail_after_tampering
        code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "diagnostic-refused")
        self.assertEqual(result["category"], "archive-envelope")
        self.assertEqual(result["secondary_categories"], ["evidence-changed"])
        self.assertIs(result["original_evidence_unchanged"], False)
        self.assertEqual(report.read_bytes(), changed)

    def test_scanner_probe_and_diagnostic_hashes_are_rechecked_after_inspection(self):
        # Simulate changed tool bytes at the digest boundary; never mutate any
        # checkout file or the original scanner to exercise this refusal.
        directory = Path(diagnostic.__file__).resolve().parent
        real_digest = diagnostic.digest
        for selected in (
            self.scanner,
            directory / "sharing_backup_probe.py",
            directory / diagnostic.HISTORY,
            Path(diagnostic.__file__).resolve(),
        ):
            changed = False

            def measure(path):
                if path == selected and changed:
                    return "0" * 64
                return real_digest(path)

            def scanned(*args):
                nonlocal changed
                changed = True
                return probe_result()

            self.probe.probe_component.side_effect = scanned
            self.probe.probe_component.reset_mock()
            with self.subTest(tool=selected.name):
                with patch.object(diagnostic, "digest", side_effect=measure):
                    code, result = self.invoke()
                self.assertEqual(self.probe.probe_component.call_count, 4)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "diagnostic-refused")
                self.assertIn(result["category"], diagnostic.CATEGORIES)


class DonorMetadataOnlyTests(OfflineCase):
    def test_real_fixture_root_refusals_precede_metadata_reads(self):
        root = self.parent / (donor.PREFIX + "synthetic-root")
        root.mkdir(mode=0o700)
        with patch.object(donor, "private_json") as metadata:
            root.chmod(0o750)
            with self.assertRaisesRegex(donor.FixtureError, "^fixture-root$"):
                donor.validate_fixture(root, allow_unrecorded=True)
            root.chmod(0o700)
            with patch.object(donor.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(donor.FixtureError, "^fixture-root$"):
                    donor.validate_fixture(root, allow_unrecorded=True)
            foreign_name = self.parent / "foreign-format-root"
            foreign_name.mkdir(mode=0o700)
            with self.assertRaisesRegex(donor.FixtureError, "^fixture-root$"):
                donor.validate_fixture(foreign_name, allow_unrecorded=True)
        metadata.assert_not_called()

    def test_real_validate_fixture_never_calls_engine_or_resource_methods(self):
        # Exercise the actual metadata-only donor function with file selectors
        # mocked individually. There is deliberately no on-disk engine store.
        root = Mock(name="synthetic_root")
        root.name = donor.PREFIX + "synthetic"
        root.is_dir.return_value = True
        root.stat.return_value = SimpleNamespace(st_uid=os.getuid(), st_mode=0o700)
        root.resolve.return_value = root
        mapping = {"podman": "/synthetic/podman", "interpreter": "/synthetic/python"}
        documents = {
            "fixture.json": {
                "format": donor.FORMAT,
                "version": 1,
                "projects": list(donor.PROJECTS),
            },
            "podman-executable.json": mapping,
            "fixture-ids.json": {},
        }
        selectors = {
            "config/containers/storage.conf": "synthetic storage selector",
            "config/containers/containers.conf": "synthetic containers selector",
            "bin/podman": "synthetic unexecuted wrapper",
        }

        def file_path(unused_root, name, **kwargs):
            return SimpleNamespace(read_text=lambda: selectors[name])

        with contextlib.ExitStack() as stack:
            forbidden = [
                stack.enter_context(
                    patch.object(
                        donor, name, side_effect=AssertionError(name + " forbidden")
                    )
                )
                for name in (
                    "validate_store",
                    "validate_store_info",
                    "verify_owned_resources",
                    "launcher",
                )
            ]
            stack.enter_context(patch.object(donor, "no_symlinks", return_value=root))
            stack.enter_context(patch.object(donor, "safe_path", side_effect=file_path))
            stack.enter_context(
                patch.object(
                    donor, "private_json", side_effect=lambda r, n: documents[n]
                )
            )
            stack.enter_context(
                patch.object(
                    donor,
                    "expected_storage",
                    return_value=selectors["config/containers/storage.conf"],
                )
            )
            stack.enter_context(
                patch.object(
                    donor,
                    "containers_config",
                    return_value=selectors["config/containers/containers.conf"],
                )
            )
            stack.enter_context(
                patch.object(
                    donor, "podman_wrapper_source", return_value=selectors["bin/podman"]
                )
            )
            native = stack.enter_context(patch.object(donor, "validate_native_podman"))
            stack.enter_context(
                patch.object(donor, "workspace_records", return_value={})
            )
            stack.enter_context(patch.object(donor.os, "access", return_value=True))
            self.assertIs(donor.validate_fixture(root, allow_unrecorded=True), root)
        self.assertEqual(
            native.call_args_list,
            [call(mapping["podman"], root), call(mapping["interpreter"], root)],
        )
        for boundary in forbidden:
            boundary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
