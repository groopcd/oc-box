import copy
import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from oc_box import tui_sessions as sessions


def session(ident="ses_root", directory="/project", **extra):
    return {
        "id": ident,
        "projectID": "project_synthetic",
        "location": {"directory": directory},
        "time": {"created": 1, "updated": 2},
        "title": "Synthetic session",
        **extra,
    }


def page(values=(), cursor=None):
    return {"data": list(values), "cursor": {"next": cursor, "previous": None}}


class NativeFixture:
    """In-memory native API only; no service, database, transcripts or network."""

    def __init__(self, *values):
        self.values = {item["id"]: copy.deepcopy(item) for item in values}
        self.calls = []
        self.active = {}
        self.jobs = []
        self.terminals = {}
        self.hook = None

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.hook is not None:
            self.hook(method, path, body)
        if path == "/api/session/active":
            return {"data": copy.deepcopy(self.active)}
        if path == "/api/experimental/job":
            return {"data": copy.deepcopy(self.jobs)}
        if path.startswith("/api/experimental/session/") and path.endswith("/terminal"):
            ident = path.split("/")[-2]
            return {"data": copy.deepcopy(self.terminals.get(ident, []))}
        parsed = urlsplit(path)
        if parsed.path == "/api/session" and method == "GET":
            query = parse_qs(parsed.query)
            assert "parentID" not in query and "archived" not in query
            values = [
                copy.deepcopy(item)
                for item in self.values.values()
                if "directory" not in query
                or item["location"]["directory"] == query["directory"][0]
            ]
            return page(values)
        ident = unquote(parsed.path.removeprefix("/api/session/"))
        if method == "GET":
            return {"data": copy.deepcopy(self.values[ident])}
        if method == "PATCH":
            assert set(body) == {"archived"}
            if body["archived"]:
                self.values[ident]["time"]["archived"] = 3
            else:
                self.values[ident]["time"].pop("archived", None)
            return None
        if method == "DELETE":
            descendants = sessions._descendants(list(self.values.values()), ident)
            for item in descendants:
                del self.values[item["id"]]
            return None
        raise AssertionError("Unexpected fixture request")

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


