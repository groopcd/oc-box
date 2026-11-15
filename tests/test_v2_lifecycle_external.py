"""Offline adapter contracts only: no native, Podman, vault or host acceptance."""

import argparse
import contextlib
import copy
import io
import json
import importlib.util
import marshal
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from tools import test_v2_lifecycle_external as external
from tools import test_v2_lifecycle_podman as fixture

box, safety, service = fixture.box, fixture.safety, fixture.workspace_service


def baseline(project):
    return {
        "info": {
            "id": "ses_synthetic_host_seed",
            "location": {"directory": str(project)},
            "time": {"created": 1, "updated": 1},
        },
        "messages": [
            {"id": "msg_synthetic_seed", "type": "user", "text": "SYNTHETIC_SEED"}
        ],
    }


class ImportSelectionTests(unittest.TestCase):
    def exercise(self, parent, corrected):
        """Real production export/import/selection; only external transports fake."""
        root = parent / "fixture"
        project = root / "source-a"
        project.mkdir(parents=True)
        path = root / "registry/workspaces" / ("a" * 32) / "workspace.json"
        data = {
            "id": "a" * 32,
            "version": 2,
            "opencode_version": "2.3.0",
            "project": str(project),
            "distro": "tumbleweed",
            "home": str(root / "home"),
            "container": "synthetic-container",
        }
        box.atomic_json(path, data)
        seed = baseline(project)
        native = {}  # No preloaded container transcript or forced selection.
        transfers, host_exports, created, callbacks = [], [], [], []

        def transport(command, **kwargs):
            if command[:1] == ["SYNTHETIC_HOST"]:
                self.assertEqual(command[1:], ["--pure", "export", seed["info"]["id"]])
                host_exports.append(True)
                return subprocess.CompletedProcess(command, 0, json.dumps(seed), "")
            payload = json.loads(kwargs["input"])
            self.assertEqual(native, {})
            transfers.append(payload["info"]["id"])
            native[payload["info"]["id"]] = payload
            return subprocess.CompletedProcess(command, 0, "", "")

        def history(_data, action, **settings):
            if action == "sessions":
                value = [item["info"] for item in native.values()]
            elif action == "export":
                value = native[settings["session"]]
            else:
                raise AssertionError("Unexpected synthetic history transport")
            return subprocess.CompletedProcess([], 0, json.dumps(value), "")

        def api(_box, _data, _connection, method, target, payload=None):
            self.assertEqual((method, target), ("POST", "/api/session"))
            ident = "ses_new_" + str(len(created) + 1)
            created.append(ident)
            native[ident] = {
                "info": {
                    "id": ident,
                    "location": payload["location"],
                    "time": {"updated": 2},
                },
                "messages": [],
            }
            return {"data": {"id": ident}}

        owner = types.SimpleNamespace(
            connection={"url": "synthetic", "password": "synthetic"},
            publish=Mock(),
            wait_clients=Mock(),
        )

        def callback(command):
            # The actual production command and persisted state determine this ID.
            ident = safety.selected_session(command, path, seed["info"]["id"])
            actual = box.export_container_session(data, ident)
            self.assertEqual(actual["messages"], seed["messages"])
            callbacks.append(ident)
            return 0

        def delegate(arguments):
            args = box.parser().parse_args(arguments)
            return box.launch_environment(path, data, args)

        adapter = external.Adapter(fixture, parent)
        adapter.root, adapter.baseline = root, seed
        patches = (
            patch.object(
                box,
                "loading_stage",
                side_effect=lambda *a, **k: contextlib.nullcontext(),
            ),
            patch.object(box, "check_private_output"),
            patch.object(box, "detect_opencode_release", return_value="2.3.0"),
            patch.object(box, "start_container", return_value={}),
            patch.object(box, "prepare_runtime"),
            patch.object(box, "stop_container"),
            patch.object(box, "private_host_env", return_value={}),
            patch.object(
                box, "host_command", side_effect=lambda d, *a: ["SYNTHETIC_HOST", *a]
            ),
            patch.object(box, "exec_command", side_effect=lambda d, args: args),
            patch.object(box, "run", side_effect=transport),
            patch.object(box, "run_v2_history", side_effect=history),
            patch.object(box.workspace_control, "capture", return_value=None),
            patch.object(box.history_recovery, "capture", return_value=None),
            patch.object(
                service, "Owner", side_effect=lambda *a: contextlib.nullcontext(owner)
            ),
            patch.object(service, "api", side_effect=api),
            patch.object(
                service,
                "client_command",
                side_effect=lambda b, d, c, command, **k: command,
            ),
            patch.object(service, "wait_history"),
            patch.object(box, "sync_sessions"),
            patch.object(box, "run_tui", side_effect=callback),
            patch.object(
                fixture, "workspace_records", return_value={"source-a": (path, data)}
            ),
            contextlib.redirect_stdout(io.StringIO()),
        )
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            arguments = ["start", "--workspace", data["id"]]
            if corrected:
                self.assertEqual(adapter.launcher(delegate, arguments), 0)
                self.assertEqual(transfers, [seed["info"]["id"]])
                self.assertEqual(host_exports, [True])
                self.assertEqual(created, [])
                self.assertEqual(callbacks, [seed["info"]["id"]])
                state = box.session_state(path)
                self.assertEqual(state["last"], seed["info"]["id"])
                self.assertIn(seed["info"]["id"], state["imported_from_host"])
                evidence = path.parent / "imports-v2" / (seed["info"]["id"] + ".json")
                self.assertEqual(box.read_json(evidence)["messages"], seed["messages"])
            else:
                with self.assertRaisesRegex(safety.FixtureError, "^selected-session$"):
                    delegate(arguments)
                self.assertEqual(transfers, [])
                self.assertEqual(host_exports, [])
                self.assertEqual(len(created), 1)
                self.assertIsNone(box.session_state(path)["last"])
                self.assertEqual(callbacks, [])
        self.assertEqual(arguments, ["start", "--workspace", data["id"]])

    def test_original_no_import_refuses_and_adapter_imports_through_production(self):
        for corrected in (False, True):
            with (
                self.subTest(corrected=corrected),
                tempfile.TemporaryDirectory() as name,
            ):
                self.exercise(Path(name), corrected)


