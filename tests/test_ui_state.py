import copy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, ui_state as ui, workspace_service as service

GENERATION, OWNER, ALTERNATE = "1" * 32, "2" * 32, "3" * 32
BINDING = {"available": True, "generation": GENERATION, "ownerClient": OWNER}
ACTIVE_PLUGIN = {"id": ui.PLUGIN_ID, "state": {"status": "active"}}


def report(**changes):
    return {
        "protocol": 1,
        "status": "valid",
        "epoch": "synthetic-epoch",
        "generation": GENERATION,
        "client": OWNER,
        "seq": 1,
        "routeType": "session",
        "selected": "ses_child",
        "tabsEnabled": True,
        "tabs": ["ses_root", "ses_other"],
        "activeRoot": "ses_root",
        "focus": "blurred",
        "source": "owner-tui",
        **changes,
    }


def session(ident, parent=None):
    return {
        "id": ident,
        "projectID": "global",
        "parentID": parent,
        "location": {"directory": "/synthetic/project"},
        "metadata": {"private-sentinel": "must not leave the verifier"},
    }


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"metadata": BINDING, "directory": "/synthetic/control"}
        self.value = report()
        self.sessions = {
            "ses_root": session("ses_root"),
            "ses_other": session("ses_other"),
            "ses_child": session("ses_child", "ses_root"),
        }
        self.calls = []
        self.snapshots = 0

    def request(self, method, route, body):
        self.calls.append((method, route, body))
        if method == "POST":
            self.snapshots += 1
            return {"output": copy.deepcopy(self.value)}
        return {"data": copy.deepcopy(self.sessions[route.rsplit("/", 1)[-1]])}

    def test_owner_blurred_exact_child_and_full_ordered_membership(self):
        result = ui.verified_snapshot(self.settings, self.request)
        self.assertEqual(
            result,
            ui.Snapshot(
                "valid",
                OWNER,
                "ses_child",
                ("ses_root", "ses_other"),
                "ses_root",
                "owner-tui",
            ),
        )
        self.assertNotIn("private-sentinel", repr(result))
        self.assertEqual(self.snapshots, 2)
        for method, route, body in self.calls:
            if method == "POST":
                self.assertEqual(route, ui._target("snapshot", "/synthetic/control"))
                self.assertEqual(
                    body,
                    {
                        "input": {
                            "generation": GENERATION,
                            "ownerClient": OWNER,
                        }
                    },
                )
            else:
                self.assertTrue(route.startswith("/api/session/"))
                self.assertIsNone(body)

    def test_home_plugin_and_disabled_tabs_do_not_invent_selection(self):
        for route in ("home", "plugin"):
            self.value = report(routeType=route, selected=None, activeRoot=None)
            self.assertIsNone(
                ui.verified_snapshot(self.settings, self.request).selected
            )
        self.value = report(tabsEnabled=False, tabs=[], activeRoot=None)
        result = ui.verified_snapshot(self.settings, self.request)
        self.assertEqual(
            (result.selected, result.tabs, result.activeRoot), ("ses_child", (), None)
        )

    def test_alternate_requires_honest_source_classification(self):
        self.value = report(client=ALTERNATE, source="single-tui")
        self.assertEqual(
            ui.verified_snapshot(self.settings, self.request).source, "single-tui"
        )
        for value in (report(client=ALTERNATE), report(source="single-tui")):
            with self.subTest(value=value["source"]), self.assertRaises(ui.StateError):
                ui._report(value, BINDING)

    def test_missing_states_issue_no_session_get(self):
        for reason in (
            "absent",
            "ambiguous",
            "overflow",
            "generation",
            "reported-unknown",
        ):
            self.calls.clear()
            self.value = {
                "protocol": 1,
                "status": "missing",
                "epoch": "new",
                "reason": reason,
            }
            result = ui.verified_snapshot(self.settings, self.request)
            self.assertEqual(result, ui.Snapshot("missing", reason=reason))
            self.assertEqual(len(self.calls), 1)

    def test_changed_selection_membership_client_or_epoch_refuses_after_native_gets(
        self,
    ):
        for change in (
            {"selected": "ses_root"},
            {"tabs": ["ses_other", "ses_root"]},
            {"client": ALTERNATE, "source": "single-tui"},
            {"epoch": "reloaded"},
        ):
            count = 0

            def request(method, route, body):
                nonlocal count
                if method == "POST":
                    count += 1
                    return {"output": report(**change) if count == 2 else report()}
                return self.request(method, route, body)

            with self.subTest(change=change):
                self.assertEqual(
                    ui.verified_snapshot(self.settings, request),
                    ui.Snapshot("missing", reason="changed"),
                )

    def test_heartbeat_sequence_change_does_not_invalidate_same_ui_snapshot(self):
        def request(method, route, body):
            if method == "POST":
                self.snapshots += 1
                return {"output": report(seq=self.snapshots)}
            return self.request(method, route, body)

        self.assertEqual(ui.verified_snapshot(self.settings, request).status, "valid")

    def test_invalid_report_matrix_never_becomes_a_heuristic(self):
        for change in (
            {"generation": "0" * 32},
            {"client": "not-a-client"},
            {"seq": True},
            {"seq": float("inf")},
            {"seq": 0},
            {"routeType": "unknown"},
            {"selected": "/private/path"},
            {"tabs": ["ses_root", "ses_root"]},
            {"tabs": ["not-a-session"]},
            {"activeRoot": "ses_missing"},
            {"focus": "current"},
            {"source": "last-viewed"},
            {"tabsEnabled": False},
            {"routeType": "home"},
            {"unexpected": "private-sentinel"},
        ):
            with self.subTest(change=change), self.assertRaises(ui.StateError):
                ui._report(report(**change), BINDING)

    def test_native_id_mismatch_nonroot_tabs_cycles_and_wrong_selected_root(self):
        for ident, changes in (
            ("ses_root", {"id": "ses_foreign"}),
            ("ses_root", {"parentID": "ses_other"}),
            ("ses_child", {"parentID": "ses_child"}),
            ("ses_child", {"parentID": "ses_other"}),
            ("ses_child", {"location": {}}),
        ):
            original = self.sessions[ident]
            self.sessions[ident] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(ui.StateError):
                ui.verified_snapshot(self.settings, self.request)
            self.sessions[ident] = original

    def test_ancestry_depth_is_bounded(self):
        self.value = report(selected="ses_depth0")
        for index in range(ui.MAX_ANCESTRY + 1):
            self.sessions[f"ses_depth{index}"] = session(
                f"ses_depth{index}", f"ses_depth{index + 1}"
            )
        with self.assertRaisesRegex(ui.StateError, "invalid-ancestry"):
            ui.verified_snapshot(self.settings, self.request)


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "id": "a" * 32,
            "home": "/home/synthetic",
            "project": "/synthetic/project",
            "container": "synthetic-no-podman",
            "opencode_version": "2.2.6",
        }
        self.connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:12345",
            "password": "x" * 43,
            "ui_state": BINDING,
        }
        self.native = SimpleNamespace(
            ContainerCommand=box.ContainerCommand,
            BoxError=box.BoxError,
            CONTAINER_OPENCODE="/native/opencode",
            container_project=lambda _data: "/synthetic/project",
            maintenance_host_env=lambda: {"PATH": "/usr/bin:/bin"},
        )
        self.ping = {"output": {"protocol": 1}}
        self.native.workspace_service = SimpleNamespace(
            validate_endpoint=service.validate_endpoint,
            client_command=Mock(return_value=["private-helper"]),
            api=Mock(side_effect=self.readiness_reply),
        )
        self.native.run = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    asdict(
                        ui.Snapshot(
                            "valid",
                            OWNER,
                            "ses_root",
                            ("ses_root",),
                            "ses_root",
                            "owner-tui",
                        )
                    )
                ),
            )
        )

    def test_absent_old_owner_or_unavailable_helper_never_executes(self):
        for setting in (None, {"available": False}):
            connection = {
                key: value
                for key, value in self.connection.items()
                if key != "ui_state"
            }
            if setting is not None:
                connection["ui_state"] = setting
            self.assertEqual(
                ui.read_snapshot(self.native, self.data, connection),
                ui.Snapshot("missing", reason="unavailable"),
            )
        self.native.run.assert_not_called()

    def test_host_reader_uses_existing_endpoint_and_stdin_helper_not_credential_argv(
        self,
    ):
        result = ui.read_snapshot(self.native, self.data, self.connection)
        self.assertEqual(result.selected, "ses_root")
        args = self.native.workspace_service.client_command.call_args.args[3]
        self.assertEqual(args[:5], ["python3", "-I", "-S", "-B", "-c"])
        self.assertNotIn(self.connection["password"], " ".join(args))
        settings = json.loads(self.native.run.call_args.kwargs["input"])
        self.assertNotIn("password", settings)
        self.assertEqual(settings["directory"], "/synthetic/project")
        self.assertTrue(self.native.run.call_args.kwargs["private"])

    def test_binding_options_and_untrusted_output_have_fixed_diagnostics(self):
        for binding in ({"available": True}, {**BINDING, "generation": "/secret"}, []):
            self.assertEqual(
                ui.read_snapshot(
                    self.native, self.data, {**self.connection, "ui_state": binding}
                ),
                ui.Snapshot("invalid", reason="invalid-binding"),
            )
        for output in ("raw-private-sentinel", "{}", "[]", '{"status":"current"}'):
            self.native.run.return_value.stdout = output
            result = ui.read_snapshot(self.native, self.data, self.connection)
            self.assertIn(result.status, ("invalid", "missing"))
            self.assertNotIn("private-sentinel", repr(result))
        for timeout in (True, 0, -1, float("inf"), float("nan"), 10**400):
            self.assertEqual(
                ui.read_snapshot(
                    self.native, self.data, self.connection, timeout=timeout
                ),
                ui.Snapshot("invalid", reason="transport"),
            )

    def test_owner_and_additional_tuis_have_distinct_nonsecret_process_labels(self):
        args = [
            self.native.CONTAINER_OPENCODE,
            self.data["project"],
            "--server",
            self.connection["url"],
        ]
        commands = [
            service.client_command(
                self.native,
                self.data,
                self.connection,
                args,
                interactive=True,
                ui_owner=owner,
            )
            for owner in (True, False, False)
        ]
        values = [json.loads(command[-len(args) - 1]) for command in commands]
        self.assertEqual(values[0]["OC_BOX_UI_STATE_CLIENT"], OWNER)
        self.assertEqual(len({value["OC_BOX_UI_STATE_CLIENT"] for value in values}), 3)
        for command, value in zip(commands, values):
            self.assertEqual(value["OC_BOX_UI_STATE_GENERATION"], GENERATION)
            self.assertNotIn(self.connection["password"], " ".join(command))
            self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", value)

    def test_other_commands_old_owners_and_image_preview_are_unchanged(self):
        for args, interactive, connection in (
            (
                [self.native.CONTAINER_OPENCODE, "api", "get", "/api/info"],
                True,
                self.connection,
            ),
            (
                [self.native.CONTAINER_OPENCODE, self.data["project"]],
                False,
                self.connection,
            ),
            (
                [self.native.CONTAINER_OPENCODE, self.data["project"]],
                True,
                {**self.connection, "ui_state": {"available": False}},
            ),
        ):
            command = service.client_command(
                self.native, self.data, connection, args, interactive=interactive
            )
            self.assertNotIn("OC_BOX_UI_STATE_", " ".join(command))
        args = [
            self.native.CONTAINER_OPENCODE,
            self.data["project"],
            "--server",
            self.connection["url"],
        ]
        command = service.client_command(
            self.native,
            self.data,
            {**self.connection, "image_preview": True},
            args,
            interactive=True,
            ui_owner=True,
        )
        names = json.loads(command[-len(args) - 1])
        self.assertEqual(
            set(json.loads(names["OPENCODE_CLI_CONFIG_CONTENT"])), {"prompt", "session"}
        )
        self.assertEqual(names["OC_BOX_UI_STATE_CLIENT"], OWNER)

    def readiness_reply(self, _box, _data, _connection, _method, route, *_args, **_kwargs):
        if route.startswith("/api/plugin?"):
            return {"data": [ACTIVE_PLUGIN]}
        return self.ping

    def test_ready_probe_is_pinned_and_quiet_on_refusal(self):
        self.assertTrue(ui.available(self.native, self.data, self.connection))
        self.assertEqual(
            self.native.workspace_service.api.call_args.args[3:5],
            (
                "POST",
                ui._target("ping", "/synthetic/project"),
            ),
        )
        self.native.workspace_service.api.side_effect = box.BoxError("private-sentinel")
        self.assertFalse(ui.available(self.native, self.data, self.connection))

    def test_early_empty_catalog_waits_for_one_active_helper_and_pings_once(self):
        api = self.native.workspace_service.api
        api.side_effect = [
            {"data": None},
            {"data": []},
            {"data": [ACTIVE_PLUGIN]},
            self.ping,
        ]
        with (
            patch.object(ui.time, "monotonic", side_effect=[0, 1, 1.1, 2, 2.1]),
            patch.object(ui.time, "sleep") as pause,
        ):
            self.assertTrue(ui.available(self.native, self.data, self.connection))
        self.assertEqual(
            [call.args[3] for call in api.call_args_list], ["GET", "GET", "GET", "POST"]
        )
        self.assertEqual(api.call_args_list[1].args[4], api.call_args_list[2].args[4])
        budgets = [call.kwargs["timeout"] for call in api.call_args_list]
        self.assertEqual(budgets, [4, 3, 2, 1.9])
        pause.assert_called_once()

    def test_active_catalog_at_deadline_does_not_send_a_ping(self):
        api = self.native.workspace_service.api
        api.side_effect = [{}, {"data": [ACTIVE_PLUGIN]}]
        with patch.object(ui.time, "monotonic", side_effect=[0, 3.8, 4]):
            self.assertFalse(ui.available(self.native, self.data, self.connection))
        self.assertEqual([call.args[3] for call in api.call_args_list], ["GET", "GET"])

    def test_missing_failed_or_ambiguous_helper_cannot_enable_reporting(self):
        api = self.native.workspace_service.api
        for plugins in (
            [],
            [{"id": ui.PLUGIN_ID, "state": {"status": "failed"}}],
            [ACTIVE_PLUGIN, ACTIVE_PLUGIN],
            [None],
        ):
            api.reset_mock()
            api.side_effect = [{}, {"data": plugins}]
            with (
                self.subTest(plugins=plugins),
                patch.object(ui.time, "monotonic", side_effect=[0, 0, 4, 4]),
                patch.object(ui.time, "sleep"),
            ):
                self.assertFalse(ui.available(self.native, self.data, self.connection))
            self.assertEqual([call.args[3] for call in api.call_args_list], ["GET", "GET"])

    def test_new_stable_patches_still_require_the_exact_reporting_protocol(self):
        for release in ("2.3.2", "2.4.0", "2.99.123"):
            data = {**self.data, "opencode_version": release}
            with self.subTest(release=release):
                self.ping = {"output": {"protocol": 1}}
                self.assertTrue(ui.available(self.native, data, self.connection))
                self.assertEqual(
                    ui.read_snapshot(self.native, data, self.connection).selected,
                    "ses_root",
                )
                for ping in (
                    {"output": {"protocol": 2}},
                    {"output": {"protocol": True}},
                    {"output": {"protocol": 1.0}},
                    {"output": {"protocol": 1, "other": 1}},
                    {"result": {"protocol": 1}},
                ):
                    self.ping = ping
                    self.assertFalse(ui.available(self.native, data, self.connection))

    def test_nonrelease_versions_do_not_probe_private_native_service(self):
        for release in ("1.19.5", "2.3.2-preview", "2.3.2+modified", "3.0.0", None):
            with self.subTest(release=release):
                data = {**self.data, "opencode_version": release}
                self.assertFalse(ui.available(self.native, data, self.connection))
                self.assertEqual(
                    ui.read_snapshot(self.native, data, self.connection),
                    ui.Snapshot("missing", reason="unsupported-version"),
                )
        self.native.workspace_service.api.assert_not_called()
        self.native.run.assert_not_called()

    def test_flat_launcher_can_probe_newer_native_v2_without_package_imports(self):
        source = Path(ui.__file__).resolve().parent
        probe = """
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import ui_state

requests = []
def api(box, data, connection, method, route, *args, **kwargs):
    requests.append((method, route))
    if route.startswith("/api/plugin?"):
        return {"data": [{"id": ui_state.PLUGIN_ID, "state": {"status": "active"}}]}
    return {"output": {"protocol": 1}}

box = SimpleNamespace(
    BoxError=RuntimeError,
    workspace_service=SimpleNamespace(api=api),
    container_project=lambda data: "/synthetic/project",
)
assert ui_state.available(box, {"opencode_version": "2.3.2"}, {})
assert [method for method, _ in requests] == ["GET", "GET", "POST"]
assert "oc_box" not in sys.modules
"""
        result = subprocess.run(
            [os.sys.executable, "-I", "-S", "-B", "-c", probe, str(source)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_owner_ui_bridge_is_opt_in_memory_only_and_probed_once(self):
        program = (
            "import json,sys;"
            "print(json.dumps({'url':'http://127.0.0.1:12345','password':'"
            + "x"
            * 43
            + "'}),flush=True);"
            "sys.stdin.buffer.read()"
        )
        original = copy.deepcopy(self.data)
        bindings = []
        with tempfile.TemporaryDirectory() as temporary:
            for enabled in (False, True, True):
                with (
                    patch.object(
                        service,
                        "socket_path",
                        return_value=Path(temporary) / "owner.sock",
                    ),
                    patch.object(
                        box,
                        "exec_command",
                        return_value=box.ContainerCommand(
                            [os.sys.executable, "-I", "-c", program], {}
                        ),
                    ),
                    patch.object(ui, "available", return_value=True) as ready,
                    service.Owner(
                        box,
                        Path(temporary) / "workspace.json",
                        self.data,
                        ui_bridge=enabled,
                    ) as owner,
                ):
                    if enabled:
                        binding = owner.connection["ui_state"]
                        self.assertTrue(binding["available"])
                        self.assertTrue(ui._nonce(binding["generation"]))
                        self.assertTrue(ui._nonce(binding["ownerClient"]))
                        bindings.append(binding)
                        ready.assert_called_once()
                    else:
                        self.assertEqual(
                            owner.connection["ui_state"], {"available": False}
                        )
                        ready.assert_not_called()
                    self.assertEqual(self.data, original)
        self.assertNotEqual(bindings[0], bindings[1])

    def test_host_rechecks_owner_classification_and_active_root_consistency(self):
        good = asdict(
            ui.Snapshot(
                "valid", OWNER, "ses_root", ("ses_root",), "ses_root", "owner-tui"
            )
        )
        for changes in (
            {"client": ALTERNATE},
            {"selected": None},
            {"activeRoot": None},
        ):
            self.native.run.return_value.stdout = json.dumps({**good, **changes})
            self.assertEqual(
                ui.read_snapshot(self.native, self.data, self.connection),
                ui.Snapshot("invalid", reason="transport"),
            )

    def test_private_package_is_v2_only_and_not_in_final_mandatory_overlay(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}),
        ):
            for release, expected in (
                ("1.19.5", False),
                ("2.2.6", True),
                ("2.3.0", True),
                ("2.3.2", True),
                ("2.4.0", True),
                ("2.3.2-preview", False),
            ):
                with tarfile.open(
                    fileobj=io.BytesIO(
                        box.config_archive(
                            {
                                **self.data,
                                "opencode_version": release,
                            }
                        )
                    ),
                    mode="r:gz",
                ) as archive:
                    names = archive.getnames()
                    profile = json.load(archive.extractfile("oc-box-profile.json"))
                    self.assertNotIn("oc-box-ui-state", json.dumps(profile))
                    found = "plugins/oc-box-ui-state/package.json" in names
                    self.assertEqual(found, expected)
                    if found:
                        self.assertTrue(
                            all(
                                "plugins/oc-box-ui-state/" + name in names
                                for name in ui.RESOURCES
                            )
                        )
            original = Path(temporary) / "opencode/plugins/oc-box-ui-state"
            original.mkdir(parents=True)
            with self.assertRaisesRegex(box.BoxError, "conflicts"):
                box.config_archive(self.data)
            self.assertTrue(original.is_dir())

    def test_history_only_archive_keeps_old_ui_policy_for_newer_native_v2(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}),
        ):
            helper = "plugins/oc-box-ui-state/package.json"
            for release, was_included in (
                ("2.2.6", True),
                ("2.3.0", True),
                ("2.3.2", False),
            ):
                data = {**self.data, "opencode_version": release}
                archive = box.config_archive(data, history_only=True)
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
                    self.assertIs(helper in opened.getnames(), was_included)
            host_plugin = Path(temporary) / "opencode/plugins/oc-box-ui-state"
            host_plugin.mkdir(parents=True)
            manifest = host_plugin / "package.json"
            content = b'{"name":"synthetic-user-plugin"}'
            manifest.write_bytes(content)
            with self.assertRaisesRegex(box.BoxError, "conflicts"):
                box.config_archive(data)
            archive = box.config_archive(data, history_only=True)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
                self.assertEqual(opened.extractfile(helper).read(), content)
            self.assertEqual(manifest.read_bytes(), content)

    def test_javascript_memory_and_serialized_reporter_contract(self):
        result = subprocess.run(
            [
                "node",
                "tests/ui_state_fixture.mjs",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UI_STATE_CONTRACT_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
