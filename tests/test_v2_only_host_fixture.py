"""Offline guard/profile contracts. Mocked engines are NOT host acceptance."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import test_v2_lifecycle_podman as fixture

legacy, safety, box = fixture.legacy, fixture.safety, fixture.box


class V2OnlyHostFixtureTests(unittest.TestCase):
    def fake_native(self, root, version="2.3.0"):
        binary = root / "native"
        binary.write_text("#!" + sys.executable + "\nprint(" + repr(version) + ")\n")
        binary.chmod(0o700)
        return binary

    def npm_native_selection(self, root):
        binary = root / "node/lib/node_modules/@opencode/cli/bin/opencode.exe"
        binary.parent.mkdir(parents=True)
        # A private ELF copy is metadata only; version probes remain mocked.
        fixture.shutil.copyfile(Path(sys.executable).resolve(), binary)
        binary.chmod(0o700)
        alias = root / "node/bin/opencode2"
        alias.parent.mkdir()
        alias.symlink_to(os.path.relpath(binary, alias.parent))
        return alias, binary

    @contextlib.contextmanager
    def retained(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            # A real ELF interpreter is metadata only; no engine is invoked.
            fixture.prepare_root(root, "tumbleweed", Path(sys.executable).resolve())
            binary = self.fake_native(root)
            fixture.prepare_native_cli(root, binary, "2.3.0")
            yield root, binary

    def test_explicit_version_support_is_bounded_before_probe(self):
        for major, version in (
            ("2", "2.3.1"),
            ("2", "2.4.0"),
            ("1", "2.3.0"),
            ("3", "3.0.0"),
            ("1", "1.20.0"),
        ):
            with self.subTest(major=major, version=version):
                with patch.object(box, "run") as run, self.assertRaises(ValueError):
                    legacy.check_native_version(
                        Path("/example"), Path("/native"), major, expected=version
                    )
                run.assert_not_called()
        self.assertEqual(legacy.NATIVE_VERSIONS, {"1": "1.19.5", "2": "2.2.6"})

    def test_new_native_requires_explicit_pin_old_defaults_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = self.fake_native(root)
            self.assertEqual(
                legacy.check_native_version(root, binary, "2", expected="2.3.0"),
                "2.3.0",
            )
            with self.assertRaisesRegex(ValueError, "expected 2.2.6"):
                legacy.check_native_version(root, binary, "2")
            with self.assertRaisesRegex(ValueError, "expected 1.19.5"):
                legacy.check_native_version(root, binary, "1")

    def test_selector_records_actual_v2_only_version_and_hash_no_v1_or_major_switch(
        self,
    ):
        for version in fixture.VERSIONS:
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                (root / "bin").mkdir()
                binary = self.fake_native(root, version)
                wrapper, profile = fixture.prepare_native_cli(root, binary, version)
                self.assertEqual(profile["version"], version)
                self.assertEqual(profile["sha256"], fixture.binary_hash(binary))
                self.assertEqual(
                    json.loads((root / "executables.json").read_text()),
                    {"2": str(binary)},
                )
                self.assertFalse((root / "host-major").exists())
                self.assertEqual(
                    subprocess.check_output(
                        [wrapper, "--version"], text=True, timeout=15
                    ).strip(),
                    version,
                )
                self.assertEqual(legacy.NATIVE_VERSIONS["2"], "2.2.6")

    def test_selector_mismatch_does_not_create_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            binary = self.fake_native(root, "2.2.6")
            with self.assertRaisesRegex(ValueError, "match selected profile"):
                fixture.prepare_native_cli(root, binary, "2.3.0")
            self.assertFalse((root / "executables.json").exists())
            self.assertFalse((root / "native-profile.json").exists())
            self.assertFalse((root / "bin/opencode").exists())

    def test_cli_npm_symlink_dispatches_and_pins_canonical_elf_without_engine_access(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            alias, binary = self.npm_native_selection(parent)
            observed = {}

            def dispatched(root, selected, version, distro, report):
                self.assertEqual(selected, binary)
                self.assertFalse(selected.is_symlink())
                with selected.open("rb") as stream:
                    self.assertEqual(stream.read(4), b"\x7fELF")
                _, profile = fixture.prepare_native_cli(root, selected, version)
                self.assertEqual(
                    profile, fixture.profiles.native_identity(binary, version)
                )
                observed.update(root=root, profile=profile)
                self.assertEqual(fixture.validate_fixture(root), root)
                report["native"] = profile
                report["status"] = "passed"
                report["checks"] = dict.fromkeys(fixture.CHECKS, True)

            with (
                patch.object(safety, "host_guard"),
                patch.object(
                    safety, "private_output", side_effect=contextlib.nullcontext
                ),
                patch.object(
                    fixture.shutil,
                    "which",
                    return_value=str(Path(sys.executable).resolve()),
                ),
                patch.object(fixture, "run", side_effect=dispatched) as run,
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "2.3.0", ""),
                ) as probes,
                patch.object(fixture, "validate_store") as store,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = fixture.main(
                    ["--opencode-v2", str(alias), "--work-dir", str(parent)]
                )
            self.assertEqual(code, 0)
            run.assert_called_once()
            store.assert_not_called()
            self.assertEqual(probes.call_count, 2)
            for call in probes.call_args_list:
                self.assertEqual(call.args[0][1:], ["--version"])
            self.assertEqual(probes.call_args_list[0].args[0][0], str(binary))
            self.assertEqual(
                safety.private_json(observed["root"], "native-profile.json"),
                observed["profile"],
            )

    def test_cli_dangling_native_symlink_refuses_before_placement_probe_or_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            alias = parent / "opencode2"
            alias.symlink_to("missing-native")
            with (
                patch.object(safety, "host_guard"),
                patch.object(fixture, "prepare_root") as prepare,
                patch.object(fixture, "run") as run,
                patch.object(fixture.shutil, "which") as discover,
                patch.object(box, "run") as engine,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                code = fixture.main(
                    ["--opencode-v2", str(alias), "--work-dir", str(parent)]
                )
            self.assertEqual(code, 1)
            for operation in (prepare, run, discover, engine):
                operation.assert_not_called()
            self.assertNotIn(str(alias), errors.getvalue())

    def test_cli_selected_symlink_target_mutation_refuses_before_store_seed_or_engine(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            alias, binary = self.npm_native_selection(parent)
            original_hash = fixture.binary_hash(binary)
            calls = []

            def version_probe(command, **kwargs):
                self.assertEqual(command[1:], ["--version"])
                calls.append(command[0])
                if command[0] != str(binary):
                    with binary.open("ab") as stream:
                        stream.write(b"changed-private-test-target")
                return subprocess.CompletedProcess(command, 0, "2.3.0", "")

            with (
                patch.object(safety, "host_guard"),
                patch.object(
                    safety, "private_output", side_effect=contextlib.nullcontext
                ),
                patch.object(
                    fixture.shutil,
                    "which",
                    return_value=str(Path(sys.executable).resolve()),
                ),
                patch.object(
                    fixture,
                    "source_identity",
                    return_value={
                        "build": {"commit": "a" * 40, "dirty": False},
                        "sha256": {},
                    },
                ),
                patch.object(box, "run", side_effect=version_probe),
                patch.object(fixture, "validate_store") as store,
                patch.object(box, "check_backend") as backend,
                patch.object(safety, "seed_native_history") as seed,
                patch.object(legacy, "make_provider") as provider,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = fixture.main(
                    ["--opencode-v2", str(alias), "--work-dir", str(parent)]
                )
            self.assertEqual(code, 1)
            for operation in (store, backend, seed, provider):
                operation.assert_not_called()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], str(binary))
            self.assertNotEqual(fixture.binary_hash(binary), original_hash)
            roots = list(parent.glob(fixture.PREFIX + "*"))
            self.assertEqual(len(roots), 1)
            profile = safety.private_json(roots[0], "native-profile.json")
            self.assertEqual(profile["path"], str(binary))
            self.assertEqual(profile["sha256"], original_hash)
            report = safety.private_json(roots[0], "result.json")
            self.assertEqual(report["status"], "failed")
            self.assertFalse(any(report["checks"].values()))

    def test_entrypoint_host_guard_precedes_placement_probe_import_or_engine(self):
        with (
            patch.object(
                safety, "host_guard", side_effect=safety.FixtureError("private")
            ),
            patch.object(fixture, "prepare_root") as prepare,
            patch.object(fixture, "prepare_native_cli") as native,
            patch.object(safety, "seed_native_history") as seed,
            patch.object(fixture.tempfile, "mkdtemp") as mkdir,
            patch.object(box, "run") as run,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(fixture.main(["--opencode-v2", "/private/native"]), 1)
        for operation in (prepare, native, seed, mkdir, run):
            operation.assert_not_called()
        self.assertNotIn("/private/native", errors.getvalue())
        self.assertNotIn("private", errors.getvalue())

    def test_library_run_and_cleanup_also_guard_before_any_access(self):
        for operation, args in (
            (
                fixture.run,
                (Path("/private"), Path("/native"), "2.3.0", "tumbleweed", {}),
            ),
            (fixture.cleanup, (Path("/private"),)),
        ):
            with (
                self.subTest(operation=operation.__name__),
                patch.object(
                    safety, "host_guard", side_effect=safety.FixtureError("host-only")
                ),
                patch.object(fixture, "source_identity") as source,
                patch.object(fixture, "validate_fixture") as validate,
                patch.object(box, "run") as run,
                self.assertRaises(safety.FixtureError),
            ):
                operation(*args)
            source.assert_not_called()
            validate.assert_not_called()
            run.assert_not_called()

    def test_host_guard_covers_root_container_environment_and_container_markers(self):
        for root, container, marker in (
            (0, "0", False),
            (1000, "1", False),
            (1000, "0", True),
        ):
            with (
                self.subTest(root=root, container=container, marker=marker),
                patch.object(os, "geteuid", return_value=root),
                patch.dict(os.environ, {"OC_BOX_CONTAINER": container}),
                patch.object(Path, "exists", return_value=marker),
                self.assertRaises(safety.FixtureError),
            ):
                safety.host_guard()

    def test_legacy_entrypoint_still_requires_both_native_executables(self):
        with (
            patch.object(os, "geteuid", return_value=1000),
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(legacy, "run") as run,
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as result,
        ):
            legacy.main(["--opencode-v2", "/example/native"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("--opencode-v1 and --opencode-v2", errors.getvalue())
        run.assert_not_called()

    def test_v2_only_entrypoint_rejects_legacy_retry_major_and_unpinned_options_quietly(
        self,
    ):
        for args in (
            ["--resume", "/private"],
            ["--retry-downgrade", "/private"],
            ["--opencode-v1", "/private"],
            ["--native-version", "2.3.1"],
        ):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()) as errors,
                patch.object(box, "run") as run,
                self.assertRaises(SystemExit) as result,
            ):
                fixture.main(args)
            self.assertEqual(result.exception.code, 2)
            self.assertNotIn("/private", errors.getvalue())
            run.assert_not_called()

    def test_new_report_never_claims_old_six_stages_global_or_untested_checks(self):
        for version in fixture.VERSIONS:
            report = fixture.new_report(version)
            self.assertEqual(report["native_versions"], {"2": version})
            self.assertEqual(report["profile"], "v2-only")
            self.assertEqual(report["status"], "failed")
            self.assertFalse(any(report["checks"].values()))
            self.assertFalse(report["global_vault_fixture_ran"])
            self.assertFalse(report["legacy_fixture_ran"])
            self.assertEqual(report["credential_scope"], "selected")
            for exclusion in (
                "legacy-v1-migration",
                "legacy-v1-downgrade-reopen",
                "global-vault-full-path-separate-fixture-required",
                "oauth-refresh",
                "interactive-tui-web",
            ):
                self.assertIn(exclusion, report["excluded"])

    def test_dirty_source_is_refused_before_native_seed_or_engine(self):
        with (
            patch.object(safety, "host_guard"),
            patch.object(
                fixture,
                "source_identity",
                return_value={"build": {"commit": "a" * 40, "dirty": True}},
            ),
            patch.object(fixture, "prepare_native_cli") as prepare,
            patch.object(safety, "seed_native_history") as seed,
            patch.object(box, "run") as engine,
            self.assertRaises(safety.FixtureError),
        ):
            fixture.run(Path("/private"), Path("/native"), "2.3.0", "tumbleweed", {})
        prepare.assert_not_called()
        seed.assert_not_called()
        engine.assert_not_called()

    def test_profile_orchestration_keeps_both_restore_kinds_rotation_reset_and_refusal(
        self,
    ):
        """Wiring only: mocked resources/owners are never live acceptance."""
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            for name in ("bin", "host-home", "source-a", "config/opencode"):
                (root / name).mkdir(parents=True, mode=0o700)
            (root / "source-a/unfinished.txt").write_text("PROJECT_RETAINED\n")
            binary = self.fake_native(root)
            baseline = {
                "info": {
                    "id": "ses_native_seed",
                    "location": {"directory": str(root / "source-a")},
                },
                "messages": [],
            }
            records, transcripts, passwords, launches, backups = {}, {}, [], [], []

            def create(project, distro):
                name = project.name
                ident = str(len(records) + 1) * 32
                data = {
                    "id": ident,
                    "project": str(project),
                    "container_project": str(root / "source-a"),
                    "container": "oc-box-" + ident,
                    "home_volume": "oc-box-home-" + ident,
                    "cid": ident,
                    "opencode_version": "2.3.0",
                }
                path = root / ("metadata-" + ident + ".json")
                records[name] = (path, data)
                transcripts[ident] = copy.deepcopy(baseline)
                (project / box.MARKER).write_text(ident)
                return path, data

            def encrypted(path, values, password):
                passwords.append(password)

            def launch(arguments):
                data = next(
                    data for _, data in records.values() if data["id"] == arguments[-1]
                )
                launches.append((arguments[0], Path(data["project"]).name))
                password = fixture.vault.hidden()
                if password != passwords[0]:
                    raise box.BoxError("Unable to unlock synthetic fixture")
                if arguments[0] == "reset":
                    box.remove_resources(data, keep_home=True)
                    data["cid"] = "reset-" + data["cid"]
                self.assertEqual(
                    box.run_tui(["opencode", "--session", baseline["info"]["id"]]), 0
                )

            def native(data, ident, state, port):
                state["markers"].update({"VAULT_SHELL_OK", "VAULT_MCP_OK"})
                state["requests"] += 1
                transcripts[data["id"]]["messages"].append(
                    {
                        "id": "msg_" + str(state["requests"]),
                        "type": "user",
                        "text": "HOST_V2_ACCEPTANCE_OK",
                    }
                )
                return subprocess.CompletedProcess([], 0, "HOST_V2_ACCEPTANCE_OK", "")

            def backup(_box, arguments):
                backups.append(arguments.portable)
                arguments.output.write_bytes(b"synthetic-encrypted-placeholder")

            def restore(_box, arguments):
                arguments.project.mkdir()
                (arguments.project / "unfinished.txt").write_text("PROJECT_RETAINED\n")
                _, data = create(arguments.project, "tumbleweed")
                source = records["source-a"][1]
                data["credentials"] = copy.deepcopy(source["credentials"])
                data["restored_from"] = {
                    "workspace_id": source["id"],
                    "kind": arguments.project.name.removeprefix("restored-"),
                }
                transcripts[data["id"]] = copy.deepcopy(transcripts[source["id"]])

            env = legacy.isolated_env(root)
            env["TMPDIR"] = str(root)
            server, worker = Mock(server_port=1234), Mock()
            source = {"build": {"commit": "a" * 40, "dirty": False}, "sha256": {}}
            python, remove = Mock(), Mock()
            patches = (
                patch.object(safety, "host_guard"),
                patch.object(safety, "isolated_env", return_value=env),
                patch.object(fixture, "source_identity", return_value=source),
                patch.object(fixture, "validate_store"),
                patch.object(box, "check_backend"),
                patch.object(fixture.crypto, "tools"),
                patch.object(fixture.vault, "crypto"),
                patch.object(safety, "seed_native_history", return_value=baseline),
                patch.object(legacy, "make_provider", return_value=(server, worker)),
                patch.object(
                    fixture.vault,
                    "locked",
                    side_effect=lambda *a, **k: contextlib.nullcontext(),
                ),
                patch.object(fixture.vault, "write_encrypted", side_effect=encrypted),
                patch.object(box, "create_workspace", side_effect=create),
                patch.object(
                    fixture, "workspace_records", side_effect=lambda _: records
                ),
                patch.object(
                    fixture,
                    "record_ids",
                    side_effect=lambda _: {
                        name: data["id"] for name, (_, data) in records.items()
                    },
                ),
                patch.object(
                    box, "session_state", return_value={"last": baseline["info"]["id"]}
                ),
                patch.object(
                    box,
                    "export_container_session",
                    side_effect=lambda data, ident: copy.deepcopy(
                        transcripts[data["id"]]
                    ),
                ),
                patch.object(safety, "launcher", side_effect=launch),
                patch.object(box, "main", side_effect=lambda args: launch(args) or 0),
                patch.object(
                    safety,
                    "verify_owned_resources",
                    side_effect=lambda root, data, **kw: {"Id": data["cid"]},
                ),
                patch.object(
                    fixture.workspace_service,
                    "live_connection",
                    return_value={"private": True},
                ),
                patch.object(
                    fixture.workspace_service,
                    "attach",
                    side_effect=lambda *a: contextlib.nullcontext(
                        {"session": baseline["info"]["id"]}
                    ),
                ),
                patch.object(fixture.workspace_service, "api"),
                patch.object(legacy, "run_fixture_session", side_effect=native),
                patch.object(fixture, "container_python", new=python),
                patch.object(
                    fixture,
                    "home_snapshot",
                    return_value={"complete-home-tree": "same"},
                ),
                patch.object(
                    fixture.crypto, "small", return_value=b"synthetic-age-material"
                ),
                patch.object(fixture.backups, "make_backup", side_effect=backup),
                patch.object(fixture.backups, "restore_backup", side_effect=restore),
                patch.object(box, "remove_resources", new=remove),
                patch.dict(os.environ, {"TMPDIR": str(root)}),
            )
            with contextlib.ExitStack() as stack:
                for replacement in patches:
                    stack.enter_context(replacement)
                report = fixture.new_report("2.3.0")
                fixture.run(root, binary, "2.3.0", "tumbleweed", report)
            self.assertEqual(backups, [False, True])
            self.assertEqual(
                launches,
                [
                    ("start", "source-a"),
                    ("start", "source-a"),
                    ("start", "restored-full"),
                    ("start", "restored-portable"),
                    ("reset", "source-a"),
                    ("reset", "source-a"),
                ],
            )
            remove.assert_called_once()
            self.assertTrue(remove.call_args.kwargs["keep_home"])
            self.assertEqual(python.call_count, 5)
            self.assertEqual(report["status"], "passed")
            self.assertTrue(all(report["checks"].values()))
            self.assertEqual(report["native_versions"], {"2": "2.3.0"})
            self.assertFalse(report["global_vault_fixture_ran"])
            self.assertEqual(report["synthetic_provider_requests"], 5)
            server.shutdown.assert_called_once()
            server.server_close.assert_called_once()
            worker.join.assert_called_once()

    def test_whole_home_manifest_hashes_bytes_links_modes_and_rejects_special_entries(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            payload = root / "nested/payload"
            payload.write_text("first")
            link = root / "link"
            link.symlink_to("nested/payload")

            def local_unshare(command):
                self.assertEqual(command[:2], ["podman", "unshare"])
                return subprocess.check_output(command[2:], stderr=subprocess.PIPE)

            with (
                patch.object(
                    fixture.backups,
                    "owned_volume",
                    return_value={"Mountpoint": str(root)},
                ),
                patch.object(legacy, "digest_home", return_value="db-digest"),
                patch.object(fixture.crypto, "small", side_effect=local_unshare),
            ):
                initial = fixture.home_snapshot({})
                self.assertEqual(len(initial["complete-home-tree"]), 64)
                self.assertEqual(initial, fixture.home_snapshot({}))
                payload.write_text("other")
                changed = fixture.home_snapshot({})
                self.assertNotEqual(initial, changed)
                payload.chmod(0o600)
                mode_changed = fixture.home_snapshot({})
                self.assertNotEqual(changed, mode_changed)
                link.unlink()
                link.symlink_to("nested/absent")
                self.assertNotEqual(mode_changed, fixture.home_snapshot({}))
                os.mkfifo(root / "unsupported")
                with self.assertRaises(subprocess.CalledProcessError):
                    fixture.home_snapshot({})

    def test_project_payload_requires_exact_files_except_the_fresh_identity_marker(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            for path, ident in ((first, "a" * 32), (second, "b" * 32)):
                path.mkdir()
                (path / box.MARKER).write_text(ident)
                (path / "project.txt").write_text("same")
            self.assertEqual(
                fixture.project_payload(first), fixture.project_payload(second)
            )
            (second / "project.txt").write_text("changed")
            self.assertNotEqual(
                fixture.project_payload(first), fixture.project_payload(second)
            )

    def test_retained_metadata_rejects_changed_pin_binary_selector_and_store(self):
        with self.retained() as (root, binary):
            self.assertEqual(fixture.validate_fixture(root), root)
            for relative, replacement in (
                ("native-profile.json", '{"profile":"v2-only","version":"2.3.1"}'),
                ("executables.json", '{"1":"/example/legacy","2":"/example/native"}'),
                ("bin/opencode", "unfamiliar selector"),
                ("bin/podman", "unfamiliar selector"),
                ("config/containers/storage.conf", "host storage"),
                ("config/containers/containers.conf", "host engine"),
                ("native", "#!/bin/sh\nexit 0\n"),
            ):
                with self.subTest(relative=relative):
                    target = root / relative
                    before = target.read_bytes()
                    target.write_text(replacement)
                    with (
                        patch.object(box, "run") as engine,
                        self.assertRaises((safety.FixtureError, ValueError)),
                    ):
                        fixture.validate_fixture(root)
                    engine.assert_not_called()
                    target.write_bytes(before)
            self.assertEqual(
                fixture.binary_hash(binary),
                safety.private_json(root, "native-profile.json")["sha256"],
            )

    def test_retained_metadata_refuses_foreign_paths_unsafe_permissions_and_symlinks(
        self,
    ):
        with self.retained() as (root, _):
            for relative in ("fixture.json", "native-profile.json", "fixture-ids.json"):
                target = root / relative
                target.chmod(0o644)
                with self.assertRaises(safety.FixtureError):
                    fixture.validate_fixture(root)
                target.chmod(0o600)
            target = root / "executables.json"
            original = target.read_bytes()
            target.unlink()
            target.symlink_to(root / "native-profile.json")
            with self.assertRaises(safety.FixtureError):
                fixture.validate_fixture(root)
            target.unlink()
            target.write_bytes(original)
            target.chmod(0o600)
            folder = root / "registry/workspaces" / ("a" * 32)
            folder.mkdir(mode=0o700)
            safety.write_json_new(
                folder / "workspace.json",
                {
                    "id": "a" * 32,
                    "project": str(root.parent / "foreign"),
                },
            )
            with (
                patch.object(box, "run") as engine,
                self.assertRaises(safety.FixtureError),
            ):
                fixture.validate_fixture(root, allow_unrecorded=True)
            engine.assert_not_called()

    def store_info(self, root):
        return {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "version": {"Version": "5.6.2"},
            "store": {
                "graphDriverName": "overlay",
                "graphRoot": str(root / "data/containers/storage"),
                "runRoot": str(root / "podman-run"),
                "volumePath": str(root / "data/containers/storage/volumes"),
                "configFile": str(root / "config/containers/storage.conf"),
                "imageCopyTmpDir": str(root / "temporary"),
            },
        }

    def test_actual_store_info_requires_all_private_paths_rootless_and_local_engine(
        self,
    ):
        with self.retained() as (root, _):
            good = self.store_info(root)
            fixture.validate_store_info(root, good, root / "temporary")
            for section, field, value in (
                ("host", "serviceIsRemote", True),
                ("host", "security", {"rootless": False}),
                ("store", "graphDriverName", "vfs"),
                *[
                    ("store", field, "/foreign")
                    for field in (
                        "graphRoot",
                        "runRoot",
                        "volumePath",
                        "configFile",
                        "imageCopyTmpDir",
                    )
                ],
            ):
                with self.subTest(field=field):
                    bad = copy.deepcopy(good)
                    bad[section][field] = value
                    with self.assertRaises(safety.FixtureError):
                        fixture.validate_store_info(root, bad, root / "temporary")
            del good["store"]["configFile"]
            with self.assertRaises(safety.FixtureError):
                fixture.validate_store_info(root, good, root / "temporary")
            good["version"]["Version"] = "6.1.4"
            fixture.validate_store_info(root, good, root / "temporary")

    def test_stale_missing_invalid_boot_marker_refuses_all_retained_engine_commands(
        self,
    ):
        with self.retained() as (root, _):
            for marker in (
                None,
                b"",
                b"invalid",
                b"11111111-1111-1111-1111-111111111111\n",
            ):
                with self.subTest(marker=marker):
                    target = root / "podman-tmp/alive"
                    if marker is not None:
                        target.write_bytes(marker)
                        target.chmod(0o600)
                    before = target.read_bytes() if target.exists() else None
                    with (
                        patch.object(
                            safety,
                            "system_boot_id",
                            return_value=b"22222222-2222-2222-2222-222222222222\n",
                        ),
                        patch.object(box, "run") as engine,
                        self.assertRaises(safety.FixtureError),
                    ):
                        fixture.validate_store(root)
                    engine.assert_not_called()
                    self.assertEqual(
                        target.read_bytes() if target.exists() else None, before
                    )

    def test_cleanup_refuses_stale_boot_before_info_inspection_or_deletion(self):
        with (
            self.retained() as (root, _),
            patch.object(safety, "host_guard"),
            patch.object(box, "run") as engine,
            patch.object(safety, "verify_owned_resources") as inspect,
            patch.object(box, "remove_resources") as remove,
            self.assertRaises(safety.FixtureError),
        ):
            fixture.cleanup(root)
        engine.assert_not_called()
        inspect.assert_not_called()
        remove.assert_not_called()

    def test_store_probes_cover_normal_maintenance_and_crypto_environments(self):
        with self.retained() as (root, _):
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            with (
                patch.object(
                    box, "maintenance_host_env", wraps=box.maintenance_host_env
                ),
                patch.object(
                    safety,
                    "isolated_env",
                    return_value={
                        "PATH": str(root / "bin") + ":/usr/bin",
                        "TMPDIR": str(root / "temporary"),
                        "XDG_RUNTIME_DIR": str(runtime),
                        "CONTAINERS_CONF": str(
                            root / "config/containers/containers.conf"
                        ),
                        "CONTAINERS_STORAGE_CONF": str(
                            root / "config/containers/storage.conf"
                        ),
                    },
                ),
                patch.object(safety, "require_runtime_boot") as boot,
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(self.store_info(root)), ""
                    ),
                ) as engine,
            ):
                fixture.validate_store(root, fresh=True)
            self.assertEqual(engine.call_count, 3)
            for call in engine.call_args_list:
                self.assertEqual(call.args[0], ["podman", "info", "--format", "json"])
                self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(call.kwargs["private"])
            self.assertEqual(boot.call_count, 2)
            self.assertTrue(boot.call_args_list[0].kwargs["allow_uninitialized"])
            self.assertNotIn("allow_uninitialized", boot.call_args_list[1].kwargs)

    def test_explicit_private_temp_directory_is_verified_for_all_store_probes(self):
        with self.retained() as (root, _), tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            info = self.store_info(root)
            info["store"]["imageCopyTmpDir"] = str(destination)
            env = {
                "PATH": str(root / "bin") + ":/usr/bin",
                "TMPDIR": str(root / "temporary"),
                "CONTAINERS_CONF": str(root / "config/containers/containers.conf"),
                "CONTAINERS_STORAGE_CONF": str(root / "config/containers/storage.conf"),
            }
            with (
                patch.object(safety, "isolated_env", return_value=env),
                patch.object(safety, "require_runtime_boot"),
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(info), ""
                    ),
                ) as engine,
            ):
                fixture.validate_store(root, fresh=True, temporary=destination)
            self.assertEqual(engine.call_count, 3)
            for call in engine.call_args_list:
                self.assertEqual(call.kwargs["env"]["TMPDIR"], str(destination))
            destination.chmod(0o755)
            with (
                patch.object(safety, "require_runtime_boot"),
                patch.object(safety, "isolated_env", return_value=env),
                patch.object(box, "run") as engine,
                self.assertRaises(safety.FixtureError),
            ):
                fixture.validate_store(root, fresh=True, temporary=destination)
            engine.assert_not_called()
            destination.chmod(0o700)

    def test_cleanup_rejects_new_run_options_before_inspecting_retained_fixture(self):
        with (
            patch.object(safety, "host_guard"),
            patch.object(safety, "private_output", side_effect=contextlib.nullcontext),
            patch.object(fixture, "cleanup") as cleanup,
            patch.object(box, "run") as engine,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                fixture.main(
                    [
                        "--cleanup",
                        "/private/fixture",
                        "--opencode-v2",
                        "/private/native",
                    ]
                ),
                1,
            )
        cleanup.assert_not_called()
        engine.assert_not_called()

    def test_failure_after_report_pass_does_not_emit_success_or_private_exception(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            parent = Path(temporary)
            binary = self.fake_native(parent)
            native_podman = Path(sys.executable).resolve()

            def failing(root, binary, version, distro, report):
                report["status"] = "passed"  # Simulate final shutdown exception.
                report["checks"]["private-store-native-selector"] = True
                raise RuntimeError("PRIVATE_ARGV_ENV_STDERR")

            with (
                patch.object(safety, "host_guard"),
                patch.object(
                    safety, "private_output", side_effect=contextlib.nullcontext
                ),
                patch.object(fixture.shutil, "which", return_value=str(native_podman)),
                patch.object(fixture, "run", side_effect=failing),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                code = fixture.main(
                    ["--opencode-v2", str(binary), "--work-dir", str(parent)]
                )
            self.assertEqual(code, 1)
            roots = [path for path in parent.iterdir() if path.is_dir()]
            self.assertEqual(len(roots), 1)
            report = safety.private_json(roots[0], "result.json")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failure"], "exception")
            self.assertNotIn(
                "PRIVATE_ARGV_ENV_STDERR",
                json.dumps(report) + output.getvalue() + errors.getvalue(),
            )
            self.assertNotIn("profile passed", output.getvalue())

    def test_exclusive_report_creation_never_replaces_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "result.json"
            safety.write_json_new(target, {"status": "first-failure"})
            with self.assertRaises(FileExistsError):
                safety.write_json_new(target, {"status": "passed"})
            self.assertEqual(
                json.loads(target.read_text()), {"status": "first-failure"}
            )

    def test_sentinel_contract_requires_full_os_bytes_and_portable_reset_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unfinished.txt").write_text("PROJECT_RETAINED\n")
            (root / "acceptance-home.txt").write_text("HOME_RETAINED\n")
            sentinel = root / "synthetic-etc-file"
            program = fixture.sentinel_check_program(sentinel)

            def execute(kind):
                return subprocess.run(
                    [sys.executable, "-B", "-c", program, str(root), kind],
                    env={**os.environ, "HOME": str(root)},
                    capture_output=True,
                    timeout=15,
                ).returncode

            sentinel.write_text("OS_RETAINED\n")
            self.assertEqual(execute("full"), 0)
            self.assertNotEqual(execute("portable"), 0)
            sentinel.write_text("wrong bytes")
            self.assertNotEqual(execute("full"), 0)
            sentinel.unlink()
            self.assertEqual(execute("portable"), 0)
            self.assertNotEqual(execute("full"), 0)
            (root / "acceptance-home.txt").write_text("lost home")
            self.assertNotEqual(execute("portable"), 0)

    def test_report_creation_failure_is_nonzero_without_success_and_preserves_old_evidence(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            parent = Path(temporary)
            binary = self.fake_native(parent)

            def completed(root, binary, version, distro, report):
                safety.write_json_new(root / "result.json", {"status": "first-failure"})
                report["status"] = "passed"
                report["checks"] = dict.fromkeys(fixture.CHECKS, True)

            with (
                patch.object(safety, "host_guard"),
                patch.object(
                    safety, "private_output", side_effect=contextlib.nullcontext
                ),
                patch.object(
                    fixture.shutil,
                    "which",
                    return_value=str(Path(sys.executable).resolve()),
                ),
                patch.object(fixture, "run", side_effect=completed),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                code = fixture.main(
                    ["--opencode-v2", str(binary), "--work-dir", str(parent)]
                )
            self.assertEqual(code, 1)
            self.assertIn("report unavailable", errors.getvalue())
            self.assertNotIn("profile passed", output.getvalue())
            roots = [path for path in parent.iterdir() if path.is_dir()]
            self.assertEqual(
                safety.private_json(roots[0], "result.json"),
                {"status": "first-failure"},
            )


if __name__ == "__main__":
    unittest.main()