class AdapterTests(unittest.TestCase):
    def setup_adapter(self, root):
        adapter = external.Adapter(fixture, root.parent)
        adapter.root = root
        adapter.baseline = baseline(root / "source-a")
        records = {
            name: (
                root / "registry" / str(index),
                {"id": str(index) * 32, "project": str(root / name)},
            )
            for index, name in enumerate(
                ("source-a", "restored-full", "restored-portable"), 1
            )
        }
        return adapter, records

    def test_exact_five_launches_append_seed_once_without_mutating_input(self):
        adapter, records = self.setup_adapter(Path("/example/fresh"))
        forwarded = []
        with patch.object(fixture, "workspace_records", return_value=records):
            for command, name in external.SEQUENCE:
                arguments = [command, "--workspace", records[name][1]["id"]]
                adapter.launcher(lambda value: forwarded.append(value), arguments)
                self.assertEqual(len(arguments), 3)
            with self.assertRaisesRegex(external.Refusal, "launch-contract"):
                adapter.launcher(
                    Mock(), ["start", "--workspace", records["source-a"][1]["id"]]
                )
        self.assertEqual(adapter.imports, 1)
        self.assertEqual(
            forwarded[0][-2:], ["--import-session", adapter.baseline["info"]["id"]]
        )
        self.assertTrue(all("--import-session" not in args for args in forwarded[1:]))
        self.assertTrue(all(item["completed"] for item in adapter.ledger))

    def test_contract_refusals_never_forward_or_consume_import(self):
        mutations = (
            lambda good: ["reset", *good[1:]],
            lambda good: [*good, "--import-session", "ses_foreign"],
            lambda good: [*good, "--new"],
            lambda good: [good[0], good[1], "f" * 32],
            lambda good: tuple(good),
            lambda good: ["start", "--project", "/foreign"],
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                adapter, records = self.setup_adapter(Path("/example/fresh"))
                delegate = Mock()
                good = ["start", "--workspace", records["source-a"][1]["id"]]
                with patch.object(fixture, "workspace_records", return_value=records):
                    with self.assertRaises(external.Refusal):
                        adapter.launcher(delegate, mutate(good))
                delegate.assert_not_called()
                self.assertEqual(adapter.imports, 0)
        adapter, records = self.setup_adapter(Path("/example/fresh"))
        records["source-a"][1]["project"] = "/foreign"
        with patch.object(fixture, "workspace_records", return_value=records):
            with self.assertRaises(external.Refusal):
                adapter.launcher(Mock(), ["start", "--workspace", "1" * 32])

    def test_fresh_root_empty_private_single_allocation_only(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name)
            adapter = external.Adapter(fixture, parent)
            root = parent / (fixture.PREFIX + "synthetic")
            root.mkdir(mode=0o700)
            actual = Mock()
            adapter.prepare_root(actual, root, "tumbleweed", Path("/synthetic"))
            actual.assert_called_once()
            with self.assertRaisesRegex(external.Refusal, "fresh-root"):
                adapter.prepare_root(actual, root, "tumbleweed", Path("/synthetic"))
            for kind in ("retained", "public", "wrong-prefix", "symlink"):
                with self.subTest(kind=kind):
                    adapter = external.Adapter(fixture, parent)
                    candidate = parent / (fixture.PREFIX + kind)
                    candidate.mkdir(mode=0o700)
                    if kind == "retained":
                        (candidate / "result.json").write_text("{}")
                    elif kind == "public":
                        candidate.chmod(0o755)
                    elif kind == "wrong-prefix":
                        candidate.rename(parent / "foreign")
                        candidate = parent / "foreign"
                    else:
                        alias = parent / (fixture.PREFIX + "alias")
                        alias.symlink_to(candidate)
                        candidate = alias
                    with self.assertRaises(external.Refusal):
                        adapter.prepare_root(Mock(), candidate)

    def test_baseline_forwarded_validated_once_no_extra_native_operations(self):
        adapter, _ = self.setup_adapter(Path("/example/fresh"))
        seed, adapter.baseline = adapter.baseline, None
        result = (
            Path("/synthetic"),
            {"version": "2.3.0", "sha256": external.NATIVE_SHA256},
            seed,
            {},
        )
        actual = Mock(return_value=result)
        self.assertIs(
            adapter.prepare_launch(actual, adapter.root, "/native", "2.3.0", {}), result
        )
        actual.assert_called_once()
        with self.assertRaisesRegex(external.Refusal, "baseline-contract"):
            adapter.prepare_launch(actual, adapter.root, "/native", "2.3.0", {})
        for field in ("foreign", "child", "version", "hash"):
            with self.subTest(field=field):
                bad = copy.deepcopy(result)
                bad = list(bad)
                if field == "foreign":
                    bad[2]["info"]["location"]["directory"] = "/foreign"
                elif field == "child":
                    bad[2]["info"]["parentID"] = "ses_child"
                elif field == "version":
                    bad[1]["version"] = "2.4.0"
                else:
                    bad[1]["sha256"] = "0" * 64
                adapter.baseline = None
                with self.assertRaises(external.Refusal):
                    adapter.prepare_launch(
                        Mock(return_value=tuple(bad)),
                        adapter.root,
                        "/native",
                        "2.3.0",
                        {},
                    )

    def test_cleanup_all_forwarded_once_primary_unmasked_and_alive_refused(self):
        for alive in (False, True):
            with self.subTest(alive=alive):
                adapter = external.Adapter(fixture, Path("/example"))
                primary = safety.FixtureError("selected-session")
                adapter.fail(primary, "selected-session")
                server = Mock()
                worker = Mock()
                server.shutdown.side_effect = OSError("PRIVATE_SHUTDOWN")
                server.server_close.side_effect = ValueError("PRIVATE_CLOSE")
                worker.join.side_effect = RuntimeError("PRIVATE_JOIN")
                worker.is_alive.return_value = alive
                wrapped, wrapped_worker = adapter.make_provider(
                    lambda: (server, worker)
                )
                wrapped.shutdown()
                wrapped.server_close()
                wrapped_worker.join(timeout=3)
                server.shutdown.assert_called_once_with()
                server.server_close.assert_called_once_with()
                worker.join.assert_called_once_with(timeout=3)
                self.assertEqual(adapter.primary["category"], "selected-session")
                self.assertEqual(len(adapter.cleanup_errors), 3 + int(alive))
                self.assertEqual(adapter.cleanup["worker_alive"], alive)
                self.assertNotIn("PRIVATE", json.dumps(adapter.cleanup_errors))
                adapter.fail(primary, "container-stop", secondary="cleanup")
                self.assertEqual(len(adapter.cleanup_errors), 3 + int(alive))

    def test_fixed_category_and_bounded_exit_matrix(self):
        for error, category, code in (
            (safety.FixtureError("selected-session"), "selected-session", None),
            (safety.FixtureError("PRIVATE"), "other-fixed", None),
            (box.BoxError("selected-session PRIVATE"), "other-fixed", None),
            (
                subprocess.CalledProcessError(1, ["PRIVATE"], stderr="PRIVATE"),
                "other-fixed",
                1,
            ),
            (subprocess.CalledProcessError(9999, ["PRIVATE"]), "other-fixed", None),
            (ValueError({"secret": "PRIVATE"}), "other-fixed", None),
        ):
            with self.subTest(error_type=type(error)):
                value = external.diagnostic(error, "synthetic", fixture)
                self.assertEqual(value["category"], category)
                self.assertEqual(value["exit_code"], code)
                self.assertNotIn("PRIVATE", json.dumps(value))

    def test_forwarded_subprocess_exit_survives_box_error_without_private_output(self):
        for code in (1, -9, 9999):
            with self.subTest(code=code):
                adapter = external.Adapter(fixture, Path("/example"))

                def start(*args):
                    subprocess.run(["PRIVATE_ARGV"], check=False)
                    raise box.BoxError("PRIVATE_ERROR")

                with (
                    patch.object(
                        subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            ["PRIVATE_ARGV"],
                            code,
                            "PRIVATE_STDOUT",
                            "PRIVATE_STDERR",
                        ),
                    ) as invoke,
                    patch.object(box, "start_container", side_effect=start),
                    adapter.installed(),
                    self.assertRaises(box.BoxError),
                ):
                    box.start_container(None, {})
                invoke.assert_called_once_with(["PRIVATE_ARGV"], check=False)
                self.assertEqual(adapter.primary["substep"], "container-start")
                self.assertEqual(
                    adapter.primary["exit_code"], code if code != 9999 else None
                )
                self.assertNotIn("PRIVATE", json.dumps(adapter.primary))

    def valid_completion(self, adapter):
        adapter.imports = 1
        adapter.ledger = [{"completed": True} for _ in external.SEQUENCE]
        adapter.provider = True
        adapter.cleanup = {"shutdown": 1, "close": 1, "join": 1, "worker_alive": False}
        report = fixture.new_report("2.3.0")
        report.update(
            status="passed",
            native={"version": "2.3.0", "sha256": external.NATIVE_SHA256},
        )
        report["checks"] = dict.fromkeys(external.CHECKS, True)
        return report

    def test_all_ten_original_checks_exclusions_and_import_cleanup_are_mandatory(self):
        adapter = external.Adapter(fixture, Path("/example"))
        report = self.valid_completion(adapter)
        adapter.complete(report, 0)
        self.assertEqual(tuple(fixture.CHECKS), external.CHECKS)
        self.assertEqual(tuple(fixture.EXCLUDED), external.EXCLUDED)
        for name in external.CHECKS:
            with self.subTest(check=name):
                changed = copy.deepcopy(report)
                changed["checks"][name] = False
                with self.assertRaises(external.Refusal):
                    adapter.complete(changed, 0)
        for mutate in (
            lambda r: r["excluded"].pop(),
            lambda r: r.update(legacy_fixture_ran=True),
            lambda r: r.update(global_vault_fixture_ran=True),
            lambda r: r.update(credential_scope="vault"),
            lambda r: r["native"].update(version="2.4.0"),
        ):
            changed = copy.deepcopy(report)
            mutate(changed)
            with self.assertRaises(external.Refusal):
                adapter.complete(changed, 0)
        for field, value in (("imports", 0), ("imports", 2), ("provider", False)):
            adapter = external.Adapter(fixture, Path("/example"))
            report = self.valid_completion(adapter)
            setattr(adapter, field, value)
            with self.assertRaises(external.Refusal):
                adapter.complete(report, 0)

    def test_installed_forwarding_records_primary_before_finally_stop_and_report(self):
        adapter = external.Adapter(fixture, Path("/example"))
        adapter.root = Path("/example/fresh")
        primary = safety.FixtureError("first-import")
        with (
            patch.object(box, "export_container_session", side_effect=primary),
            patch.object(box, "stop_container", side_effect=OSError("PRIVATE_STOP")),
            patch.object(
                safety, "write_json_new", side_effect=OSError("PRIVATE_REPORT")
            ),
            adapter.installed(),
        ):
            with self.assertRaises(safety.FixtureError):
                box.export_container_session({}, "ses_synthetic")
            with self.assertRaises(OSError):
                box.stop_container({})
            with self.assertRaises(OSError):
                safety.write_json_new(adapter.root / "result.json", {})
        self.assertEqual(adapter.primary["substep"], "transcript")
        self.assertEqual(adapter.primary["category"], "first-import")
        self.assertEqual(adapter.cleanup_errors[0]["substep"], "container-stop")
        self.assertEqual(adapter.reporting_errors[0]["substep"], "original-report")


