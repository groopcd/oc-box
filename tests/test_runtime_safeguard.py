import argparse
import contextlib
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
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
        actual = Path(sys.executable).resolve(strict=True)
        if actual.read_bytes()[:4] != b"\x7fELF":
            self.skipTest("requires Linux ELF Python")
        with patch.object(guard.shutil, "which", return_value=str(actual)) as which:
            with guard.pinned_host(box) as selected:
                result = subprocess.run(
                    [selected.command(box), "-I", "-c", "print('pinned')"],
                    env={"PATH": "/non-existent"},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), "pinned")
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
        command = [
            sys.executable,
            "-I",
            "-c",
            native_preflight.EXEC_SOURCE,
            str(prefix),
            proof,
            sys.executable,
            "-I",
            "-c",
            bridge,
            str(alias),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Python ", result.stdout)
        command[5] = "0" * 64
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(blocked.returncode, 126)
        self.assertEqual(blocked.stdout, "")

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
        self.assertIn("--userconfig=" + options["env"]["HOME"] + "/user.npmrc", command)
        self.assertIn("--globalconfig=/dev/null", command)
