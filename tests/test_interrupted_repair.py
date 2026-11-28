import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from oc_box import interrupted_repair as repair, oc_box as box, opencode_v2_io as bridge
from oc_box.web_named_native_profile import named_native_worker_source
from oc_box import interrupted_repair_observer as observer


def candidate():
    return {
        **dict.fromkeys(repair.FLAGS, False),
        **dict.fromkeys(repair.COUNTS, 0),
        "session": "ses_child",
        "parent": "ses_parent",
        "agent": "general",
        "message": "msg_original",
        "tail_message": "msg_original",
        "last_kind": "assistant",
        "unfinished": True,
        "claim": True,
        "busy": True,
        "job_count": 1,
        "job": "msg_job",
        "job_status": "running",
        "job_kind": "subagent",
    }


class GuidedRepairTest(unittest.TestCase):
    def test_scoped_metadata_distinguishes_pending_removed_job_and_settled_original(
        self,
    ):
        parent = "ses_" + "1" * 26
        child = "ses_" + "2" * 26
        original = "msg_" + "3" * 26
        job = "msg_" + "4" * 26
        idle = "msg_" + "5" * 26
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            for table, by_type in observer._SCHEMA.items():
                primary = "notification_id" if table == "job" else "id"
                columns = [
                    f"{name} {kind}" + (" PRIMARY KEY" if name == primary else "")
                    for kind, names in by_type.items()
                    for name in names.split()
                ]
                db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
            observer._probe_schema(db)
            db.execute(
                "INSERT INTO session_v2 (id, project_id, directory) VALUES (?, 'p', '/demo')",
                (parent,),
            )
            db.execute(
                "INSERT INTO session_v2 (id, project_id, directory, parent_id, agent, time_suspended, time_busy) "
                "VALUES (?, 'p', '/demo', ?, 'general', 1, 1)",
                (child, parent),
            )
            db.execute(
                "INSERT INTO session_message (id, session_id, type, seq, data) VALUES (?, ?, 'assistant', 1, ?)",
                (original, child, json.dumps({"time": {"created": 1}})),
            )
            db.execute(
                "INSERT INTO job (notification_id, id, session_id, child_session_id, kind, status, recovery) "
                "VALUES (?, ?, ?, ?, 'subagent', 'running', ?)",
                (
                    job,
                    child,
                    parent,
                    child,
                    json.dumps(
                        {
                            "kind": "subagent",
                            "parentSessionID": parent,
                            "childSessionID": child,
                            "agent": "general",
                        }
                    ),
                ),
            )
            selection = [{"session": child, "message": original}]

            def rows(targets):
                db.set_authorizer(observer._authorizer(sqlite3))
                try:
                    return observer._rows(db, Path("/demo"), targets)
                finally:
                    db.set_authorizer(None)

            self.assertTrue(repair._candidate(rows(None)[0]))
            db.execute("DELETE FROM job")
            self.assertFalse(rows(None))
            self.assertTrue(repair._no_job(rows(selection)[0]))
            db.execute(
                "UPDATE session_message SET data = ? WHERE id = ?",
                (json.dumps({"time": {"created": 1, "completed": 2}}), original),
            )
            db.execute(
                "INSERT INTO session_message (id, session_id, type, seq, data) VALUES (?, ?, 'idle', 2, '{}')",
                (idle, child),
            )
            db.execute(
                "UPDATE session_v2 SET time_suspended = NULL, time_idle = 2 WHERE id = ?",
                (child,),
            )
            after = rows(selection)[0]
            self.assertEqual(
                (after["message"], after["tail_message"]), (original, idle)
            )
            self.assertTrue(after["original_completed"])
            self.assertFalse(after["claim"])

    def test_bad_flag_is_refused_before_journal_or_dispatch(self):
        for command in ("purge", "credentials", "tui"):
            with (
                self.subTest(command=command),
                patch.object(sys, "argv", ["oc-box", command, "--repair-int"]),
                patch.object(box, "main") as main,
                patch.object(box.lifecycle_output, "console_event") as journal,
                contextlib.redirect_stderr(StringIO()),
            ):
                self.assertEqual(box.entrypoint(), 1)
                main.assert_not_called()
                journal.assert_not_called()

    def test_hint_requires_the_original_stopped_container_and_mounts(self):
        ident = "a" * 64
        data = {"project": "/demo", "home": "/home/demo", "home_volume": "home-demo"}
        stopped = {
            "Id": ident,
            "State": {"Running": False, "Pid": 0, "Status": "exited"},
            "HostConfig": {},
            "Mounts": [
                {"Type": "bind", "Source": "/demo", "Destination": "/demo"},
                {
                    "Type": "volume",
                    "Source": "/private",
                    "Destination": "/home/demo",
                    "Name": "home-demo",
                },
            ],
        }
        cases = (
            (stopped, True),
            (None, False),
            ({**stopped, "Id": "b" * 64}, False),
            (
                {**stopped, "State": {"Running": True, "Pid": 42, "Status": "running"}},
                False,
            ),
            ({**stopped, "Mounts": []}, False),
        )
        for info, expected in cases:
            with (
                self.subTest(info=info),
                patch.object(box, "_inspect_sync_container", return_value=info),
            ):
                self.assertEqual(
                    box._interrupted_repair_hint_stopped(data, {"Id": ident}), expected
                )
        with patch.object(
            box, "_inspect_sync_container", side_effect=box.BoxError("private")
        ):
            self.assertFalse(box._interrupted_repair_hint_stopped(data, {"Id": ident}))

    def test_worker_bounds_private_outputs(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                command = [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    f"import sys; sys.stdin.read(); sys.{stream}.write('x' * {repair.WORKER_LIMIT + 1})",
                ]
                with self.assertRaisesRegex(repair.Unverified, "too large"):
                    repair._worker_reply(command, {}, {})

    def test_isolated_bridge_checks_pending_and_settled_transcripts(self):
        row = candidate()
        with tempfile.TemporaryDirectory() as directory:
            requests = []
            message = {"id": row["message"], "type": "assistant", "time": {}}
            sessions = {
                row["parent"]: {
                    "id": row["parent"],
                    "location": {"directory": directory},
                    "time": {"updated": 1},
                },
                row["session"]: {
                    "id": row["session"],
                    "parentID": row["parent"],
                    "agent": row["agent"],
                    "location": {"directory": directory},
                    "time": {"updated": 1},
                },
            }

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    requests.append(self.path)
                    if self.path == "/api/info":
                        value = {"pid": 42, "version": "2.4.2", "urls": [url]}
                    elif self.path == "/api/session/active":
                        value = {"data": {}}
                    elif "/message?" in self.path:
                        value = {"data": [message], "cursor": {}}
                    elif self.path.endswith("/export"):
                        value = {
                            "data": {
                                "info": sessions[row["session"]],
                                "messages": [message],
                            }
                        }
                    else:
                        value = {"data": sessions[self.path.rsplit("/", 1)[-1]]}
                    wire = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(wire)))
                    self.end_headers()
                    self.wfile.write(wire)

            with HTTPServer(("127.0.0.1", 0), Handler) as server:
                url = f"http://127.0.0.1:{server.server_port}"
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    source = named_native_worker_source(
                        Path(bridge.__file__).read_text(encoding="utf-8")
                    )
                    for expect in ("tail", "settled"):
                        if expect == "settled":
                            message["time"]["completed"] = 2
                        payload = {
                            "endpoint": {"url": url, "password": "synthetic"},
                            "directory": directory,
                            "expect": expect,
                            "target": {
                                key: row[key]
                                for key in ("session", "parent", "agent", "message")
                            },
                        }
                        result = subprocess.run(
                            [
                                sys.executable,
                                "-I",
                                "-S",
                                "-B",
                                "-c",
                                source,
                                "interrupted-repair-probe",
                                "/synthetic/opencode",
                            ],
                            input=json.dumps(payload),
                            cwd=directory,
                            text=True,
                            capture_output=True,
                            timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            json.loads(result.stdout),
                            {"pid": 42, "active": [], "check": "verified"},
                        )
                finally:
                    server.shutdown()
                    thread.join(3)
            self.assertEqual(sum(route.endswith("/export") for route in requests), 1)

    def test_parser_dispatch_is_early_and_only_for_sync_selectors(self):
        prefix = ["sync", "--repair-interrupted"]
        with patch.object(repair, "guide", return_value=7) as guide:
            self.assertEqual(box.main([*prefix, "--workspace", "a" * 32]), 7)
            self.assertEqual(guide.call_count, 1)
            for args in (
                [*prefix, "--persisted"],
                [*prefix, "--memory", "2g"],
                ["start", "--repair-interrupted"],
            ):
                with (
                    self.subTest(args=args),
                    self.assertRaisesRegex(box.BoxError, "workspace selectors"),
                ):
                    box.main(args)
            self.assertEqual(guide.call_count, 1)

    def test_verification_rejects_replaced_job_or_tail(self):
        reviewed = candidate()
        owner = repair.Owner.__new__(repair.Owner)
        owner.recheck = Mock()
        owner.probe = Mock(return_value=(set(), "verified"))
        cancelled = {
            **reviewed,
            "job": None,
            "job_status": None,
            "job_kind": None,
            "job_count": 0,
        }
        settled = {
            **cancelled,
            "last_kind": "idle",
            "tail_message": "msg_idle",
            "unfinished": False,
            "original_completed": True,
            "claim": False,
            "busy": False,
        }
        cases = (
            ("reviewed", reviewed, True),
            ("reviewed", {**reviewed, "job": "msg_next"}, False),
            ("cancelled", {**cancelled, "tail_message": "msg_other"}, False),
            ("cancelled", {**cancelled, "child_count": 1}, False),
            ("cancelled", cancelled, True),
            ("settled", {**settled, "original_completed": False}, False),
            ("settled", settled, True),
        )
        for stage, observed, accepted in cases:
            with self.subTest(stage=stage, observed=observed):
                owner.observe = Mock(return_value={reviewed["session"]: observed})
                if accepted:
                    owner.check({}, reviewed, stage)
                else:
                    with self.assertRaises(repair.Unverified):
                        owner.check({}, reviewed, stage)

    def test_only_confirmed_child_opens_parent_and_only_verified_cancel_shows_followup(
        self,
    ):
        row = candidate()
        owner, browser = Mock(), Mock()
        owner.pinned = {"id": "test"}
        owner.container = "a" * 64
        owner.environment = {}
        browser.return_value = contextlib.nullcontext()
        for responses, verification, opened in (
            ([""], [], False),
            ([row["session"], ""], ["reviewed", "reviewed"], True),
            (
                [row["session"], "check", "check"],
                ["reviewed", "reviewed", "cancelled", "settled"],
                True,
            ),
        ):
            with self.subTest(responses=responses):
                owner.reset_mock()
                browser.reset_mock()
                output = StringIO()
                with (
                    patch.object(repair, "_prompt", side_effect=responses),
                    patch.object(repair.workspace_service, "open_web_session", browser),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(
                        repair._guide_child(owner, {}, row), "settled" in verification
                    )
                self.assertEqual(browser.called, opened)
                self.assertEqual(
                    [call.args[-1] for call in owner.check.call_args_list], verification
                )
                self.assertEqual(
                    "Saved job removal verified." in output.getvalue(),
                    "cancelled" in verification,
                )
                if opened:
                    self.assertEqual(
                        browser.call_args.args[-1].web_session, row["parent"]
                    )

    def test_hint_is_pending_only_and_never_appears_for_uncertain_cleanup(self):
        for reason, recovery, expected in (
            ("pending", False, True),
            ("pending", True, False),
            ("cleanup-unverified", False, False),
        ):
            with self.subTest(reason=reason, recovery=recovery):
                report = box.history_recovery.Report(
                    manual=True,
                    recovery_attempted=recovery,
                    reason=reason,
                    pending_sessions=1,
                    review_interrupted=True,
                )
                with (
                    patch.object(box.lifecycle_output, "concise", return_value=True),
                    patch.object(box.lifecycle_output, "record"),
                    patch.object(box.startup_output, "line") as line,
                ):
                    box._report_history_result(report, recovered=False, error=None)
                self.assertEqual(
                    "sync --repair-interrupted" in line.call_args.args[0], expected
                )

    def test_probe_is_get_only_and_rejects_an_active_parent(self):
        row = candidate()
        settings = {
            "endpoint": {"url": "http://127.0.0.1:12345", "password": "synthetic"},
            "directory": "/demo",
            "target": {
                key: row[key] for key in ("session", "parent", "agent", "message")
            },
            "expect": "tail",
        }
        info = {"version": "2.4.2", "pid": 42, "urls": [settings["endpoint"]["url"]]}
        mocked_request = Mock(
            side_effect=[info, {"data": {row["parent"]: {"type": "running"}}}]
        )
        with (
            patch.object(bridge.os, "getcwd", return_value="/demo"),
            patch.object(bridge, "endpoint_request", return_value=mocked_request),
        ):
            self.assertEqual(
                bridge.interrupted_repair_probe(settings)["check"], "active"
            )
        self.assertEqual(
            [call.args for call in mocked_request.call_args_list],
            [
                ("GET", "/api/info"),
                ("GET", "/api/session/active"),
            ],
        )