class CompanionReportTests(unittest.TestCase):
    """Synthetic outer orchestration, not the host lifecycle or native provider."""

    def run_outer(
        self,
        parent,
        *,
        fail_primary=False,
        fail_cleanup=False,
        alive=False,
        fail_report=None,
        check_change=False,
    ):
        state = {}
        source = {
            "build": {"commit": external.COMMIT, "commit_count": 123, "dirty": False},
            "sha256": {"synthetic": "a" * 64},
        }
        provenance = {
            "application": {
                "commit": external.COMMIT,
                "commit_count": 123,
                "sha256": source["sha256"],
            },
            "frozen_helpers": external.FROZEN,
            "runner_sha256": "b" * 64,
            "offline_probe_sha256": "c" * 64,
            "native": {"version": "2.3.0", "sha256": external.NATIVE_SHA256},
        }
        server = Mock(server_port=1234)
        worker = Mock()
        worker.is_alive.return_value = alive
        if fail_cleanup:
            server.shutdown.side_effect = (
                KeyboardInterrupt()
                if fail_cleanup == "interrupt"
                else OSError("PRIVATE_SHUTDOWN")
            )
        real_write = safety.write_json_new

        def write(path, value):
            if path.name == fail_report:
                raise OSError("PRIVATE_REPORT")
            return real_write(path, value)

        def records(root):
            return {
                name: (
                    root / "registry" / str(index),
                    {"id": str(index) * 32, "project": str(root / name)},
                )
                for index, name in enumerate(
                    ("source-a", "restored-full", "restored-portable"), 1
                )
            }

        def prepare(root, binary, version, report):
            report["source"] = source
            return (
                Path("/synthetic"),
                provenance["native"],
                baseline(root / "source-a"),
                {},
            )

        def main(arguments):
            self.assertNotIn("--cleanup", arguments)
            root = Path(tempfile.mkdtemp(prefix=fixture.PREFIX, dir=parent))
            root.chmod(0o700)
            state["root"] = root
            report = fixture.new_report("2.3.0")
            fixture.prepare_root(root, "tumbleweed", Path("/synthetic"))
            fixture.prepare_launch(root, Path("/synthetic"), "2.3.0", report)
            wrapped_server, wrapped_worker = fixture.legacy.make_provider({})
            try:
                if fail_primary:
                    fixture.require(False, "selected-session")
                for command, name in external.SEQUENCE:
                    safety.launcher(
                        [command, "--workspace", records(root)[name][1]["id"]]
                    )
                report.update(status="passed", native=provenance["native"])
                report["checks"] = dict.fromkeys(external.CHECKS, True)
                if check_change:
                    report["checks"][external.CHECKS[0]] = 1
                code = 0
            except BaseException:
                report.update(status="failed", failure="fixture-error")
                code = 1
            finally:
                wrapped_server.shutdown()
                wrapped_server.server_close()
                wrapped_worker.join(timeout=3)
            try:
                safety.write_json_new(root / "result.json", report)
                state["original_bytes"] = (root / "result.json").read_bytes()
            except BaseException:
                return 1
            return code

        with (
            patch.object(fixture, "main", side_effect=main),
            patch.object(fixture, "prepare_root"),
            patch.object(fixture, "prepare_launch", side_effect=prepare),
            patch.object(fixture, "workspace_records", side_effect=records),
            patch.object(safety, "launcher"),
            patch.object(
                fixture.legacy, "make_provider", return_value=(server, worker)
            ),
            patch.object(safety, "write_json_new", side_effect=write),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = external.execute_fixture(
                fixture,
                parent,
                Path("/synthetic"),
                "tumbleweed",
                None,
                provenance,
                Mock(),
            )
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        root = state["root"]
        if "original_bytes" in state:
            self.assertEqual(
                (root / "result.json").read_bytes(), state["original_bytes"]
            )
        state["outer"] = (
            json.loads((root / external.REPORT).read_text())
            if (root / external.REPORT).exists()
            else None
        )
        state.update(code=code, server=server, worker=worker, stderr=errors.getvalue())
        return state

    def test_companion_success_preserves_original_and_all_identities(self):
        with tempfile.TemporaryDirectory() as name:
            state = self.run_outer(Path(name))
            self.assertEqual(state["code"], 0)
            report = state["outer"]
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["first_import_count"], 1)
            self.assertEqual(report["checks"], dict.fromkeys(external.CHECKS, True))
            self.assertEqual(report["excluded"], list(external.EXCLUDED))
            self.assertEqual(report["provenance"]["frozen_helpers"], external.FROZEN)
            self.assertEqual(
                report["original_report_sha256"],
                external.digest(state["root"] / "result.json"),
            )
            self.assertNotIn("fixture_ids", report)
            self.assertNotIn("ses_", json.dumps(report))

    def test_cleanup_or_alive_failure_cannot_pass_even_when_original_passed(self):
        for kwargs in (
            {"fail_cleanup": True},
            {"alive": True},
            {"fail_primary": True, "fail_cleanup": True},
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as name:
                state = self.run_outer(Path(name), **kwargs)
                report = state["outer"]
                self.assertEqual(state["code"], 1)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["cleanup_failures"])
                if kwargs.get("fail_primary"):
                    self.assertEqual(
                        report["primary_failure"]["category"], "selected-session"
                    )
                else:
                    self.assertIsNone(report["primary_failure"])
                    self.assertTrue(report["original_exit_zero"])
                state["server"].shutdown.assert_called_once_with()
                state["server"].server_close.assert_called_once_with()
                state["worker"].join.assert_called_once_with(timeout=3)

    def test_original_and_companion_reporting_failures_are_distinct_nonzero(self):
        for filename in ("result.json", external.REPORT):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as name:
                state = self.run_outer(Path(name), fail_report=filename)
                self.assertEqual(state["code"], 1)
                if filename == "result.json":
                    self.assertTrue(state["outer"]["reporting_failures"])
                    self.assertIsNone(state["outer"]["primary_failure"])
                else:
                    self.assertIsNone(state["outer"])
                    self.assertTrue((state["root"] / "result.json").is_file())

    def test_companion_write_failure_keeps_fixed_primary_and_cleanup_on_stderr(self):
        with tempfile.TemporaryDirectory() as name:
            state = self.run_outer(
                Path(name),
                fail_primary=True,
                fail_cleanup=True,
                fail_report=external.REPORT,
            )
            diagnostic = json.loads(state["stderr"].splitlines()[-1])
            self.assertEqual(
                diagnostic["primary_failure"]["category"], "selected-session"
            )
            self.assertEqual(len(diagnostic["cleanup_failures"]), 1)
            self.assertEqual(
                diagnostic["reporting_failures"][-1]["substep"], "external-report"
            )
            self.assertNotIn("ses_", state["stderr"])

    def test_cleanup_interruption_retains_130_even_if_companion_write_also_fails(self):
        for filename in (None, external.REPORT):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as name:
                state = self.run_outer(
                    Path(name), fail_cleanup="interrupt", fail_report=filename
                )
                self.assertEqual(state["code"], 130)

    def test_integer_truthy_check_cannot_be_promoted_to_original_pass(self):
        with tempfile.TemporaryDirectory() as name:
            state = self.run_outer(Path(name), check_change=True)
            self.assertEqual(state["code"], 1)
            self.assertFalse(state["outer"]["checks"][external.CHECKS[0]])


