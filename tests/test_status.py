import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, APP / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


box = load("oc_box")
status = load("status_report")


class StatusTests(unittest.TestCase):
    def setUp(self):
        modules = patch.dict(sys.modules, {"status_report": status})
        modules.start()
        self.addCleanup(modules.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-status-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        box.atomic_json(
            box.session_file(self.path),
            {
                "last": "ses_latest",
                "sessions": ["ses_latest"],
                "host_sessions": {"ses_latest": "ses_host_archive"},
            },
        )
        self.now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        self.info = {
            "Id": "c" * 64,
            "Config": {"Labels": {box.LABEL: self.data["id"]}},
            "State": {
                "Running": True,
                "Status": "running",
                "StartedAt": "2026-11-05T11:00:00Z",
                "ExitCode": 0,
                "OOMKilled": False,
            },
            "HostConfig": {
                "Memory": 8 * 1024**3,
                "NanoCpus": 4_000_000_000,
                "PidsLimit": 2048,
            },
            "SizeRw": 12 * 1024**2,
            "SizeRootFs": 512 * 1024**2,
            "Driver": "overlay",
            "Created": "2026-11-04T11:00:00Z",
            "RestartCount": 1,
        }
        self.volume = {
            "Labels": {box.LABEL: self.data["id"]},
            "Mountpoint": str(self.root / "private-home"),
        }
        self.calls = []

    def podman(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("container", "inspect"):
            return subprocess.CompletedProcess(args, 0, json.dumps([self.info]), "")
        if args[:2] == ("volume", "inspect"):
            return subprocess.CompletedProcess(args, 0, json.dumps([self.volume]), "")
        if args[0] == "stats":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "ContainerID": self.info["Id"],
                        "CPU": 23.5,
                        "MemUsage": 300 * 1024**2,
                        "MemLimit": 8 * 1024**3,
                        "MemPerc": 3.66,
                        "PIDs": 8,
                        "BlockInput": 1024,
                        "BlockOutput": 2048,
                    }
                )
                + "\n",
                "",
            )
        self.fail(f"Unexpected command (must never start/exec): {args}")

    def collect(self):
        with patch.object(status, "directory_bytes", return_value=(4096, None)):
            return status.collect_reports(
                [(self.path, self.data)],
                podman=self.podman,
                session_state=box.session_state,
            )

    def test_running_metrics_limits_storage_and_sessions(self):
        report = self.collect()
        item = report["workspaces"][0]
        self.assertEqual(item["state"], "running")
        self.assertEqual(item["metrics"]["cpu_percent"], 23.5)
        self.assertEqual(item["metrics"]["memory_bytes"], 300 * 1024**2)
        self.assertEqual(item["metrics"]["pids"], 8)
        self.assertEqual(item["limits"]["cpu_cores"], 4)
        self.assertEqual(item["storage"]["writable_layer_bytes"], 12 * 1024**2)
        self.assertEqual(item["storage"]["private_home_bytes"], 4096)
        self.assertEqual(item["storage"]["project_bytes"], 4096)
        self.assertEqual(item["session_id"], "ses_latest")
        self.assertEqual(item["host_archive_id"], "ses_host_archive")
        self.assertFalse(any("start" in call or "exec" in call for call in self.calls))
        command = next(call for call in self.calls if call[0] == "stats")
        self.assertIn("--no-stream", command)
        self.assertEqual(command[-1], self.info["Id"])

    def test_stopped_container_has_storage_but_no_live_sample(self):
        self.info["State"].update(
            Running=False, Status="exited", ExitCode=137, OOMKilled=True
        )
        report = self.collect()
        item = report["workspaces"][0]
        self.assertEqual(item["state"], "exited")
        self.assertIsNone(item["metrics"])
        self.assertIsNone(item["uptime_seconds"])
        self.assertEqual(item["exit_code"], 137)
        self.assertTrue(item["oom_killed"])
        self.assertEqual(item["storage"]["private_home_bytes"], 4096)
        self.assertFalse(any(call[0] == "stats" for call in self.calls))

    def test_owner_mismatch_does_not_measure_unrelated_volume(self):
        self.info["Config"]["Labels"][box.LABEL] = "someone-else"
        self.volume["Labels"][box.LABEL] = "someone-else"
        with patch.object(status, "directory_bytes", return_value=(4096, None)) as scan:
            report = status.collect_reports(
                [(self.path, self.data)],
                podman=self.podman,
                session_state=box.session_state,
            )
        item = report["workspaces"][0]
        self.assertEqual(item["state"], "unavailable")
        self.assertIsNone(item["storage"]["private_home_bytes"])
        self.assertIsNone(item["storage"]["writable_layer_bytes"])
        self.assertEqual(scan.call_count, 1)  # registered project only
        self.assertIn("ownership label mismatch", " ".join(item["notes"]))

    def test_unavailable_stats_are_not_reported_as_zero(self):
        real = self.podman

        def fake(*args, **kwargs):
            if args[0] == "stats":
                raise RuntimeError("stats unavailable")
            return real(*args, **kwargs)

        with patch.object(status, "directory_bytes", return_value=(4096, None)):
            report = status.collect_reports(
                [(self.path, self.data)], podman=fake, session_state=box.session_state
            )
        self.assertIsNone(report["workspaces"][0]["metrics"])
        self.assertIn(
            "Live metrics unavailable", " ".join(report["workspaces"][0]["notes"])
        )

    def test_layer_size_failure_does_not_hide_container_state_or_other_metrics(self):
        real = self.podman
        self.info.pop("SizeRw")
        self.info.pop("SizeRootFs")

        def fake(*args, **kwargs):
            if "--size" in args:
                return subprocess.CompletedProcess(args, 125, "", "cannot measure size")
            return real(*args, **kwargs)

        with patch.object(status, "directory_bytes", return_value=(4096, None)):
            report = status.collect_reports(
                [(self.path, self.data)], podman=fake, session_state=box.session_state
            )
        item = report["workspaces"][0]
        self.assertEqual(item["state"], "running")
        self.assertEqual(item["metrics"]["cpu_percent"], 23.5)
        self.assertIsNone(item["storage"]["writable_layer_bytes"])
        self.assertIn("size unavailable", " ".join(item["notes"]))

    def test_missing_resources_and_project_are_reported_without_starting(self):
        (self.project / box.MARKER).unlink()
        self.project.rmdir()

        def missing(*args, **kwargs):
            self.assertIn(args[1], ("inspect", "exists"))
            return subprocess.CompletedProcess(
                args, 1 if args[1] == "exists" else 125, "", "missing"
            )

        report = status.collect_reports(
            [(self.path, self.data)], podman=missing, session_state=box.session_state
        )
        item = report["workspaces"][0]
        self.assertEqual(item["state"], "missing")
        self.assertEqual(item["project_status"], "missing directory")
        self.assertEqual(item["storage"]["private_home_state"], "missing")

    def test_project_identity_change_is_not_scanned(self):
        self.data["project_identity"] = [0, 0]
        self.info["State"].update(Running=False, Status="exited")
        with patch.object(status, "directory_bytes", return_value=(4096, None)) as scan:
            report = status.collect_reports(
                [(self.path, self.data)],
                podman=self.podman,
                session_state=box.session_state,
            )
        self.assertEqual(report["workspaces"][0]["project_status"], "identity changed")
        self.assertTrue(all(call.kwargs.get("private") for call in scan.call_args_list))

    def test_missing_marker_and_duplicates_include_specific_recovery_notes(self):
        (self.project / box.MARKER).unlink()
        with patch.object(status, "directory_bytes", return_value=(4096, None)):
            missing = status.collect_reports(
                [(self.path, self.data)],
                podman=self.podman,
                session_state=box.session_state,
            )["workspaces"][0]
        self.assertEqual(missing["project_status"], "marker missing")
        self.assertIn(
            "repair --workspace " + self.data["id"], " ".join(missing["notes"])
        )
        other_path, other = box.create_workspace(self.project, "ubuntu")
        with patch.object(status, "directory_bytes", return_value=(4096, None)):
            reports = status.collect_reports(
                [(self.path, self.data), (other_path, other)],
                podman=self.podman,
                session_state=box.session_state,
            )["workspaces"]
        self.assertEqual(reports[0]["project_status"], "marker mismatch")
        self.assertEqual(reports[0]["project_marker_workspace"], other["id"])
        self.assertEqual(reports[0]["related_workspace_ids"], [other["id"]])
        self.assertIsNone(reports[0]["storage"]["project_bytes"])
        self.assertIn(
            "Do not use --all or --delete-sessions", " ".join(reports[0]["notes"])
        )
        self.assertEqual(reports[1]["project_status"], "present")
        self.assertIn(self.data["id"], reports[1]["related_workspace_ids"])

    def test_invalid_marker_types_do_not_crash_or_authorize_a_project_scan(self):
        marker = self.project / box.MARKER
        for kind in ("invalid id", "binary", "symlink", "directory"):
            with self.subTest(kind=kind):
                marker.unlink(missing_ok=True)
                if kind == "invalid id":
                    marker.write_text("not-a-workspace")
                elif kind == "binary":
                    marker.write_bytes(b"\xff\xfe")
                elif kind == "symlink":
                    marker.symlink_to(self.root / "nonexistent")
                else:
                    marker.mkdir()
                self.assertEqual(
                    status.project_condition(self.data), ("invalid marker", None)
                )
                if marker.is_dir() and not marker.is_symlink():
                    marker.rmdir()

    def test_text_report_distinguishes_storage_and_limits(self):
        report = self.collect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status.print_report(report)
        text = out.getvalue()
        for field in (
            "CPU",
            "RAM / LIMIT",
            "WRITABLE OS",
            "PRIVATE HOME",
            "PROJECT",
            "23.5%",
            "300.0 MiB",
            "8.0 GiB",
            "ses_latest",
            "Block I/O",
            "Storage driver: overlay",
        ):
            self.assertIn(field, text)

    def test_status_alias_and_selectors_are_read_only(self):
        for arguments in (
            ["--status"],
            ["status"],
            ["--status", "--workspace", self.data["id"]],
            ["--status", "--project", str(self.project)],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(box, "check_backend"),
                patch.object(box, "podman", side_effect=self.podman),
                patch.object(status, "directory_bytes", return_value=(4096, None)),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(box, "create_workspace") as create,
                patch.object(box, "launch") as launch,
            ):
                self.assertEqual(box.main(arguments), 0)
                create.assert_not_called()
                launch.assert_not_called()

    def test_json_report_is_clean_numeric_output(self):
        out = io.StringIO()
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "podman", side_effect=self.podman),
            patch.object(status, "directory_bytes", return_value=(4096, None)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["--status", "--json"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(
            report["workspaces"][0]["metrics"]["memory_bytes"], 300 * 1024**2
        )

    def test_default_reports_all_workspaces_and_explicit_selector_limits_scope(self):
        project = self.root / "second"
        project.mkdir()
        other_path, _ = box.create_workspace(project, "ubuntu")
        expected = (
            (["--status"], {self.path, other_path}),
            (["--status", "--workspace", self.data["id"]], {self.path}),
            (["--status", "--project", str(self.project)], {self.path}),
        )
        for arguments, paths in expected:
            with (
                self.subTest(arguments=arguments),
                patch.object(box, "check_backend"),
                patch.object(
                    status,
                    "collect_reports",
                    return_value={"sampled_at": "fixture", "workspaces": []},
                ) as collect,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(box.main(arguments), 0)
            self.assertEqual({path for path, _ in collect.call_args.args[0]}, paths)

    def test_empty_status_needs_no_podman_and_creates_no_registry(self):
        with (
            patch.dict(
                os.environ, {"OC_BOX_STATE_HOME": str(self.root / "never-created")}
            ),
            patch.object(box, "check_backend") as backend,
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["--status"]), 0)
        backend.assert_not_called()
        self.assertFalse((self.root / "never-created").exists())
        self.assertIn("No oc-box workspaces", out.getvalue())

    def test_status_rejects_mutating_options_before_backend(self):
        for arguments in (
            ["purge", "--status"],
            ["--status", "--new"],
            ["--status", "--distro", "ubuntu"],
            ["--status", "--all"],
            ["list", "--json"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(box, "check_backend") as backend,
            ):
                with self.assertRaises(box.BoxError):
                    box.main(arguments)
                backend.assert_not_called()

    def test_directory_scan_uses_no_shell_or_symlink_dereference(self):
        result = subprocess.CompletedProcess([], 0, "8192\tpath with spaces\n", "")
        with patch.object(status.subprocess, "run", return_value=result) as run:
            self.assertEqual(
                status.directory_bytes(self.project, private=True), (8192, None)
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["podman", "unshare", "du"])
        self.assertIn("--one-file-system", command)
        self.assertNotIn("-L", command)
        self.assertEqual(command[-1], str(self.project))
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_directory_scan_timeout_reports_unavailable_not_partial(self):
        with patch.object(
            status.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 15)
        ):
            size, error = status.directory_bytes(self.project)
        self.assertIsNone(size)
        self.assertIn("15s", error)

    def test_uptime_and_size_helpers(self):
        self.assertEqual(
            status.uptime("2026-11-05T11:00:00.123456789Z", self.now), 3599
        )
        self.assertIsNone(status.uptime("0001-01-01T00:00:00Z", self.now))
        self.assertIsNone(status.uptime("invalid", self.now))
        self.assertEqual(status.memory_limit("8g"), 8 * 1024**3)
        self.assertEqual(status.memory_limit("512MiB"), 512 * 1024**2)
        self.assertEqual(status.byte_size(0), "0 B")
        self.assertEqual(status.byte_size(None), "n/a")
        self.assertEqual(status.duration(90060), "1d 1h")


if __name__ == "__main__":
    unittest.main()
