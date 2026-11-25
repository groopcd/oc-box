import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import probe_legacy_start as probe


SOURCE = "oc-box-" + "a" * 32
SOURCE_ID = "b" * 64
SCRATCH_ID = "c" * 64
IMAGE = "d" * 64


class Fixture(probe.Probe):
    def __init__(self):
        super().__init__("podman", Path("/unused"), SOURCE)
        self.calls = []
        self.scratch = None
        self.fail_start = False
        self.extra_mount = False
        self.config_loaded = True
        self.source_info = {
            "Id": SOURCE_ID,
            "Name": SOURCE,
            "Image": "sha256:" + IMAGE,
            "OCIRuntime": "runc",
            "Mounts": [{"Source": "/private/source"}],
            "State": {
                "Status": "created",
                "Running": False,
                "Pid": 0,
                "Paused": False,
                "Restarting": False,
                "Error": "runc: rlimit type 6: operation not permitted",
            },
            "HostConfig": {"PidsLimit": 2048},
            "Config": {
                "Entrypoint": ["/bin/sh"],
                "Cmd": probe._CMD,
                "Labels": {probe._WORKSPACE: "a" * 32},
                "CreateCommand": ["podman", "create", "--ulimit=nproc=8192:8192"],
            },
        }

    def run(self, *args):
        self.calls.append(args)

        def result(status=0, stdout="", stderr=""):
            return subprocess.CompletedProcess(args, status, stdout, stderr)

        if args[0] == "info":
            return result(
                stdout=json.dumps(
                    {"host": {"serviceIsRemote": False, "security": {"rootless": True}}}
                )
            )
        if args[:2] in (("container", "inspect"), ("container", "exists")):
            item = self.source_info if args[2] == SOURCE else self.scratch
            if args[1] == "inspect":
                return result(0, json.dumps([item])) if item else result(125)
            return result(0 if item else 1)
        if args[:2] == ("--runtime=runc", "create"):
            self.scratch = {
                "Id": SCRATCH_ID,
                "Name": self.name,
                "Image": IMAGE,
                "OCIRuntime": "runc",
                "State": {**self.source_info["State"], "Error": ""},
                "Mounts": [{"Source": "/unexpected"}] if self.extra_mount else [],
                "Config": {
                    "Entrypoint": ["/bin/sh"],
                    "Cmd": probe._CMD,
                    "User": "0:0",
                    "Annotations": {probe._CONFIG_MARK: self.token}
                    if self.config_loaded
                    else {},
                    "Labels": {
                        probe._LABEL: self.token,
                        "io.oc-box.managed": "false",
                        probe._WORKSPACE: "",
                    },
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "Privileged": False,
                    "PidMode": "private",
                    "PidsLimit": 2048,
                    "RestartPolicy": {"Name": "no"},
                },
            }
            return result(stdout=SCRATCH_ID)
        if args == ("start", SCRATCH_ID):
            if self.fail_start:
                return result(125, stderr="runc: synthetic later OCI error\n")
            self.scratch["State"].update(Status="running", Running=True, Pid=42)
            return result()
        if args == ("stop", "--time", "3", SCRATCH_ID):
            self.scratch["State"].update(Status="exited", Running=False, Pid=0)
            return result()
        if args == ("container", "rm", SCRATCH_ID):
            self.scratch = None
            return result()
        raise AssertionError(args)


class ProbeTests(unittest.TestCase):
    def test_only_disposable_image_boots_without_replayed_limits(self):
        engine = Fixture()
        original = copy.deepcopy(engine.source_info)
        engine.boot()
        self.assertTrue(engine.cleanup())
        create = next(
            args for args in engine.calls if args[:2] == ("--runtime=runc", "create")
        )
        self.assertFalse(
            any(arg.startswith(("--ulimit", "--volume", "--mount")) for arg in create)
        )
        self.assertIn("--pids-limit=2048", create)
        self.assertIn("--pull=never", create)
        self.assertEqual(create[-3:], (IMAGE, *probe._CMD))
        self.assertEqual(engine.source_info, original)
        self.assertEqual(
            [args for args in engine.calls if SOURCE in args],
            [("container", "inspect", SOURCE)],
        )
        self.assertNotIn(SOURCE_ID, [arg for args in engine.calls for arg in args])

    def test_reports_next_failure_and_cleans_only_verified_probe(self):
        engine = Fixture()
        engine.fail_start = True
        with self.assertRaisesRegex(probe.Refusal, "synthetic later OCI error"):
            engine.boot()
        self.assertTrue(engine.cleanup())
        self.assertIn(("container", "rm", SCRATCH_ID), engine.calls)

    def test_unexpected_mount_refuses_before_start(self):
        engine = Fixture()
        engine.extra_mount = True
        with self.assertRaisesRegex(probe.Refusal, "disposable, unmounted"):
            engine.boot()
        with self.assertRaises(probe.Refusal):
            engine.cleanup()
        self.assertNotIn(("start", SCRATCH_ID), engine.calls)
        self.assertNotIn(("container", "rm", SCRATCH_ID), engine.calls)

    def test_device_defaults_are_replaced_and_cdi_path_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostic = probe.Probe("podman", root, SOURCE)
            diagnostic.write_config()
            self.assertIn(f"--cdi-spec-dir={root / 'cdi'}", diagnostic.command)
            self.assertEqual(
                diagnostic.env["CONTAINERS_CONF_OVERRIDE"], str(diagnostic.override)
            )
            config = diagnostic.override.read_text()
            self.assertIn("devices = [{append = false}]", config)
            self.assertIn("volumes = [{append = false}]", config)
            self.assertIn(f"{probe._CONFIG_MARK}={diagnostic.token}", config)

    def test_missing_override_receipt_refuses_before_start(self):
        engine = Fixture()
        engine.config_loaded = False
        with self.assertRaises(probe.Refusal):
            engine.boot()
        self.assertNotIn(("start", SCRATCH_ID), engine.calls)

    def test_out_of_scope_original_is_only_inspected(self):
        engine = Fixture()
        engine.source_info["State"]["Error"] = "different OCI refusal"
        with self.assertRaisesRegex(
            probe.Refusal, "not the expected stopped nproc failure"
        ):
            engine.boot()
        self.assertTrue(engine.cleanup())
        self.assertEqual(
            engine.calls, [("info", "--format=json"), ("container", "inspect", SOURCE)]
        )