class SourceBindingTests(unittest.TestCase):
    @contextlib.contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "helper.py").write_text("value = 1\n")
            (root / ".gitignore").write_text("ignored/\n__pycache__/\n")
            subprocess.run(
                ["git", "-C", str(root), "add", "helper.py", ".gitignore"], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Synthetic",
                    "-c",
                    "user.email=synthetic@example.invalid",
                    "commit",
                    "-qm",
                    "synthetic",
                ],
                check=True,
            )
            commit = external.git(root, "rev-parse", "HEAD").decode().strip()
            with (
                patch.object(external, "COMMIT", commit),
                patch.object(
                    external,
                    "FROZEN",
                    {"helper.py": external.digest(root / "helper.py")},
                ),
            ):
                yield root, commit

    def test_head_blobs_clean_full_history_ignored_sources_and_symlinks(self):
        for mutation, category in (
            ("none", None),
            ("dirty", "clean-source-required"),
            ("untracked", "clean-source-required"),
            ("ignored", "ignored-source"),
            ("shallow", "clean-source-required"),
            ("symlink", "source-path"),
            ("blob", "source-changed"),
            ("mode", "source-entry"),
        ):
            with self.subTest(mutation=mutation), self.repository() as (root, commit):
                original_git = external.git
                if mutation == "dirty":
                    (root / "helper.py").write_text("PRIVATE\n")
                elif mutation == "untracked":
                    (root / "new.py").write_text("PRIVATE\n")
                elif mutation == "ignored":
                    (root / "ignored").mkdir()
                    (root / "ignored/hidden.py").write_text("PRIVATE\n")
                elif mutation == "shallow":
                    (root / ".git/shallow").write_text(commit + "\n")
                elif mutation == "symlink":
                    alias = root.parent / (root.name + "-alias")
                    alias.symlink_to(root)
                    self.addCleanup(alias.unlink)
                    root = alias
                elif mutation in ("blob", "mode"):
                    # Ignore a dishonest status/index; HEAD bytes/modes must still win.
                    if mutation == "blob":
                        (root / "helper.py").write_text("PRIVATE\n")
                    else:
                        (root / "helper.py").chmod(0o755)

                def git(source, *args):
                    if mutation in ("blob", "mode") and args[0] == "status":
                        return b""
                    return original_git(source, *args)

                with patch.object(external, "git", side_effect=git):
                    if category is None:
                        value = external.snapshot(root, commit)
                        self.assertEqual(value["commit_count"], 1)
                    else:
                        with self.assertRaisesRegex(
                            external.Refusal, "^" + category + "$"
                        ):
                            external.snapshot(root, commit)

    def test_only_pycache_is_ignored(self):
        with self.repository() as (root, commit):
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "helper.cpython-310.pyc").write_bytes(b"untrusted bytecode")
            self.assertIn("helper.py", external.snapshot(root, commit)["sha256"])
            (root / "ignored").mkdir()
            (root / "ignored/legacy.pyc").write_bytes(b"untrusted bytecode")
            with self.assertRaisesRegex(external.Refusal, "ignored-source"):
                external.snapshot(root, commit)

    def test_isolated_loader_ignores_valid_stale_pyc_and_retains_original_host_guard(
        self,
    ):
        # A disposable source-only clone: never alter the frozen/shared checkout.
        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--shared",
                    "--no-checkout",
                    str(checkout),
                    str(source),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "-C", str(source), "checkout", "--quiet", external.COMMIT],
                check=True,
                capture_output=True,
                timeout=15,
            )
            target = source / "tools/test_v2_lifecycle_podman.py"
            cached = Path(importlib.util.cache_from_source(str(target)))
            cached.parent.mkdir(exist_ok=True)
            # Timestamp/size-valid malicious cache must not execute. This is
            # synthetic bytecode under the disposable clone, not a live fixture.
            info = target.stat()
            cached.write_bytes(
                importlib.util.MAGIC_NUMBER
                + struct.pack("<III", 0, int(info.st_mtime), info.st_size)
                + marshal.dumps(
                    compile(
                        "raise RuntimeError('STALE_BYTECODE_EXECUTED')",
                        str(target),
                        "exec",
                    )
                )
            )
            code = """
import importlib.util,os,pathlib,sys
spec=importlib.util.spec_from_file_location('external_driver',sys.argv[1])
driver=importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)
source=pathlib.Path(sys.argv[2])
identity=driver.snapshot(source,driver.COMMIT)
refuse=(os.geteuid()==0 or os.environ.get('OC_BOX_CONTAINER')=='1'
        or pathlib.Path('/.dockerenv').exists() or pathlib.Path('/run/.containerenv').exists())
try:
    fixture,cache=driver.load_candidate(source,identity)
except BaseException as error:
    # Running as root/in a container must still hit the unchanged candidate
    # host guard after our verified, isolated source import. No native calls.
    assert refuse and type(error).__name__=='SourceError' and error.args==('host-only',)
    print('original-host-guard-preserved; stale-bytecode-unused')
else:
    assert not refuse and fixture._CLI_SOURCE==identity
    cache.cleanup()
    print('original-source-checker-preserved; stale-bytecode-unused')
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(Path(external.__file__).resolve()),
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            refuse = (
                os.geteuid() == 0
                or os.environ.get("OC_BOX_CONTAINER") == "1"
                or Path("/.dockerenv").exists()
                or Path("/run/.containerenv").exists()
            )
            expected = "host-guard" if refuse else "source-checker"
            self.assertEqual(
                result.stdout.strip(),
                "original-" + expected + "-preserved; stale-bytecode-unused",
            )
            self.assertNotIn("STALE_BYTECODE_EXECUTED", result.stderr)

    def test_pin_and_host_refusals_precede_candidate_import_native_or_allocation(self):
        args = argparse.Namespace(
            source=Path("/example"),
            expect_commit="f" * 40,
            opencode_v2=Path("/native"),
            work_dir=Path("/work"),
            temp_dir=None,
        )
        with (
            patch.object(
                external, "host_guard", side_effect=external.Refusal("host-only")
            ),
            patch.object(external, "snapshot") as checked,
            patch.object(external, "load_candidate") as imported,
            patch.object(external.tempfile, "TemporaryDirectory") as allocate,
            patch.object(external, "digest") as native,
            self.assertRaisesRegex(external.Refusal, "host-only"),
        ):
            external.preflight(args, Path("/runner"))
        for operation in (checked, imported, allocate, native):
            operation.assert_not_called()
        with self.repository() as (root, commit), patch.object(external, "git") as git:
            with self.assertRaisesRegex(external.Refusal, "source-pin"):
                external.snapshot(root, "f" * 40)
            git.assert_not_called()

    def test_native_hash_and_source_placement_refuse_before_import_or_allocation(self):
        with self.repository() as (source, commit):
            binary = source.parent / (source.name + "-native")
            binary.write_bytes(b"synthetic executable, never invoked")
            binary.chmod(0o700)
            self.addCleanup(binary.unlink)
            args = argparse.Namespace(
                source=source,
                expect_commit=commit,
                opencode_v2=binary,
                work_dir=source.parent,
                temp_dir=None,
            )
            runner = source.parent / "external.py"
            for refusal in ("native", "runner", "work", "temporary"):
                selected = copy.copy(args)
                if refusal == "work":
                    selected.work_dir = source
                elif refusal == "temporary":
                    selected.temp_dir = source
                chosen_runner = (
                    source / "external.py" if refusal == "runner" else runner
                )
                checksum = (
                    "not-the-pinned-native"
                    if refusal == "native"
                    else external.digest(binary)
                )
                with (
                    self.subTest(refusal=refusal),
                    patch.object(external, "host_guard"),
                    # Model a fresh CLI interpreter; the real guards are covered
                    # independently, without calling any owning operation.
                    patch.object(external, "import_guard"),
                    patch.object(external, "NATIVE_SHA256", checksum),
                    patch.object(external, "load_candidate") as imported,
                    patch.object(external.tempfile, "TemporaryDirectory") as allocated,
                    patch.object(box, "run") as native,
                    self.assertRaises(external.Refusal),
                ):
                    external.preflight(selected, chosen_runner)
                imported.assert_not_called()
                allocated.assert_not_called()
                native.assert_not_called()

    def test_preexisting_protected_modules_and_untracked_candidate_import_refuse(self):
        source = Path("/synthetic/source")
        identity = {"sha256": {"tools/checked.py": "a" * 64}}
        for name, module in (
            ("oc_box.foreign", types.SimpleNamespace(__file__="/foreign/oc_box.py")),
            ("tools.foreign", types.SimpleNamespace(__file__="/foreign/tools.py")),
            ("unrelated", types.SimpleNamespace(__file__=str(source / "ignored.py"))),
        ):
            with (
                self.subTest(name=name),
                patch.dict(sys.modules, {name: module}, clear=True),
            ):
                with self.assertRaisesRegex(external.Refusal, "source-shadowing"):
                    external.import_guard(source, identity, before=True)
        with patch.object(external.tempfile, "TemporaryDirectory") as allocate:
            with self.assertRaisesRegex(external.Refusal, "source-shadowing"):
                external.load_candidate(source, identity)
            allocate.assert_not_called()
        for filename, allowed in (
            (source / "tools/checked.py", True),
            (source / "tools/unchecked.py", False),
            (Path("/foreign/tools.py"), False),
        ):
            module = types.SimpleNamespace(__file__=str(filename))
            with (
                self.subTest(filename=filename),
                patch.dict(sys.modules, {"tools.checked": module}, clear=True),
            ):
                if allowed:
                    external.import_guard(source, identity)
                else:
                    with self.assertRaises(external.Refusal):
                        external.import_guard(source, identity)

    def test_runner_probe_native_and_source_rechecked_before_fixture_allocation(self):
        runner = Path(external.__file__).absolute()
        probe = runner.parents[1] / "tests/test_v2_lifecycle_external.py"
        binary = Path("/synthetic/native")
        identity = {"commit": external.COMMIT, "sha256": {}}
        candidate = types.SimpleNamespace(source_identity=lambda: {"sha256": {}})
        argv = [
            "--source",
            "/source",
            "--expect-commit",
            external.COMMIT,
            "--opencode-v2",
            str(binary),
            "--work-dir",
            "/work",
        ]
        for mutated in ("runner", "probe", "native", "source"):
            counts = {}

            def checksum(path):
                counts[path] = counts.get(path, 0) + 1
                if (
                    mutated == "runner"
                    and path == runner
                    and counts[path] == 2
                    or mutated == "probe"
                    and path == probe
                    and counts[path] == 2
                ):
                    return "changed"
                if path == binary:
                    return "changed" if mutated == "native" else external.NATIVE_SHA256
                return "a" * 64

            with (
                self.subTest(mutated=mutated),
                patch.object(external, "host_guard"),
                patch.object(external, "digest", side_effect=checksum),
                patch.object(
                    external,
                    "preflight",
                    return_value=(
                        Path("/source"),
                        identity,
                        binary,
                        Path("/work"),
                        None,
                    ),
                ),
                patch.object(
                    external, "load_candidate", return_value=(candidate, Mock())
                ),
                patch.object(
                    external,
                    "snapshot",
                    return_value={} if mutated == "source" else identity,
                ),
                patch.object(external, "import_guard"),
                patch.object(external, "execute_fixture") as execute,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(external.main(argv), 1)
                execute.assert_not_called()
                self.assertNotIn("/synthetic", errors.getvalue())

    def test_cli_has_no_retry_cleanup_unknown_native_or_abbreviated_flags(self):
        for flag in (
            "--retry",
            "--cleanup",
            "--resume",
            "--native-version=2.4.0",
            "--sou",
        ):
            with (
                self.subTest(flag=flag),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                with self.assertRaises(SystemExit) as result:
                    external.main(
                        [
                            "--source",
                            "/source",
                            "--expect-commit",
                            external.COMMIT,
                            "--opencode-v2",
                            "/native",
                            "--work-dir",
                            "/work",
                            flag,
                        ]
                    )
                self.assertEqual(result.exception.code, 2)
                self.assertNotIn("/source", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
