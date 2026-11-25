import argparse
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from oc_box import native_preflight, native_runtime_guard
from oc_box import oc_box as box
from oc_box import runtime_safeguard as guard
from oc_box import workspace_control, workspace_service


OLD = "2.3.2"
NEW = "2.4.0"
CONTAINER = "1" * 64
DIGEST = "2" * 64
VOLUME = "oc-box-home-" + "3" * 32


class RuntimeSafeguardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ("3" * 32)
        self.root.mkdir()
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "OC_BOX_CONTAINER": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / "workspace.json"
        self.project = self.root / "synthetic-project"
        self.project.mkdir()
        self.data = {
            "id": "3" * 32,
            "version": 2,
            "ready": True,
            "project": str(self.project),
            "container": "oc-box-existing",
            "home_volume": VOLUME,
            "home": "/home/synthetic",
            "opencode_version": OLD,
            "distro": "fedora",
        }
        box.atomic_json(self.path, self.data)
        self.record = native_runtime_guard.Record(CONTAINER, VOLUME, OLD, DIGEST, True)
        self.binary = guard.HostBinary(10, (), DIGEST)

    def args(self, command="start", **values):
        return argparse.Namespace(command=command, runtime_guard=False, **values)

    @contextlib.contextmanager
    def synthetic_admission(self, version=NEW):
        with (
            patch.object(
                guard, "_inspect", return_value=(CONTAINER, ("fixed-mounts",))
            ),
            patch.object(
                guard, "pinned_host", return_value=contextlib.nullcontext(self.binary)
            ),
            patch.object(guard, "_host_release", return_value=version),
            patch.object(
                guard.HostBinary, "command", return_value="/pinned/native/opencode"
            ),
            patch.object(guard, "_probe_digest", return_value="4" * 64),
        ):
            yield

    def seed(self, *, ready=True):
        pending = replace(self.record, ready=False)
        native_runtime_guard.publish(self.path, None, pending)
        if ready:
            native_runtime_guard.publish(self.path, pending, self.record)
            return self.record
        return pending

    def test_every_stable_v2_upgrade_triggers_check(self):
        for item, expected in (
            ("2.3.2", False),
            ("2.3.3", True),
            ("2.4.0", True),
            ("2.10.0", True),
            ("2.2.9", False),
            ("1.19.5", False),
            ("3.0.0", False),
            ("2.4.0-beta.1", False),
        ):
            with self.subTest(candidate=item):
                self.assertIs(guard.version_upgrade(OLD, item), expected)

    def test_resource_refusals_identify_the_specific_fix_before_engine_access(self):
        cases = (
            (
                {"restore_incomplete": True, "ready": False},
                {},
                "restore.*fresh destination",
            ),
            ({"ready": False}, {}, "completed setup.*status --table"),
            ({"distro": "alpine"}, {}, "do not support Alpine"),
            (
                {"podman_args": ["--group-add=keep-groups"]},
                {},
                "saved custom Podman options",
            ),
            ({}, {"clear_devices": True}, "Retry without --device or --clear-devices"),
            (
                {},
                {"podman_arg": ["--group-add=keep-groups"]},
                "Retry without --podman-arg",
            ),
            ({}, {"import_session": "ses_test"}, "Retry without --import-session"),
            ({"devices": ["some.cdi/device=node"]}, {}, "saved plain accelerator"),
        )
        for data, flags, expected in cases:
            with (
                self.subTest(data=data, flags=flags),
                patch.object(guard, "_inspect") as inspect,
            ):
                with self.assertRaisesRegex(box.BoxError, expected):
                    with guard.admit(
                        box, self.path, {**self.data, **data}, self.args(**flags), None
                    ):
                        self.fail("unsupported setup entered admission")
                inspect.assert_not_called()

    def test_saved_device_nodes_use_pinned_admission_without_option_changes(self):
        data = {**self.data, "devices": ["/dev/accel/accel0", "/dev/dri/renderD128"]}
        box.atomic_json(self.path, data)
        engine = {
            "PATH": "/usr/bin",
            "CONTAINERS_STORAGE_CONF": "/synthetic/storage.conf",
        }
        with (
            self.synthetic_admission(version=OLD),
            patch.object(
                guard.entrypoint_migration, "engine_environment", return_value=engine
            ),
        ):
            with guard.admit(
                box, self.path, data, self.args(), self.record
            ) as admission:
                self.assertIs(admission.engine_env, engine)
                guard._inspect.assert_called_once_with(
                    box, self.path, data, running=False, engine_env=engine
                )
        self.assertEqual(box.read_json(self.path)["devices"], data["devices"])

    def test_retirement_requires_successful_start_and_mount_access(self):
        marker = guard.entrypoint_migration.RETIRE
        for failure in ("start", "inconsistent", "mount", "device-access", None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as name:
                path = Path(name) / "workspace.json"
                data = {**self.data, marker: {"synthetic": True}}
                if failure == "device-access":
                    data["devices"] = ["/dev/accel/accel0"]
                box.atomic_json(path, data)
                token = workspace_control.capture(path)
                env = {
                    "PATH": "/usr/bin",
                    "CONTAINERS_STORAGE_CONF": "/synthetic/storage.conf",
                }
                state = {
                    "Status": "running",
                    "Running": True,
                    "Paused": False,
                    "Restarting": False,
                    "Pid": 0 if failure == "inconsistent" else 123,
                }
                scope = guard.Admission(
                    path,
                    self.binary,
                    None,
                    self.record,
                    (CONTAINER, ()),
                    token,
                    transition=True,
                    engine_env=env,
                )
                calls = []

                def start(*args, **kwargs):
                    self.assertEqual(args, ("--remote=false", "start", CONTAINER))
                    self.assertIs(kwargs["env"], env)
                    calls.append("start")
                    if failure == "start":
                        raise box.BoxError("synthetic start failure")

                def mount(command, **kwargs):
                    self.assertTrue(command.guarded_spawn)
                    self.assertIs(command.environment, env)
                    self.assertIn("--remote=false", command)
                    self.assertIn(CONTAINER, command)
                    self.assertIn("/bin/sh", command)
                    self.assertNotIn("/usr/bin/python3", command)
                    if command[-1] in data.get("devices", []):
                        self.assertFalse(kwargs["check"])
                        calls.append("device-access")
                        return subprocess.CompletedProcess(command, 1)
                    self.assertEqual(
                        list(command[-2:]), [data["project"], data["home"]]
                    )
                    calls.append("mount")
                    if failure == "mount":
                        raise box.BoxError("synthetic mount failure")

                def retire(
                    module,
                    reg,
                    current,
                    decision,
                    candidate,
                    *,
                    after_start=False,
                    env=None,
                ):
                    self.assertIs(module, box)
                    self.assertEqual(
                        (reg, decision, candidate), (path, token, CONTAINER)
                    )
                    self.assertIs(current, data)
                    self.assertIs(env, scope.engine_env)
                    self.assertTrue(workspace_control.unchanged(path, token))
                    calls.append("retire" if after_start else "verify-parked")

                with (
                    patch.object(scope, "require_live"),
                    patch.object(box, "check_resource_controllers"),
                    patch.object(box, "podman", side_effect=start),
                    patch.object(
                        box,
                        "inspect_owned",
                        return_value={"Id": CONTAINER, "State": state},
                    ),
                    patch.object(box, "run", side_effect=mount),
                    patch.object(
                        guard.entrypoint_migration,
                        "verify_retirement",
                        side_effect=retire,
                    ),
                ):
                    if failure:
                        message = (
                            "staged replacement running"
                            if failure == "inconsistent"
                            else "saved device nodes"
                            if failure == "device-access"
                            else failure
                        )
                        with self.assertRaisesRegex(box.BoxError, message):
                            scope.start(box, data)
                    else:
                        scope.start(box, data)
                self.assertEqual(
                    calls,
                    ["verify-parked", "start"]
                    if failure in ("start", "inconsistent")
                    else ["verify-parked", "start", "mount"]
                    if failure == "mount"
                    else ["verify-parked", "start", "mount", "device-access"]
                    if failure == "device-access"
                    else ["verify-parked", "start", "mount", "retire"],
                )
                if failure == "device-access":
                    self.assertIn(marker, box.read_json(path))
                if failure in ("start", "inconsistent"):
                    with self.assertRaisesRegex(
                        workspace_control.ControlError, "uncertain-control"
                    ):
                        workspace_control.require_certain(path)

    def test_device_stop_pins_store_without_requiring_device_access(self):
        data = {**self.data, "devices": ["/dev/accel/accel0"]}
        engine = {
            "PATH": "/usr/bin",
            "CONTAINERS_STORAGE_CONF": "/synthetic/local.conf",
        }
        running = {"Id": CONTAINER, "State": {"Running": True}}
        stopped = {
            "Id": CONTAINER,
            "State": {
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Status": "exited",
                "Pid": 0,
                "ConmonPid": 0,
            },
        }
        with (
            patch.object(box, "find_workspace", return_value=(self.path, data)),
            patch.object(guard, "load", return_value=self.record),
            patch.object(
                guard.entrypoint_migration, "engine_environment", return_value=engine
            ),
            patch.object(guard.entrypoint_migration, "require_local_engine") as local,
            patch.object(
                box, "inspect_owned", side_effect=[running, running, stopped]
            ) as inspect,
            patch.object(box, "podman") as podman,
            patch.object(guard.guarded_devices, "verify") as devices,
        ):
            box._stop_selected(self.path, data, admitted=True)
        local.assert_called_once_with(box, engine)
        devices.assert_not_called()
        self.assertEqual(inspect.call_count, 3)
        for call in inspect.call_args_list:
            self.assertEqual(call.kwargs, {"local_only": True, "env": engine})
        podman.assert_called_once_with(
            "--remote=false",
            "stop",
            "--time",
            "10",
            CONTAINER,
            capture=True,
            env=engine,
        )

    def test_explicit_stop_during_retirement_targets_only_verified_replacement(self):
        migration = guard.entrypoint_migration
        data = {
            **self.data,
            migration.RETIRE: {"synthetic": True},
            migration.RECEIPT: {"id": CONTAINER, "bootstrap": False, "volume": [1, 2]},
        }
        idle = {
            "Id": CONTAINER,
            "Name": data["container"],
            "State": {
                "Status": "created",
                "Running": False,
                "Pid": 0,
                "Paused": False,
                "Restarting": False,
            },
        }
        running = {
            **idle,
            "State": {
                **idle["State"],
                "Status": "running",
                "Running": True,
                "Pid": 123,
            },
        }
        with (
            patch.object(box, "find_workspace", return_value=(self.path, data)),
            patch.object(guard, "load", return_value=None),
            patch.object(migration, "retirement_target", return_value=CONTAINER),
            patch.object(
                migration, "engine_environment", return_value={"PATH": "/usr/bin"}
            ),
            patch.object(migration, "require_local_engine"),
            patch.object(box, "inspect_owned", return_value=idle) as inspect,
            patch.object(box, "podman") as stop,
        ):
            box._stop_selected(self.path, self.data, admitted=True)
            inspect.assert_called_once_with(
                {**data, "container": CONTAINER},
                local_only=True,
                env={"PATH": "/usr/bin"},
            )
            stop.assert_not_called()
            inspect.side_effect = [running, idle]
            box._stop_selected(self.path, self.data, admitted=True)
            stop.assert_called_once_with(
                "--remote=false",
                "stop",
                "--time",
                "10",
                CONTAINER,
                capture=True,
                private=True,
                timeout=15,
                env={"PATH": "/usr/bin"},
            )
            inspect.side_effect = [running, running]
            with self.assertRaisesRegex(box.BoxError, "Could not verify"):
                box._stop_selected(self.path, self.data, admitted=True)
            inspect.side_effect = None
            inspect.return_value = {**idle, "Id": "4" * 64}
            with self.assertRaisesRegex(box.BoxError, "no substitute"):
                box._stop_selected(self.path, self.data, admitted=True)
            self.assertEqual(stop.call_count, 2)

    def test_selected_engine_must_be_local_even_without_remote_environment_flags(self):
        migration = guard.entrypoint_migration
        environment = {"CONTAINERS_CONF": "/synthetic/containers.conf"}
        output = '{"host":{"serviceIsRemote":true,"security":{"rootless":true}}}'
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                box, "podman", return_value=subprocess.CompletedProcess([], 0, output)
            ) as podman,
            self.assertRaisesRegex(box.BoxError, "local rootless"),
        ):
            migration.require_local_engine(box, environment)
        podman.assert_called_once_with(
            "--remote=false",
            "info",
            "--format=json",
            capture=True,
            private=True,
            timeout=15,
            env=environment,
        )

    def test_final_bound_observation_must_have_a_live_process_before_clearing_uncertainty(
        self,
    ):
        migration = guard.entrypoint_migration
        data = {**self.data, migration.RETIRE: {}}
        box.atomic_json(self.path, data)
        token = workspace_control.capture(self.path)
        signature = ("project", "work", "/synthetic/mountpoint", "home")
        idle = {
            "Id": CONTAINER,
            "Mounts": [{}, {}],
            "State": {
                "Status": "exited",
                "Running": False,
                "Pid": 0,
                "Paused": False,
                "Restarting": False,
            },
        }
        running = {
            **idle,
            "State": {
                **idle["State"],
                "Status": "running",
                "Running": True,
                "Pid": 123,
            },
        }
        inconsistent = {**running, "State": {**running["State"], "Pid": 0}}
        volume = {
            "Name": VOLUME,
            "Driver": "local",
            "Labels": {box.LABEL: data["id"]},
            "Mountpoint": signature[2],
        }
        scope = guard.Admission(
            self.path,
            self.binary,
            None,
            self.record,
            (CONTAINER, signature),
            token,
            transition=True,
            engine_env={"PATH": "/usr/bin"},
        )
        with (
            patch.object(
                box, "inspect_owned", side_effect=[idle, running, inconsistent]
            ),
            patch.object(box, "validate_project", return_value=self.project),
            patch.object(box, "find_workspace", return_value=(self.path, data)),
            patch.object(guard.workspace_identity, "same_project", return_value=True),
            patch.object(
                guard.history_recovery,
                "passive_workspace_mounts",
                return_value=signature,
            ),
            patch.object(box, "check_resource_controllers"),
            patch.object(
                box,
                "podman",
                return_value=subprocess.CompletedProcess([], 0, json.dumps([volume])),
            ),
            patch.object(migration, "verify_retirement") as retire,
            patch.object(box, "run") as mount,
            self.assertRaisesRegex(box.BoxError, "same running workspace"),
        ):
            scope.start(box, data)
        self.assertEqual(retire.call_count, 1)
        self.assertFalse(retire.call_args.kwargs.get("after_start", False))
        mount.assert_not_called()
        with self.assertRaisesRegex(
            workspace_control.ControlError, "uncertain-control"
        ):
            workspace_control.require_certain(self.path)

    def test_v2_staged_stop_does_not_dispatch_without_admission(self):
        data = {**self.data, guard.entrypoint_migration.RETIRE: {}}
        with (
            patch.object(box, "find_workspace", return_value=(self.path, data)),
            patch.object(guard, "load", return_value=None),
            patch.object(
                workspace_control,
                "admission",
                side_effect=workspace_control.ControlError("admission-timeout"),
            ),
            patch.object(guard.entrypoint_migration, "stop_staged") as stop,
            self.assertRaisesRegex(box.BoxError, "stop was not admitted"),
        ):
            box._stop_v2(self.path, data)
        stop.assert_not_called()

    def test_v2_staged_stop_does_not_repeat_when_uncertainty_remains(self):
        workspace_control.capture(self.path)
        workspace_control.mark_uncertain(self.path)
        with (
            patch.object(box, "_stop_selected") as stop,
            self.assertRaisesRegex(box.BoxError, "uncertain-control"),
        ):
            box._stop_v2(self.path, self.data)
        stop.assert_called_once_with(self.path, self.data, admitted=True)

    def test_partial_marker_blocks_start_and_sync_without_preparing_environment(self):
        data = {**self.data, guard.entrypoint_migration.RETIRE: {"synthetic": True}}
        for command in ("start", "sync"):
            with (
                self.subTest(command=command),
                contextlib.ExitStack() as context,
                patch.object(box, "detect_opencode_release") as probe,
                patch.object(box, "launch_credentials") as credentials,
                self.assertRaisesRegex(box.BoxError, "waiting for verified startup"),
            ):
                box._locked_workspace_command(
                    self.path,
                    data,
                    self.args(command=command),
                    context,
                    None,
                )
            probe.assert_not_called()
            credentials.assert_not_called()

    def test_preparation_failure_after_retirement_cleans_up_on_same_local_engine(self):
        data = {
            **self.data,
            guard.entrypoint_migration.RECEIPT: {
                "id": CONTAINER,
                "bootstrap": True,
                "volume": [1, 2],
            },
        }
        box.atomic_json(self.path, data)
        token = workspace_control.capture(self.path)
        engine = {"PATH": "/usr/bin", "CONTAINERS_CONF": "/synthetic/approved.conf"}
        scope = guard.Admission(
            self.path,
            self.binary,
            None,
            self.record,
            (CONTAINER, ()),
            token,
            transition=True,
            engine_env=engine,
        )
        calls = []

        def admitted(command, admission, **options):
            with admission():
                self.assertEqual(command[:3], ["podman", "--remote=false", "exec"])
                self.assertIn(CONTAINER, command)
                self.assertEqual(options["env"], engine)
                calls.append("baseline-failure")
            return subprocess.CompletedProcess(command, 125)

        def ordinary(command, **options):
            self.assertEqual(options["env"], engine)
            self.assertEqual(command[:2], ["podman", "--remote=false"])
            if command[2:4] == ["container", "inspect"]:
                self.assertEqual(command[4:], [CONTAINER])
                info = {
                    "Id": CONTAINER,
                    "State": {"Running": True},
                    "Config": {"Labels": {box.LABEL: data["id"]}},
                }
                options["stdout"].write(json.dumps([info]).encode())
                calls.append("inspect")
            else:
                self.assertEqual(command[2:], ["stop", "--time", "10", CONTAINER])
                calls.append("stop")
            return subprocess.CompletedProcess(command, 0)

        marker = guard._ACTIVE.set(scope)
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "CONTAINERS_CONF": "/synthetic/wrong.conf",
                        "SYNTHETIC_UNRELATED": "not-forwarded",
                    },
                ),
                patch.object(
                    guard.HostBinary, "command", return_value="/pinned/native/opencode"
                ),
                patch.object(
                    guard.history_recovery, "run_admitted", side_effect=admitted
                ),
                patch.object(subprocess, "run", side_effect=ordinary),
            ):
                with self.assertRaisesRegex(box.BoxError, "exit 125"):
                    box.install_baseline(data, guarded=scope)
                self.assertNotIn(guard.entrypoint_migration.RETIRE, data)
                box.stop_container(data)
        finally:
            guard._ACTIVE.reset(marker)
        self.assertEqual(calls, ["baseline-failure", "inspect", "stop"])

    def test_native_install_and_owner_client_stay_pinned_after_retirement(self):
        data = {
            **self.data,
            guard.entrypoint_migration.RECEIPT: {
                "id": CONTAINER,
                "bootstrap": False,
                "volume": [1, 2],
            },
        }
        box.atomic_json(self.path, data)
        token = workspace_control.capture(self.path)
        engine = {"PATH": "/usr/bin", "CONTAINERS_CONF": "/synthetic/approved.conf"}
        scope = guard.Admission(
            self.path,
            self.binary,
            None,
            self.record,
            (CONTAINER, ()),
            token,
            transition=True,
            engine_env=engine,
        )
        password = "synthetic-private-owner-password"
        marker = guard._ACTIVE.set(scope)
        try:
            with (
                patch.dict(os.environ, {"CONTAINERS_CONF": "/synthetic/wrong.conf"}),
                patch.object(box, "run") as run,
                patch.object(guard, "container_digest", return_value=DIGEST),
            ):
                guard.install_container(box, data)
                install = run.call_args.args[0]
                client = workspace_service.client_command(
                    box, data, {"password": password}, ["/bin/true"]
                )
        finally:
            guard._ACTIVE.reset(marker)
        for command in (install, client):
            self.assertEqual(command[:3], ["podman", "--remote=false", "exec"])
            self.assertIn(CONTAINER, command)
            self.assertNotIn(password, command)
            self.assertEqual(
                command.environment["CONTAINERS_CONF"], engine["CONTAINERS_CONF"]
            )
        self.assertEqual(install.environment, engine)
        self.assertEqual(client.environment, {**engine, "OPENCODE_PASSWORD": password})
        self.assertNotIn(guard.entrypoint_migration.RETIRE, data)

    def test_ordinary_rewrites_keep_migration_markers_durable(self):
        migration = guard.entrypoint_migration
        previous = box.read_json(self.path)
        fsync = os.fsync
        for field in (migration.FIELD, migration.RECEIPT, migration.RETIRE):
            with (
                self.subTest(field=field),
                patch.object(os, "fsync", wraps=fsync) as sync,
            ):
                data = {**previous, field: {"synthetic": True}}
                box.atomic_json(self.path, data)
                self.assertEqual(sync.call_count, 2)
                self.assertEqual(box.read_json(self.path), data)
        data = {**previous, migration.RETIRE: {"authorized": True}}
        existing = box.read_json(self.path)
        for failure, expected in ((1, existing), (2, data)):
            with self.subTest(fsync_failure=failure):
                calls = []

                def fault(fd):
                    calls.append(fd)
                    if len(calls) == failure:
                        raise OSError("synthetic fsync failure")
                    fsync(fd)

                with (
                    patch.object(os, "fsync", side_effect=fault),
                    self.assertRaises(OSError),
                ):
                    box.atomic_json(self.path, data)
                self.assertEqual(box.read_json(self.path), expected)

    def test_explicit_check_does_not_find_or_register_workspace(self):
        def fake_probe(_box, _version):
            for phase in ("stage", "probe", "cleanup"):
                with native_preflight._logged_phase(phase):
                    pass
            return DIGEST

        with (
            patch.object(box, "find_workspace") as lookup,
            patch.object(native_preflight, "check", side_effect=fake_probe) as check,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(box.main(["runtime-check", NEW]), 0)
        check.assert_called_once_with(box, NEW)
        lookup.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("passed isolated Build-plugin preflight", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            self.assertEqual(box.main(["logs", "--tail"]), 0)
        for task in (
            "Checking an isolated OpenCode runtime",
            "Staging the exact native CLI and SDK",
            "Verifying the required Build plugin offline",
            "Verifying native preflight cleanup",
        ):
            self.assertIn(f"runtime-check: {task}", logs.getvalue())

    def test_default_upgrade_refusal_precedes_credential_and_live_paths(self):
        category = next(iter(native_preflight.CONFIRMED_REFUSALS))
        before = self.path.read_bytes()
        with (
            self.synthetic_admission(),
            patch.object(box, "check_backend"),
            patch.object(box, "state_root", return_value=self.root),
            patch.object(box, "find_workspace", return_value=(self.path, self.data)),
            patch.object(box, "detect_opencode_release", return_value=NEW),
            patch.object(workspace_service, "controller_running", return_value=False),
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "start_container") as start,
            patch.object(box, "launch") as launch,
            patch.object(
                native_preflight,
                "check",
                side_effect=native_preflight.Refusal(category, proof=DIGEST),
            ) as check,
        ):
            for expected in (
                "not supported.*github.com/groopcd/oc-box/issues/new",
                "previously refused",
            ):
                with self.assertRaisesRegex(box.BoxError, expected):
                    box.main(["start", "--project", str(self.project)])
        check.assert_called_once()
        for call in (credentials, start, launch):
            call.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(native_runtime_guard.load(self.path))
        self.assertEqual(native_runtime_guard.rejected(self.path).version, NEW)

    def test_legacy_replacement_cannot_run_without_consent_or_with_an_enrolled_floor(
        self,
    ):
        error = box.BoxError("PATH-dependent startup")
        error.legacy_entrypoint = CONTAINER
        with (
            patch.object(guard, "_inspect", side_effect=error),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(guard, "pinned_host") as preflight,
            patch.object(guard.entrypoint_migration, "convert") as convert,
        ):
            for record, pattern in (
                (None, "--convert-legacy-container"),
                (self.record, "already bound"),
            ):
                with self.subTest(enrolled=record is not None):
                    with self.assertRaisesRegex(box.BoxError, pattern):
                        with guard.admit(
                            box, self.path, self.data, self.args(), record
                        ):
                            self.fail("unapproved replacement was admitted")
        preflight.assert_not_called()
        convert.assert_not_called()

    def test_legacy_replacement_still_requires_preflight_and_binds_the_new_id(self):
        error = box.BoxError("PATH-dependent startup")
        error.legacy_entrypoint = CONTAINER
        replacement = "5" * 64
        order = []

        def preflight(*_):
            order.append("preflight")
            return DIGEST

        def convert(module, path, data, token, source, *, rebuild, env):
            order.append("convert")
            self.assertIs(module, box)
            self.assertIs(data, self.data)
            self.assertEqual((path, source, rebuild), (self.path, CONTAINER, False))
            self.assertEqual(env, guard.entrypoint_migration.engine_environment(box))
            self.assertTrue(workspace_control.unchanged(path, token))
            self.assertIsNone(native_runtime_guard.load(self.path))

        with (
            self.synthetic_admission(),
            patch.object(
                guard,
                "_inspect",
                side_effect=[error, (replacement, ("fixed-mounts",))],
            ),
            patch.object(native_preflight, "check", side_effect=preflight),
            patch.object(guard.entrypoint_migration, "convert", side_effect=convert),
        ):
            args = self.args(convert_legacy_container=True)
            with guard.admit(
                box,
                self.path,
                self.data,
                args,
                None,
                prepare_replacement=lambda: order.append("credentials"),
            ) as scope:
                self.assertEqual(scope.record.container, replacement)
                self.assertFalse(scope.record.ready)
        self.assertEqual(order, ["preflight", "credentials", "convert"])

    def test_bad_vault_does_not_retire_original_os_on_rebuild(self):
        data = {**self.data, guard.entrypoint_migration.FIELD: {}}
        box.atomic_json(self.path, data)
        with (
            self.synthetic_admission(),
            patch.object(native_preflight, "check", return_value=DIGEST) as preflight,
            patch.object(
                box, "launch_credentials", side_effect=box.vault.VaultError("bad vault")
            ),
            patch.object(guard.entrypoint_migration, "convert") as convert,
            contextlib.ExitStack() as stack,
            self.assertRaisesRegex(box.BoxError, "bad vault"),
        ):
            box._locked_workspace_command(
                self.path, data, self.args(rebuild_legacy_container=True), stack, None
            )
        preflight.assert_called_once_with(box, NEW)
        convert.assert_not_called()
        self.assertEqual(box.read_json(self.path), data)

    def test_completed_swap_cannot_fall_back_to_ordinary_start_before_enrollment(self):
        data = {
            **self.data,
            guard.entrypoint_migration.RECEIPT: {
                "id": CONTAINER,
                "bootstrap": False,
                "volume": [1, 2],
            },
        }
        with (
            patch.object(guard, "load", return_value=None),
            patch.object(
                guard, "admit", side_effect=box.BoxError("required guard")
            ) as admit,
            patch.object(box, "detect_opencode_release") as probe,
            patch.object(box, "launch_credentials") as credentials,
            contextlib.ExitStack() as stack,
            self.assertRaisesRegex(box.BoxError, "required guard"),
        ):
            box._locked_workspace_command(self.path, data, self.args(), stack, None)
        self.assertEqual(admit.call_args.args[2], data)
        probe.assert_not_called()
        credentials.assert_not_called()

    def test_unavailable_scratch_probe_is_not_cached(self):
        with (
            self.synthetic_admission(),
            patch.object(
                native_preflight,
                "check",
                side_effect=native_preflight.Refusal("backend"),
            ) as check,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(
                    box.BoxError, "not a recorded compatibility failure"
                ):
                    with guard.admit(box, self.path, self.data, self.args(), None):
                        self.fail("unavailable scratch probe was admitted")
        self.assertEqual(check.call_count, 2)
        self.assertIsNone(native_runtime_guard.rejected(self.path))

    def test_build_failure_of_different_artifact_does_not_flag_host_release(self):
        baseline = self.seed()
        with (
            self.synthetic_admission(),
            patch.object(
                native_preflight,
                "check",
                side_effect=native_preflight.Refusal(
                    "build-plugin", proof="0" * 64, exit_code=44
                ),
            ),
            self.assertRaisesRegex(
                box.BoxError, "artifact.*not a recorded compatibility failure"
            ),
        ):
            with guard.admit(box, self.path, self.data, self.args(), baseline):
                self.fail("mismatched proof was admitted")
        self.assertIsNone(native_runtime_guard.rejected(self.path))
        self.assertEqual(native_runtime_guard.load(self.path), baseline)

    def test_refused_candidate_does_not_disqualify_the_next_patch(self):
        baseline = self.seed()
        category = next(iter(native_preflight.CONFIRMED_REFUSALS))
        with (
            self.synthetic_admission(NEW),
            patch.object(
                native_preflight,
                "check",
                side_effect=native_preflight.Refusal(category, proof=DIGEST),
            ),
            self.assertRaisesRegex(box.BoxError, "not supported"),
        ):
            with guard.admit(box, self.path, self.data, self.args(), baseline):
                self.fail("unsupported release was admitted")
        self.assertEqual(native_runtime_guard.load(self.path), baseline)
        with (
            self.synthetic_admission("2.4.1"),
            patch.object(native_preflight, "check", return_value=DIGEST) as check,
        ):
            with guard.admit(box, self.path, self.data, self.args(), baseline) as scope:
                scope.begin(box, self.data)
                scope.ready(box, self.data)
            check.assert_called_once_with(box, "2.4.1")
        self.assertEqual(native_runtime_guard.load(self.path).version, "2.4.1")

    def test_write_floor_precedes_native_open_and_persists_without_readiness(self):
        previous = self.seed()
        with (
            self.synthetic_admission(),
            patch.object(native_preflight, "check", return_value=DIGEST),
        ):
            with guard.admit(box, self.path, self.data, self.args(), previous) as scope:
                self.assertEqual(native_runtime_guard.load(self.path), previous)
                scope.begin(box, self.data)
                self.assertEqual(native_runtime_guard.load(self.path), scope.record)
                self.assertFalse(scope.record.ready)
                self.assertEqual(scope.record.version, NEW)
        with (
            self.synthetic_admission(OLD),
            self.assertRaisesRegex(box.BoxError, "will not launch below"),
        ):
            with guard.admit(
                box,
                self.path,
                self.data,
                self.args(),
                native_runtime_guard.load(self.path),
            ):
                self.fail("an older binary passed the durable version floor")

    def test_stop_after_probe_cannot_publish_new_floor(self):
        previous = self.seed()
        with (
            self.synthetic_admission(),
            patch.object(native_preflight, "check", return_value=DIGEST),
        ):
            with guard.admit(box, self.path, self.data, self.args(), previous) as scope:
                workspace_control.request_stop(self.path)
                with self.assertRaisesRegex(box.BoxError, "Stop was requested"):
                    scope.begin(box, self.data)
        self.assertEqual(native_runtime_guard.load(self.path), previous)

    def test_explicit_stop_publishes_intent_before_reading_guard_state(self):
        baseline = self.seed()
        token = workspace_control.capture(self.path)

        def guarded_record(_box, path):
            self.assertEqual(path, self.path)
            self.assertFalse(workspace_control.unchanged(self.path, token))
            return baseline

        with (
            patch.object(box, "check_backend"),
            patch.object(box, "state_root", return_value=self.root),
            patch.object(box, "find_workspace", return_value=(self.path, self.data)),
            patch.object(guard, "load", side_effect=guarded_record),
            patch.object(box, "inspect_owned", return_value={"Id": CONTAINER}),
            patch.object(box, "stop_container") as stop,
        ):
            self.assertEqual(box.main(["stop", "--project", str(self.project)]), 0)
        stop.assert_called_once_with({**self.data, "container": CONTAINER})

    def test_live_config_copy_cannot_spawn_after_stop_is_published(self):
        baseline = self.seed()

        def config(*_args, **_kwargs):
            workspace_control.request_stop(self.path)
            return b"synthetic config copy payload"

        with (
            self.synthetic_admission(OLD),
            guard.admit(box, self.path, self.data, self.args(), baseline),
            patch.object(box, "ensure_opencode_version"),
            patch.object(box, "config_archive", side_effect=config),
            patch.object(
                box, "run"
            ),  # The earlier seed command must not run in this fixture.
            patch.object(subprocess, "Popen") as spawn,
            self.assertRaisesRegex(box.BoxError, "Stop was requested"),
        ):
            box.prepare_runtime(self.path, self.data, OLD)
        spawn.assert_not_called()

    def test_guarded_sync_does_not_open_the_unbound_history_source_lease(self):
        baseline = self.seed()
        args = box.parser().parse_args(["sync"])
        with (
            self.synthetic_admission(OLD),
            guard.admit(box, self.path, self.data, args, baseline) as scope,
            patch.object(guard.Admission, "start", return_value=None),
            patch.object(box, "check_private_output"),
            patch.object(box, "prepare_runtime"),
            patch.object(workspace_service, "history_source") as lease,
            patch.object(box, "sync_sessions") as sync,
            patch.object(box, "stop_container"),
        ):
            self.assertEqual(box.launch_environment(self.path, self.data, args), 0)
            self.assertTrue(scope.launching)
        lease.assert_not_called()
        sync.assert_called_once_with(self.path, self.data, announce=False)

    def test_guarded_owner_matches_authenticated_native_info_not_the_lease_frame(self):
        connection = {"url": "http://127.0.0.1:49152", "password": "synthetic-only"}
        with patch.object(
            workspace_service, "api", return_value={"version": NEW}
        ) as api:
            workspace_service.confirm_guarded_owner(
                box, self.data, connection, NEW, time.monotonic() + 30
            )
        api.assert_called_once_with(
            box, self.data, connection, "GET", "/api/info", timeout=10
        )
        with (
            patch.object(workspace_service, "api", return_value={"version": OLD}),
            self.assertRaisesRegex(box.BoxError, "readiness did not match"),
        ):
            workspace_service.confirm_guarded_owner(
                box, self.data, connection, NEW, time.monotonic() + 30
            )
        with (
            patch.object(workspace_service, "api", return_value={"version": NEW}),
            patch.object(workspace_service.time, "monotonic", side_effect=[40, 50]),
            self.assertRaisesRegex(box.BoxError, "readiness expired"),
        ):
            workspace_service.confirm_guarded_owner(box, self.data, connection, NEW, 50)

    def test_real_owner_signal_completes_receipt_and_warm_launch_skips_probe(self):
        with (
            self.synthetic_admission(),
            patch.object(native_preflight, "check", return_value=DIGEST) as check,
        ):
            with guard.admit(box, self.path, self.data, self.args(), None) as scope:
                scope.begin(box, self.data)
                scope.ready(box, self.data)
            confirmed = native_runtime_guard.load(self.path)
            self.assertTrue(confirmed.ready)
            with guard.admit(box, self.path, self.data, self.args(), confirmed) as warm:
                warm.begin(box, self.data)
                command = box.host_command(
                    {**self.data, "opencode_version": NEW}, "auth", "export"
                )
                self.assertEqual(command[0], "/pinned/native/opencode")
                with self.assertRaisesRegex(box.BoxError, "does not match"):
                    box.host_command(self.data, "auth", "export")
            check.assert_called_once_with(box, NEW)

    def test_no_mutating_alternate_route_on_version_upgrade(self):
        for command, options in (
            ("reset", {}),
            ("sync", {}),
            ("web", {}),
            ("purge", {"all": False, "delete_sessions": False}),
            ("terminal", {"maintenance": True}),
            ("start", {"import_session": "ses_synthetic"}),
        ):
            with (
                self.subTest(command=command, options=options),
                self.synthetic_admission(),
                patch.object(native_preflight, "check") as check,
                self.assertRaises(box.BoxError),
            ):
                with guard.admit(
                    box, self.path, self.data, self.args(command, **options), None
                ):
                    self.fail("unsupported transition route reached live preparation")
            check.assert_not_called()

    def test_pinned_descriptor_executes_and_does_not_resolve_path_again(self):
        # Use a system ELF and a shell builtin, independent of how a distro
        # dispatches external coreutils applets. Keep the executable's basename
        # and argv[0] aligned for a shell provided by a multicall binary.
        source = Path("/bin/sh")
        self.assertTrue(source.is_file())
        actual = self.root / "sh"
        with source.open("rb") as stream, actual.open("xb") as target:
            size = os.fstat(stream.fileno()).st_size
            self.assertLessEqual(size, min(8 * 1024 * 1024, guard._MAX_BINARY))
            self.assertEqual(stream.read(4), b"\x7fELF", "system sh must be an ELF")
            stream.seek(0)
            while size:
                block = stream.read(min(size, 1024 * 1024))
                self.assertTrue(block, "system sh changed during the fixture copy")
                target.write(block)
                size -= len(block)
            self.assertEqual(stream.read(1), b"")
        actual.chmod(0o700)
        alias = self.root / "opencode"
        alias.symlink_to(actual.name)
        with patch.object(guard.shutil, "which", return_value=str(alias)) as which:
            with guard.pinned_host(box) as selected:
                # Remove the lookup alias without changing the pinned inode.
                alias.unlink()
                command = selected.command(box)
                self.assertEqual(command, f"/proc/{os.getpid()}/fd/{selected.fd}")
                result = subprocess.run(
                    ["sh", "-c", "echo pinned"],
                    executable=command,
                    env={"PATH": str(self.root / "missing-bin")},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr[:512])
                self.assertEqual(result.stdout, "pinned\n")
            which.assert_called_once_with("opencode")

    def test_isolated_release_read_does_not_inherit_config_or_credentials(self):
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "synthetic-only",
                    "OPENCODE_CONFIG": "/synthetic/config",
                },
            ),
            patch.object(
                box, "run", return_value=subprocess.CompletedProcess([], 0, NEW + "\n")
            ) as run,
        ):
            self.assertEqual(box.detect_opencode_release(isolated=True), NEW)
        options = run.call_args.kwargs
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("OPENCODE_CONFIG", options["env"])
        self.assertEqual(options["cwd"], options["env"]["HOME"])
        self.assertNotEqual(options["cwd"], self.project)

    def test_in_container_bridge_exec_binds_verified_native_through_parent_fd(self):
        actual = Path(sys.executable).resolve(strict=True)
        prefix = self.root / "runtime"
        target = prefix / "lib/node_modules/@opencode/cli/bin/opencode.exe"
        target.parent.mkdir(parents=True)
        shutil.copyfile(actual, target)
        target.chmod(0o700)
        alias = prefix / "bin/opencode"
        alias.parent.mkdir()
        alias.symlink_to("../lib/node_modules/@opencode/cli/bin/opencode.exe")
        proof = hashlib.sha256(target.read_bytes()).hexdigest()
        bridge = "import subprocess,sys; print(subprocess.check_output([sys.argv[1], '--version'], text=True).strip())"
        marker = self.root / "shadow-executed"
        shadow = alias.with_name("python3")
        shadow.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 41\n")
        shadow.chmod(0o700)
        environment = {"PATH": str(alias.parent) + ":/usr/bin:/bin"}
        arguments = ["python3", "-c", bridge, str(alias)]
        with patch.object(guard, "PREFIX", str(prefix)):
            command = guard.bound_command(arguments, proof)
            wrong = guard.bound_command(arguments, "0" * 64)
        self.assertNotIn("python3", command)
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Python ", result.stdout)
        blocked = subprocess.run(
            wrong, env=environment, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(blocked.returncode, 126)
        self.assertEqual(blocked.stdout, "")
        self.assertFalse(marker.exists())

    def test_existing_guarded_owner_binds_noninteractive_native_helpers(self):
        digest = "d" * 64
        connection = {
            "url": "http://127.0.0.1:48231",
            "password": "fixture",
            "_runtime_container": "a" * 64,
            "_runtime_digest": digest,
        }
        source = (box.PACKAGE / "opencode_v2_io.py").read_text()
        command = box.workspace_service.client_command(
            box,
            self.data,
            connection,
            ["python3", "-c", source, "sessions", box.CONTAINER_OPENCODE],
        )
        self.assertNotIn("python3", command)
        self.assertEqual(command.count("/usr/bin/python3"), 3)
        self.assertIn(digest, command)
        self.assertTrue(
            any(
                "PATH" in item and "/opt/oc-box/runtime/bin" in item for item in command
            )
        )

    def test_live_install_is_scriptless_and_does_not_read_workspace_npm_config(self):
        prefix = str(self.root / "install-prefix")
        with (
            patch.object(sys, "argv", ["<guarded installer>", prefix, NEW]),
            patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as run,
            self.assertRaises(SystemExit) as completed,
        ):
            exec(guard._INSTALL_SOURCE, {})
        self.assertEqual(completed.exception.code, 0)
        command = run.call_args.args[0]
        self.assertIn("--ignore-scripts", command)
        self.assertIn(f"@opencode/cli@{NEW}", command)
        options = run.call_args.kwargs
        self.assertEqual(options["cwd"], "/")
        self.assertEqual(set(options["env"]), {"HOME", "PATH", "LANG"})
        self.assertEqual(options["env"]["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertIn("--userconfig=" + options["env"]["HOME"] + "/user.npmrc", command)
        self.assertIn("--globalconfig=/dev/null", command)
