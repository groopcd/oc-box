"""Compose native tab policy with launcher and existing-owner client routing."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import native_tabs, oc_box as box, workspace_service as service


class NativeTabRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-tab-routing-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {"OC_BOX_STATE_HOME": str(root / "state"), "XDG_RUNTIME_DIR": str(root)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        project = root / "project"
        project.mkdir()
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data["opencode_version"] = "2.3.0"
        box.atomic_json(self.path, self.data)
        self.connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
        }
        self.inventory = [
            {
                "id": "ses_" + name,
                "location": {"directory": str(project)},
                "time": {"updated": updated},
            }
            for updated, name in enumerate(("a", "b", "c"), 1)
        ]
        self.inventory += [
            {
                "id": "ses_archived",
                "location": {"directory": str(project)},
                "time": {"updated": 4, "archived": 5},
            },
            {
                "id": "ses_child",
                "parentID": "ses_a",
                "location": {"directory": str(project)},
                "time": {"updated": 6},
            },
        ]

    def test_startup_uses_open_membership_without_filtering_synchronized_history(self):
        missing = native_tabs.Snapshot("missing", reason="absent")
        cases = {
            "newest-closed": (
                native_tabs.Snapshot("valid", ("ses_a", "ses_b")),
                [],
                "ses_a",
            ),
            "all-closed": (native_tabs.Snapshot("valid"), [], None),
            "ineligible-open-tabs": (
                native_tabs.Snapshot(
                    "valid", ("ses_archived", "ses_child", "ses_foreign", "ses_missing")
                ),
                [],
                None,
            ),
            "invalid-snapshot": (
                native_tabs.Snapshot("invalid", reason="invalid-state"),
                [],
                None,
            ),
            "first-client": (missing, [], "ses_c"),
            "explicit-resume": (native_tabs.Snapshot("valid"), ["--resume"], "ses_c"),
            "explicit-model": (
                native_tabs.Snapshot("valid"),
                ["--model", "fixture/model"],
                "ses_c",
            ),
            "explicit-new": (native_tabs.Snapshot("valid"), ["--new"], "ses_new"),
            "explicit-import": (native_tabs.Snapshot("valid"), [], "ses_c"),
        }
        for name, (snapshot, options, expected) in cases.items():
            with self.subTest(case=name):
                args = box.parser().parse_args(["start", *options])
                state = {
                    "last": "ses_c",
                    "sessions": [info["id"] for info in self.inventory],
                }
                before = copy.deepcopy(state)
                owner = SimpleNamespace(
                    connection=self.connection, publish=Mock(), wait_clients=Mock()
                )
                requests = []

                def api(_box, _data, connection, method, target, value=None, **_kwargs):
                    self.assertIs(connection, self.connection)
                    requests.append((method, target))
                    return {"data": {"id": "ses_new"}}

                def client(_box, _data, connection, arguments, **_kwargs):
                    self.assertIs(connection, self.connection)
                    return box.ContainerCommand(arguments, {})

                imported = {} if name == "explicit-import" else None
                with (
                    patch.object(box, "refresh_sessions", return_value=state),
                    patch.object(box, "import_host_session", return_value=state),
                    patch.object(
                        native_tabs, "inspect", return_value=snapshot
                    ) as inspect,
                    patch.object(
                        box, "list_container_sessions", return_value=self.inventory
                    ),
                    patch.object(service, "api", side_effect=api),
                    patch.object(service, "client_command", side_effect=client),
                    patch.object(box, "run_tui", return_value=0) as tui,
                    patch.object(service, "wait_history"),
                    patch.object(box, "sync_sessions") as sync,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        box.launch_client(
                            self.path, self.data, args, "2.3.0", imported, owner
                        ),
                        0,
                    )
                owner.publish.assert_called_once_with(expected)
                command = tui.call_args.args[0]
                self.assertIn(self.connection["url"], command)
                self.assertNotIn("--standalone", command)
                if expected is None:
                    self.assertNotIn("--session", command)
                else:
                    self.assertEqual(command[command.index("--session") + 1], expected)
                self.assertEqual(
                    ("POST", "/api/session") in requests, name == "explicit-new"
                )
                self.assertEqual(
                    inspect.call_count,
                    int(not options and name != "explicit-import"),
                )
                sync.assert_called_once_with(self.path, self.data)
                self.assertEqual(state, before)

    def test_controller_rechecks_membership_in_its_own_bound_thread_context(self):
        cases = (
            (native_tabs.Snapshot("valid", ("ses_a", "ses_b")), "ses_c", "ses_a"),
            (native_tabs.Snapshot("valid"), "ses_c", None),
            (native_tabs.Snapshot("missing", reason="absent"), "ses_c", "ses_c"),
            (native_tabs.Snapshot("missing", reason="absent"), None, None),
            (native_tabs.Snapshot("invalid", reason="transport"), "ses_c", None),
            (native_tabs.Snapshot("valid", ("ses_archived",)), "ses_c", None),
        )
        for snapshot, current, expected in cases:
            with self.subTest(snapshot=snapshot, current=current):
                owner = service.Owner(box, self.path, self.data)
                owner.connection = self.connection
                owner.publish(current)
                observed, results, errors = [], [], []

                def inspect(_box, _data, *, connection):
                    self.assertIs(connection, self.connection)
                    self.assertEqual(
                        service.live_connection()["workspace"],
                        "unrelated-thread-context",
                    )
                    observed.append("inspect")
                    return snapshot

                def catalog(_box, _data, connection):
                    self.assertIs(connection, self.connection)
                    self.assertEqual(
                        service.live_connection()["workspace"],
                        "unrelated-thread-context",
                    )
                    observed.append("catalog")
                    return self.inventory

                def worker():
                    previous = {"workspace": "unrelated-thread-context"}
                    token = service.CONNECTION.set(previous)
                    try:
                        results.append(owner.default_session())
                        self.assertIs(service.live_connection(), previous)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        service.CONNECTION.reset(token)

                with (
                    patch.object(native_tabs, "inspect", side_effect=inspect),
                    patch.object(service, "tab_inventory", side_effect=catalog),
                ):
                    thread = threading.Thread(target=worker)
                    thread.start()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [expected])
                self.assertEqual(observed[0], "inspect")
                self.assertEqual(
                    "catalog" in observed,
                    snapshot.status != "invalid"
                    and not (snapshot.status == "valid" and not snapshot.tabs),
                )
                self.assertIsNone(service.live_connection())

    def test_controller_catalog_never_uses_credential_forwarding_owner_execution(self):
        owner = service.Owner(box, self.path, self.data)
        owner.connection = self.connection
        owner.publish("ses_c")
        observed = []

        def metadata(command, **settings):
            self.assertTrue(settings["private"])
            self.assertTrue(settings["capture"])
            self.assertNotIn("synthetic-provider", " ".join(command))
            self.assertNotIn(self.connection["password"], " ".join(command))
            self.assertNotIn("OPENAI_API_KEY", command.environment)
            self.assertNotIn("OPENCODE_AUTH_CONTENT", command.environment)
            request = json.loads(settings["input"])
            self.assertEqual(request["directory"], self.data["project"])
            self.assertEqual(
                request["endpoint"],
                {key: self.connection[key] for key in ("url", "password")},
            )
            tail = command[command.index(self.data["container"]) + 1 :]
            self.assertEqual(tail[:4], ["python3", "-I", "-S", "-c"])
            self.assertIn("sessions", tail)
            observed.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.inventory))

        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "synthetic-provider",
                    "OPENCODE_AUTH_CONTENT": "synthetic-auth",
                },
            ),
            patch.object(
                native_tabs,
                "inspect",
                return_value=native_tabs.Snapshot("valid", ("ses_a",)),
            ),
            patch.object(
                box,
                "exec_command",
                side_effect=AssertionError("owner environment must not be used"),
            ),
            patch.object(
                box,
                "list_container_sessions",
                side_effect=AssertionError("owner history transport must not be used"),
            ),
            patch.object(box, "run", side_effect=metadata),
        ):
            self.assertEqual(owner.default_session(), "ses_a")
        self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
