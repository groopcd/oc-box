"""Authorization and destructive-operation preflight regressions (no engine)."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box


class LifecycleHardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-hardening-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "OC_BOX_CONTAINER": "0",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data.update(
            opencode_version="1.19.5",
            credentials={"mode": "env", "env": [], "vault": []},
        )
        box.atomic_json(self.path, self.data)

    def test_trusted_roots_refuse_both_overlap_directions_and_symlinks(self):
        application = self.root / "application"
        application.mkdir()
        roots = {
            "state": box.state_root(),
            "launcher": application,
            "default config": self.home / ".config/opencode",
            "default data": self.home / ".local/share/opencode",
            "effective config": self.root / "config/opencode",
            "effective data": self.root / "data/opencode",
            "vault": self.root / "vault-private",
        }
        with (
            patch.object(box, "APP", application),
            patch.object(
                box.vault, "vault_path", return_value=roots["vault"] / "vault"
            ),
        ):
            for name, trusted in roots.items():
                child = trusted / "child"
                child.mkdir(parents=True, exist_ok=True)
                alias = self.root / (name.replace(" ", "-") + "-alias")
                alias.symlink_to(child, target_is_directory=True)
                for kind, selected in (
                    ("equal", trusted),
                    ("descendant", child),
                    ("symlink", alias),
                ):
                    with self.subTest(root=name, overlap=kind):
                        with self.assertRaises(box.BoxError):
                            box.validate_project(selected)
                with self.subTest(root=name, overlap="ancestor"):
                    with self.assertRaises(box.BoxError):
                        box.validate_project(trusted.parent)
            self.assertEqual(box.validate_project(self.project), self.project)

    def test_trusted_descendant_refuses_launch_before_registration(self):
        selected = self.root / "config/opencode/plugins"
        selected.mkdir(parents=True)
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "create_workspace") as create,
            patch.object(box, "launch") as launch,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(box.BoxError),
        ):
            box.main(["start", "--project", str(selected)])
        create.assert_not_called()
        launch.assert_not_called()
        self.assertFalse((selected / box.MARKER).exists())

    def intervening_update(self, update):
        """Complete another writer under the real lock, before the next reader locks."""
        original_lock = box.workspace_lock

        @contextlib.contextmanager
        def lock(path):
            with original_lock(path):
                current = box.read_json(path)
                update(current)
                box.atomic_json(path, current)
            with original_lock(path):
                yield

        return lock

    def test_owner_reloads_restrictive_policy_and_unrelated_settings_under_lock(self):
        selected = {"mode": "selected", "env": [], "vault": []}

        def update(data):
            data.update(credentials=selected, memory="12g", devices=["/dev/synthetic"])

        original_lock = box.workspace_lock

        def launch(path, data, args):
            with self.assertRaises(box.BoxError):
                with original_lock(path):
                    self.fail("owner must retain its actual lock")
            self.assertEqual(box.credentials.effective_policy(data), selected)
            return 0

        with (
            patch.object(box, "workspace_lock", self.intervening_update(update)),
            patch.object(box, "check_backend"),
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "launch", side_effect=launch),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        saved = box.read_json(self.path)
        self.assertEqual(saved["credentials"], selected)
        self.assertEqual(saved["memory"], "12g")
        self.assertEqual(saved["devices"], ["/dev/synthetic"])

    def test_credentials_mutation_reloads_other_writer_policy_and_settings(self):
        def update(data):
            data.update(
                credentials={"mode": "selected", "env": ["FIRST_SECRET"], "vault": []},
                memory="12g",
            )

        with (
            patch.object(box, "workspace_lock", self.intervening_update(update)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.main(
                    [
                        "credentials",
                        "--workspace",
                        self.data["id"],
                        "--env-secret",
                        "SECOND_SECRET",
                    ]
                ),
                0,
            )
        saved = box.read_json(self.path)
        self.assertEqual(saved["credentials"]["mode"], "selected")
        self.assertEqual(saved["credentials"]["env"], ["FIRST_SECRET", "SECOND_SECRET"])
        self.assertEqual(saved["memory"], "12g")

    def test_reloaded_invalid_workspace_is_refused_before_policy_or_persistence(self):
        for field, value in (("id", "f" * 32), ("version", 1), ("project", None)):
            with self.subTest(field=field):
                box.atomic_json(self.path, self.data)
                with (
                    patch.object(
                        box,
                        "workspace_lock",
                        self.intervening_update(
                            lambda data: data.update({field: value})
                        ),
                    ),
                    patch.object(box, "check_backend"),
                    patch.object(box, "launch_credentials") as unlock,
                    patch.object(box, "launch") as launch,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(box.BoxError),
                ):
                    box.main(["start", "--workspace", self.data["id"]])
                unlock.assert_not_called()
                launch.assert_not_called()
                self.assertEqual(box.read_json(self.path)[field], value)

    def test_plain_purge_replacement_preserves_home_registry_and_archives(self):
        original = self.root / "original"
        self.project.rename(original)
        self.project.mkdir()
        replacement = self.project / "unrelated-work"
        replacement.write_text("must survive")
        archive = self.path.parent / "history/ses_recovery.json"
        box.atomic_json(archive, {"synthetic": "recovery"})
        before = {path: path.read_bytes() for path in (self.path, archive, replacement)}
        with (
            patch.object(box.dependencies, "require"),
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(
                box, "inspect_owned", return_value={"State": {"Running": False}}
            ),
            patch.object(box, "podman") as podman,
            patch.object(box, "sync_sessions") as sync,
            patch.object(box, "stop_container") as stop,
            patch.object(box, "remove_resources") as remove,
            self.assertRaises(box.BoxError),
        ):
            box.purge(self.path, self.data, box.parser().parse_args(["purge", "--yes"]))
        for mocked in (podman, sync, stop, remove):
            mocked.assert_not_called()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_v2_purge_preflights_every_id_and_child_inventory_before_any_deletion(self):
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        valid = {"ses_one": "ses_archive_one", "ses_two": "ses_archive_two"}
        cases = {
            "later child": (valid, {"data": [{"id": "ses_host_child"}], "cursor": {}}),
            "missing cursor": (valid, {"data": []}),
            "wrong cursor": (valid, {"data": [], "cursor": []}),
            "invalid continuation": (valid, {"data": [], "cursor": {"next": 1}}),
            "malformed child": (valid, {"data": [None], "cursor": {}}),
            "unfinished empty page": (valid, {"data": [], "cursor": {"next": "next"}}),
            "nonobject": (valid, []),
            "malformed JSON": (valid, "not-json"),
            "later invalid id": ({**valid, "ses_bad": "invalid/private?data"}, None),
            "later nonstring id": ({**valid, "ses_bad": {"private": "data"}}, None),
            "bad source id": ({**valid, "invalid source": "ses_archive_three"}, None),
        }
        for flag in ("--delete-sessions", "--all"):
            for name, (ids, response) in cases.items():
                with self.subTest(flag=flag, case=name):
                    box.atomic_json(box.session_file(self.path), {"host_sessions": ids})
                    calls = []

                    def native(command, **kwargs):
                        calls.append(command)
                        if "api" in command:
                            value = (
                                {"data": [], "cursor": {}}
                                if len(calls) == 1
                                else response
                            )
                            stdout = (
                                value if isinstance(value, str) else json.dumps(value)
                            )
                            return subprocess.CompletedProcess(command, 0, stdout, "")
                        self.fail("deletion must not precede the complete preflight")

                    with (
                        patch.object(box.dependencies, "require"),
                        patch.object(
                            box, "detect_opencode_release", return_value="2.2.6"
                        ),
                        patch.object(box, "run", side_effect=native),
                        patch.object(box, "stop_container") as stop,
                        patch.object(box, "remove_resources") as remove,
                        self.assertRaises(box.BoxError),
                    ):
                        box.purge(
                            self.path,
                            self.data,
                            box.parser().parse_args(["purge", "--yes", flag]),
                        )
                    stop.assert_not_called()
                    remove.assert_not_called()
                    self.assertTrue(self.path.exists())
                    self.assertTrue((self.project / box.MARKER).exists())
                    if name in (
                        "later invalid id",
                        "later nonstring id",
                        "bad source id",
                    ):
                        self.assertEqual(calls, [])

    def test_v2_purge_no_children_checks_whole_set_before_first_delete(self):
        for flag in ("--delete-sessions", "--all"):
            with self.subTest(flag=flag):
                project = self.root / flag.lstrip("-")
                project.mkdir()
                path, data = box.create_workspace(project, "tumbleweed")
                data["opencode_version"] = "2.2.6"
                box.atomic_json(path, data)
                ids = ["ses_archive_one", "ses_archive_two"]
                box.atomic_json(
                    box.session_file(path),
                    {"host_sessions": dict(zip(("ses_one", "ses_two"), ids))},
                )
                events = []

                def native(command, **kwargs):
                    if "api" in command:
                        events.append("check")
                        return subprocess.CompletedProcess(
                            command, 0, '{"data":[],"cursor":{}}', ""
                        )
                    self.assertIn("delete", command)
                    events.append("delete")
                    return subprocess.CompletedProcess(command, 0, "", "")

                with (
                    patch.object(box.dependencies, "require"),
                    patch.object(box, "detect_opencode_release", return_value="2.2.6"),
                    patch.object(box, "run", side_effect=native),
                    patch.object(box, "stop_container"),
                    patch.object(box, "remove_resources"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    box.purge(
                        path, data, box.parser().parse_args(["purge", "--yes", flag])
                    )
                self.assertEqual(events, ["check", "check", "delete", "delete"])
                self.assertFalse(path.exists())
                self.assertEqual(project.exists(), flag != "--all")

    def test_reset_uses_requested_limits_before_os_removal(self):
        controller_file = (
            self.root
            / "cgroup/user.slice/user-1000.slice/user@1000.service/cgroup.controllers"
        )
        controller_file.parent.mkdir(parents=True)
        controller_file.write_text("pids\n")
        checker = box.check_resource_controllers
        cases = (
            ("enable missing memory", "0", "0", ["--memory", "8g"], False),
            ("enable missing cpu", "0", "0", ["--cpus", "4"], False),
            (
                "remove unavailable old limits",
                "4",
                "8g",
                ["--cpus", "0", "--memory", "0"],
                True,
            ),
            ("invalid memory", "0", "0", ["--memory", "nonsense"], False),
            ("invalid cpu", "0", "0", ["--cpus", "nan"], False),
        )
        for name, cpus, memory, flags, allowed in cases:
            with self.subTest(case=name):
                data = copy.deepcopy(self.data)
                data.update(cpus=cpus, memory=memory)
                box.atomic_json(self.path, data)
                before = self.path.read_bytes()
                with (
                    patch.object(box, "check_backend"),
                    patch.object(box, "detect_opencode_release", return_value="1.19.5"),
                    patch.object(
                        box,
                        "check_resource_controllers",
                        side_effect=lambda candidate: checker(
                            candidate, cgroup_root=self.root / "cgroup", uid=1000
                        ),
                    ),
                    patch.object(box, "remove_resources") as remove,
                    patch.object(box, "launch", return_value=0) as launch,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    if allowed:
                        self.assertEqual(
                            box.main(["reset", "--workspace", data["id"], *flags]), 0
                        )
                    else:
                        with self.assertRaises(box.BoxError):
                            box.main(["reset", "--workspace", data["id"], *flags])
                if allowed:
                    remove.assert_called_once()
                    self.assertEqual(launch.call_args.args[1]["cpus"], "0")
                    self.assertEqual(launch.call_args.args[1]["memory"], "0")
                else:
                    remove.assert_not_called()
                    launch.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), before)
