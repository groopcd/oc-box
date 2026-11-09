"""Multi-session discovery, archive isolation and source-deletion regressions."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, opencode_v2_io as v2, workspace_service


class SessionSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-session-sync-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "state")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)

    def session(self, ident, updated=1, parent=None):
        info = {
            "id": ident,
            "location": {"directory": str(self.project)},
            "time": {"created": 1, "updated": updated},
            "title": ident,
        }
        if parent:
            info["parentID"] = parent
        return info

    def result(self, value):
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    def test_inventory_pages_include_children_and_archived_sessions(self):
        root = self.session("ses_root")
        child = self.session("ses_child", parent="ses_root")
        child["time"]["archived"] = 2
        calls = []

        def request(method, target):
            calls.append((method, target))
            return (
                {"data": [root], "cursor": {"next": "next/+?="}}
                if len(calls) == 1
                else {"data": [child], "cursor": {"next": None}}
            )

        self.assertEqual(
            v2.session_inventory(request, str(self.project)), [root, child]
        )
        self.assertIn("cursor=next%2F%2B%3F%3D", calls[1][1])
        self.assertNotIn("parentID", calls[0][1])
        self.assertNotIn("archived=", calls[0][1])

    def test_inventory_rejects_bad_pages_duplicates_and_cursor_loops(self):
        entry = self.session("ses_root")
        pages = {
            "nonobject": [None],
            "missing cursor": [{"data": []}],
            "bad entry": [{"data": [None], "cursor": {}}],
            "foreign directory": [
                {"data": [self.session("ses_foreign")], "cursor": {}}
            ],
            "duplicate": [
                {"data": [entry], "cursor": {"next": "next"}},
                {"data": [entry], "cursor": {}},
            ],
            "cursor loop": [
                {"data": [], "cursor": {"next": "next"}},
                {"data": [], "cursor": {"next": "next"}},
            ],
            "bad cursor": [{"data": [], "cursor": {"next": 1}}],
        }
        pages["foreign directory"][0]["data"][0]["location"]["directory"] = "/foreign"
        for name, responses in pages.items():
            with self.subTest(name=name):
                iterator = iter(responses)
                with self.assertRaises(ValueError):
                    v2.session_inventory(lambda *_: next(iterator), str(self.project))

    def test_live_inventory_reuses_endpoint_through_private_stdin_only(self):
        connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:1234",
            "password": "synthetic-private-password",
        }
        token = workspace_service.CONNECTION.set(connection)
        try:
            with patch.object(
                box, "run", return_value=self.result([self.session("ses_root")])
            ) as run:
                values = box.list_container_sessions(self.data)
            self.assertEqual(values[0]["id"], "ses_root")
            call = run.call_args
            self.assertEqual(
                json.loads(call.kwargs["input"])["endpoint"],
                {key: connection[key] for key in ("url", "password")},
            )
            self.assertNotIn(connection["password"], " ".join(call.args[0]))
            self.assertTrue(call.kwargs["private"])
        finally:
            workspace_service.CONNECTION.reset(token)

    def test_refresh_reconciles_missing_sources_without_selecting_children(self):
        state = {
            "sessions": ["ses_deleted", "ses_root"],
            "last": "ses_deleted",
            "host_sessions": {"ses_deleted": "ses_archive_deleted"},
            "synced": {"ses_deleted": "retained-digest"},
        }
        box.atomic_json(box.session_file(self.path), state)
        listing = [
            self.session("ses_root", 2),
            self.session("ses_child", 100, "ses_root"),
        ]
        with patch.object(box, "run", return_value=self.result(listing)):
            refreshed = box.refresh_sessions(self.path, self.data)
        self.assertEqual(refreshed["sessions"], ["ses_root", "ses_child"])
        self.assertEqual(refreshed["last"], "ses_root")
        self.assertEqual(refreshed["missing"], ["ses_deleted"])
        self.assertEqual(refreshed["host_sessions"], state["host_sessions"])
        self.assertEqual(refreshed["synced"], state["synced"])

    def test_empty_inventory_clears_resume_and_rediscovery_clears_missing(self):
        box.atomic_json(
            box.session_file(self.path), {"sessions": ["ses_root"], "last": "ses_root"}
        )
        with patch.object(box, "run", return_value=self.result([])):
            state = box.refresh_sessions(self.path, self.data)
        self.assertEqual(state["sessions"], [])
        self.assertIsNone(state["last"])
        self.assertEqual(state["missing"], ["ses_root"])
        with patch.object(
            box, "run", return_value=self.result([self.session("ses_root")])
        ):
            state = box.refresh_sessions(self.path, self.data)
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["last"], "ses_root")

    def test_invalid_or_failed_inventory_does_not_change_resume_or_archive_state(self):
        box.atomic_json(
            box.session_file(self.path), {"sessions": ["ses_root"], "last": "ses_root"}
        )
        before = box.session_file(self.path).read_bytes()
        invalid = [
            None,
            {"data": []},
            [None],
            [self.session("invalid")],
            [self.session("ses_root"), self.session("ses_root")],
            [{**self.session("ses_root"), "location": None}],
            [{**self.session("ses_root"), "time": {}}],
        ]
        for value in invalid:
            with (
                self.subTest(value=value),
                patch.object(box, "run", return_value=self.result(value)),
            ):
                with self.assertRaises(box.BoxError):
                    box.sync_sessions(self.path, self.data)
                self.assertEqual(box.session_file(self.path).read_bytes(), before)
                self.assertFalse((self.path.parent / "history").exists())
        with patch.object(
            box, "run", side_effect=box.BoxError("synthetic transport failure")
        ):
            with self.assertRaises(box.BoxError):
                box.sync_sessions(self.path, self.data)
        self.assertEqual(box.session_file(self.path).read_bytes(), before)

    def test_deleted_source_keeps_last_archive_while_live_child_syncs_idempotently(
        self,
    ):
        dead_archive = self.path.parent / "history" / "ses_old_archive.json"
        box.atomic_json(dead_archive, {"retained": "last good transcript"})
        before = dead_archive.read_bytes()
        box.atomic_json(
            box.session_file(self.path),
            {
                "sessions": ["ses_deleted"],
                "last": "ses_deleted",
                "host_sessions": {"ses_deleted": "ses_old_archive"},
            },
        )
        infos = [self.session("ses_root"), self.session("ses_child", 2, "ses_root")]
        exports = {item["id"]: {"info": item, "messages": []} for item in infos}
        host, imports = {}, []

        def host_run(command, **_kwargs):
            if "import" in command:
                archived = box.read_json(Path(command[-2]))
                imports.append(archived["info"]["id"])
                archived["info"]["location"] = {
                    "directory": str(self.path.parent / "controller")
                }
                host[archived["info"]["id"]] = archived
            elif "export" in command:
                return self.result(host[command[-2]])
            elif "api" in command:
                return self.result({"data": [], "cursor": {}})
            elif "delete" in command:
                host.pop(command[-2])
            return self.result(None)

        with (
            patch.object(box, "list_container_sessions", return_value=infos),
            patch.object(
                box,
                "export_container_session",
                side_effect=lambda _, ident: copy.deepcopy(exports[ident]),
            ) as export,
            patch.object(box, "run", side_effect=host_run) as run,
            patch("sys.stdout", io.StringIO()),
        ):
            box.sync_sessions(self.path, self.data)
            self.assertEqual(len(imports), 2)
            box.sync_sessions(self.path, self.data)
            self.assertEqual(len(imports), 2)
            exports["ses_child"]["info"]["title"] = "Changed child"
            box.sync_sessions(self.path, self.data)
        self.assertEqual(dead_archive.read_bytes(), before)
        self.assertNotIn(
            "ses_deleted", [call.args[1] for call in export.call_args_list]
        )
        deletion = next(
            call.args[0] for call in run.call_args_list if "delete" in call.args[0]
        )
        self.assertEqual(
            deletion[-2], box.archive_id(self.data["id"] + "-v2", "ses_child", "ses")
        )
        archived = box.read_json(
            self.path.parent / "history" / (deletion[-2] + ".json")
        )
        self.assertNotIn("parentID", archived["info"])
        self.assertEqual(
            archived["info"]["metadata"]["oc-box.archive"]["parentID"],
            box.archive_id(self.data["id"] + "-v2", "ses_root", "ses"),
        )

    def test_v1_discovery_keeps_legacy_transport_and_resume_behavior(self):
        self.data["opencode_version"] = "1.19.5"
        box.atomic_json(self.path, self.data)
        listing = [{"id": "ses_root", "directory": str(self.project), "updated": 1}]
        with patch.object(box, "run", return_value=self.result(listing)) as run:
            state = box.refresh_sessions(self.path, self.data)
        self.assertEqual(state["last"], "ses_root")
        self.assertEqual(box.session_file(self.path).name, "sessions.json")
        self.assertIn("--pure", run.call_args.args[0])
        self.assertNotIn("sessions", run.call_args.args[0])

    def test_relationship_provenance_is_namespaced_without_reactivating_privileges(
        self,
    ):
        info = self.session("ses_child", parent="ses_root")
        info.update(
            {
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
                "fork": {
                    "sessionID": "ses_root",
                    "boundary": {"type": "through", "messageID": "msg_parent"},
                },
                "revert": {"messageID": "msg_child"},
                "metadata": {"user": {"sessionID": "ses_root"}},
            }
        )
        source = {
            "info": info,
            "messages": [
                {
                    "id": "msg_child",
                    "type": "user",
                    "text": "ses_root msg_parent",
                    "time": {"created": 1},
                }
            ],
        }
        before = copy.deepcopy(source)
        archived = box.archive_session(source, self.data)
        metadata = archived["info"]["metadata"]
        self.assertEqual(metadata["user"], info["metadata"]["user"])
        self.assertEqual(
            metadata["oc-box.archive"]["fork"]["boundary"]["messageID"],
            box.archive_id(self.data["id"] + "-v2", "msg_parent", "msg"),
        )
        for key in ("parentID", "fork", "permissions", "revert"):
            self.assertNotIn(key, archived["info"])
        self.assertEqual(archived["messages"][0]["text"], source["messages"][0]["text"])
        self.assertEqual(source, before)

    def test_existing_relationship_annotation_is_retained_as_source_metadata(self):
        source = {"info": self.session("ses_child", parent="ses_root"), "messages": []}
        source["info"]["metadata"] = {"oc-box.archive": {"user": "original value"}}
        archived = box.archive_session(source, self.data)
        annotation = archived["info"]["metadata"]["oc-box.archive"]
        self.assertEqual(annotation["version"], 1)
        self.assertEqual(annotation["sourceMetadata"], {"user": "original value"})

    def test_host_child_links_refuse_replacement_and_keep_digest_pending(self):
        ident = "ses_root"
        source = {"info": self.session(ident), "messages": []}
        state = {
            "sessions": [ident],
            "last": ident,
            "synced": {ident: "previous"},
            "host_sessions": {ident: "ses_archive"},
        }
        box.atomic_json(box.session_file(self.path), state)
        with (
            patch.object(box, "list_container_sessions", return_value=[source["info"]]),
            patch.object(box, "export_container_session", return_value=source),
            patch.object(
                box,
                "run",
                return_value=self.result(
                    {"data": [{"id": "ses_host_child"}], "cursor": {}}
                ),
            ) as run,
        ):
            with self.assertRaisesRegex(box.BoxError, "linked child sessions"):
                box.sync_sessions(self.path, self.data)
        self.assertFalse(
            any(
                "delete" in call.args[0] or "import" in call.args[0]
                for call in run.call_args_list
            )
        )
        self.assertEqual(box.session_state(self.path)["synced"], state["synced"])

    def test_native_import_conflict_noop_is_not_marked_as_synced(self):
        source = {"info": self.session("ses_root"), "messages": []}
        cases = {
            "title": {"title": "Unchanged host after native CLI conflict"},
            "permissions": {
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]
            },
            "fork": {
                "fork": {
                    "sessionID": "ses_other",
                    "boundary": {"type": "through", "messageID": "msg_other"},
                }
            },
            "revert": {"revert": {"messageID": "msg_other"}},
            "created": {"time": {"created": 2, "updated": 999}},
            "archived": {"time": {"created": 1, "updated": 999, "archived": 3}},
            "idle": {"time": {"created": 1, "updated": 999, "idle": 3}},
            "usage": {"cost": 12},
        }
        for name, changes in cases.items():
            wrong = box.archive_session(source, self.data)
            wrong["info"]["location"] = {
                "directory": str(self.path.parent / "controller")
            }
            wrong["info"].update(changes)
            with (
                self.subTest(field=name),
                patch.object(
                    box, "list_container_sessions", return_value=[source["info"]]
                ),
                patch.object(box, "export_container_session", return_value=source),
                patch.object(
                    box, "run", side_effect=[self.result(None), self.result(wrong)]
                ),
                self.assertRaisesRegex(box.BoxError, "verification failed"),
            ):
                box.sync_sessions(self.path, self.data)
            self.assertNotIn("ses_root", box.session_state(self.path).get("synced", {}))

    def test_host_archive_verification_accepts_only_native_timestamp_normalization(
        self,
    ):
        source = {"info": self.session("ses_root"), "messages": []}
        source["info"]["time"].update(busy=30, idle=20, viewed=40, archived=50)
        source["info"]["outcome"] = "succeeded"
        archived = box.archive_session(source, self.data)
        imported = copy.deepcopy(archived)
        imported["info"]["location"] = {
            "directory": str(self.path.parent / "controller")
        }
        imported["info"]["time"].update(updated=999, busy=20, viewed=20)
        with patch.object(box, "run", return_value=self.result(imported)):
            box.verify_host_archive(self.path, self.data, archived)
        imported["info"]["time"]["viewed"] = 40
        with (
            patch.object(box, "run", return_value=self.result(imported)),
            self.assertRaisesRegex(box.BoxError, "verification failed"),
        ):
            box.verify_host_archive(self.path, self.data, archived)

        zero_cases = {
            "zero creation/archive": {"created": 0, "archived": 0},
            "zero idle/outcome": {"idle": 0, "busy": 0, "viewed": 0},
            "zero busy/viewed": {"idle": 10, "busy": 0, "viewed": 0},
        }
        for name, changes in zero_cases.items():
            zero_source = {"info": self.session("ses_zero"), "messages": []}
            zero_source["info"]["time"].update(changes)
            if "idle" in changes:
                zero_source["info"]["outcome"] = "succeeded"
            zero_archive = box.archive_session(zero_source, self.data)
            zero_import = copy.deepcopy(zero_archive)
            zero_import["info"]["location"] = {
                "directory": str(self.path.parent / "controller")
            }
            zero_import["info"]["time"]["updated"] = 999
            with (
                self.subTest(case=name),
                patch.object(box, "run", return_value=self.result(zero_import)),
            ):
                box.verify_host_archive(self.path, self.data, zero_archive)

    def test_checked_export_pages_through_empty_tail_and_matches_projection(self):
        message = {
            "id": "msg_user",
            "type": "user",
            "text": "retained",
            "time": {"created": 1},
        }
        exported = {"info": self.session("ses_root"), "messages": [message]}
        responses = iter(
            [
                {"data": [message], "cursor": {"next": "last/+"}},
                {"data": [], "cursor": {}},
                {"data": exported},
            ]
        )
        calls = []

        def request(method, target):
            calls.append(target)
            return next(responses)

        self.assertEqual(v2.checked_export(request, "ses_root"), exported)
        self.assertIn("order=asc", calls[0])
        self.assertIn("cursor=last%2F%2B", calls[1])
        self.assertNotIn("order=", calls[1])

    def test_checked_export_refuses_unsettled_or_changing_history(self):
        messages = [
            {"id": "msg_assistant", "type": "assistant", "time": {"created": 1}},
            {"id": "msg_shell", "type": "shell", "status": "running"},
            {"id": "msg_compaction", "type": "compaction", "status": "running"},
        ]
        for message in messages:
            calls = []

            def request(method, target, calls=calls, message=message):
                calls.append(target)
                return {"data": [message], "cursor": {}}

            with (
                self.subTest(type=message["type"]),
                self.assertRaisesRegex(ValueError, "unsettled"),
            ):
                v2.checked_export(request, "ses_root")
            self.assertEqual(len(calls), 1)
        responses = iter(
            [
                {"data": [], "cursor": {}},
                {"data": {"info": self.session("ses_root"), "messages": messages}},
            ]
        )
        with self.assertRaisesRegex(ValueError, "changed or omitted"):
            v2.checked_export(lambda *_: next(responses), "ses_root")

    def test_checked_export_rejects_malformed_pages_and_repeated_message_ids(self):
        message = {
            "id": "msg_user",
            "type": "user",
            "text": "retained",
            "time": {"created": 1},
        }
        cases = {
            "missing cursor": [{"data": []}],
            "bad message": [{"data": [None], "cursor": {}}],
            "bad empty cursor": [{"data": [], "cursor": {"next": 1}}],
            "duplicate message": [
                {"data": [message], "cursor": {"next": "next"}},
                {"data": [message], "cursor": {}},
            ],
            "repeated cursor": [
                {"data": [message], "cursor": {"next": "next"}},
                {"data": [{**message, "id": "msg_second"}], "cursor": {"next": "next"}},
            ],
        }
        for name, pages in cases.items():
            with self.subTest(name=name):
                responses = iter(pages)
                with self.assertRaises(ValueError):
                    v2.checked_export(lambda *_: next(responses), "ses_root")


if __name__ == "__main__":
    unittest.main()