class CatalogTests(unittest.TestCase):
    def catalog(self, fixture, **kwargs):
        return sessions.SessionCatalog(
            fixture,
            "workspace",
            "synthetic-store",
            ownership=kwargs.pop("ownership", lambda *_: "exclusive"),
            **kwargs,
        )

    def assert_refused(self, reason, action):
        with self.assertRaises(sessions.CatalogError) as raised:
            action()
        self.assertEqual(raised.exception.reason, reason)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_inventory_all_archives_children_and_opaque_empty_short_pages(self):
        opaque = "opaque+/=?& #\u00e9"
        child = session(
            "ses_child",
            "/project & \u00e9",
            parentID="ses_root",
            time={"created": 1, "updated": 2, "archived": 3},
        )
        request = Mock(
            side_effect=[
                page([session(directory="/project & \u00e9")], opaque),
                page([], "last"),
                page([child]),
            ]
        )
        result = sessions.list_sessions(request, directory="/project & \u00e9")
        self.assertEqual([item["id"] for item in result], ["ses_root", "ses_child"])
        for call in request.call_args_list:
            query = parse_qs(urlsplit(call.args[1]).query)
            self.assertEqual(query["directory"], ["/project & \u00e9"])
            self.assertNotIn("archived", query)
            self.assertNotIn("parentID", query)
        self.assertEqual(
            parse_qs(urlsplit(request.call_args_list[1].args[1]).query)["cursor"],
            [opaque],
        )

    def test_inventory_validation_matrix_never_returns_partial(self):
        bad_values = [
            (None, "invalid-inventory"),
            ({"data": [], "cursor": {}}, "invalid-inventory"),
            ({"data": {}, "cursor": {"next": None}}, "invalid-inventory"),
            (page([None]), "invalid-session"),
            (page([session(id="wrong")]), "invalid-session"),
            (page([session(location={"directory": "relative"})]), "invalid-session"),
            (page([session(projectID="")]), "invalid-session"),
            (page([session(time={"created": True, "updated": 1})]), "invalid-session"),
            (
                page([session(time={"created": 1, "updated": float("nan")})]),
                "invalid-session",
            ),
            (
                page([session(time={"created": 1, "updated": 10**1000})]),
                "invalid-session",
            ),
            (page([session(parentID="ses_root")]), "invalid-session"),
            (page([], ""), "invalid-cursor"),
            (page([], 4), "invalid-cursor"),
            (page([], "\ud800"), "invalid-cursor"),
        ]
        for bad, reason in bad_values:
            with self.subTest(reason=reason, bad=bad):
                request = Mock(side_effect=[page([session("ses_valid")], "next"), bad])
                self.assert_refused(reason, lambda: sessions.list_sessions(request))

    def test_duplicates_repeated_cursor_directory_and_limits(self):
        for pages, reason in [
            ([page([session()], "next"), page([session()])], "duplicate-session"),
            ([page([], "next"), page([], "next")], "invalid-cursor"),
        ]:
            with self.subTest(reason=reason):
                self.assert_refused(
                    reason, lambda: sessions.list_sessions(Mock(side_effect=pages))
                )
        self.assert_refused(
            "inconsistent-directory",
            lambda: sessions.list_sessions(
                Mock(return_value=page([session()])), directory="/other"
            ),
        )
        with patch.object(sessions, "MAX_PAGES", 2):
            self.assert_refused(
                "inventory-limit",
                lambda: sessions.list_sessions(
                    Mock(side_effect=[page([], "a"), page([], "b")])
                ),
            )
        with patch.object(sessions, "MAX_SESSIONS", 1):
            self.assert_refused(
                "inventory-limit",
                lambda: sessions.list_sessions(
                    Mock(return_value=page([session(), session("ses_child")]))
                ),
            )
        with patch.object(sessions, "MAX_CURSOR_BYTES", 2):
            self.assert_refused(
                "invalid-cursor",
                lambda: sessions.list_sessions(Mock(return_value=page([], "long"))),
            )

    def test_cancel_before_or_during_page_and_fixed_native_error(self):
        request = Mock(return_value=page())
        self.assert_refused(
            "cancelled", lambda: sessions.list_sessions(request, cancelled=lambda: True)
        )
        request.assert_not_called()
        self.assert_refused(
            "cancelled",
            lambda: sessions.list_sessions(
                request, cancelled=Mock(side_effect=[False, True])
            ),
        )
        self.assert_refused(
            "request-failed",
            lambda: sessions.list_sessions(
                Mock(side_effect=RuntimeError("PRIVATE native error"))
            ),
        )

    def test_snapshot_whitelist_and_independent_copies(self):
        value = session(
            permissions=[{"PRIVATE": "allow"}],
            metadata={"PRIVATE": "secret"},
            model={"PRIVATE": "secret"},
            cost=9,
            tokens={},
        )
        fixture = NativeFixture(value)
        catalog = self.catalog(fixture)
        listed = catalog.list()
        self.assertEqual(
            set(listed[0]), {"id", "projectID", "location", "time", "title"}
        )
        self.assertNotIn("PRIVATE", json.dumps(listed))
        listed[0]["title"] = "UI change"
        self.assertEqual(catalog._snapshots["ses_root"]["title"], "Synthetic session")
        fixture.values["ses_root"]["time"]["updated"] = 10
        self.assert_refused(
            "snapshot-changed", lambda: catalog.archive("ses_root", True)
        )
        self.assertEqual(fixture.mutations(), [])

    def test_failed_refresh_retains_last_complete_snapshot(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture)
        catalog.list()
        self.assert_refused("cancelled", lambda: catalog.list(cancelled=lambda: True))
        self.assertIsNone(catalog.archive("ses_root", True))

    def test_archive_and_unarchive_patch_exact_boolean_support_204(self):
        fixture = NativeFixture(session(), session("ses_mirror"))
        catalog = self.catalog(fixture)
        for archived in (True, False):
            catalog.list()
            self.assertIsNone(catalog.archive("ses_root", archived))
        self.assertEqual(
            fixture.mutations(),
            [
                ("PATCH", "/api/session/ses_root", {"archived": True}),
                ("PATCH", "/api/session/ses_root", {"archived": False}),
            ],
        )
        self.assertNotIn("archived", fixture.values["ses_mirror"]["time"])
        self.assert_refused(
            "snapshot-required", lambda: catalog.archive("ses_root", True)
        )
        self.assert_refused(
            "invalid-archive-state", lambda: catalog.archive("ses_root", 1)
        )

    def test_unknown_ownership_default_and_probe_failure_refuse(self):
        for ownership in (
            None,
            lambda *_: None,
            lambda *_: "other-process",
            Mock(side_effect=RuntimeError("PRIVATE")),
        ):
            with self.subTest(ownership=ownership):
                fixture = NativeFixture(session())
                catalog = self.catalog(fixture, ownership=ownership)
                catalog.list()
                self.assert_refused(
                    "ownership-unknown", lambda: catalog.archive("ses_root", True)
                )
                self.assert_refused(
                    "ownership-unknown", lambda: catalog.prepare_delete("ses_root")
                )
                self.assertEqual(fixture.mutations(), [])

    def test_foreground_and_all_unconsumed_job_kinds_statuses_refuse_no_abort(self):
        fixtures = []
        active = NativeFixture(session())
        active.active = {"ses_other": {"type": "running"}}
        fixtures.append(active)
        for kind in ("shell", "subagent", "image"):
            for status in ("running", "completed", "error", "cancelled"):
                fixture = NativeFixture(session())
                fixture.jobs = [
                    {
                        "id": "job_fixture",
                        "sessionID": "ses_other",
                        "kind": kind,
                        "status": status,
                        "command": "PRIVATE",
                    }
                ]
                fixtures.append(fixture)
        for fixture in fixtures:
            with self.subTest(active=bool(fixture.active), jobs=fixture.jobs):
                catalog = self.catalog(fixture)
                catalog.list()
                self.assert_refused(
                    "session-active", lambda: catalog.archive("ses_root", True)
                )
                self.assert_refused(
                    "session-active", lambda: catalog.prepare_delete("ses_root")
                )
                self.assertEqual(fixture.mutations(), [])

    def test_unsupported_or_malformed_activity_refuses(self):
        for target, result, reason in [
            ("/api/session/active", None, "invalid-activity"),
            (
                "/api/session/active",
                {"data": {"ses_root": {"type": "idle"}}},
                "invalid-activity",
            ),
            ("/api/experimental/job", {"data": [{"id": "PRIVATE"}]}, "invalid-jobs"),
        ]:
            with self.subTest(target=target):
                fixture = NativeFixture(session())

                def request(method, path, body=None):
                    return result if path == target else fixture(method, path, body)

                catalog = self.catalog(request)
                self.assert_refused(reason, lambda: catalog.prepare_delete("ses_root"))
                self.assertEqual(fixture.mutations(), [])

    def test_delete_preview_all_directories_and_recursive_selected_copy_only(self):
        fixture = NativeFixture(
            session(),
            session("ses_child", "/other", parentID="ses_root"),
            session("ses_grandchild", "/third", parentID="ses_child"),
            session("ses_mirror", "/mirror"),
        )
        catalog = self.catalog(fixture)
        catalog.list(directory="/project")
        preview = catalog.prepare_delete("ses_root")
        self.assertEqual(preview["descendant_count"], 2)
        self.assertEqual(
            [item["location"]["directory"] for item in preview["sessions"]],
            ["/project", "/other", "/third"],
        )
        self.assertEqual(fixture.mutations(), [])
        self.assertIsNone(catalog.delete(preview))
        self.assertEqual(set(fixture.values), {"ses_mirror"})
        self.assertEqual(
            fixture.mutations(), [("DELETE", "/api/session/ses_root", None)]
        )
        self.assert_refused("invalid-preview", lambda: catalog.delete(preview))

    def test_delete_revalidation_change_matrix(self):
        changes = {
            "new-child": lambda f: f.values.update(
                {"ses_new": session("ses_new", "/other", parentID="ses_root")}
            ),
            "changed-title": lambda f: f.values["ses_child"].update(title="Changed"),
            "moved-child": lambda f: f.values["ses_child"].update(
                location={"directory": "/moved"}
            ),
            "removed-child": lambda f: f.values.pop("ses_child"),
            "reparented-child": lambda f: f.values["ses_child"].update(
                parentID="ses_other"
            ),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                fixture = NativeFixture(
                    session(), session("ses_child", parentID="ses_root")
                )
                catalog = self.catalog(fixture)
                preview = catalog.prepare_delete("ses_root")
                change(fixture)
                self.assert_refused("snapshot-changed", lambda: catalog.delete(preview))
                self.assertEqual(fixture.mutations(), [])

    def test_mutation_after_inventory_identity_refuses(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture)

        def hook(method, path, body):
            if path == "/api/session/ses_root":
                fixture.values["ses_root"]["time"]["updated"] += 1

        fixture.hook = hook
        self.assert_refused(
            "snapshot-changed", lambda: catalog.prepare_delete("ses_root")
        )
        self.assertEqual(fixture.mutations(), [])

    def test_identity_changed_between_inventory_and_direct_get_refuses(self):
        fixture = NativeFixture(session())

        def request(method, path, body=None):
            if path == "/api/session/ses_root":
                return {"data": session("ses_other")}
            return fixture(method, path, body)

        catalog = self.catalog(request)
        self.assert_refused(
            "identity-changed", lambda: catalog.prepare_delete("ses_root")
        )
        self.assertEqual(fixture.mutations(), [])

    def test_child_linked_during_activity_is_caught_by_final_tree_inventory(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture)
        preview = catalog.prepare_delete("ses_root")

        def hook(method, path, body):
            if path == "/api/session/active":
                fixture.values["ses_new"] = session(
                    "ses_new", "/other", parentID="ses_root"
                )

        fixture.hook = hook
        self.assert_refused("snapshot-changed", lambda: catalog.delete(preview))
        self.assertEqual(fixture.mutations(), [])

    def test_active_after_preview_or_ownership_change_refuses(self):
        for activity in ("active", "ownership"):
            with self.subTest(activity=activity):
                fixture = NativeFixture(session())
                probe = Mock(return_value="exclusive")
                catalog = self.catalog(fixture, ownership=probe)
                preview = catalog.prepare_delete("ses_root")
                if activity == "active":
                    fixture.active = {"ses_root": {"type": "running"}}
                    reason = "session-active"
                else:
                    probe.return_value = "process-local"
                    reason = "snapshot-changed"
                self.assert_refused(reason, lambda: catalog.delete(preview))
                self.assertEqual(fixture.mutations(), [])

    def test_delete_refuses_running_terminals_in_other_directory_descendants(self):
        fixture = NativeFixture(
            session(), session("ses_child", "/other", parentID="ses_root")
        )
        fixture.terminals["ses_child"] = [
            {"status": "running", "sessionID": "ses_child"}
        ]
        catalog = self.catalog(fixture)
        self.assert_refused(
            "session-terminal-running", lambda: catalog.prepare_delete("ses_root")
        )
        self.assertEqual(fixture.mutations(), [])
        fixture.terminals["ses_child"] = [
            {"status": "exited", "sessionID": "ses_child"}
        ]
        preview = catalog.prepare_delete("ses_root")
        fixture.terminals["ses_child"] = [
            {"status": "running", "sessionID": "ses_child"}
        ]
        self.assert_refused("session-terminal-running", lambda: catalog.delete(preview))
        self.assertEqual(fixture.mutations(), [])

    def test_unknown_terminal_inventory_refuses_delete_without_changing_archive_policy(
        self,
    ):
        fixture = NativeFixture(session())
        fixture.terminals["ses_root"] = [{"status": "unknown"}]
        catalog = self.catalog(fixture)
        self.assert_refused(
            "invalid-terminals", lambda: catalog.prepare_delete("ses_root")
        )
        catalog.list()
        self.assertIsNone(catalog.archive("ses_root", True))
        self.assertEqual(len(fixture.mutations()), 1)

    def test_preview_tampering_foreign_catalog_and_binding_change_refuse(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture)
        preview = catalog.prepare_delete("ses_root")
        foreign = self.catalog(fixture)
        self.assert_refused("invalid-preview", lambda: foreign.delete(preview))
        altered = copy.deepcopy(preview)
        altered["sessions"][0]["title"] = "changed"
        self.assert_refused("invalid-preview", lambda: catalog.delete(altered))
        catalog.store = "other"
        self.assert_refused("origin-changed", lambda: catalog.delete(preview))
        self.assert_refused("origin-changed", lambda: catalog.archive("ses_root", True))
        self.assertEqual(fixture.mutations(), [])

    def test_final_ownership_downgrade_refuses_after_earlier_quiet_checks(self):
        fixture = NativeFixture(session())
        probe = Mock(side_effect=["exclusive", "exclusive", "process-local"])
        catalog = self.catalog(fixture, ownership=probe)
        preview = catalog.prepare_delete("ses_root")
        self.assert_refused("snapshot-changed", lambda: catalog.delete(preview))
        self.assertEqual(fixture.mutations(), [])
        self.assertEqual(probe.call_count, 3)

    def test_new_delete_preparation_invalidates_cancelled_preview(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture)
        old = catalog.prepare_delete("ses_root")
        current = catalog.prepare_delete("ses_root")
        self.assert_refused("invalid-preview", lambda: catalog.delete(old))
        self.assertEqual(fixture.mutations(), [])
        self.assertIsNone(catalog.delete(current))
        self.assertEqual(fixture.values, {})

    def test_managed_archive_children_refuse_without_title_guessing(self):
        fixture = NativeFixture(
            session(), session("ses_child", "/other", parentID="ses_root")
        )
        catalog = self.catalog(fixture, managed_ids=(ident for ident in ["ses_root"]))
        self.assert_refused(
            "managed-archive-has-children", lambda: catalog.prepare_delete("ses_root")
        )
        self.assertEqual(fixture.mutations(), [])
        fixture.values.pop("ses_child")
        catalog.delete(catalog.prepare_delete("ses_root"))
        self.assertEqual(fixture.values, {})

    def test_native_parent_cycle_refuses(self):
        fixture = NativeFixture(
            session(parentID="ses_child"), session("ses_child", parentID="ses_root")
        )
        self.assert_refused(
            "invalid-session-tree",
            lambda: self.catalog(fixture).prepare_delete("ses_root"),
        )

    def test_process_local_preview_exposes_race_warning(self):
        fixture = NativeFixture(session())
        catalog = self.catalog(fixture, ownership=lambda *_: "process-local")
        preview = catalog.prepare_delete("ses_root")
        self.assertEqual(preview["warning"], sessions.PROCESS_LOCAL_WARNING)
        self.assertEqual(preview["ownership"], "process-local")

    def test_connection_rebound_by_ownership_callback_refuses_before_mutation(self):
        fixture = NativeFixture(session())
        other = NativeFixture(session())
        catalog = self.catalog(fixture)
        catalog.list()

        def rebind(*_):
            catalog.request = other
            return "exclusive"

        catalog.ownership = rebind
        self.assert_refused("origin-changed", lambda: catalog.archive("ses_root", True))
        self.assertEqual(fixture.mutations(), [])
        self.assertEqual(other.calls, [])

    def test_unexpected_mutation_body_is_never_returned_to_ui(self):
        fixture = NativeFixture(session())

        def request(method, path, body=None):
            if method == "PATCH":
                return {"metadata": "PRIVATE", "permissions": ["PRIVATE"]}
            return fixture(method, path, body)

        catalog = self.catalog(request)
        catalog.list()
        self.assert_refused(
            "invalid-mutation-response", lambda: catalog.archive("ses_root", True)
        )
        self.assert_refused(
            "snapshot-required", lambda: catalog.archive("ses_root", True)
        )


class ArchiveAssociationTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"id": "a" * 32, "project": "/project"}
        prefix = (
            "ses_" + hashlib.sha256(("a" * 32 + "-v2").encode()).hexdigest()[:12] + "_"
        )
        self.root, self.child = prefix + "root", prefix + "child"
        self.state = {"host_sessions": {"ses_root": self.root, "ses_child": self.child}}
        self.provenance = {
            self.root: session(self.root),
            self.child: session(
                self.child,
                metadata={
                    "oc-box.archive": {
                        "version": 1,
                        "parentID": self.root,
                        "sourceMetadata": {"PRIVATE": "secret"},
                    },
                    "PRIVATE": "secret",
                },
            ),
        }

    def associate(self, **kwargs):
        return sessions.archive_associations(
            kwargs.get("registry", self.registry),
            kwargs.get("state", self.state),
            kwargs.get("provenance", self.provenance),
            source_origin="workspace",
            source_store="synthetic-workspace",
            host_store="synthetic-host",
            native=kwargs.get("native", True),
        )

    def test_registry_mapping_and_saved_provenance_relationships_only(self):
        result = self.associate()
        self.assertEqual(
            result[0]["source"],
            {"origin": "workspace", "store": "synthetic-workspace", "id": "ses_root"},
        )
        self.assertEqual(result[1]["parent"], result[0]["archive"])
        self.assertEqual(result[0]["project"], "/project")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotEqual(result[0]["source"], result[0]["archive"])

    def test_title_or_prefix_without_mapping_and_missing_provenance_not_authority(self):
        self.provenance[self.root]["title"] = "[oc-box project] fake authority"
        self.assertEqual(self.associate(state={"host_sessions": {}}), [])
        self.assertEqual(self.associate(provenance={}), [])

    def test_wrong_project_id_namespace_and_duplicate_mapping_refuse(self):
        for label, change in [
            ("project", lambda p: p[self.root]["location"].update(directory="/wrong")),
            ("identity", lambda p: p[self.root].update(id="ses_wrong")),
        ]:
            with self.subTest(label=label):
                provenance = copy.deepcopy(self.provenance)
                change(provenance)
                with self.assertRaises(sessions.CatalogError):
                    self.associate(provenance=provenance)
        with self.assertRaises(sessions.CatalogError):
            self.associate(native=False)
        with self.assertRaises(sessions.CatalogError):
            self.associate(
                state={"host_sessions": {"ses_root": self.root, "ses_other": self.root}}
            )

    def test_unmapped_relationship_does_not_create_native_parent(self):
        self.provenance[self.child]["metadata"]["oc-box.archive"]["parentID"] = (
            "ses_unverified"
        )
        self.assertNotIn("parent", self.associate()[1])
        self.assertNotIn("parentID", self.provenance[self.child])

    def test_missing_parent_provenance_never_promotes_relationship(self):
        self.provenance.pop(self.root)
        result = self.associate()
        self.assertEqual(len(result), 1)
        self.assertNotIn("parent", result[0])

    def test_v1_namespace_mapping_can_attribute_migrated_host_copy(self):
        prefix = "ses_" + hashlib.sha256(("a" * 32).encode()).hexdigest()[:12] + "_"
        host_id = prefix + "root"
        result = self.associate(
            state={"host_sessions": {"ses_root": host_id}},
            provenance={host_id: {"id": host_id, "directory": "/project"}},
            native=False,
        )
        self.assertEqual(result[0]["archive"]["id"], host_id)

    def test_project_selection_retains_native_identity_and_location(self):
        relationships = self.associate()
        values = [
            session("ses_native"),
            session(self.root, "/private/controller"),
            session("ses_title_impostor", "/elsewhere", title="[oc-box project] Fake"),
        ]
        result = sessions.sessions_for_project(
            values,
            "/project",
            origin="host",
            store="synthetic-host",
            associations=relationships,
        )
        self.assertEqual([item["id"] for item in result], ["ses_native", self.root])
        self.assertEqual(result[1]["location"]["directory"], "/private/controller")
        other_store = sessions.sessions_for_project(
            values,
            "/project",
            origin="host",
            store="other-host",
            associations=relationships,
        )
        self.assertEqual([item["id"] for item in other_store], ["ses_native"])


class FavoriteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tui-favorites-",
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "favorites"
        self.store = sessions.FavoriteStore(self.root)

    def test_toggle_roundtrip_separate_origins_and_private_json(self):
        self.assertFalse(self.store.contains("host", "global", "ses_root"))
        self.assertTrue(self.store.toggle("host", "global", "ses_root"))
        self.assertFalse(self.store.contains("workspace", "global", "ses_root"))
        self.assertFalse(self.store.contains("host", "other", "ses_root"))
        self.assertTrue(
            sessions.FavoriteStore(self.root).contains("host", "global", "ses_root")
        )
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for file in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertFalse(self.store.toggle("host", "global", "ses_root"))
        self.assertEqual(self.store.list(), [])

    def test_locks_serialize_independent_writers_and_no_staging_left(self):
        def toggle(index):
            return sessions.FavoriteStore(self.root).toggle(
                "host", "global", f"ses_{index}"
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertTrue(all(executor.map(toggle, range(40))))
        self.assertEqual(len(self.store.list()), 40)
        self.assertEqual(
            {file.name for file in self.root.iterdir()},
            {"favorites.json", "favorites.lock"},
        )

    def test_corrupt_state_refuses_without_overwrite(self):
        self.store.toggle("host", "global", "ses_root")
        target = self.root / "favorites.json"
        for content in [
            b"broken PRIVATE",
            b"[" * 2000 + b"]" * 2000,
            b'{"version":1,"favorites":[{"origin":"host","store":"global","id":"ses_root","metadata":"PRIVATE"}]}',
            b'{"version":1,"favorites":[{"origin":"host","store":"global","id":"ses_root"},{"origin":"host","store":"global","id":"ses_root"}]}',
        ]:
            with self.subTest(content=content):
                target.write_bytes(content)
                with self.assertRaises(sessions.CatalogError) as raised:
                    self.store.toggle("host", "global", "ses_other")
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertEqual(target.read_bytes(), content)

    def test_symlink_hardlink_and_public_modes_refuse(self):
        self.store.toggle("host", "global", "ses_root")
        target = self.root / "favorites.json"
        original = target.read_bytes()
        target.chmod(0o644)
        with self.assertRaises(sessions.CatalogError):
            self.store.list()
        target.chmod(0o600)
        linked = self.root.parent / "linked"
        os.link(target, linked)
        with self.assertRaises(sessions.CatalogError):
            self.store.list()
        linked.unlink()
        target.unlink()
        linked.write_bytes(original)
        target.symlink_to(linked)
        with self.assertRaises(sessions.CatalogError):
            self.store.toggle("host", "global", "ses_other")
        self.assertEqual(linked.read_bytes(), original)
        target.unlink()
        self.root.chmod(0o755)
        with self.assertRaises(sessions.CatalogError):
            self.store.list()

    def test_lock_symlink_or_symlink_root_refuses(self):
        self.root.mkdir(mode=0o700)
        outside = self.root.parent / "outside"
        outside.write_text("PRIVATE")
        (self.root / "favorites.lock").symlink_to(outside)
        with self.assertRaises(sessions.CatalogError):
            self.store.list()
        self.assertEqual(outside.read_text(), "PRIVATE")
        link = self.root.parent / "symlink-root"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(sessions.CatalogError):
            sessions.FavoriteStore(link).list()

    def test_atomic_write_failure_keeps_previous_json_and_removes_own_temporary(self):
        self.store.toggle("host", "global", "ses_root")
        old = (self.root / "favorites.json").read_bytes()
        with patch.object(sessions.os, "replace", side_effect=OSError("PRIVATE")):
            with self.assertRaises(sessions.CatalogError) as raised:
                self.store.toggle("host", "global", "ses_other")
        self.assertEqual(raised.exception.reason, "favorites-state-failed")
        self.assertEqual((self.root / "favorites.json").read_bytes(), old)
        self.assertEqual(
            {file.name for file in self.root.iterdir()},
            {"favorites.json", "favorites.lock"},
        )

    def test_busy_favorites_lock_refuses_boundedly_without_replacing_state(self):
        self.store.toggle("host", "global", "ses_root")
        target = self.root / "favorites.json"
        before = target.read_bytes()
        with (self.root / "favorites.lock").open("rb") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sessions, "FAVORITES_LOCK_TIMEOUT", 0):
                with self.assertRaises(sessions.CatalogError) as caught:
                    self.store.list()
                self.assertEqual(caught.exception.reason, "favorites-busy")
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
