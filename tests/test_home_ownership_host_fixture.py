"""Offline safety/process contracts. NO engine or host-mapping acceptance."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import backup_crypto as crypto, backups, oc_box as box
from tools import test_home_ownership_podman as probe

fixture = probe.global_fixture
REAL_PIPELINE = crypto.stream_pipeline
REAL_SOURCE_SNAPSHOT = probe.source_snapshot


class HomeOwnershipHostFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="home-unit-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / (probe.PREFIX + "unit")
        self.root.mkdir(mode=0o700)
        runtime = self.parent / "runtime"
        runtime.mkdir(mode=0o700)
        self.enter(
            patch.dict(
                os.environ,
                {
                    "PATH": os.defpath,
                    "XDG_RUNTIME_DIR": str(runtime),
                    "OC_BOX_CONTAINER": "0",
                },
                clear=True,
            )
        )
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.boot = b"00000000-0000-0000-0000-000000000001\n"
        self.enter(patch.object(fixture, "system_boot_id", return_value=self.boot))
        # Hard tripwires at ALL engine/pipeline host boundaries. Each test must
        # explicitly replace only the boundary it intends to exercise.
        self.box_run = self.enter(
            patch.object(box, "run", side_effect=AssertionError("engine tripwire"))
        )
        self.command = self.enter(
            patch.object(
                backups, "command", side_effect=AssertionError("engine tripwire")
            )
        )
        self.pipeline = self.enter(
            patch.object(
                crypto,
                "stream_pipeline",
                side_effect=AssertionError("pipeline tripwire"),
            )
        )
        fixture.prepare_root(self.root, "tumbleweed")
        fixture.prepare_podman_cli(self.root, Path(sys.executable).resolve())
        self.enter(patch.dict(os.environ, fixture.isolated_env(self.root), clear=True))
        (self.root / "podman-tmp/alive").write_bytes(self.boot)
        self.current = {
            "identity": {
                "checkout": str(probe.APP),
                "version": "1.6.47+gcd912d9",
                "commit": "cd912d9862777c1b959567da94344bfe94eae525",
                "commit_count": 76,
                "dirty": False,
            },
            "audited_sha256": {"synthetic.py": "a" * 64},
        }
        self.enter(probe.invocation_source(self.current))
        self.enter(patch.object(probe, "source_snapshot", return_value=self.current))
        self.purpose = probe.write_purpose(self.root, self.current)
        self.paths = {}
        self.metadata = {}
        for role, item in self.purpose["roles"].items():
            path = Path(item["path"])
            path.mkdir(parents=True, mode=0o700)
            self.paths[role] = path
            self.metadata[role] = {
                "Name": item["name"],
                "Labels": item["labels"],
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(path),
                "Anonymous": False,
            }
            fixture.write_json_new(self.root / ("home-created-" + role + ".json"), item)
        fixture.write_json_new(
            self.root / "home-ready.json", {"roles": self.purpose["roles"]}
        )
        self.output = io.StringIO()
        self.enter(contextlib.redirect_stdout(self.output))
        self.enter(contextlib.redirect_stderr(self.output))

    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def rewrite(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def dispatch(self, ignored_box, *command, **kwargs):
        command = list(command)
        if command[:2] == ["volume", "ls"]:
            value = (
                "\n".join(item["name"] for item in self.purpose["roles"].values())
                + "\n"
            )
        elif command[:2] == ["volume", "inspect"]:
            role = next(
                role
                for role, item in self.purpose["roles"].items()
                if item["name"] == command[2]
            )
            value = json.dumps([self.metadata[role]])
        elif command[:2] == ["volume", "rm"]:
            value = command[2]
        else:
            raise AssertionError("unexpected offline dispatch")
        return subprocess.CompletedProcess(command, 0, value, "")

    def raw_mapping(self):
        uid1 = os.getuid() + 100000
        gid1 = os.getgid() + 200000
        return {
            "euid": 0,
            "egid": 0,
            "uid": f"0 {os.getuid()} 1\n1 {uid1} 65536\n",
            "gid": f"0 {os.getgid()} 1\n1 {gid1} 65536\n",
        }

    def manifest(self):
        value = copy.deepcopy(probe.EXPECTED)
        for name, item in value.items():
            if item[0] == "file":
                item.append(
                    hashlib.sha256(
                        probe.FILES.get(name, b"synthetic-db-bytes")
                    ).hexdigest()
                )
        return value

    def test_host_optimization_and_options_refuse_before_placement_or_source(self):
        for name, argv, host_error, optimized in (
            (
                "container",
                ["--work-dir", "/never"],
                fixture.FixtureError("host-only"),
                0,
            ),
            ("optimized", ["--work-dir", "/never"], None, 1),
            ("missing-parent", [], None, 0),
            (
                "cleanup-plus-staging",
                ["--cleanup", "/never", "--temp-dir", "/never"],
                None,
                0,
            ),
        ):
            with (
                self.subTest(name=name),
                patch.object(fixture, "host_guard", side_effect=host_error),
                patch.object(probe.sys, "flags", SimpleNamespace(optimize=optimized)),
                patch.object(
                    probe,
                    "source_snapshot",
                    side_effect=AssertionError("source before guard"),
                ) as source,
                patch.object(
                    probe.interactive,
                    "fresh_parent",
                    side_effect=AssertionError("placement before guard"),
                ) as placement,
            ):
                self.assertEqual(probe.main(argv), 1)
                source.assert_not_called()
                placement.assert_not_called()
        for flag in (
            "--image",
            "--podman-args",
            "--opencode-v2",
            "--distro",
            "--registry",
            "--mapping",
        ):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                probe.main([flag, "arbitrary"])
        self.command.assert_not_called()
        self.box_run.assert_not_called()

    def test_both_placement_parents_reject_completed_renamed_and_symlinked_roots(self):
        for parent in (self.root, self.root / "temporary", self.parent / "renamed"):
            if parent.name == "renamed":
                parent.mkdir()
                (parent / "fixture-ids.json").write_text("{}")
            with self.subTest(parent=parent), self.assertRaises(fixture.FixtureError):
                probe.fresh_parent(parent)
        link = self.parent / "redirect"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(fixture.FixtureError):
            probe.fresh_parent(link)
        # Exercise main for BOTH flags, not merely the common validator.
        for work, staging in (
            (self.root, None),
            (self.parent, self.root / "temporary"),
        ):
            argv = ["--work-dir", str(work)] + (
                ["--temp-dir", str(staging)] if staging else []
            )
            with (
                patch.object(fixture, "host_guard"),
                patch.object(probe, "source_snapshot", return_value=self.current),
                patch.object(probe.resource, "setrlimit"),
                patch.object(
                    probe.tempfile,
                    "mkdtemp",
                    side_effect=AssertionError("placement escaped"),
                ) as create,
            ):
                self.assertEqual(probe.main(argv), 1)
                create.assert_not_called()

    def test_marker_only_retained_parents_refuse_both_flags_before_source_or_allocation(
        self,
    ):
        markers = (
            *probe.HOME_MARKERS,
            "home-run-original-report.json",
            "home-cleanup-original-report.json",
        )
        for marker in markers:
            retained = self.parent / ("renamed-" + marker)
            retained.mkdir()
            (retained / marker).write_text("retained-evidence")
            child = retained / "descendant"
            child.mkdir()
            for path in (retained, child):
                for flag in ("--work-dir", "--temp-dir"):
                    argv = (
                        ["--work-dir", str(path)]
                        if flag == "--work-dir"
                        else ["--work-dir", str(self.parent), "--temp-dir", str(path)]
                    )
                    with (
                        self.subTest(marker=marker, path=path.name, flag=flag),
                        patch.object(fixture, "host_guard"),
                        patch.object(probe.resource, "setrlimit"),
                        patch.object(
                            probe,
                            "source_snapshot",
                            side_effect=AssertionError(
                                "source before placement refusal"
                            ),
                        ) as source,
                        patch.object(
                            probe.tempfile,
                            "mkdtemp",
                            side_effect=AssertionError(
                                "retained evidence used for placement"
                            ),
                        ) as allocate,
                    ):
                        self.assertEqual(probe.main(argv), 1)
                        source.assert_not_called()
                        allocate.assert_not_called()
                        self.assertEqual(
                            (retained / marker).read_text(), "retained-evidence"
                        )
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_purpose_source_path_and_empty_registry_guards_are_file_only(self):
        self.assertEqual(probe.validate_purpose(self.root, self.current), self.purpose)
        later = copy.deepcopy(self.current)
        later["identity"].update(
            version="1.6.48+g" + "b" * 7, commit="b" * 40, commit_count=77
        )
        self.assertEqual(
            probe.validate_purpose(self.root, later)["source"], self.current
        )
        for name, mutate in (
            ("purpose-root", lambda data: data.update(root="/foreign")),
            (
                "purpose-role",
                lambda data: data["roles"]["source"].update(name="foreign"),
            ),
            (
                "purpose-uid-label",
                lambda data: data["roles"]["destination"]["labels"].update(
                    {probe.LABEL + ".uid": "foreign"}
                ),
            ),
            (
                "purpose-path",
                lambda data: data["roles"]["source"].update(path="/foreign/_data"),
            ),
            ("purpose-schema", lambda data: data.update(schema=True)),
            (
                "source-hash",
                lambda data: data["source"]["audited_sha256"].update(
                    {"synthetic.py": "f" * 64}
                ),
            ),
            (
                "source-dirty",
                lambda data: data["source"]["identity"].update(dirty=True),
            ),
            ("native-hash", lambda data: data["native_sha256"].update(podman="f" * 64)),
        ):
            data = copy.deepcopy(self.purpose)
            mutate(data)
            self.rewrite("home-component.json", data)
            with self.subTest(name=name), self.assertRaises(fixture.FixtureError):
                probe.validate_purpose(self.root, self.current)
        self.rewrite("home-component.json", self.purpose)
        self.rewrite("fixture-ids.json", {"source-a": "a" * 32})
        with self.assertRaises(fixture.FixtureError):
            probe.validate_purpose(self.root, self.current)
        self.rewrite("fixture-ids.json", {})
        marker = self.root / "home-component.json"
        marker.rename(self.root / "original-purpose.json")
        marker.symlink_to(self.root / "original-purpose.json")
        with self.assertRaises(fixture.FixtureError):
            probe.validate_purpose(self.root, self.current)
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_other_family_report_only_parents_refuse_both_flags_before_source(self):
        for marker in (
            "history-purpose.json",
            "history-result.json",
            "edges.json",
            "edges-result.json",
            "edges-corrupt-vault-start-baseline.json",
            "edges-populated-first-owner-report.json",
            "cleanup-purpose.json",
            "full-restore-result.json",
            "downgrade-result.json",
            "retry-result.json",
            "result.json",
            "seed-baseline.json",
            "interactive-start-original-report.json",
            "home-mapping.json",
        ):
            retained = self.parent / ("renamed-" + marker)
            retained.mkdir()
            (retained / marker).write_bytes(b"UNCHANGED_SYNTHETIC_FAILED_EVIDENCE\n")
            child = retained / "nested"
            child.mkdir()
            for path in (retained, child):
                for flag in ("--work-dir", "--temp-dir"):
                    argv = (
                        [flag, str(path)]
                        if flag == "--work-dir"
                        else ["--work-dir", str(self.parent), flag, str(path)]
                    )
                    with (
                        self.subTest(marker=marker, path=path.name, flag=flag),
                        patch.object(fixture, "host_guard"),
                        patch.object(probe.resource, "setrlimit"),
                        patch.object(probe, "source_snapshot") as source,
                        patch.object(probe.tempfile, "mkdtemp") as allocate,
                    ):
                        self.assertEqual(probe.main(argv), 1)
                        source.assert_not_called()
                        allocate.assert_not_called()
                        self.assertEqual(
                            (retained / marker).read_bytes(),
                            b"UNCHANGED_SYNTHETIC_FAILED_EVIDENCE\n",
                        )
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_shared_guard_bytes_are_bound_in_home_source_snapshot(self):
        current_hash = ["a" * 64]

        def file_hash(path):
            return current_hash[0] if path.name == "acceptance_guards.py" else "b" * 64

        with (
            patch.object(
                probe.interactive,
                "source_identity",
                return_value=self.current["identity"],
            ),
            patch.object(probe.interactive, "file_hash", side_effect=file_hash),
        ):
            initial = REAL_SOURCE_SNAPSHOT()
            self.assertEqual(
                initial["audited_sha256"]["tools/acceptance_guards.py"], "a" * 64
            )
            current_hash[0] = "c" * 64
            changed = REAL_SOURCE_SNAPSHOT()
        self.assertNotEqual(initial, changed)
        self.assertEqual(
            initial["audited_sha256"]["tools/acceptance_guards.py"], "a" * 64
        )
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_dangling_literal_markers_refuse_both_parents_before_source_or_allocation(
        self,
    ):
        for marker in ("edges.json", "home-component.json", "home-ready.json"):
            retained = self.parent / ("dangling-" + marker)
            retained.mkdir()
            (retained / marker).symlink_to("missing-synthetic-evidence")
            child = retained / "nested"
            child.mkdir()
            for path in (retained, child):
                for flag in ("--work-dir", "--temp-dir"):
                    argv = (
                        [flag, str(path)]
                        if flag == "--work-dir"
                        else ["--work-dir", str(self.parent), flag, str(path)]
                    )
                    with (
                        self.subTest(marker=marker, path=path.name, flag=flag),
                        patch.object(fixture, "host_guard"),
                        patch.object(probe.resource, "setrlimit"),
                        patch.object(probe, "source_snapshot") as source,
                        patch.object(probe.tempfile, "mkdtemp") as allocate,
                    ):
                        self.assertEqual(probe.main(argv), 1)
                        source.assert_not_called()
                        allocate.assert_not_called()
                        self.assertEqual(
                            os.readlink(retained / marker), "missing-synthetic-evidence"
                        )
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_lifecycle_excludes_concurrent_run_cleanup_and_rejects_redirect(self):
        with probe.lifecycle(self.root), self.assertRaises(fixture.FixtureError):
            with probe.lifecycle(self.root):
                self.fail("concurrent fixture entered")
        lock = self.root / "home-component.lock"
        lock.unlink()
        lock.symlink_to(self.root / "fixture.json")
        with self.assertRaises((fixture.FixtureError, OSError)):
            with probe.lifecycle(self.root):
                self.fail("redirected lock entered")

    def test_missing_stale_invalid_boot_refuses_all_retained_engine_entries(self):
        marker = self.root / "podman-tmp/alive"
        for name, value in (
            ("missing", None),
            ("empty", b""),
            ("invalid", b"bad"),
            ("stale", self.boot.replace(b"1", b"2")),
        ):
            if value is None:
                marker.unlink()
            else:
                marker.write_bytes(value)
            with self.subTest(name=name):
                for operation in (
                    lambda: probe.validate_private_store(self.root),
                    lambda: probe.cleanup(self.root, self.current),
                    lambda: probe.engine(
                        self.root,
                        self.purpose,
                        ["volume", "inspect", self.purpose["roles"]["source"]["name"]],
                    ),
                    lambda: probe.archive(self.root, self.purpose, "create"),
                ):
                    with self.assertRaises(fixture.FixtureError):
                        operation()
                self.box_run.assert_not_called()
                self.command.assert_not_called()
            marker.write_bytes(self.boot)

    def test_private_selector_loss_cannot_fall_back_to_normal_engine(self):
        wrapper = self.root / "bin/podman"
        source = wrapper.read_text()
        command = probe.volume_command("inspect", self.purpose["roles"]["source"])
        for name in ("missing", "modified", "unselected"):
            if name == "missing":
                wrapper.unlink()
            elif name == "modified":
                wrapper.write_text(source + "# changed\n")
            else:
                wrapper.write_text(source)
            with (
                self.subTest(name=name),
                patch.dict(
                    os.environ, {"PATH": os.defpath} if name == "unselected" else {}
                ),
            ):
                with self.assertRaises((fixture.FixtureError, OSError)):
                    probe.engine(self.root, self.purpose, command)
                self.command.assert_not_called()
            wrapper.write_text(source)
            wrapper.chmod(0o700)

    def test_live_source_changes_refuse_every_engine_kind_before_any_query(self):
        probe.validate_purpose(self.root, self.current)
        probe.same_source(self.current)
        commands = [
            probe.volume_command(action, item)
            for item in self.purpose["roles"].values()
            for action in ("exists", "create", "inspect", "rm")
        ]
        commands += [
            [
                "volume",
                "ls",
                "--filter",
                "label=" + probe.LABEL + "=" + self.purpose["token"],
                "--format",
                "{{.Name}}",
            ],
            ["unshare", sys.executable, "-B", "-c", probe.MAP_PROGRAM],
        ]
        commands += [
            [
                "unshare",
                sys.executable,
                "-B",
                "-c",
                probe.DATA_PROGRAM,
                action,
                item["path"],
                json.dumps(probe.EXPECTED),
                json.dumps({key: value.hex() for key, value in probe.FILES.items()}),
            ]
            for role, item in self.purpose["roles"].items()
            for action in (
                ("seed", "inspect") if role == "source" else ("empty", "inspect")
            )
        ]
        operations = [
            (
                str(command[:2]),
                lambda command=command: probe.engine(self.root, self.purpose, command),
            )
            for command in commands
        ]
        operations += [
            ("info", lambda: probe.validate_private_store(self.root)),
            ("cleanup-before-info", lambda: probe.cleanup(self.root, self.current)),
            (
                "capture",
                lambda: crypto.encrypt_stream(
                    probe.archive(self.root, self.purpose, "create"),
                    "public",
                    self.root / "home.age",
                ),
            ),
            (
                "extract",
                lambda: crypto.decrypt_stream(
                    self.root / "home.age",
                    b"memory-only",
                    probe.archive(self.root, self.purpose, "extract"),
                ),
            ),
        ]
        changed = {**self.current, "audited_sha256": {}}
        for change in (changed, fixture.FixtureError("clean-source-required")):
            for name, operation in operations:
                with (
                    self.subTest(change=type(change).__name__, operation=name),
                    patch.object(probe, "source_snapshot", side_effect=[change]),
                ):
                    with self.assertRaises(fixture.FixtureError):
                        operation()
                    self.box_run.assert_not_called()
                    self.command.assert_not_called()
                    self.pipeline.assert_not_called()

    def test_live_source_change_after_store_refuses_cleanup_before_volume_list(self):
        live = [self.current]

        def after_store(root):
            probe.validate_purpose(root, self.current)
            live[0] = {**self.current, "audited_sha256": {}}

        with (
            patch.object(probe, "source_snapshot", side_effect=lambda: live[0]),
            patch.object(
                probe, "validate_private_store", side_effect=after_store
            ) as store,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                probe.cleanup(self.root, self.current)
            store.assert_called_once_with(self.root)
        self.box_run.assert_not_called()
        self.command.assert_not_called()

    def test_source_change_between_info_observations_stops_the_next_query(self):
        for change_after in (1, 2):
            live = [self.current]
            calls = []

            def info(command, **kwargs):
                calls.append(command)
                if len(calls) == change_after:
                    live[0] = {**self.current, "audited_sha256": {}}
                return SimpleNamespace(stdout="{}")

            with (
                self.subTest(change_after=change_after),
                patch.object(probe, "source_snapshot", side_effect=lambda: live[0]),
                patch.object(fixture, "validate_store_info"),
                patch.object(box, "run", side_effect=info),
            ):
                with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                    probe.validate_private_store(self.root)
                self.assertEqual(len(calls), change_after)
        self.command.assert_not_called()

    def test_archive_dispatch_rechecks_live_source_after_metadata_before_pipeline(self):
        for action in ("create", "extract"):
            live = [self.current]

            def inspected(root, purpose, role):
                probe.same_source(self.current)
                live[0] = {**self.current, "audited_sha256": {}}
                return self.paths[role]

            with (
                self.subTest(action=action),
                patch.object(probe, "source_snapshot", side_effect=lambda: live[0]),
                patch.object(probe, "inspect_volume", side_effect=inspected),
            ):
                with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                    if action == "create":
                        crypto.encrypt_stream(
                            probe.archive(self.root, self.purpose, action),
                            "public",
                            self.root / "home.age",
                        )
                    else:
                        crypto.decrypt_stream(
                            self.root / "home.age",
                            b"memory-only",
                            probe.archive(self.root, self.purpose, action),
                        )
                self.box_run.assert_not_called()
                self.command.assert_not_called()
                self.pipeline.assert_not_called()

    def test_invocation_context_is_required_and_execute_restores_it_on_all_exits(self):
        with probe.invocation_source(None):
            for operation in (
                lambda: probe.validate_private_store(self.root),
                lambda: probe.inspect_volume(self.root, self.purpose, "source"),
                lambda: probe.archive(self.root, self.purpose, "extract"),
            ):
                with self.assertRaisesRegex(fixture.FixtureError, "invocation-context"):
                    operation()
        self.box_run.assert_not_called()
        self.command.assert_not_called()
        later = {
            **self.current,
            "identity": {**self.current["identity"], "commit": "b" * 40},
        }

        def failed_operation():
            self.assertIs(probe.INVOCATION_SOURCE.get(), later)
            raise RuntimeError("private detail")

        with (
            patch.object(probe, "source_snapshot", return_value=later),
            patch.object(fixture, "private_output", contextlib.nullcontext),
        ):
            self.assertEqual(
                probe.execute(self.root, "run", later, failed_operation), 1
            )
            self.assertIs(probe.INVOCATION_SOURCE.get(), self.current)
            with patch.object(
                fixture, "write_json_new", side_effect=OSError("private report detail")
            ):
                self.assertEqual(probe.execute(self.root, "run", later, lambda: {}), 1)
            self.assertIs(probe.INVOCATION_SOURCE.get(), self.current)
            with probe.lifecycle(self.root), self.assertRaises(fixture.FixtureError):
                probe.execute(self.root, "run", later, lambda: {})
            self.assertIs(probe.INVOCATION_SOURCE.get(), self.current)

    def test_docs_only_cleanup_checks_current_invocation_not_original_run_source(self):
        later = copy.deepcopy(self.current)
        later["identity"].update(
            version="1.6.48+g" + "b" * 7, commit="b" * 40, commit_count=77
        )
        original_purpose = (self.root / "home-component.json").read_bytes()
        with (
            patch.object(probe, "source_snapshot", return_value=later),
            patch.object(box, "run", return_value=SimpleNamespace(stdout="{}")) as info,
            patch.object(fixture, "validate_store_info"),
            patch.object(backups, "command", side_effect=self.dispatch) as native,
            patch.object(fixture, "private_output", contextlib.nullcontext),
        ):
            self.assertEqual(
                probe.execute(
                    self.root, "cleanup", later, lambda: probe.cleanup(self.root, later)
                ),
                0,
            )
            self.assertEqual(info.call_count, 3)
            self.assertEqual(
                [call.args[2] for call in native.call_args_list],
                ["ls", "inspect", "inspect", "rm", "rm"],
            )
        report = json.loads(
            next(self.root.glob("home-cleanup-*-report.json")).read_text()
        )
        self.assertEqual(report["source"], later)
        self.assertEqual(report["proof"]["run_source"], self.current)
        self.assertEqual(
            (self.root / "home-component.json").read_bytes(), original_purpose
        )
        self.assertIs(probe.INVOCATION_SOURCE.get(), self.current)

    def test_initial_store_checks_three_environments_but_only_first_may_initialize(
        self,
    ):
        marker = self.root / "podman-tmp/alive"
        marker.unlink()
        calls = []

        def info(command, **kwargs):
            calls.append(kwargs["env"])
            marker.write_bytes(self.boot)
            store = {
                "graphDriverName": "overlay",
                "graphRoot": str(self.root / "data/containers/storage"),
                "runRoot": str(self.root / "podman-run"),
                "volumePath": str(self.root / "data/containers/storage/volumes"),
                "configFile": str(self.root / "config/containers/storage.conf"),
                "imageCopyTmpDir": kwargs["env"]["TMPDIR"],
            }
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "host": {
                            "security": {"rootless": True},
                            "serviceIsRemote": False,
                        },
                        "store": store,
                    }
                ),
                "",
            )

        with patch.object(box, "run", side_effect=info):
            probe.validate_private_store(self.root, initial=True)
        self.assertEqual(len(calls), 3)
        self.assertIn("CONTAINERS_STORAGE_CONF", calls[0])
        self.assertNotIn("CONTAINERS_STORAGE_CONF", calls[2])
        self.assertEqual(
            [env["PATH"].split(":")[0] for env in calls], [str(self.root / "bin")] * 3
        )
        marker.unlink()
        with patch.object(box, "run", return_value=None) as native:
            # First call fails before its response is interpreted: no second
            # info can initialize a missing marker silently.
            with patch.object(fixture, "validate_store_info"):
                native.return_value = SimpleNamespace(stdout="{}")
                with self.assertRaises(fixture.FixtureError):
                    probe.validate_private_store(self.root, initial=True)
            self.assertEqual(native.call_count, 1)

    def test_command_allowlist_has_exact_local_volume_and_fixed_unshare_commands_only(
        self,
    ):
        item = self.purpose["roles"]["source"]
        create = probe.volume_command("create", item)
        self.assertEqual(
            create[:5], ["volume", "create", "--driver", "local", "--label"]
        )
        self.assertEqual(create[-1], item["name"])
        self.assertEqual(create.count("--label"), 3)
        for command in (
            ["pull", "image"],
            ["create", "container"],
            ["run", "image"],
            ["build", "."],
            ["login"],
            ["image", "rm", "image"],
            ["system", "prune"],
            ["volume", "rm", "--force", item["name"]],
            ["volume", "create", "--ignore", item["name"]],
            ["volume", "inspect", "foreign"],
            ["unshare", "sh", "-c", "true"],
            [
                "unshare",
                sys.executable,
                "-B",
                "-c",
                probe.DATA_PROGRAM,
                "seed",
                self.purpose["roles"]["destination"]["path"],
                json.dumps(probe.EXPECTED),
                "{}",
            ],
        ):
            with (
                self.subTest(command=command[:3]),
                self.assertRaises(fixture.FixtureError),
            ):
                probe.engine(self.root, self.purpose, command)
        self.command.assert_not_called()
        with patch.object(backups, "command", side_effect=self.dispatch) as native:
            probe.engine(self.root, self.purpose, probe.volume_command("inspect", item))
        self.assertEqual(native.call_args.args[1:], ("volume", "inspect", item["name"]))

    def test_volume_inspection_rejects_foreign_driver_options_backing_labels_paths_and_links(
        self,
    ):
        original = copy.deepcopy(self.metadata["source"])
        for name, change in (
            ("name", {"Name": "foreign"}),
            ("labels", {"Labels": {}}),
            ("role", {"Labels": self.metadata["destination"]["Labels"]}),
            ("driver", {"Driver": "image"}),
            ("plugin", {"Driver": "foreign"}),
            ("options", {"Options": {"device": "/foreign"}}),
            ("image-backing", {"StorageID": "image"}),
            ("mount", {"Mountpoint": str(self.paths["destination"])}),
            ("anonymous", {"Anonymous": True}),
        ):
            self.metadata["source"] = {**copy.deepcopy(original), **change}
            with (
                self.subTest(name=name),
                patch.object(backups, "command", side_effect=self.dispatch),
                self.assertRaises(fixture.FixtureError),
            ):
                probe.inspect_volume(self.root, self.purpose, "source")
        self.metadata["source"] = original
        path = self.paths["source"]
        path.rmdir()
        path.symlink_to(self.paths["destination"], target_is_directory=True)
        with (
            patch.object(backups, "command", side_effect=self.dispatch),
            self.assertRaises(fixture.FixtureError),
        ):
            probe.inspect_volume(self.root, self.purpose, "source")

    def test_preexisting_or_indeterminate_second_volume_refuses_before_either_create(
        self,
    ):
        for result in (0, 2):
            calls = []

            def exists(ignored_box, *command, **kwargs):
                calls.append(command)
                self.assertEqual(command[:2], ("volume", "exists"))
                return SimpleNamespace(returncode=1 if len(calls) == 1 else result)

            with (
                self.subTest(exit=result),
                patch.object(backups, "command", side_effect=exists),
            ):
                with self.assertRaisesRegex(fixture.FixtureError, "volume-preexisting"):
                    probe.create_volumes(self.root, self.purpose)
            self.assertEqual(len(calls), 2)

    def test_host_stat_independently_translates_numeric_ids_not_merely_copied_content(
        self,
    ):
        # Ordinary offline files are NOT UID-1 evidence. Simulate only the
        # sentinel's HOST stat result to exercise translation without chown.
        root = self.paths["source"]
        for name in (".local", ".config"):
            (root / name).mkdir()
        for name, item in probe.EXPECTED.items():
            if item[0] == "file":
                (root / name).write_bytes(probe.FILES.get(name, b"synthetic-db-bytes"))
            (root / name).chmod(item[3])
        measured = probe.mapping(self.raw_mapping())
        original = Path.lstat

        def metadata(path):
            value = original(path)
            if path == root / "mapped-sentinel":
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=measured["uid"][1],
                    st_gid=measured["gid"][1],
                    st_nlink=value.st_nlink,
                    st_size=value.st_size,
                )
            return value

        with patch.object(Path, "lstat", metadata):
            self.assertEqual(probe.host_manifest(root, measured), probe.EXPECTED)
        # Same bytes/mode with invoking-user ownership MUST fail the fixed
        # namespace-1 sentinel contract, even if a copy would match perfectly.
        with self.assertRaisesRegex(fixture.FixtureError, "fixed-manifest"):
            probe.host_manifest(root, measured)
        wrong = copy.deepcopy(measured)
        wrong["uid"][0] += 1
        with self.assertRaisesRegex(fixture.FixtureError, "host-ownership"):
            probe.host_manifest(root, wrong)

    def test_dirty_source_refuses_before_retained_access_and_changed_return_cannot_pass(
        self,
    ):
        with (
            patch.object(
                probe,
                "source_snapshot",
                side_effect=fixture.FixtureError("clean-source-required"),
            ),
            patch.object(fixture, "host_guard"),
            patch.object(probe.resource, "setrlimit"),
            patch.object(
                probe,
                "validate_purpose",
                side_effect=AssertionError("retained access before source"),
            ) as validate,
        ):
            self.assertEqual(probe.main(["--cleanup", str(self.root)]), 1)
            validate.assert_not_called()
        with (
            patch.object(
                probe,
                "source_snapshot",
                return_value={**self.current, "audited_sha256": {}},
            ),
            patch.object(fixture, "private_output", contextlib.nullcontext),
        ):
            self.assertEqual(
                probe.execute(
                    self.root, "run", self.current, lambda: {"component": True}
                ),
                1,
            )
        report = json.loads(next(self.root.glob("home-run-*-report.json")).read_text())
        self.assertEqual(report["source"], self.current)
        self.assertEqual(report["primary_failure"], "source-changed")
        self.assertEqual(report["outcome"], "failed")

    def test_measured_mapping_named_invalid_and_sufficient_matrices(self):
        raw = self.raw_mapping()
        measured = probe.mapping(raw)
        self.assertEqual(measured["uid"], [os.getuid(), os.getuid() + 100000])
        for name, text in (
            ("empty", ""),
            ("invalid", "0 bad 1"),
            ("zero-count", f"0 {os.getuid()} 0"),
            ("no-one", f"0 {os.getuid()} 1"),
            ("wrong-root", "0 987654 2"),
            ("inside-overlap", f"0 {os.getuid()} 2\n1 200000 2"),
            ("outside-overlap", f"0 {os.getuid()} 1\n1 {os.getuid()} 2"),
            ("overflow", f"0 {os.getuid()} 1\n1 4294967294 5"),
        ):
            with self.subTest(name=name), self.assertRaises(fixture.FixtureError):
                probe.parse_map(text, os.getuid())
        for change in (
            {"euid": 1},
            {"egid": 1},
            {"euid": False},
            {"gid": "0 999999 2"},
        ):
            with self.subTest(change=change), self.assertRaises(fixture.FixtureError):
                probe.mapping({**raw, **change})

    def test_fixed_seed_contract_and_mapping_change_refuse_before_capture(self):
        measured = probe.mapping(self.raw_mapping())
        good = self.manifest()

        def native(ignored_box, *command, **kwargs):
            if command[:2] == ("volume", "inspect"):
                return self.dispatch(ignored_box, *command, **kwargs)
            return SimpleNamespace(
                stdout=json.dumps({"mapping": raw, "manifest": manifest})
            )

        for name, mutate in (
            ("bad-owner", lambda data: data["mapped-sentinel"].__setitem__(1, 0)),
            ("bad-mode", lambda data: data[".local"].__setitem__(3, 0o777)),
            (
                "bad-content",
                lambda data: data[".config/synthetic.json"].__setitem__(4, "0" * 64),
            ),
            (
                "extra-path",
                lambda data: data.update(foreign=["file", 0, 0, 0o600, "0" * 64]),
            ),
        ):
            raw, manifest = self.raw_mapping(), copy.deepcopy(good)
            mutate(manifest)
            with (
                self.subTest(name=name),
                patch.object(backups, "command", side_effect=native),
                self.assertRaises(fixture.FixtureError),
            ):
                probe.namespace(self.root, self.purpose, "source", "seed", measured)
        for name in ("mapped-one", "other-range"):
            raw, manifest = self.raw_mapping(), good
            if name == "mapped-one":
                raw["uid"] = raw["uid"].replace(
                    str(os.getuid() + 100000), str(os.getuid() + 300000)
                )
            else:
                raw["uid"] += f"70000 {os.getuid() + 1000000} 1\n"
            with (
                self.subTest(mapping_change=name),
                patch.object(backups, "command", side_effect=native),
                self.assertRaisesRegex(fixture.FixtureError, "mapping-changed"),
            ):
                probe.namespace(self.root, self.purpose, "source", "inspect", measured)

    def test_mapping_refusal_precedes_any_volume_creation(self):
        # Exercise ordering through the real fresh workflow; setup functions
        # are file-only and already exercised by every other test.
        other = self.parent / (probe.PREFIX + "second")
        other.mkdir(mode=0o700)
        args = SimpleNamespace(temp_dir=None)
        with (
            patch.object(
                probe.shutil, "which", return_value=str(Path(sys.executable).resolve())
            ),
            patch.object(probe, "validate_private_store"),
            patch.object(
                probe,
                "engine",
                return_value=SimpleNamespace(
                    stdout=json.dumps({**self.raw_mapping(), "euid": 1})
                ),
            ),
            patch.object(
                probe,
                "create_volumes",
                side_effect=AssertionError("created before map"),
            ) as create,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "namespace-mapping"):
                probe.run_fresh(other, args, self.current)
            create.assert_not_called()

    def test_exact_production_archive_command_includes_private_owners_and_positive_limit(
        self,
    ):
        with patch.object(backups, "command", side_effect=self.dispatch):
            for action, role in (("create", "source"), ("extract", "destination")):
                with self.subTest(action=action):
                    value = probe.archive(self.root, self.purpose, action)
                    expected = backups.archive_command(
                        box,
                        action,
                        self.paths[role],
                        private=True,
                        **({"limit": probe.LIMIT} if action == "extract" else {}),
                    )
                    self.assertEqual(value, expected)
                    self.assertEqual(value[:2], ["podman", "unshare"])
            with (
                patch.object(
                    backups, "archive_command", return_value=["podman", "run", "image"]
                ),
                self.assertRaisesRegex(fixture.FixtureError, "archive-command"),
            ):
                probe.archive(self.root, self.purpose, "create")

    def test_transport_refuses_source_changes_and_destination_not_fixed_or_not_empty(
        self,
    ):
        good = self.manifest()
        measured = probe.mapping(self.raw_mapping())
        for name in (
            "source-after-capture",
            "source-after-restore",
            "destination",
            "not-empty",
        ):
            changed = copy.deepcopy(good)
            changed[".local/history.db"][4] = "f" * 64
            calls = []

            def namespace(root, purpose, role, action, mapping):
                calls.append((role, action))
                if action == "empty":
                    if name == "not-empty":
                        raise fixture.FixtureError("destination-empty")
                    return {".": good["."]}
                if (
                    (
                        name == "source-after-capture"
                        and calls.count(("source", "inspect")) == 1
                        and role == "source"
                        and action == "inspect"
                    )
                    or (
                        name == "source-after-restore"
                        and calls.count(("source", "inspect")) == 2
                        and role == "source"
                        and action == "inspect"
                    )
                    or (name == "destination" and role == "destination")
                ):
                    return changed
                return good

            with (
                self.subTest(name=name),
                patch.object(probe, "preflight", return_value=self.paths),
                patch.object(probe, "namespace", side_effect=namespace),
                patch.object(probe, "host_manifest", return_value=probe.EXPECTED),
                patch.object(
                    crypto, "small", return_value=("age1" + "a" * 58).encode()
                ),
                patch.object(probe, "archive", return_value=["production-command"]),
                patch.object(crypto, "encrypt_stream") as encrypt,
                patch.object(crypto, "decrypt_stream") as decrypt,
            ):
                with self.assertRaises(fixture.FixtureError):
                    probe.transport(self.root, self.purpose, measured)
                self.assertEqual(encrypt.call_count, 0 if name == "not-empty" else 1)
                self.assertEqual(
                    decrypt.call_count,
                    0 if name in ("not-empty", "source-after-capture") else 1,
                )

    def test_cleanup_preflights_both_before_remove_and_preserves_every_file(self):
        for name in ("home.age", "unrelated-image-evidence.json", "old-report.json"):
            (self.root / name).write_bytes(b"SYNTHETIC_RETAINED")
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        with (
            patch.object(probe, "validate_private_store") as store,
            patch.object(probe, "same_source"),
            patch.object(backups, "command", side_effect=self.dispatch) as native,
        ):
            value = probe.cleanup(self.root, self.current)
        store.assert_called_once_with(self.root)
        self.assertEqual(value["run_source"], self.current)
        calls = [list(call.args[1:]) for call in native.call_args_list]
        self.assertEqual(
            [command[1] for command in calls], ["ls", "inspect", "inspect", "rm", "rm"]
        )
        self.assertTrue(all("--force" not in command for command in calls))
        self.assertEqual(
            before,
            {
                path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )
        self.assertTrue(all(path.is_dir() for path in self.paths.values()))

    def test_cleanup_foreign_missing_unjournaled_or_unexpected_candidate_never_removes_anything(
        self,
    ):
        original = copy.deepcopy(self.metadata["destination"])
        for name in ("foreign", "missing", "unexpected", "journal"):
            self.metadata["destination"] = copy.deepcopy(original)
            if name == "foreign":
                self.metadata["destination"]["Labels"] = {}

            def dispatch(ignored_box, *command, **kwargs):
                if (
                    name == "missing"
                    and command[:2] == ("volume", "inspect")
                    and command[2] == self.purpose["roles"]["destination"]["name"]
                ):
                    raise crypto.BackupError("private engine detail")
                result = self.dispatch(ignored_box, *command, **kwargs)
                if name == "unexpected" and command[:2] == ("volume", "ls"):
                    result.stdout += "foreign-same-label\n"
                return result

            self.rewrite(
                "home-ready.json",
                {"roles": self.purpose["roles"]} if name != "journal" else {},
            )
            with (
                self.subTest(name=name),
                patch.object(probe, "validate_private_store"),
                patch.object(probe, "same_source"),
                patch.object(backups, "command", side_effect=dispatch) as native,
            ):
                with self.assertRaises((fixture.FixtureError, crypto.BackupError)):
                    probe.cleanup(self.root, self.current)
                self.assertFalse(
                    any(
                        call.args[1:3] == ("volume", "rm")
                        for call in native.call_args_list
                    )
                )

    def test_failure_evidence_is_new_redacted_and_preserves_primary_through_late_errors(
        self,
    ):
        prior = self.root / "home-run-original-report.json"
        prior.write_text("old-evidence")
        failure = fixture.FixtureError("source-preservation")
        with (
            patch.object(
                probe, "same_source", side_effect=fixture.FixtureError("source-changed")
            ),
            patch.object(fixture, "private_output", contextlib.nullcontext),
        ):
            self.assertEqual(
                probe.execute(
                    self.root,
                    "run",
                    self.current,
                    lambda: (_ for _ in ()).throw(failure),
                ),
                1,
            )
        report_path = next(
            path for path in self.root.glob("home-run-*-report.json") if path != prior
        )
        report = json.loads(report_path.read_text())
        self.assertEqual(report["primary_failure"], "source-preservation")
        self.assertEqual(report["secondary_failures"], ["source-changed"])
        self.assertEqual(prior.read_text(), "old-evidence")
        with (
            patch.object(probe, "same_source"),
            patch.object(fixture, "private_output", contextlib.nullcontext),
            patch.object(
                fixture, "write_json_new", side_effect=OSError("PRIVATE_KEY_OR_STDERR")
            ),
        ):
            self.assertEqual(
                probe.execute(
                    self.root,
                    "run",
                    self.current,
                    lambda: (_ for _ in ()).throw(failure),
                ),
                1,
            )
        self.assertIn("category=source-preservation", self.output.getvalue())
        self.assertNotIn("PRIVATE_KEY_OR_STDERR", self.output.getvalue())
        self.assertEqual(
            probe.category(RuntimeError("PRIVATE_KEY_OR_STDERR")), "exception"
        )

    @unittest.skipUnless(
        shutil.which("age")
        and shutil.which("age-keygen")
        and hasattr(os, "memfd_create"),
        "installed age + Linux memfd required; never install for test",
    )
    def test_real_age_production_stream_with_fake_unshare_is_offline_only(self):
        # This is real archive/age/memfd transport, NOT an actual UID-1 mapping
        # or Podman test. Ordinary fixture files retain this process's UID/GID.
        source, destination = self.paths["source"], self.paths["destination"]
        source.chmod(0o750)
        (source / "synthetic.txt").write_bytes(b"OFFLINE_ONLY_COMPONENT")
        (source / "synthetic.txt").chmod(0o640)
        with sqlite3.connect(source / "history.db") as db:
            db.execute("CREATE TABLE fixture (value TEXT)")
            db.execute("INSERT INTO fixture VALUES ('OFFLINE_ONLY_HISTORY')")
        db.close()
        producer = backups.archive_command(box, "create", source, private=True)
        consumer = backups.archive_command(
            box, "extract", destination, private=True, limit=probe.LIMIT
        )
        wrapper = self.root / "bin/podman"
        # Exact two production commands, dispatching to actual host Python only.
        wrapper.write_text(
            "#!"
            + str(Path(sys.executable).resolve())
            + "\nimport os,sys\nallowed="
            + repr([producer[1:], consumer[1:]])
            + "\nif sys.argv[1:] not in allowed: raise SystemExit(99)\nos.execv(sys.argv[2],sys.argv[2:])\n"
        )
        wrapper.chmod(0o700)
        observed = []
        private = None

        def checked(first, second, **kwargs):
            observed.append((first, second))
            if first[:2] == ["age", "--decrypt"]:
                fd = kwargs["first_fds"][0]
                self.assertEqual(
                    first[first.index("--identity") + 1], f"/proc/self/fd/{fd}"
                )
                self.assertIn("memfd:", os.readlink(f"/proc/self/fd/{fd}"))
                self.assertEqual(os.pread(fd, len(private), 0), private)
            self.assertNotIn(private.decode(), json.dumps([first, second]))
            return REAL_PIPELINE(first, second, **kwargs)

        original = {
            path.name: (
                path.read_bytes(),
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_mode,
            )
            for path in source.iterdir()
        }
        with (
            patch.dict(os.environ, fixture.isolated_env(self.root), clear=True),
            patch.object(crypto, "stream_pipeline", side_effect=checked),
        ):
            private = crypto.small(["age-keygen"])
            recipient = (
                crypto.small(["age-keygen", "-y"], data=private).decode().strip()
            )
            path = self.root / "home.age"
            crypto.encrypt_stream(producer, recipient, path)
            crypto.decrypt_stream(path, private, consumer)
        self.assertEqual(len(observed), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            destination.stat().st_mode & 0o777, 0o700
        )  # root mode intentionally NOT archived
        self.assertEqual(source.stat().st_mode & 0o777, 0o750)
        self.assertEqual(
            original,
            {
                path.name: (
                    path.read_bytes(),
                    path.stat().st_uid,
                    path.stat().st_gid,
                    path.stat().st_mode,
                )
                for path in source.iterdir()
            },
        )
        for name, (content, uid, gid, mode) in original.items():
            restored = destination / name
            self.assertEqual(
                (
                    restored.read_bytes(),
                    restored.stat().st_uid,
                    restored.stat().st_gid,
                    restored.stat().st_mode,
                ),
                (content, uid, gid, mode),
            )
        for persisted in self.root.rglob("*"):
            if persisted.is_file():
                self.assertNotIn(private, persisted.read_bytes())
                self.assertNotIn(b"AGE-SECRET-KEY-1", persisted.read_bytes())
        self.assertFalse(any(path.suffix == ".tar" for path in self.root.rglob("*")))
        self.command.assert_not_called()
        self.box_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
