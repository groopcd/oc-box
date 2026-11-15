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
    def exercise(
        self, parent, corrected, *, rejected_import=False, uncheckpointed=False
    ):
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
                value = copy.deepcopy(native[settings["session"]])
                if rejected_import:
                    value["messages"][0]["text"] = "SYNTHETIC_CHANGED_IMPORT"
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
            patch.object(
                box,
                "stop_container",
                side_effect=OSError("PRIVATE_STOP") if rejected_import else None,
            ),
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
            if rejected_import:
                real_import, real_verify = (
                    box.import_host_session,
                    box.verify_session_import,
                )
                with adapter.installed(), contextlib.ExitStack() as forwarding:
                    if uncheckpointed:
                        # Emulate an unforeseen/uncheckpointed import refusal,
                        # while still running both unchanged production functions.
                        forwarding.enter_context(
                            patch.object(box, "import_host_session", real_import)
                        )
                        forwarding.enter_context(
                            patch.object(box, "verify_session_import", real_verify)
                        )
                    with self.assertRaisesRegex(OSError, "^PRIVATE_STOP$"):
                        adapter.launcher(delegate, arguments)
                self.assertEqual(adapter.primary["error_class"], "box-error")
                self.assertEqual(
                    adapter.primary["substep"],
                    "owning-launch" if uncheckpointed else "import-verification",
                )
                self.assertEqual(adapter.cleanup_errors[0]["substep"], "container-stop")
                self.assertEqual(adapter.cleanup_errors[0]["error_class"], "os-error")
                self.assertEqual(len(adapter.cleanup_errors), 1)
                self.assertNotIn(
                    "PRIVATE",
                    json.dumps(
                        [
                            adapter.primary,
                            *adapter.cleanup_errors,
                        ]
                    ),
                )
                self.assertEqual(transfers, [seed["info"]["id"]])
                self.assertEqual(host_exports, [True])
                self.assertEqual(created, [])
                self.assertEqual(callbacks, [])
                self.assertIsNone(box.session_state(path)["last"])
            elif corrected:
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

    def test_actual_import_verification_unwind_preserves_primary_when_stop_also_fails(
        self,
    ):
        for uncheckpointed in (False, True):
            with (
                self.subTest(uncheckpointed=uncheckpointed),
                tempfile.TemporaryDirectory() as name,
            ):
                self.exercise(
                    Path(name),
                    True,
                    rejected_import=True,
                    uncheckpointed=uncheckpointed,
                )


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

    def test_actual_owner_enter_preserves_startup_error_before_failed_shutdown(self):
        with tempfile.TemporaryDirectory() as name:
            owner = object.__new__(service.Owner)
            owner.box, owner.data = box, {}
            owner.address = Path(name) / "synthetic-owner.sock"
            startup, cleanup = OSError("PRIVATE_STARTUP"), OSError("PRIVATE_SHUTDOWN")
            adapter = external.Adapter(fixture, Path(name))
            command = types.SimpleNamespace(environment={})
            with (
                patch.object(box, "exec_command", return_value=command),
                patch.object(
                    box,
                    "captured_output",
                    side_effect=lambda **k: contextlib.nullcontext(io.BytesIO()),
                ),
                patch.object(subprocess, "Popen", side_effect=startup) as process,
                patch.object(
                    service.Owner, "shutdown", side_effect=cleanup
                ) as shutdown,
                adapter.installed(),
                self.assertRaisesRegex(OSError, "^PRIVATE_SHUTDOWN$"),
            ):
                owner.__enter__()
            process.assert_called_once()
            shutdown.assert_called_once_with(owner)
            self.assertIs(adapter.seen[0], startup)
            self.assertIs(adapter.seen[1], cleanup)
            self.assertEqual(adapter.primary["substep"], "owner-start")
            self.assertEqual(adapter.cleanup_errors[0]["substep"], "owner-stop")
            self.assertEqual(len(adapter.cleanup_errors), 1)
            self.assertNotIn(
                "PRIVATE", json.dumps([adapter.primary, *adapter.cleanup_errors])
            )

    def test_cleanup_repropagation_is_never_promoted_to_primary(self):
        adapter = external.Adapter(fixture, Path("/example"))
        cleanup = OSError("PRIVATE_CLEANUP")
        try:
            with adapter.checkpoint("container-stop", cleanup=True):
                raise cleanup
        except OSError:
            with adapter.checkpoint("owner-stop", cleanup=True):
                pass
        self.assertIsNone(adapter.primary)
        self.assertEqual(len(adapter.cleanup_errors), 1)
        nested = external.Adapter(fixture, Path("/example"))
        with self.assertRaises(OSError):
            with nested.checkpoint("owner-stop", cleanup=True):
                with nested.checkpoint("history-sync"):
                    raise OSError("PRIVATE_NESTED_CLEANUP")
        self.assertIsNone(nested.primary)
        self.assertEqual(len(nested.cleanup_errors), 1)
        self.assertEqual(nested.cleanup_errors[0]["substep"], "history-sync")


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
        integrity_failure=None,
        stage_fault=None,
        publication_fault=False,
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
        real_digest = external.digest
        real_stage_write = external.write_report_stage
        real_fdopen, real_fsync, real_link = os.fdopen, os.fsync, os.link

        class StageStream:
            """Controlled late failures, only on the new stage writer's stream."""

            def __init__(self, stream):
                self.stream = stream
                self.text = ""

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *info):
                result = self.stream.__exit__(*info)
                state["stage_closed"] = self.stream.closed
                if stage_fault == "close":
                    raise OSError("PRIVATE_STAGE_CLOSE")
                return result

            def write(self, text):
                result = self.stream.write(text)
                self.text += text
                if stage_fault == "write":
                    try:
                        json.loads(self.text)
                    except ValueError:
                        pass
                    else:
                        self.stream.flush()
                        state["late_write_after_complete_json"] = True
                        raise OSError("PRIVATE_STAGE_WRITE")
                return result

            def flush(self):
                self.stream.flush()
                if stage_fault == "flush":
                    raise OSError("PRIVATE_STAGE_FLUSH")

            def fileno(self):
                return self.stream.fileno()

        def fdopen(descriptor, *args, **kwargs):
            stream = real_fdopen(descriptor, *args, **kwargs)
            return StageStream(stream) if kwargs.get("encoding") == "utf-8" else stream

        def fsync(descriptor):
            if stage_fault == "fsync":
                raise OSError("PRIVATE_STAGE_FSYNC")
            return real_fsync(descriptor)

        def stage_write(descriptor, value):
            if fail_report == external.REPORT:
                with real_fdopen(descriptor, "w", encoding="utf-8"):
                    raise OSError("PRIVATE_REPORT")
            return real_stage_write(descriptor, value)

        def link(*args, **kwargs):
            state["publication_calls"] = state.get("publication_calls", 0) + 1
            self.assertTrue(state["stage_closed"])
            if publication_fault:
                raise OSError("PRIVATE_PUBLISH_PATH")
            return real_link(*args, **kwargs)

        def checksum(path):
            if path.name == "result.json":
                state["original_digest_reads"] = (
                    state.get("original_digest_reads", 0) + 1
                )
                if state["original_digest_reads"] == 2:
                    if integrity_failure == "io":
                        raise OSError("PRIVATE_INTEGRITY")
                    if integrity_failure == "mismatch":
                        return "0" * 64
                self.assertLessEqual(state["original_digest_reads"], 2)
            return real_digest(path)

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
            patch.object(external, "digest", side_effect=checksum),
            patch.object(external, "write_report_stage", side_effect=stage_write),
            patch.object(os, "fdopen", side_effect=fdopen),
            patch.object(os, "fsync", side_effect=fsync),
            patch.object(os, "link", side_effect=link),
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
        state["stages"] = list(root.glob(external.REPORT_STAGE_PREFIX + "*.json"))
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
            self.assertEqual(len(report["launches"]), 5)
            self.assertTrue(report["first_import_verified"])
            self.assertEqual(
                report["provenance"]["native"]["sha256"], external.NATIVE_SHA256
            )
            self.assertEqual(
                report["companion_publication"],
                {
                    "method": "closed-private-stage-link-no-replace",
                    "staging_retained": True,
                    "staging_authoritative": False,
                },
            )
            self.assertEqual(len(state["stages"]), 1)
            stage = state["stages"][0]
            final = state["root"] / external.REPORT
            self.assertEqual(stage.stat().st_ino, final.stat().st_ino)
            self.assertEqual(stage.stat().st_nlink, 2)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o600)
            self.assertEqual((state["root"] / "result.json").stat().st_nlink, 1)
            # The frozen reader still enforces single-link originals. It is
            # deliberately not the reader for the new retained-alias receipt.
            safety.private_json(state["root"], "result.json")
            with self.assertRaisesRegex(safety.FixtureError, "^unsafe-metadata$"):
                safety.private_json(state["root"], external.REPORT)

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

    def test_original_integrity_failures_are_resolved_before_companion_publication(
        self,
    ):
        for integrity in ("io", "mismatch"):
            for report_failure in (None, external.REPORT):
                with (
                    self.subTest(integrity=integrity, report_failure=report_failure),
                    tempfile.TemporaryDirectory() as name,
                ):
                    state = self.run_outer(
                        Path(name),
                        integrity_failure=integrity,
                        fail_report=report_failure,
                    )
                    self.assertEqual(state["code"], 1)
                    self.assertEqual(state["original_digest_reads"], 2)
                    if state["outer"] is not None:
                        self.assertEqual(state["outer"]["status"], "failed")
                        failures = state["outer"]["reporting_failures"]
                        self.assertIsNone(state["outer"]["primary_failure"])
                    else:
                        failures = json.loads(state["stderr"].splitlines()[-1])[
                            "reporting_failures"
                        ]
                    self.assertEqual(
                        failures[0]["substep"], "original-report-integrity"
                    )
                    self.assertNotIn("PRIVATE", json.dumps(failures))
                    # run_outer also requires original bytes unchanged; no
                    # parseable passed companion may survive any of these cases.
                    self.assertFalse(
                        state["outer"] and state["outer"]["status"] == "passed"
                    )

    def test_late_stage_failures_never_publish_authoritative_pass(self):
        for fault in ("write", "flush", "fsync", "close"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as name:
                state = self.run_outer(Path(name), stage_fault=fault)
                self.assertEqual(state["code"], 1)
                self.assertIsNone(state["outer"])
                self.assertFalse((state["root"] / external.REPORT).exists())
                self.assertEqual(state.get("publication_calls", 0), 0)
                self.assertTrue(state["stage_closed"])
                self.assertEqual(len(state["stages"]), 1)
                staged = json.loads(state["stages"][0].read_text())
                self.assertEqual(staged["status"], "passed")
                self.assertFalse(
                    staged["companion_publication"]["staging_authoritative"]
                )
                if fault == "write":
                    self.assertTrue(state["late_write_after_complete_json"])
                failure = json.loads(state["stderr"].splitlines()[-1])
                self.assertEqual(failure["status"], "failed")
                self.assertEqual(
                    failure["reporting_failures"][-1]["substep"], "external-report"
                )
                self.assertNotIn("PRIVATE", json.dumps(failure))

    def test_atomic_publication_failure_preserves_fixed_primary_and_cleanup_context(
        self,
    ):
        for failed_run in (False, True):
            with (
                self.subTest(failed_run=failed_run),
                tempfile.TemporaryDirectory() as name,
            ):
                state = self.run_outer(
                    Path(name),
                    publication_fault=True,
                    fail_primary=failed_run,
                    fail_cleanup=failed_run,
                )
                self.assertEqual(state["code"], 1)
                self.assertIsNone(state["outer"])
                self.assertFalse((state["root"] / external.REPORT).exists())
                self.assertEqual(state["publication_calls"], 1)
                self.assertEqual(len(state["stages"]), 1)
                failure = json.loads(state["stderr"].splitlines()[-1])
                if failed_run:
                    self.assertEqual(
                        failure["primary_failure"]["category"], "selected-session"
                    )
                    self.assertEqual(len(failure["cleanup_failures"]), 1)
                else:
                    self.assertIsNone(failure["primary_failure"])
                    self.assertFalse(failure["cleanup_failures"])
                self.assertEqual(
                    failure["reporting_failures"][-1]["substep"], "external-report"
                )
                self.assertNotIn("PRIVATE", json.dumps(failure))

    def test_late_stage_failure_retains_existing_primary_and_cleanup_diagnostics(self):
        for fault in ("write", "flush", "fsync", "close"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as name:
                state = self.run_outer(
                    Path(name),
                    stage_fault=fault,
                    fail_primary=True,
                    fail_cleanup=True,
                )
                self.assertEqual(state["code"], 1)
                self.assertIsNone(state["outer"])
                self.assertEqual(state.get("publication_calls", 0), 0)
                failure = json.loads(state["stderr"].splitlines()[-1])
                self.assertEqual(
                    failure["primary_failure"]["category"], "selected-session"
                )
                self.assertEqual(len(failure["cleanup_failures"]), 1)
                self.assertEqual(
                    failure["reporting_failures"][-1]["substep"], "external-report"
                )
                self.assertNotIn("PRIVATE", json.dumps(failure))


class AtomicPublicationTests(unittest.TestCase):
    def test_existing_file_and_symlink_refuse_without_overwriting_any_evidence(self):
        for kind in ("file", "symlink"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as name,
            ):
                root = Path(name)
                safety.write_json_new(root / "result.json", {"synthetic": "original"})
                original = (root / "result.json").read_bytes()
                target = root / "PRIVATE_TARGET"
                target.write_bytes(b"PRIVATE_TARGET_BYTES")
                final = root / external.REPORT
                if kind == "symlink":
                    final.symlink_to(target)
                    link_value = os.readlink(final)
                else:
                    final.write_bytes(b'{"status":"passed","preexisting":true}\n')
                before = final.read_bytes()
                with patch.object(os, "link", wraps=os.link) as link:
                    with self.assertRaises(FileExistsError) as failure:
                        external.publish_report(root, {"status": "passed"})
                link.assert_called_once()
                self.assertFalse(link.call_args.kwargs["follow_symlinks"])
                self.assertEqual(final.read_bytes(), before)
                self.assertEqual(target.read_bytes(), b"PRIVATE_TARGET_BYTES")
                if kind == "symlink":
                    self.assertEqual(os.readlink(final), link_value)
                stages = list(root.glob(external.REPORT_STAGE_PREFIX + "*.json"))
                self.assertEqual(len(stages), 1)
                self.assertEqual(stages[0].stat().st_nlink, 1)
                self.assertEqual(stages[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(stages[0].read_text())["status"], "passed")
                self.assertEqual((root / "result.json").read_bytes(), original)
                self.assertEqual((root / "result.json").stat().st_nlink, 1)
                diagnostic = json.dumps(
                    external.diagnostic(failure.exception, "external-report", fixture)
                )
                self.assertNotIn("PRIVATE", diagnostic)
                self.assertNotIn(str(root), diagnostic)
                self.assertNotIn(stages[0].name, diagnostic)

    def test_success_retains_closed_stage_without_postpublication_cleanup(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as name:
            root = Path(name)
            with patch.object(
                os, "unlink", side_effect=AssertionError("postpublication cleanup")
            ):
                external.publish_report(root, {"status": "passed"})
            stages = list(root.glob(external.REPORT_STAGE_PREFIX + "*.json"))
            final = root / external.REPORT
            self.assertEqual(len(stages), 1)
            self.assertEqual(final.read_bytes(), stages[0].read_bytes())
            self.assertEqual(final.stat().st_nlink, 2)
            # TemporaryDirectory cleanup is outside this assertion and is only
            # disposal of this offline synthetic test, never harness cleanup.


class FreshPlacementTests(unittest.TestCase):
    def test_retained_prefix_and_marker_ancestors_refuse_without_content_reads(self):
        cases = [
            (prefix + "synthetic", None, False) for prefix in external.FIXTURE_PREFIXES
        ] + [
            ("renamed-" + str(index), marker, symlink)
            for index, marker in enumerate(external.FIXTURE_MARKERS)
            for symlink in (False, True)
        ]
        with tempfile.TemporaryDirectory() as name:
            common = Path(name)
            ordinary = common / "fixtures"
            ordinary.mkdir()
            for folder, marker, symlink in cases:
                retained = ordinary / (folder + ("-link" if symlink else "-file"))
                retained.mkdir()
                child = retained / "child"
                deep = child / "deeper" / "scratch"
                deep.mkdir(parents=True)
                if marker:
                    if symlink:
                        (retained / marker).symlink_to("missing-synthetic-target")
                    else:
                        (retained / marker).write_text("PRIVATE_MARKER_CONTENT")
                for parent in (retained, child, deep):
                    with (
                        self.subTest(
                            folder=folder,
                            marker=marker,
                            symlink=symlink,
                            depth=parent.name,
                        ),
                        patch.object(
                            Path, "open", side_effect=AssertionError("marker read")
                        ),
                        self.assertRaisesRegex(
                            external.Refusal, "^fresh-parent-required$"
                        ),
                    ):
                        external.fresh_parent(parent)
            # Markers/prefixes in children do not poison a common fixture parent.
            self.assertEqual(external.fresh_parent(ordinary), ordinary)
            self.assertEqual(external.fresh_parent(common), common)

    def test_work_and_temp_retained_ancestors_refuse_before_import_hash_or_allocation(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, work = root / "source", root / "fixtures"
            source.mkdir()
            work.mkdir()
            retained = work / "oc-box-v2-only-host-synthetic"
            retained.mkdir()
            deep = retained / "child/deeper"
            deep.mkdir(parents=True)
            for option in ("work_dir", "temp_dir"):
                for selected in (retained, retained / "child", deep):
                    args = argparse.Namespace(
                        source=source,
                        expect_commit=external.COMMIT,
                        opencode_v2=Path("/synthetic/missing-native"),
                        work_dir=work,
                        temp_dir=None,
                    )
                    setattr(args, option, selected)
                    with (
                        self.subTest(option=option, depth=selected.name),
                        patch.object(external, "host_guard"),
                        patch.object(external, "snapshot", return_value={"sha256": {}}),
                        patch.object(external, "load_candidate") as imports,
                        patch.object(external, "digest") as native_hash,
                        patch.object(
                            external.tempfile, "TemporaryDirectory"
                        ) as allocate,
                        patch.object(box, "run") as native,
                        self.assertRaisesRegex(
                            external.Refusal, "^fresh-parent-required$"
                        ),
                    ):
                        external.preflight(args, root / "runner.py")
                    imports.assert_not_called()
                    native_hash.assert_not_called()
                    allocate.assert_not_called()
                    native.assert_not_called()

    def test_both_cache_allocations_ignore_poisoned_cached_tempdir_and_tmpdir(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, scratch, retained = (
                root / "source",
                root / "scratch",
                root / "oc-box-gv-host-retained",
            )
            for path in (source, scratch, retained):
                path.mkdir()
            (source / "unchanged").write_text("SOURCE")
            (retained / "fixture.json").write_text("PRIVATE_RETAINED")
            identity = {"sha256": {}}
            actual_allocate = tempfile.TemporaryDirectory
            for ambient in (retained, source):
                allocations = []
                cli = types.SimpleNamespace(
                    APP=source, CHECKS=external.CHECKS, EXCLUDED=external.EXCLUDED
                )

                def allocate(*a, **k):
                    directory = actual_allocate(*a, **k)
                    allocations.append(Path(directory.name))
                    return directory

                def prepare_cli(selected):
                    self.assertEqual(selected, source)
                    cli.cache = tempfile.TemporaryDirectory(
                        prefix="synthetic-candidate-cache-"
                    )
                    return identity

                cli._prepare_cli_imports = prepare_cli
                before_source = sorted(source.iterdir())
                before_retained = sorted(retained.iterdir())
                with (
                    self.subTest(ambient=ambient.name),
                    patch.dict(os.environ, {"TMPDIR": str(ambient)}),
                    patch.object(tempfile, "tempdir", str(ambient)),
                    patch.object(tempfile, "TemporaryDirectory", side_effect=allocate),
                    patch.dict(sys.modules),
                    patch.object(sys, "path", list(sys.path)),
                    patch.object(sys, "pycache_prefix", sys.pycache_prefix),
                    patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
                    patch.object(external, "import_guard"),
                    patch.object(external, "snapshot", return_value=identity),
                    patch.object(external.importlib, "import_module", return_value=cli),
                ):
                    selected, cache = external.load_candidate(source, identity, scratch)
                    self.assertIs(selected, cli)
                    self.assertEqual(len(allocations), 2)
                    self.assertTrue(all(path.parent == scratch for path in allocations))
                    self.assertEqual(tempfile.tempdir, str(ambient))
                    self.assertEqual(os.environ["TMPDIR"], str(ambient))
                    cache.cleanup()
                    cli.cache.cleanup()
                self.assertEqual(sorted(source.iterdir()), before_source)
                self.assertEqual(sorted(retained.iterdir()), before_retained)
                self.assertEqual(list(scratch.iterdir()), [])

    def test_retained_or_source_cache_parent_refuses_before_loading_or_allocating(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, retained = root / "source", root / "oc-box-live-backup-retained"
            source.mkdir()
            retained.mkdir()
            for selected in (retained, source):
                with (
                    self.subTest(selected=selected.name),
                    patch.object(external.importlib, "import_module") as imports,
                    patch.object(tempfile, "TemporaryDirectory") as allocate,
                    self.assertRaises(external.Refusal),
                ):
                    external.load_candidate(source, {"sha256": {}}, selected)
                imports.assert_not_called()
                allocate.assert_not_called()

    def test_main_chooses_explicit_safe_scratch_or_work_parent_not_ambient_defaults(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, work, temporary = root / "source", root / "work", root / "scratch"
            poison = root / "oc-box-live-v2-retained"
            for path in (source, work, temporary, poison):
                path.mkdir()
            binary = root / "synthetic-native"
            identity = {"sha256": {}}
            cli = types.SimpleNamespace(source_identity=lambda: identity)
            for selected in (None, temporary):
                arguments = [
                    "--source",
                    str(source),
                    "--expect-commit",
                    external.COMMIT,
                    "--opencode-v2",
                    str(binary),
                    "--work-dir",
                    str(work),
                ]
                if selected is not None:
                    arguments += ["--temp-dir", str(selected)]
                with (
                    self.subTest(explicit_temp=selected is not None),
                    patch.dict(os.environ, {"TMPDIR": str(poison)}),
                    patch.object(tempfile, "tempdir", str(poison)),
                    patch.object(external, "host_guard"),
                    patch.object(
                        external,
                        "preflight",
                        return_value=(source, identity, binary, work, selected),
                    ),
                    patch.object(
                        external, "load_candidate", return_value=(cli, Mock())
                    ) as load,
                    patch.object(
                        external,
                        "digest",
                        side_effect=lambda p: (
                            external.NATIVE_SHA256 if p == binary else "a" * 64
                        ),
                    ),
                    patch.object(external, "snapshot", return_value=identity),
                    patch.object(external, "import_guard"),
                    patch.object(external, "execute_fixture", return_value=0),
                ):
                    self.assertEqual(external.main(arguments), 0)
                    load.assert_called_once_with(source, identity, selected or work)


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
    fixture,cache=driver.load_candidate(source,identity,source.parent)
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
        with tempfile.TemporaryDirectory() as name:
            with patch.object(external.tempfile, "TemporaryDirectory") as allocate:
                with self.assertRaisesRegex(external.Refusal, "source-shadowing"):
                    external.load_candidate(source, identity, Path(name))
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
