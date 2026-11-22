"""Open means native TUI membership, not active drains or historical roots."""

import base64
import contextlib
from copy import deepcopy
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
import socket
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from oc_box import native_tabs, oc_box as box, opencode_v2_io as bridge
from oc_box import (
    ui_state,
    web_attachments,
    web_sessions as web,
    workspace_service as service,
)


class ShellHTML(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.frames = []
        self.state = None
        self.scripts = []
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "iframe":
            self.frames.append(attrs)
        if attrs.get("id") == "web-shell":
            self.state = json.loads(attrs["data-bootstrap"])
        if tag == "script":
            self.scripts.append(attrs.get("src"))


class WebSessionsTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "id": "a" * 32,
            "project": "/host/restored",
            "container_project": "/original/project",
            "opencode_version": "2.3.0",
        }
        self.connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "session": "ses_one",
        }
        self.inventory = [
            {
                "id": "ses_" + name,
                "title": title,
                "location": {"directory": "/original/project"},
                "time": {"updated": 1},
            }
            for name, title in (
                ("one", "First"),
                ("two", '<img src=x onerror="bad">'),
                ("three", "Third"),
                ("four", "Fourth"),
                ("closed", "Closed newest root"),
                ("archived", "Archived"),
                ("child", "Child"),
                ("foreign", "Foreign"),
            )
        ]
        self.inventory[4]["time"]["updated"] = 100
        self.inventory[1]["location"]["directory"] = "/private/worktree"
        self.inventory[2]["location"]["directory"] = "/private/moved"
        self.inventory[3]["time"]["archived"] = 0
        self.inventory[5]["time"]["archived"] = 0
        self.inventory[6]["parentID"] = "ses_one"
        self.inventory[7]["location"]["directory"] = "/original/project-sibling"
        self.members = (
            "ses_four",
            "ses_one",
            "ses_two",
            "ses_three",
            "ses_absent",
        )

    def collect(self, snapshot, inventory=None, connection=None):
        with (
            patch.object(native_tabs, "inspect", return_value=snapshot),
            patch.object(
                service,
                "web_session_catalog",
                return_value=self.inventory if inventory is None else inventory,
            ),
        ):
            return web.open_sessions(
                box, self.data, self.connection if connection is None else connection
            )

    def test_four_idle_tabs_include_open_worktree_moved_and_archived_in_saved_scope(
        self,
    ):
        before = deepcopy(self.inventory)
        for scope in ("cwd", "global"):
            with self.subTest(scope=scope):
                sessions, status = self.collect(
                    native_tabs.Snapshot("valid", self.members, scope)
                )
                self.assertEqual(status, "valid")
                self.assertEqual(
                    [item["id"] for item in sessions],
                    ["ses_four", "ses_one", "ses_two", "ses_three"],
                )
        self.assertEqual(self.inventory, before)

    def test_explicit_saved_child_id_is_not_silently_removed_or_mapped_to_parent(self):
        sessions, _ = self.collect(native_tabs.Snapshot("valid", ("ses_child",)))
        self.assertEqual([info["id"] for info in sessions], ["ses_child"])
        self.assertEqual(sessions[0]["parentID"], "ses_one")

    def test_missing_empty_invalid_and_changed_state_never_reopen_history(self):
        for reason in ("absent", "disabled", "scope-absent"):
            with self.subTest(reason=reason):
                sessions, status = self.collect(
                    native_tabs.Snapshot("missing", reason=reason)
                )
                self.assertEqual(status, "missing")
                self.assertEqual(sessions, [])
        self.assertEqual(self.collect(native_tabs.Snapshot("valid")), ([], "valid"))
        with self.assertRaises(box.BoxError):
            self.collect(native_tabs.Snapshot("invalid", reason="transport"))
        for selected in (
            None,
            "ses_archived",
            "ses_child",
            "ses_foreign",
            "ses_absent",
        ):
            self.assertEqual(
                self.collect(
                    native_tabs.Snapshot("missing", reason="absent"),
                    connection={**self.connection, "session": selected},
                ),
                ([], "missing"),
            )
        with (
            patch.object(
                native_tabs,
                "inspect",
                side_effect=[
                    native_tabs.Snapshot("valid", ("ses_one",)),
                    native_tabs.Snapshot("valid"),
                ],
            ),
            patch.object(service, "web_session_catalog", return_value=self.inventory),
            self.assertRaisesRegex(box.BoxError, "changed"),
        ):
            web.open_sessions(box, self.data, self.connection)

    def test_foreign_source_malformed_or_duplicate_catalog_refuses(self):
        with self.assertRaises(box.BoxError):
            self.collect(
                native_tabs.Snapshot("valid", self.members),
                connection={**self.connection, "workspace": "b" * 32},
            )
        for inventory in ({}, self.inventory + self.inventory[:1], [{}]):
            with self.subTest(inventory=type(inventory).__name__):
                with self.assertRaises((box.BoxError, bridge.FixedError)):
                    self.collect(native_tabs.Snapshot("valid", self.members), inventory)

    def test_complete_native_catalog_pagination_retains_all_four_not_first_page_only(
        self,
    ):
        calls = []

        def request(method, route):
            self.assertEqual(method, "GET")
            self.assertNotIn("directory=", route)
            self.assertNotIn("parentID=", route)
            self.assertNotIn("archived=", route)
            self.assertNotIn("/active", route)
            index = len(calls)
            calls.append(route)
            return {
                "data": self.inventory[index : index + 1],
                "cursor": {"next": "page+" + str(index + 1) if index < 3 else None},
            }

        catalog = bridge.service_session_catalog(request)
        self.assertEqual(len(calls), 4)
        self.assertIn("cursor=page%2B1", calls[1])
        sessions, _ = self.collect(native_tabs.Snapshot("valid", self.members), catalog)
        self.assertEqual(len(sessions), 4)

    def test_native_get_verifies_member_identity_and_omits_deleted_or_missing_ids(self):
        calls = []

        def request(method, route):
            self.assertEqual(method, "GET")
            calls.append(route)
            if route.startswith("/api/session?"):
                return {"data": self.inventory, "cursor": {"next": None}}
            ident = route.rsplit("/", 1)[1]
            if ident == "ses_child":
                raise HTTPError("http://127.0.0.1/synthetic", 404, "", {}, None)
            return {
                "data": next(info for info in self.inventory if info["id"] == ident)
            }

        members = ["ses_four", "ses_two", "ses_child", "ses_absent"]
        result = bridge.verified_web_sessions(request, members)
        self.assertEqual([info["id"] for info in result], ["ses_four", "ses_two"])
        self.assertEqual(
            calls[1:],
            [
                "/api/session/ses_four",
                "/api/session/ses_two",
                "/api/session/ses_child",
            ],
        )
        self.assertNotIn("/api/session/ses_closed", calls)
        with self.assertRaises(bridge.FixedError):
            bridge.verified_web_sessions(
                lambda method, route: (
                    {"data": self.inventory, "cursor": {"next": None}}
                    if "?" in route
                    else {"data": self.inventory[1]}
                ),
                ["ses_one"],
            )
        for invalid in (["ses_one", "ses_one"], ["not-a-session"], {}):
            with self.subTest(invalid=invalid), self.assertRaises(bridge.FixedError):
                bridge.verified_web_sessions(request, invalid)

    def test_private_service_catalog_refuses_partial_malformed_or_repeated_pages(self):
        for pages in (
            [{"data": [{}], "cursor": {"next": None}}],
            [{"data": self.inventory[:1] * 2, "cursor": {"next": None}}],
            [{"data": [], "cursor": {"next": ""}}],
            [
                {"data": self.inventory[:1], "cursor": {"next": "repeat"}},
                {"data": [], "cursor": {"next": "repeat"}},
            ],
        ):
            with self.subTest(pages=len(pages)), self.assertRaises(bridge.FixedError):
                bridge.service_session_catalog(Mock(side_effect=pages))

    def test_web_catalog_transport_is_providerless_and_bound_to_existing_endpoint(self):
        data = {**self.data, "home": "/private/home", "container": "synthetic"}

        def metadata(command, **settings):
            self.assertIn("web-sessions", command)
            self.assertNotIn(self.connection["password"], " ".join(command))
            self.assertNotIn("OPENAI_API_KEY", command.environment)
            payload = json.loads(settings["input"])
            self.assertEqual(set(payload), {"endpoint", "members"})
            self.assertEqual(payload["members"], list(self.members))
            self.assertEqual(
                payload["endpoint"],
                {key: self.connection[key] for key in ("url", "password")},
            )
            self.assertTrue(settings["private"])
            return SimpleNamespace(stdout=json.dumps(self.inventory[:4]))

        with (
            patch.object(box, "run", side_effect=metadata),
            patch.object(box, "exec_command", side_effect=AssertionError("owner env")),
            patch.object(
                service, "tab_inventory", side_effect=AssertionError("resume scope")
            ),
        ):
            self.assertEqual(
                service.web_session_catalog(box, data, self.connection, self.members),
                self.inventory[:4],
            )
        with patch.object(bridge, "server", side_effect=AssertionError("discovery")):
            with patch.object(sys, "stdin", io.StringIO('{"members":[]}')):
                with self.assertRaises(KeyError):
                    bridge.main(["web-sessions", "unused"])

    def test_document_escapes_titles_and_uses_public_native_routes_without_secrets(
        self,
    ):
        sessions, status = self.collect(native_tabs.Snapshot("valid", self.members))
        sessions[3]["title"] = "\ud800"  # Native JSON can contain a lone surrogate.
        url = "http://127.0.0.1:23456"
        text = web.document(url, sessions, status).decode()
        self.assertEqual(text.count('href="/server/'), 4)
        self.assertIn('href="/"', text)
        self.assertIn("&lt;img", text)
        self.assertNotIn("<img", text)
        self.assertIn("independent of TUI tabs", text)
        self.assertIn(web.INDEX, text)
        self.assertNotIn("ses_closed", text)
        self.assertNotIn(self.connection["password"], text)
        parsed = ShellHTML(text.encode())
        self.assertEqual(len(parsed.frames), 1)
        self.assertEqual(parsed.frames[0]["id"], "native")
        self.assertEqual(parsed.scripts, [web.SCRIPT])
        self.assertEqual(len(parsed.state["sessions"]), 4)
        self.assertEqual(
            base64.urlsafe_b64decode(
                web.native_session_route(url, "ses_one").split("/")[2] + "=="
            ).decode(),
            url,
        )

    def test_selection_resolver_never_uses_order_time_or_activity_as_focus(self):
        members = ["ses_two", "ses_one"]
        self.assertIsNone(web.resolve_initial_selection(members)[0])
        self.assertIsNone(
            web.resolve_initial_selection(members, declared="ses_closed")[0]
        )
        ident, origin = web.resolve_initial_selection(members, declared="ses_one")
        self.assertEqual(ident, "ses_one")
        self.assertIn("not verified native focus", origin)
        self.assertEqual(
            web.resolve_initial_selection(
                members, requested="ses_two", override="ses_one", declared="ses_two"
            )[0],
            "ses_one",
        )
        self.assertIsNone(
            web.resolve_initial_selection(
                members,
                declared="ses_one",
                report_requested=True,
                report={"status": "unavailable", "selectedSessionID": "ses_one"},
            )[0]
        )
        self.assertEqual(
            web.resolve_initial_selection(
                members,
                report_requested=True,
                report={
                    "status": "verified",
                    "selectedSessionID": "ses_two",
                    "source": "synthetic native report",
                },
            )[0],
            "ses_two",
        )
        self.assertEqual(
            web.resolve_initial_selection(
                members,
                override="ses_one",
                report_requested=True,
                report={
                    "status": "verified",
                    "selectedSessionID": "ses_two",
                    "source": "synthetic",
                },
            )[0],
            "ses_one",
        )

    def test_initial_selection_reader_seam_is_private_read_only_and_explicit_wins(self):
        reader = Mock(
            return_value={
                "status": "verified",
                "selectedSessionID": "ses_three",
                "source": "synthetic report",
                "orderedtabs": ["ses_one", "ses_three"],
                "activeRoot": "ses_three",
            }
        )
        gateway = service.Gateway(
            box,
            self.data,
            self.connection,
            selection_snapshot_reader=reader,
            shell_addon_scripts=(),
        )
        gateway.url = "http://127.0.0.1:23456"
        state = web.initial_state(gateway, self.inventory[:4], "valid")
        reader.assert_called_once_with(box, self.data, self.connection)
        self.assertEqual(state["selection"]["id"], "ses_three")
        gateway.connection = {**self.connection, "web_session_override": "ses_two"}
        reader.reset_mock()
        state = web.initial_state(gateway, self.inventory[:4], "valid")
        reader.assert_not_called()
        self.assertEqual(state["selection"]["id"], "ses_two")

    def test_common_shell_declared_and_explicit_private_selection_are_distinct(self):
        gateway = SimpleNamespace(
            box=box,
            data=self.data,
            connection=self.connection,
            url="http://127.0.0.1:23456",
        )
        sessions = self.inventory[:4]
        state = web.initial_state(gateway, sessions, "valid")
        self.assertEqual(state["selection"]["id"], "ses_one")
        self.assertIn("Launcher-declared", state["selection"]["origin"])
        parsed = ShellHTML(web.document(gateway.url, sessions, "valid", state))
        self.assertEqual(
            parsed.frames[0]["src"], web.native_session_route(gateway.url, "ses_one")
        )
        with patch.object(
            service, "web_session_catalog", return_value=[self.inventory[4]]
        ):
            state = web.initial_state(
                gateway, sessions, "valid", requested="ses_closed"
            )
        self.assertEqual(state["selection"]["id"], "ses_closed")
        self.assertFalse(state["selection"]["member"])
        self.assertEqual(len(state["sessions"]), 4)
        with self.assertRaises(ValueError):
            web.document(
                gateway.url,
                sessions,
                "valid",
                state,
                addon_scripts=("https://evil.invalid/x.js",),
            )
        self.assertIn(
            "/auth/oc-box-attachments.js",
            ShellHTML(
                web.document(
                    gateway.url,
                    sessions,
                    "valid",
                    state,
                    addon_scripts=("/auth/oc-box-attachments.js",),
                )
            ).scripts,
        )

    def test_native_snapshot_supplies_exact_child_and_full_ordered_membership(self):
        snapshot = SimpleNamespace(
            status="valid",
            tabs=("ses_four", "ses_one"),
            selected="ses_child",
            activeRoot="ses_one",
            source="owner-tui",
        )
        reader = Mock(return_value=snapshot)
        gateway = service.Gateway(
            box, self.data, self.connection, selection_snapshot_reader=reader
        )
        gateway.url = "http://127.0.0.1:23456"
        by_id = {info["id"]: info for info in self.inventory}

        def lookup(_box, _data, connection, ids):
            self.assertEqual(connection, self.connection)
            return [by_id[ident] for ident in reversed(ids)]

        with (
            patch.object(
                native_tabs, "inspect", side_effect=AssertionError("not saved tabs")
            ),
            patch.object(service, "web_session_catalog", side_effect=lookup),
        ):
            report = web.selection_report(gateway)
            sessions, status = web.reported_open_sessions(gateway, report)
            state = web.initial_state(gateway, sessions, status, report=report)
        reader.assert_called_once_with(box, self.data, self.connection)
        self.assertEqual([info["id"] for info in sessions], list(snapshot.tabs))
        self.assertEqual(state["selection"]["id"], "ses_child")
        self.assertEqual(state["selection"]["activeRoot"], "ses_one")
        self.assertFalse(state["selection"]["member"])
        self.assertEqual(len(state["sessions"]), 2)
        self.assertTrue(state["selection"]["route"].endswith("/session/ses_child"))
        self.assertIn("owner-tui", state["selection"]["origin"])

    def test_missing_native_snapshot_never_revives_declared_owner_selection(self):
        gateway = service.Gateway(
            box,
            self.data,
            self.connection,
            selection_snapshot_reader=Mock(
                return_value=SimpleNamespace(status="missing")
            ),
        )
        gateway.url = "http://127.0.0.1:23456"
        report = web.selection_report(gateway)
        state = web.initial_state(gateway, self.inventory[:4], "valid", report=report)
        self.assertIsNone(state["selection"])
        gateway.connection = {**self.connection, "web_session_override": "ses_two"}
        state = web.initial_state(gateway, self.inventory[:4], "valid", report=report)
        self.assertEqual(state["selection"]["id"], "ses_two")
        self.assertIn("Explicit --session", state["selection"]["origin"])

    def test_malformed_native_snapshot_is_unavailable_not_a_partial_focus_report(self):
        good = dict(
            status="valid",
            tabs=("ses_one",),
            selected="ses_one",
            activeRoot="ses_one",
            source="owner-tui",
        )
        for replacement in (
            {"tabs": ["ses_one"]},
            {"tabs": ("ses_one", "ses_one")},
            {"tabs": ("../wrong",)},
            {"selected": "../wrong"},
            {"activeRoot": "ses_absent"},
            {"source": "newest-reporter"},
        ):
            with self.subTest(replacement=replacement):
                gateway = service.Gateway(
                    box,
                    self.data,
                    self.connection,
                    selection_snapshot_reader=Mock(
                        return_value=SimpleNamespace(**{**good, **replacement})
                    ),
                )
                self.assertEqual(
                    web.selection_report(gateway), {"status": "unavailable"}
                )

    def test_native_snapshot_catalog_refuses_missing_or_duplicated_members(self):
        gateway = service.Gateway(box, self.data, self.connection)
        report = {"_native_snapshot": True, "tabs": ("ses_one", "ses_two")}
        for found in ([self.inventory[0]], [self.inventory[0], self.inventory[0]]):
            with (
                self.subTest(found=found),
                patch.object(service, "web_session_catalog", return_value=found),
                self.assertRaises(box.BoxError),
            ):
                web.reported_open_sessions(gateway, report)

    def test_child_root_display_uses_only_same_service_ancestry_and_is_bounded(self):
        gateway = service.Gateway(box, self.data, self.connection)
        with patch.object(
            web, "explicit_session", return_value=self.inventory[0]
        ) as read:
            self.assertEqual(
                web.root_member(gateway, self.inventory[6], {"ses_one"}), "ses_one"
            )
        self.assertEqual(
            read.call_args.args, (box, self.data, self.connection, "ses_one")
        )
        self.assertGreater(read.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(read.call_args.kwargs["timeout"], 4)
        cycle = {"id": "ses_child", "parentID": "ses_child"}
        with patch.object(web, "explicit_session", return_value=cycle) as read:
            self.assertIsNone(web.root_member(gateway, cycle, {"ses_one"}))
        self.assertEqual(read.call_count, 1)

    def test_explicit_child_keeps_exact_route_and_root_when_tui_is_elsewhere(self):
        report = {
            "status": "verified",
            "_native_snapshot": True,
            "selectedSessionID": "ses_two",
            "activeRoot": "ses_two",
            "tabs": ("ses_one", "ses_two"),
            "source": "owner-tui",
        }
        by_id = {info["id"]: info for info in self.inventory}
        gateway = service.Gateway(box, self.data, self.connection)
        gateway.url = "http://127.0.0.1:23456"
        for mode in ("cli", "url"):
            with self.subTest(mode=mode):
                gateway.connection = {
                    **self.connection,
                    **({"web_session_override": "ses_child"} if mode == "cli" else {}),
                }
                with patch.object(
                    service,
                    "web_session_catalog",
                    side_effect=lambda _box, _data, _connection, ids, **_kwargs: [
                        by_id[ident] for ident in ids
                    ],
                ):
                    state = web.initial_state(
                        gateway,
                        self.inventory[:2],
                        "valid",
                        requested="ses_child" if mode == "url" else None,
                        report=report,
                    )
                self.assertEqual(state["selection"]["id"], "ses_child")
                self.assertEqual(state["selection"]["activeRoot"], "ses_one")
                self.assertTrue(
                    state["selection"]["route"].endswith("/session/ses_child")
                )
                self.assertEqual(len(state["sessions"]), 2)

    def test_unavailable_child_ancestry_never_prevents_explicit_selection(self):
        gateway = service.Gateway(
            box,
            self.data,
            {**self.connection, "web_session_override": "ses_child"},
        )
        gateway.url = "http://127.0.0.1:23456"
        with patch.object(
            service,
            "web_session_catalog",
            side_effect=[[self.inventory[6]], box.BoxError("private parent failure")],
        ):
            state = web.initial_state(gateway, self.inventory[:2], "valid")
        self.assertEqual(state["selection"]["id"], "ses_child")
        self.assertIsNone(state["selection"]["activeRoot"])
        self.assertNotIn("private parent failure", json.dumps(state))

    @unittest.skipUnless(
        shutil.which("node"), "Node is required for common-shell DOM checks"
    )
    def test_common_shell_dom_navigation_and_draft_preservation(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("web_sessions_dom.mjs"))],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS common shell DOM", result.stdout)

    def test_explicit_web_override_preserves_attach_and_owner_selection_without_creation(
        self,
    ):
        args = SimpleNamespace(
            new=False, model=None, resume=False, command="web", web_session="ses_two"
        )
        owner = SimpleNamespace(
            connection=self.connection, publish=Mock(), wait_clients=Mock()
        )
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_one"}),
            patch.object(
                web, "explicit_session", return_value=self.inventory[1]
            ) as verify,
            patch.object(service, "api") as native,
            patch.object(service, "web_session", return_value=0) as browser,
            patch.object(service, "wait_history"),
            patch.object(box, "sync_sessions"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.launch_client(
                    None,
                    {**self.data, "distro": "synthetic"},
                    args,
                    "2.3.0",
                    None,
                    owner,
                ),
                0,
            )
        verify.assert_called_once_with(
            box, {**self.data, "distro": "synthetic"}, self.connection, "ses_two"
        )
        owner.publish.assert_called_once_with("ses_two")
        native.assert_not_called()
        self.assertEqual(browser.call_args.args[2]["session"], "ses_two")
        gateway = SimpleNamespace(url="http://127.0.0.1:23456")
        with (
            patch.object(
                web, "explicit_session", return_value=self.inventory[1]
            ) as verify,
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ) as factory,
            patch.object(service, "api", return_value={"code": "synthetic"}),
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            service.web_session(
                box,
                self.data,
                self.connection,
                SimpleNamespace(web_session="ses_two", port=0, no_open=True),
            )
        verify.assert_called_once_with(box, self.data, self.connection, "ses_two")
        self.assertEqual(factory.call_args.args[2]["web_session_override"], "ses_two")
        self.assertIs(
            factory.call_args.kwargs["selection_snapshot_reader"],
            ui_state.read_snapshot,
        )

    def test_empty_missing_and_authorized_error_pages_offer_home_without_session_rows(
        self,
    ):
        pages = [
            web.document("http://127.0.0.1:23456", [], status)
            for status in ("valid", "missing")
        ]
        pages.append(web.error_document())
        for page in pages:
            with self.subTest(page=page[:32]):
                self.assertIn(b'href="/"', page)
                self.assertIn(b"Open native OpenCode Home", page)
                self.assertNotIn(b'href="/server/', page)

    def test_history_requires_native_auth_and_trusted_layout_before_querying(self):
        from oc_box import web_history

        gateway = service.Gateway(box, self.data, self.connection)
        gateway.url = "http://127.0.0.1:23456"
        row = {
            "id": "ses_history",
            "title": "Synthetic <title>",
            "time": {"created": 1, "updated": 3, "archived": 3},
            "isChild": True,
        }
        for enabled, authorized, suffix, expected in (
            (True, False, "", "401 Unauthorized"),
            (False, True, "", "404 Not Found"),
            (True, True, "?session=ses_history", "200 OK"),
            (True, True, "", "200 OK"),
        ):
            with self.subTest(enabled=enabled, authorized=authorized, suffix=suffix):
                gateway.workspace_layout = enabled
                request = (
                    f"GET {web.HISTORY}{suffix} HTTP/1.1\r\nHost: 127.0.0.1:23456\r\n\r\n"
                ).encode()
                with (
                    patch.object(web, "_authenticated", return_value=authorized),
                    patch.object(web, "reported_open_sessions") as open_tabs,
                    patch.object(web_history, "catalog", return_value=[row]) as catalog,
                    patch.object(web_history, "selection", return_value=row) as select,
                ):
                    stream = Mock()
                    self.assertTrue(web.respond(gateway, stream, None, request))
                head, body = stream.sendall.call_args.args[0].split(b"\r\n\r\n", 1)
                self.assertIn(expected.encode(), head)
                open_tabs.assert_not_called()
                if expected == "200 OK":
                    if suffix:
                        self.assertEqual(json.loads(body), row)
                        select.assert_called_once_with(
                            box, self.data, self.connection, "ses_history"
                        )
                        catalog.assert_not_called()
                    else:
                        self.assertEqual(json.loads(body), [row])
                        catalog.assert_called_once_with(box, self.data, self.connection)
                        select.assert_not_called()
                else:
                    catalog.assert_not_called()
                    select.assert_not_called()

    def test_attachment_slot_preserves_all_four_members_single_frame_and_home(self):
        sessions, status = self.collect(native_tabs.Snapshot("valid", self.members))
        gateway = service.Gateway(
            box, self.data, {**self.connection, "session": None}, attachments=True
        )
        gateway.url = "http://127.0.0.1:23456"
        body = web.document(
            gateway.url, sessions, status, addon_scripts=(web_attachments.SCRIPT,)
        )
        self.assertEqual(gateway.session_route(), web.INDEX)
        for item in sessions:
            self.assertIn(gateway.native_session_route(item["id"]).encode(), body)
        self.assertEqual(body.count(b"<iframe "), 1)
        self.assertEqual(body.count(web_attachments.SCRIPT.encode()), 1)
        self.assertNotIn(b"ses_closed", body)
        self.assertIn(b'href="/"', body)
        for empty_status in ("valid", "missing"):
            empty = web.document(
                gateway.url, [], empty_status, addon_scripts=(web_attachments.SCRIPT,)
            )
            self.assertIn(b'href="/"', empty)
            self.assertNotIn(b"?session=", empty)

    def test_explicit_web_invalid_id_refuses_before_native_discovery_or_credentials(
        self,
    ):
        with (
            patch.dict("os.environ", {"OC_BOX_CONTAINER": "0"}),
            patch.object(box, "detect_opencode_release") as native,
            patch.object(box, "prepare_runtime") as prepare,
            self.assertRaisesRegex(box.BoxError, "--session"),
        ):
            box.main(["web", "--session", "../wrong"])
        native.assert_not_called()
        prepare.assert_not_called()
        with patch.object(service, "web_session_catalog", return_value=[]):
            with self.assertRaisesRegex(box.BoxError, "unavailable"):
                web.explicit_session(box, self.data, self.connection, "ses_unknown")

    def test_native_service_worker_navigation_boundary_and_selected_row_routes(self):
        # Native v2.2.6 and v2.3.0 packages/app/vite.pwa.ts navigation denylist:
        # /^\/(?:api|auth)(?:\/|$)/. Test routing behavior, not documentation text.
        excluded = re.compile(r"^/(?:api|auth)(?:/|$)")
        gateway = service.Gateway(box, self.data, self.connection)
        gateway.url = "http://127.0.0.1:23456"
        self.assertRegex(gateway.session_route(), excluded)
        route = gateway.native_session_route("ses_one")
        self.assertEqual(route, web.native_session_route(gateway.url, "ses_one"))
        self.assertIsNone(excluded.match(route))
        self.assertNotEqual(route, gateway.session_route())
        with self.assertRaises(box.BoxError):
            gateway.native_session_route("../auth")

    def test_web_can_pair_at_native_home_without_fabricating_a_selected_session(self):
        gateway = SimpleNamespace(url="http://127.0.0.1:23456")
        with (
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ),
            patch.object(service, "api", return_value={"code": "synthetic_code"}),
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                service.web_session(
                    box,
                    self.data,
                    {**self.connection, "session": None},
                    SimpleNamespace(port=0, no_open=True),
                ),
                0,
            )
        self.assertIn("workspace session index", output.getvalue())

    def test_owning_web_honors_saved_empty_or_open_tabs_instead_of_newest_history(self):
        state = {"last": "ses_closed"}
        for snapshot, expected in (
            (native_tabs.Snapshot("valid"), None),
            (native_tabs.Snapshot("valid", ("ses_two", "ses_one")), "ses_one"),
            (native_tabs.Snapshot("valid", ("ses_four",)), None),
        ):
            with self.subTest(snapshot=snapshot):
                owner = SimpleNamespace(
                    connection=self.connection, publish=Mock(), wait_clients=Mock()
                )
                args = SimpleNamespace(
                    new=False, model=None, resume=False, command="web"
                )
                with (
                    patch.object(box, "refresh_sessions", return_value=state),
                    patch.object(native_tabs, "inspect", return_value=snapshot),
                    patch.object(
                        box, "list_container_sessions", return_value=self.inventory
                    ),
                    patch.object(service, "api") as native,
                    patch.object(service, "web_session", return_value=0) as browser,
                    patch.object(service, "wait_history"),
                    patch.object(box, "sync_sessions"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        box.launch_client(
                            None,
                            {**self.data, "distro": "synthetic"},
                            args,
                            "2.3.0",
                            None,
                            owner,
                        ),
                        0,
                    )
                owner.publish.assert_called_once_with(expected)
                native.assert_not_called()
                self.assertEqual(browser.call_args.args[2]["session"], expected)

    def test_authenticated_gateway_lists_four_without_weakening_native_cookie_origin_checks(
        self,
    ):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(handler):
                observed.append(
                    (
                        handler.path,
                        {key.lower(): value for key, value in handler.headers.items()},
                    )
                )
                if handler.path.startswith("/auth/connect/"):
                    handler.send_response(302)
                    handler.send_header("Location", "/")
                    handler.send_header(
                        "Set-Cookie", "native=fixture; HttpOnly; Path=/"
                    )
                    body = b""
                elif handler.path == "/api/info":
                    allowed = (
                        handler.headers.get("Cookie") == "native=fixture"
                        and handler.headers.get(
                            "Origin", "http://" + handler.headers["Host"]
                        )
                        == "http://" + handler.headers["Host"]
                    )
                    handler.send_response(200 if allowed else 401)
                    body = b"{}"
                else:
                    handler.send_response(200)
                    body = b"native-unchanged"
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        with (
            patch.object(service, "relay_command", side_effect=local_relay),
            patch.object(
                native_tabs,
                "inspect",
                return_value=native_tabs.Snapshot("valid", self.members),
            ),
            patch.object(
                service, "web_session_catalog", return_value=self.inventory
            ) as inventory,
            service.Gateway(
                box,
                self.data,
                {
                    **self.connection,
                    "url": "http://127.0.0.1:" + str(upstream.server_port),
                },
            ) as gateway,
        ):
            with self.assertRaises(HTTPError) as refusal:
                opener.open(gateway.url + web.INDEX, timeout=5)
            self.assertEqual(refusal.exception.code, 401)
            self.assertNotIn(b'href="/"', refusal.exception.read())
            inventory.assert_not_called()
            authority = gateway.url.removeprefix("http://")
            for headers, method, code in (
                ("Host: 127.0.0.1:1\r\nCookie: native=fixture", "GET", b"401"),
                ("Cookie: native=fixture", "GET", b"401"),
                (
                    "Host: "
                    + authority
                    + "\r\nCookie: native=fixture\r\nCookie: native=fixture",
                    "GET",
                    b"401",
                ),
                ("Host: " + authority + "\r\nCookie: native=fixture", "POST", b"400"),
            ):
                with socket.create_connection(
                    ("127.0.0.1", int(authority.rsplit(":", 1)[1])), timeout=5
                ) as stream:
                    stream.sendall(
                        (
                            method
                            + " "
                            + web.INDEX
                            + " HTTP/1.1\r\n"
                            + headers
                            + "\r\n\r\n"
                        ).encode()
                    )
                    self.assertIn(code, stream.recv(4096).split(b"\r\n", 1)[0])
            inventory.assert_not_called()
            with opener.open(
                gateway.url + "/auth/connect/fixture", timeout=5
            ) as response:
                self.assertEqual(response.geturl(), gateway.url + web.INDEX)
                body = response.read()
                self.assertEqual(body.count(b'href="/server/'), 4)
                self.assertIn(b'href="/"', body)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            for route, mime in ((web.SCRIPT, "javascript"), (web.STYLE, "text/css")):
                with opener.open(gateway.url + route, timeout=5) as response:
                    self.assertIn(mime, response.headers["Content-Type"])
                    self.assertTrue(response.read())
            with opener.open(gateway.url + web.CATALOG, timeout=5) as response:
                data = json.loads(response.read())
                self.assertEqual(
                    [row["id"] for row in data["sessions"]],
                    ["ses_four", "ses_one", "ses_two", "ses_three"],
                )
            with opener.open(
                gateway.url + web.INDEX + "?session=ses_two", timeout=5
            ) as response:
                parsed = ShellHTML(response.read())
                self.assertEqual(parsed.state["selection"]["id"], "ses_two")
                self.assertEqual(len(parsed.frames), 1)
            with (
                patch.object(
                    web, "open_sessions", side_effect=box.BoxError("unavailable")
                ),
                patch.object(
                    service, "web_session_catalog", return_value=[self.inventory[1]]
                ),
                opener.open(
                    gateway.url + web.INDEX + "?session=ses_two", timeout=5
                ) as response,
            ):
                parsed = ShellHTML(response.read())
                self.assertEqual(parsed.state["status"], "unavailable")
                self.assertEqual(parsed.state["sessions"], [])
                self.assertEqual(parsed.state["selection"]["id"], "ses_two")
                self.assertFalse(parsed.state["selection"]["member"])
            for route in (
                web.SCRIPT,
                web.STYLE,
                web.CATALOG,
                web.SELECTION + "?session=ses_one",
            ):
                with self.assertRaises(HTTPError) as refusal:
                    opener.open(
                        Request(
                            gateway.url + route,
                            headers={"Sec-Fetch-Site": "cross-site"},
                        ),
                        timeout=5,
                    )
                self.assertEqual(refusal.exception.code, 401)
            self.assertTrue(
                all(
                    headers["host"] == gateway.url.removeprefix("http://")
                    and "authorization" not in headers
                    for path, headers in observed
                    if path == "/api/info"
                )
            )
            with self.assertRaises(HTTPError) as refusal:
                opener.open(
                    Request(
                        gateway.url + web.INDEX,
                        headers={"Origin": "http://foreign.invalid"},
                    ),
                    timeout=5,
                )
            self.assertEqual(refusal.exception.code, 401)
            for route in (
                "/",
                "/oc-box/sessions",
                "/api/session/active",
                "/server/example/session/ses_one",
            ):
                with opener.open(gateway.url + route, timeout=5) as response:
                    self.assertEqual(response.read(), b"native-unchanged")
            with patch.object(
                service, "web_session_catalog", side_effect=ValueError("private")
            ):
                with self.assertRaises(HTTPError) as refusal:
                    opener.open(gateway.url + web.INDEX, timeout=5)
                self.assertEqual(refusal.exception.code, 503)
                body = refusal.exception.read()
                self.assertIn("text/html", refusal.exception.headers["Content-Type"])
                self.assertIn(b'href="/"', body)
                self.assertNotIn(b'href="/server/', body)
                self.assertNotIn(b"private", body)


if __name__ == "__main__":
    unittest.main()
