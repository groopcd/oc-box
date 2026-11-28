"""Multi-session discovery, archive isolation and source-deletion regressions."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    lifecycle_output,
    oc_box as box,
    opencode_v2_io as v2,
    workspace_service,
)


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

    def test_archive_checksum_preserves_json_bytes_and_rejects_an_unsafe_copy(self):
        private = self.root / "archive.json"
        value = {"text": "línea 🧪\r\n", "parts": [{"ok": True, "count": 1}]}
        expected = (json.dumps(value, indent=2) + "\n").encode()
        checksum = hashlib.sha256(expected).hexdigest()
        with patch.object(box.os, "fsync", wraps=os.fsync) as sync:
            self.assertEqual(
                box.atomic_json(private, value, durable=True, checksum=True), checksum
            )
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(private.read_bytes(), expected)
        self.assertTrue(
            box._archive_matches_checkpoint(private, checksum, len(expected))
        )
        alias = self.root / "alias.json"
        alias.symlink_to(private)
        self.assertFalse(
            box._archive_matches_checkpoint(alias, checksum, len(expected))
        )
        self.assertFalse(
            box._archive_matches_checkpoint(private, "0" * 64, len(expected))
        )

    def test_history_archive_reuses_byte_identical_copies_and_bounds_buffering(self):
        private = self.root / "archive.json"
        original = {"text": "línea \\ 🧪\n", "messages": [{"text": "." * 70000}]}
        changed = {**original, "extra": ["other", True, None, 1.5]}
        expected = (json.dumps(original, indent=2) + "\n").encode()
        next_expected = (json.dumps(changed, indent=2) + "\n").encode()
        checksum = hashlib.sha256(expected).hexdigest()
        next_checksum = hashlib.sha256(next_expected).hexdigest()
        for cap, changed_encodings in ((1024 * 1024, 1), (1024, 2)):
            with (
                self.subTest(cap=cap),
                patch.object(box, "_HISTORY_ARCHIVE_BUFFER", cap),
                patch.object(
                    box, "_json_byte_chunks", wraps=box._json_byte_chunks
                ) as encode,
            ):
                box.atomic_json(private, original, checksum=True)
                previous_inode = private.stat().st_ino
                encode.reset_mock()
                self.assertEqual(
                    box._write_history_archive(private, original, checksum), checksum
                )
                self.assertEqual(private.stat().st_ino, previous_inode)
                self.assertEqual(encode.call_count, 1)
                self.assertEqual(private.read_bytes(), expected)

                encode.reset_mock()
                self.assertEqual(
                    box._write_history_archive(private, changed, checksum),
                    next_checksum,
                )
                self.assertEqual(encode.call_count, changed_encodings)
                self.assertEqual(private.read_bytes(), next_expected)

                # Even same-size corruption needs repair; the previously
                # verified host checkpoint is still the freshly computed hash.
                private.write_bytes(b"!" + next_expected[1:])
                self.assertEqual(
                    box._write_history_archive(private, changed, next_checksum),
                    next_checksum,
                )
                self.assertEqual(private.read_bytes(), next_expected)

    def test_sync_archive_fast_copy_preserves_native_payload_and_bytes(self):
        ident = "ses_root"
        source = {
            "info": {
                **self.session(ident, parent="ses_parent"),
                "permissions": [{"action": "*"}],
                "metadata": {
                    "oc-box.archive": {"unknown": "keep"},
                    "nested": {"x": [1]},
                },
                "fork": {
                    "sessionID": "ses_parent",
                    "boundary": {"type": "through", "messageID": "msg_first"},
                },
            },
            "messages": [
                {"id": "msg_first", "type": "user", "text": "👋"},
                {
                    "id": "msg_compact",
                    "type": "compaction",
                    "metadata": {"keepID": "msg_first", "keep": ["text"]},
                },
            ],
            "ocBoxInterrupted": {"assistant": 1},
        }
        before = copy.deepcopy(source)
        expected = box.archive_session(before, self.data)
        observed = box.archive_session(source, self.data, copy_payload=False)
        self.assertEqual(source, before)
        self.assertEqual(json.dumps(observed, indent=2), json.dumps(expected, indent=2))
        self.assertIsNot(observed["info"]["metadata"], source["info"]["metadata"])
        self.assertIsNot(
            observed["messages"][1]["metadata"], source["messages"][1]["metadata"]
        )

    def test_sync_archive_fast_copy_preserves_v1_payload_and_bytes(self):
        source = {
            "info": {
                "id": "ses_parent",
                "directory": str(self.project),
                "parentID": "ses_other",
                "permission": {"edit": "allow"},
                "title": "Original",
            },
            "messages": [
                {
                    "info": {
                        "id": "msg_first",
                        "sessionID": "ses_parent",
                        "role": "user",
                    },
                    "parts": [
                        {
                            "id": "prt_first",
                            "sessionID": "ses_parent",
                            "messageID": "msg_first",
                            "type": "text",
                            "text": "é 🧪",
                        }
                    ],
                }
            ],
        }
        before = copy.deepcopy(source)
        expected = box.archive_session(before, self.data)
        observed = box.archive_session(source, self.data, copy_payload=False)
        self.assertEqual(source, before)
        self.assertEqual(json.dumps(observed, indent=2), json.dumps(expected, indent=2))
        observed["messages"][0]["parts"][0]["text"] = "different"
        self.assertEqual(source, before)

    def test_quiet_source_refusal_reports_only_validated_reason(self):
        for reason in ("queued-work", "private-unknown-value"):
            error = box.BoxError("private-native-error")
            error.history_source_category = reason
            report = box.history_recovery.Report(manual=True, reason="sync-failed")
            with (
                lifecycle_output.invocation("sync", self.root / "state"),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                box._report_history_result(report, recovered=False, error=error)
            self.assertNotIn("private-", errors.getvalue())
            if reason == "queued-work":
                self.assertIn("(queued-work)", errors.getvalue())
                self.assertIn("standalone export was not retried", errors.getvalue())
            else:
                self.assertIn("verification did not finish", errors.getvalue())

    def test_concise_outcome_waits_for_native_verification_source_and_final_stop(self):
        cases = {
            "live": ("start", "History synced.", None),
            "live-omissions": (
                "start",
                "History synced. Native export omitted interrupted assistant messages.",
                None,
            ),
            "manual-omissions": (
                "sync",
                "History synced. Native export omitted interrupted assistant messages.",
                None,
            ),
            "empty": ("sync", "No history to sync.", None),
            "partial": (
                "sync",
                "History partially synced: some sessions were still unsettled.",
                box.HistoryPending,
            ),
            "unsettled": (
                "sync",
                "History not synced: some sessions were still unsettled.",
                box.HistoryPending,
            ),
            "pending-stop": (
                "sync",
                "History sync cleanup could not be verified.",
                box.BoxError,
            ),
            "unverified": (
                "sync",
                "History not synced: verification did not finish.",
                box.BoxError,
            ),
            "source-close": (
                "sync",
                "History sync cleanup could not be verified.",
                box.BoxError,
            ),
            "owner-close": (
                "start",
                "History sync cleanup could not be verified.",
                box.BoxError,
            ),
            "owner-exit": (
                "start",
                "History sync cleanup could not be verified.",
                None,
            ),
            "final-stop": (
                "start",
                "History sync cleanup could not be verified.",
                box.BoxError,
            ),
            "interrupt": (
                "start",
                "History not synced: the operation was interrupted.",
                KeyboardInterrupt,
            ),
        }
        matrix = [(case, False) for case in cases] + [
            (case, True)
            for case in (
                "live-omissions",
                "partial",
                "unsettled",
                "pending-stop",
                "owner-close",
                "owner-exit",
                "final-stop",
            )
        ]
        legacy_success = "Session history synced to your normal host OpenCode database."
        for case, verbose in matrix:
            command, message, error = cases[case]
            with self.subTest(case=case, diagnostic=verbose):
                events, host = [], {}
                args = SimpleNamespace(
                    command=command,
                    import_session=None,
                    _opencode_release="2.2.6",
                    resume=True,
                    new=False,
                    model=None,
                )
                good = self.session(f"ses_good_{case.replace('-', '_')}_{int(verbose)}")
                omitted = case.endswith("-omissions")
                pending = self.session("ses_pending_private")
                infos = [] if case == "empty" else [good]
                if case in ("partial", "pending-stop"):
                    infos.insert(0, pending)
                elif case == "unsettled":
                    infos = [pending]
                owner = SimpleNamespace(
                    connection={"url": "http://127.0.0.1:7777"},
                    publish=Mock(),
                    wait_clients=Mock(),
                    clients=0,
                    child=SimpleNamespace(returncode=7 if case == "owner-exit" else 0),
                )

                @contextlib.contextmanager
                def source(*_args, **_kwargs):
                    token = workspace_service.CONNECTION.set(owner.connection)
                    try:
                        yield owner
                    finally:
                        workspace_service.CONNECTION.reset(token)
                        events.append("lease closed")
                        if case in ("source-close", "owner-close"):
                            raise box.BoxError("private-lease-error")

                def export(_data, ident):
                    if ident == pending["id"]:
                        error = box.HistoryPending("private-pending-error")
                        error.history_pending_reason = "assistant-tail"
                        raise error
                    result = {"info": copy.deepcopy(good), "messages": []}
                    if omitted:
                        result[v2.INTERRUPTED] = {"assistant": 2}
                    return result

                def host_run(argv, **_kwargs):
                    if "import" in argv:
                        archived = box.read_json(Path(argv[-2]))
                        archived["info"]["location"] = {
                            "directory": str(self.path.parent / "controller")
                        }
                        host[archived["info"]["id"]] = archived
                        events.append("imported")
                    elif "export" in argv:
                        archived = copy.deepcopy(host[argv[-2]])
                        if case == "unverified":
                            archived["info"]["title"] = "private-verification-mismatch"
                        events.append("host export checked")
                        return self.result(archived)
                    else:
                        self.fail("Unexpected native command.")
                    return self.result(None)

                def stop(_data):
                    events.append("final stop")
                    self.assertIsNone(workspace_service.live_connection())
                    self.assertFalse(stdout.getvalue().startswith("History synced."))
                    self.assertNotIn(legacy_success, stdout.getvalue())
                    self.assertNotIn("Pending reasons:", stderr.getvalue())
                    if not verbose:
                        self.assertEqual(stderr.getvalue(), "")
                    if case in ("final-stop", "pending-stop"):
                        raise box.BoxError("private-stop-error")

                def record(event, **details):
                    if event == "history.result":
                        events.append(event)
                        self.assertEqual(events[-2], "final stop")

                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    lifecycle_output.invocation(
                        command, self.root / "state", verbose=verbose
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch.object(
                        lifecycle_output, "record", side_effect=record
                    ) as journal,
                    patch.object(
                        box, "loading_stage", return_value=contextlib.nullcontext()
                    ),
                    patch.object(box, "check_private_output"),
                    patch.object(box, "start_container"),
                    patch.object(box, "prepare_runtime"),
                    patch.object(box, "run_native_tui", return_value=0),
                    patch.object(workspace_service, "Owner", side_effect=source),
                    patch.object(workspace_service, "client_command", return_value=[]),
                    patch.object(
                        workspace_service, "history_source", side_effect=source
                    ),
                    patch.object(
                        workspace_service,
                        "wait_history",
                        side_effect=KeyboardInterrupt if case == "interrupt" else None,
                    ),
                    patch.object(box, "list_container_sessions", return_value=infos),
                    patch.object(box, "export_container_session", side_effect=export),
                    patch.object(box, "run", side_effect=host_run),
                    patch.object(box, "stop_container", side_effect=stop),
                ):
                    if error is None:
                        self.assertEqual(
                            box.launch_environment(self.path, self.data, args), 0
                        )
                    else:
                        with self.assertRaises(error) as raised:
                            box.launch_environment(self.path, self.data, args)
                        self.assertEqual(
                            getattr(
                                raised.exception, "history_outcome_reported", False
                            ),
                            not verbose or case in ("partial", "unsettled"),
                        )
                        if case == "pending-stop":
                            self.assertEqual(
                                str(raised.exception), "private-stop-error"
                            )
                if verbose:
                    self.assertIn("oc-box: History sync summary:", stdout.getvalue())
                    self.assertEqual(
                        stdout.getvalue().splitlines().count(legacy_success),
                        int(omitted),
                    )
                    if omitted:
                        self.assertIn("omits from its export", stdout.getvalue())
                    if case in ("partial", "unsettled"):
                        self.assertIn(
                            "Pending reasons: unfinished assistant message "
                            "at the end of the session: 1.",
                            stderr.getvalue(),
                        )
                        self.assertNotIn(pending["id"], stderr.getvalue())
                    elif case == "pending-stop":
                        self.assertIn(
                            "Inspect the workspace before retrying with 'oc-box sync'.",
                            stderr.getvalue(),
                        )
                        self.assertNotIn("Retry with 'oc-box sync'", stderr.getvalue())
                        self.assertNotIn("Pending reasons:", stderr.getvalue())
                else:
                    lines = [
                        line
                        for line in stdout.getvalue().splitlines()
                        if not line.startswith("oc-box: ")
                    ] + stderr.getvalue().splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertTrue(lines[0].startswith(message), lines)
                    self.assertNotIn("private-", lines[0])
                    self.assertNotIn("Interrupted work may be incomplete", lines[0])
                if case in ("partial", "unsettled"):
                    self.assertEqual(stderr.getvalue().count(message), 1)
                    self.assertIn("pending: 1.", stderr.getvalue())
                    self.assertIn(
                        "Sync does not settle unfinished work.", stderr.getvalue()
                    )
                    self.assertNotIn("Retry with 'oc-box sync'", stderr.getvalue())
                    self.assertNotIn("private-pending-error", stderr.getvalue())
                    self.assertEqual(
                        "--lifecycle-diagnostics" in stderr.getvalue(), not verbose
                    )
                self.assertEqual(
                    events[-3:], ["lease closed", "final stop", "history.result"]
                )
                self.assertEqual(events.count("history.result"), 1)
                for sentinel in (
                    "private-pending-error",
                    "private-lease-error",
                    "private-stop-error",
                    "private-verification-mismatch",
                    good["id"],
                    pending["id"],
                ):
                    self.assertNotIn(sentinel, repr(journal.call_args_list))
                passes = [
                    call.kwargs
                    for call in journal.call_args_list
                    if call.args == ("history.pass",)
                ]
                if case == "interrupt":
                    self.assertEqual(passes, [])
                else:
                    self.assertEqual(passes[0]["stage"], "private-endpoint")
                    self.assertEqual(
                        passes[0]["metrics"]["updated"],
                        int(case not in ("empty", "unsettled", "unverified")),
                    )
                    state = box.session_state(self.path)
                    self.assertEqual(
                        good["id"] in state.get("synced", {}),
                        bool(passes[0]["metrics"]["updated"]),
                    )
                    self.assertEqual(passes[0]["metrics"]["interrupted"], int(omitted))
                    if omitted:
                        ident = state["host_sessions"][good["id"]]
                        annotation = host[ident]["info"]["metadata"]["oc-box.archive"]
                        self.assertEqual(annotation["interrupted"], {"assistant": 2})

    def test_pre_history_stop_failure_reports_shutdown_not_history(self):
        args = SimpleNamespace(
            command="start", _opencode_release="2.2.6", import_session=None
        )
        for stop_error, diagnostic in (
            (None, False),
            (box.BoxError("private-native-stop-error"), False),
            (box.BoxError("native-stop-error"), True),
            (KeyboardInterrupt(), False),
            (SystemExit(7), False),
        ):
            startup_error = box.BoxError("actionable-startup-error")
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(stop_error=type(stop_error), diagnostic=diagnostic),
                patch.object(
                    sys,
                    "argv",
                    [
                        "oc-box",
                        "start",
                        *(["--lifecycle-diagnostics"] if diagnostic else []),
                    ],
                ),
                patch.object(
                    box,
                    "main",
                    side_effect=lambda: box.launch_environment(
                        self.path, self.data, args
                    ),
                ),
                patch.object(box, "check_private_output"),
                patch.object(box, "start_container"),
                patch.object(box, "prepare_runtime", side_effect=startup_error),
                patch.object(box, "stop_container", side_effect=stop_error) as stop,
                patch.object(box, "sync_sessions") as sync,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                if isinstance(stop_error, SystemExit):
                    with self.assertRaises(SystemExit) as raised:
                        box.entrypoint()
                    self.assertIs(raised.exception, stop_error)
                    self.assertEqual(raised.exception.code, 7)
                else:
                    self.assertEqual(
                        box.entrypoint(),
                        130 if isinstance(stop_error, KeyboardInterrupt) else 1,
                    )
            stop.assert_called_once_with(self.data)
            sync.assert_not_called()
            self.assertNotIn("History", output.getvalue() + errors.getvalue())
            self.assertNotIn("oc-box sync", errors.getvalue())
            if stop_error is None:
                self.assertIn(str(startup_error), errors.getvalue())
                self.assertNotIn("Workspace shutdown", errors.getvalue())
            else:
                notice = (
                    "oc-box: Workspace shutdown could not be verified. "
                    "Container data retained; inspect the workspace on the host."
                )
                self.assertEqual(errors.getvalue().count(notice), 1)
                self.assertNotIn("private-native-stop-error", errors.getvalue())
                if diagnostic:
                    self.assertEqual(errors.getvalue().count("native-stop-error"), 1)

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

    def test_archived_roots_remain_in_sync_inventory_but_are_not_automatic_resume_targets(
        self,
    ):
        archived = self.session("ses_archived", 100)
        archived["time"]["archived"] = 101
        available = self.session("ses_available", 2)
        child = self.session("ses_child", 200, "ses_available")
        with patch.object(
            box, "run", return_value=self.result([archived, available, child])
        ):
            state = box.refresh_sessions(self.path, self.data)
        self.assertEqual(state["last"], "ses_available")
        self.assertEqual(
            set(state["sessions"]), {"ses_archived", "ses_available", "ses_child"}
        )
        with patch.object(box, "run", return_value=self.result([archived])):
            state = box.refresh_sessions(self.path, self.data)
        self.assertIsNone(state["last"])
        self.assertEqual(state["sessions"], ["ses_archived"])
        archived["time"]["archived"] = 0
        with patch.object(box, "run", return_value=self.result([archived])):
            state = box.refresh_sessions(self.path, self.data)
        self.assertIsNone(state["last"])
        self.assertEqual(state["sessions"], ["ses_archived"])

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

        output = io.StringIO()
        with (
            patch.object(
                box, "list_container_sessions", return_value=infos
            ) as inventory,
            patch.object(
                box,
                "export_container_session",
                side_effect=lambda _, ident: copy.deepcopy(exports[ident]),
            ) as export,
            patch.object(box, "run", side_effect=host_run) as run,
            patch("sys.stdout", output),
        ):
            box.sync_sessions(self.path, self.data)
            self.assertEqual(len(imports), 2)
            box.sync_sessions(self.path, self.data)
            self.assertEqual(len(imports), 2)
            exports["ses_child"]["info"]["title"] = "Changed child"
            box.sync_sessions(self.path, self.data)
        self.assertEqual(inventory.call_count, 3)
        self.assertEqual(export.call_count, 6)
        self.assertEqual(len(imports), 3)
        summaries = [
            line
            for line in output.getvalue().splitlines()
            if line.startswith("oc-box: History sync summary:")
        ]
        self.assertEqual(len(summaries), 3)
        for summary, unchanged, updated in zip(summaries, (0, 2, 1), (2, 0, 1)):
            self.assertRegex(
                summary,
                r"^oc-box: History sync summary: source=standalone, "
                r"sessions=2, exported=2, "
                rf"unchanged={unchanged}, updated={updated}, pending=0, "
                r"elapsed=\d+\.\ds\.$",
            )
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

    def test_quiet_export_wire_keeps_only_the_fully_checked_value(self):
        message = {**self.user("msg_user"), "text": "línea 🧪"}
        unfinished = self.stale("msg_unfinished")
        ident = "ses_root"
        export = {"info": self.session(ident), "messages": [message]}
        wire = json.dumps(
            {"data": export}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        for case in (
            "direct",
            "large",
            "other-envelope-fields",
            "duplicate-keys",
            "not-utf8",
            "omitted",
            "pending",
            "changed",
        ):
            with self.subTest(case=case):
                projected = [message]
                body = wire
                if case == "other-envelope-fields":
                    body = wire[:-1] + b',"unexpected":"not passed to host"}'
                elif case == "duplicate-keys":
                    body = wire.replace(
                        b'"messages":', b'"messages":[{"ignored":1}],"messages":', 1
                    )
                elif case == "not-utf8":
                    body = wire.decode().encode("utf-16")
                elif case == "omitted":
                    projected.insert(0, unfinished)
                elif case == "pending":
                    projected.append(unfinished)
                elif case == "changed":
                    projected = [{**message, "text": "different"}]
                calls = []

                def request(method, route, *, raw=False):
                    self.assertEqual(method, "GET")
                    calls.append(route)
                    if route.endswith("/export"):
                        self.assertTrue(raw)
                        return v2._read_quiet_wire(body)
                    self.assertFalse(raw)
                    if route == "/api/session/active":
                        return {"data": {}}
                    return {"data": projected, "cursor": {}}

                cap = 1 if case == "large" else 16 * 1024 * 1024
                with patch.object(v2, "_QUIET_EXPORT_WIRE_LIMIT", cap):
                    if case in ("pending", "changed"):
                        with self.assertRaises(v2.FixedError) as refused:
                            v2._quiet_export_wire(request, ident)
                        self.assertEqual(
                            str(refused.exception),
                            v2.UNSETTLED if case == "pending" else v2.CHANGED,
                        )
                        self.assertEqual(len(calls), 1 if case == "pending" else 2)
                        continue
                    result = v2._quiet_export_wire(request, ident)
                expected = export
                if case == "omitted":
                    expected = {**export, v2.INTERRUPTED: {"assistant": 1}}
                    self.assertEqual(calls[-1], "/api/session/active")
                self.assertEqual(json.loads(result), {"data": expected})
                self.assertEqual(result == body, case == "direct")

    def test_quiet_export_envelope_cannot_skip_host_identity_validation(self):
        ident = "ses_root"
        payload = {"info": self.session(ident), "messages": [self.user("msg_user")]}
        for value in (
            payload,
            {"data": payload, "extra": True},
            {
                "data": {**payload, "info": self.session("ses_other")},
            },
        ):
            with self.subTest(value=value):
                result = self.result(value)
                result.quiet_export_envelope = True
                with (
                    patch.object(box, "run_v2_history", return_value=result),
                    self.assertRaises(box.BoxError),
                ):
                    box.export_container_session(self.data, ident)
        result = self.result({"data": payload})
        result.quiet_export_envelope = True
        with patch.object(box, "run_v2_history", return_value=result):
            self.assertEqual(box.export_container_session(self.data, ident), payload)

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

    def export_requests(self, messages, exported=None, active=None):
        """A request double for the message pages, the export and idle activity."""
        info = self.session("ses_root")
        calls = []

        def request(method, target):
            calls.append(target)
            if target.endswith("/export"):
                return {"data": {"info": info, "messages": exported or []}}
            if target == "/api/session/active":
                return {"data": active or {}}
            return {"data": messages, "cursor": {}}

        return request, calls

    def stale(self, ident, completed=None):
        time = {"created": 1}
        if completed is not None:
            time["completed"] = completed
        return {"id": ident, "type": "assistant", "time": time}

    def user(self, ident):
        return {"id": ident, "type": "user", "text": "kept", "time": {"created": 1}}

    def test_superseded_unfinished_assistants_are_omitted_and_counted(self):
        messages = [
            self.user("msg_a"),
            self.stale("msg_stale_one"),
            self.stale("msg_stale_two"),
            self.user("msg_b"),
            self.stale("msg_done", completed=5),
        ]
        expected = [messages[0], messages[3], messages[4]]
        request, calls = self.export_requests(messages, expected)
        exported = v2.checked_export(request, "ses_root")
        self.assertEqual(exported["messages"], expected)
        self.assertEqual(exported[v2.INTERRUPTED], {"assistant": 2})
        self.assertEqual(calls[-1], "/api/session/active")
        # Sessions without such messages are returned exactly as before.
        request, calls = self.export_requests([messages[0]], [messages[0]])
        self.assertNotIn(v2.INTERRUPTED, v2.checked_export(request, "ses_root"))
        self.assertNotIn("/api/session/active", calls)

    def test_unfinished_history_that_may_still_change_is_still_refused(self):
        stale = self.stale("msg_stale")
        done = self.user("msg_later")
        running = {"id": "msg_shell", "type": "shell", "status": "running"}
        cases = {
            "tail": [self.user("msg_a"), stale],
            "only unfinished": [stale],
            "unfinished then unfinished tail": [
                self.user("msg_a"),
                stale,
                self.stale("msg_tail"),
            ],
            "running shell with later settled": [running, done],
            "settled before, unfinished after the last settled": [
                stale,
                done,
                self.stale("msg_tail"),
            ],
        }
        for name, messages in cases.items():
            with self.subTest(name=name):
                request, _ = self.export_requests(messages, [])
                with self.assertRaises(v2.FixedError) as raised:
                    v2.checked_export(request, "ses_root")
                self.assertEqual(str(raised.exception), v2.UNSETTLED)
        # A session executing right now is live even when later messages exist.
        messages = [stale, done]
        request, _ = self.export_requests(
            messages, [done], {"ses_root": {"type": "running"}}
        )
        with self.assertRaises(v2.FixedError) as raised:
            v2.checked_export(request, "ses_root")
        self.assertEqual(str(raised.exception), v2.UNSETTLED)
        # A native export that disagrees with the projection stays a refusal.
        request, _ = self.export_requests(messages, [stale, done])
        with self.assertRaises(v2.FixedError) as raised:
            v2.checked_export(request, "ses_root")
        self.assertEqual(str(raised.exception), v2.CHANGED)
        request, _ = self.export_requests(messages, [done], {"ses_root": 1})
        with self.assertRaises(v2.FixedError) as raised:
            v2.checked_export(request, "ses_root")
        self.assertEqual(str(raised.exception), v2.UNSETTLED)
        request, _ = self.export_requests(messages, [done], active=None)
        bad = lambda method, target: (
            {"data": []} if target == "/api/session/active" else request(method, target)
        )
        with self.assertRaises(v2.FixedError) as raised:
            v2.checked_export(bad, "ses_root")
        self.assertEqual(str(raised.exception), v2.ACTIVITY)

    def test_only_whitelisted_constant_messages_can_be_fixed_errors(self):
        import ast

        tree = ast.parse(Path(v2.__file__).read_text())
        names = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and isinstance(node.targets[0], ast.Name)
        }
        raised = [
            node.args[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "FixedError"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ]
        self.assertTrue(raised)
        for name in raised:
            self.assertIn(names[name], v2.FIXED_MESSAGES, name)
        self.assertTrue(v2.PENDING_MESSAGES < v2.FIXED_MESSAGES)
        with self.assertRaises(AssertionError):
            v2.FixedError("native said: secret-token")
        self.assertEqual(
            v2.fixed_message("noise\n" + v2.MARKER + v2.UNSETTLED + "\n"), v2.UNSETTLED
        )
        for stderr in (
            "",
            None,
            v2.MARKER + "native said: secret-token",
            "xx" + v2.MARKER + v2.UNSETTLED,
        ):
            self.assertIsNone(v2.fixed_message(stderr))

    def test_bridge_process_exposes_only_fixed_messages(self):
        source = Path(v2.__file__).read_text()
        endpoint = {"url": "http://example.invalid:1", "password": "p"}
        for payload, code, marker in (
            ({"directory": "/x", "endpoint": endpoint}, 2, v2.ENDPOINT),
            ({}, 1, None),
        ):
            result = subprocess.run(
                ["python3", "-c", source, "sessions", "/nonexistent/opencode"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, code)
            self.assertEqual(v2.fixed_message(result.stderr), marker)
            self.assertNotIn("Traceback", result.stderr)

    def test_pending_reason_is_closed_and_paired_with_the_latest_fixed_error(self):
        fixed = v2.MARKER + v2.UNSETTLED + "\n"
        detail = v2.PENDING_REASON_MARKER + "assistant-tail\n"
        changed = v2.MARKER + v2.CHANGED + "\n"
        self.assertEqual(v2.pending_reason(fixed + detail), "assistant-tail")
        self.assertEqual(v2.pending_reason(fixed + detail + changed), "source-changed")
        for stderr in (
            fixed,
            detail,
            fixed + v2.PENDING_REASON_MARKER + "private-native-text",
            fixed + "other diagnostic\n" + detail,
            detail + fixed,
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(v2.pending_reason(stderr), "unavailable")

    def test_history_failures_show_fixed_reasons_never_private_output(self):
        def failed(stderr, code=1):
            return subprocess.CompletedProcess([], code, "", stderr)

        with patch.object(box, "run", return_value=failed(v2.MARKER + v2.UNSETTLED, 2)):
            with self.assertRaises(box.HistoryPending) as raised:
                box.run_v2_history(self.data, "export", session="ses_root")
        self.assertEqual(str(raised.exception), v2.UNSETTLED)
        with patch.object(box, "run", return_value=failed(v2.MARKER + v2.INVENTORY, 2)):
            with self.assertRaises(box.BoxError) as raised:
                box.run_v2_history(self.data, "sessions")
        self.assertNotIsInstance(raised.exception, box.HistoryPending)
        self.assertEqual(str(raised.exception), v2.INVENTORY)
        with patch.object(
            box,
            "run",
            return_value=failed(
                "provider token=secret-token\n" + v2.MARKER + "token=secret-token"
            ),
        ):
            with self.assertRaises(box.BoxError) as raised:
                box.run_v2_history(self.data, "sessions")
        self.assertEqual(str(raised.exception), "podman failed (exit 1)")
        self.assertNotIn("secret-token", str(raised.exception))
        ok = subprocess.CompletedProcess([], 0, "[]", "")
        with patch.object(box, "run", return_value=ok):
            self.assertIs(box.run_v2_history(self.data, "sessions"), ok)

    def test_boxed_bridge_contract_refusals_never_authorize_generic_automatic_retry(
        self,
    ):
        data = {**self.data, "opencode_version": "2.3.2"}
        for streaming in (False, True):
            for message in v2.FIXED_MESSAGES:
                with self.subTest(streaming=streaming, message=message):
                    result = subprocess.CompletedProcess([], 2, "", v2.MARKER + message)
                    worker = Mock()
                    worker.run.return_value = result
                    token = box.history_reader.EXPORTS.set(
                        worker if streaming else None
                    )
                    try:
                        with patch.object(box, "run", return_value=result):
                            with self.assertRaises(box.BoxError) as raised:
                                box.run_v2_history(data, "sessions")
                    finally:
                        box.history_reader.EXPORTS.reset(token)
                    self.assertEqual(str(raised.exception), message)
                    self.assertEqual(
                        box.history_recovery.exit_retry_eligible(
                            box, data, raised.exception
                        ),
                        message in v2.PENDING_MESSAGES or message == v2.NOT_READY,
                    )

    def test_successful_bridge_output_rejected_by_host_validators_never_enables_retry(
        self,
    ):
        data = {**self.data, "opencode_version": "2.3.2"}
        cases = (
            (
                [self.session("ses_root", updated="private-invalid-time")],
                lambda: box.list_container_sessions(data),
            ),
            (
                {"info": self.session("ses_other"), "messages": []},
                lambda: box.export_container_session(data, "ses_root"),
            ),
            (
                {
                    "info": self.session("ses_root"),
                    "messages": ["private-invalid-message"],
                },
                lambda: box.export_container_session(data, "ses_root"),
            ),
        )
        for value, validate in cases:
            with (
                self.subTest(value=value),
                patch.object(box, "run", return_value=self.result(value)),
            ):
                with self.assertRaises(box.BoxError) as raised:
                    validate()
            self.assertFalse(
                box.history_recovery.exit_retry_eligible(box, data, raised.exception)
            )

    def test_pending_session_does_not_block_other_sessions_and_is_reported(self):
        infos = [
            self.session("ses_first"),
            self.session("ses_stuck", 2),
            self.session("ses_last", 3),
        ]
        exports = {item["id"]: {"info": item, "messages": []} for item in infos}
        exports["ses_last"][v2.INTERRUPTED] = {"assistant": 2}
        host, imports = {}, []

        def export(_data, ident):
            if ident == "ses_stuck":
                raise box.HistoryPending(v2.UNSETTLED)
            return copy.deepcopy(exports[ident])

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
            return self.result(None)

        output = io.StringIO()
        with (
            patch.object(box, "list_container_sessions", return_value=infos),
            patch.object(box, "export_container_session", side_effect=export),
            patch.object(box, "run", side_effect=host_run),
            patch("sys.stdout", output),
        ):
            with self.assertRaises(box.BoxError) as raised:
                box.sync_sessions(self.path, self.data)
        archive = lambda ident: (
            self.path.parent
            / "history"
            / (box.archive_id(self.data["id"] + "-v2", ident, "ses") + ".json")
        )
        self.assertTrue(archive("ses_first").exists() and archive("ses_last").exists())
        self.assertFalse(archive("ses_stuck").exists())
        self.assertEqual(len(imports), 2)
        state = box.session_state(self.path)
        self.assertEqual(set(state["synced"]), {"ses_first", "ses_last"})
        message = str(raised.exception)
        self.assertIn("1 session(s) not settled yet", message)
        self.assertIn(v2.UNSETTLED, message)
        self.assertIn("other sessions were synchronized", message)
        self.assertNotIn("ses_stuck", message)
        summaries = [
            line
            for line in output.getvalue().splitlines()
            if line.startswith("oc-box: History sync summary:")
        ]
        self.assertEqual(len(summaries), 1)
        self.assertRegex(
            summaries[0],
            r"^oc-box: History sync summary: source=standalone, "
            r"sessions=3, exported=2, unchanged=0, updated=2, pending=1, "
            r"elapsed=\d+\.\ds\.$",
        )
        self.assertIn("1 session(s) contain interrupted", output.getvalue())
        self.assertLess(
            output.getvalue().index(summaries[0]),
            output.getvalue().index("1 session(s) contain interrupted"),
        )
        self.assertNotIn("Session history synced", output.getvalue())
        annotation = box.read_json(archive("ses_last"))["info"]["metadata"][
            "oc-box.archive"
        ]
        self.assertEqual(annotation["interrupted"], {"assistant": 2})
        self.assertNotIn(v2.INTERRUPTED, box.read_json(archive("ses_last")))
        self.assertNotIn(
            "oc-box.archive",
            box.read_json(archive("ses_first"))["info"].get("metadata", {}),
        )
        # A later settled retry completes normally and keeps the other archives.
        exports["ses_stuck"] = {"info": infos[1], "messages": []}
        before = archive("ses_first").read_bytes()

        def settled(_data, ident):
            return copy.deepcopy(exports[ident])

        output = io.StringIO()
        with (
            patch.object(box, "list_container_sessions", return_value=infos),
            patch.object(box, "export_container_session", side_effect=settled),
            patch.object(box, "run", side_effect=host_run),
            patch("sys.stdout", output),
        ):
            box.sync_sessions(self.path, self.data)
        self.assertEqual(len(imports), 3)
        self.assertEqual(archive("ses_first").read_bytes(), before)
        self.assertRegex(
            output.getvalue(),
            r"oc-box: History sync summary: source=standalone, "
            r"sessions=3, exported=3, unchanged=2, updated=1, pending=0, "
            r"elapsed=\d+\.\ds\.",
        )
        self.assertIn("Session history synced", output.getvalue())

    def test_admitted_quiet_read_failure_keeps_prior_archive_and_does_not_replay(self):
        sessions = ["ses_saved", "ses_failed", "ses_later"]
        before = {"info": self.session("ses_failed"), "messages": []}
        archives = self.path.parent / "history"
        failed_archive = box.archive_session(before, self.data)
        failed_path = archives / (failed_archive["info"]["id"] + ".json")
        old_digest = box.atomic_json(failed_path, failed_archive, checksum=True)
        old_bytes = failed_path.read_bytes()
        binding = box._QuietSyncBinding(
            self.data["container"], (), {"PATH": "/usr/bin"}
        )
        binding.start_verified = True
        connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:1234",
            "password": "synthetic-test-password",
        }
        for failure in ("redirect", "timeout"):
            with self.subTest(failure=failure):
                box.atomic_json(
                    box.session_file(self.path),
                    {"sessions": sessions, "synced": {"ses_failed": old_digest}},
                )
                requests = []
                source = Mock()
                source.read_command.side_effect = lambda _data, action: [
                    "quiet",
                    action,
                ]

                def run(command, **kwargs):
                    if command[:1] == ["quiet"]:
                        if command[1] == "sessions":
                            return self.result(
                                [self.session(ident) for ident in sessions]
                            )
                        ident = json.loads(kwargs["input"])["session"]
                        requests.append(ident)
                        if ident == "ses_failed":
                            if failure == "timeout":
                                raise box.BoxError(
                                    "test timeout"
                                ) from subprocess.TimeoutExpired(command, 180)
                            return subprocess.CompletedProcess(
                                command, 2, "", v2.MARKER + v2.ENDPOINT + "\n"
                            )
                        return self.result(
                            {"data": {"info": self.session(ident), "messages": []}}
                        )
                    self.assertIn("import", command)
                    return self.result(None)

                output = io.StringIO()
                token = box._QUIET_SYNC_BINDING.set(binding)
                try:
                    with (
                        patch.object(
                            workspace_service,
                            "live_connection",
                            return_value=connection,
                        ),
                        patch.object(
                            workspace_service,
                            "quiet_history_source",
                            return_value=source,
                        ),
                        patch.object(box, "run", side_effect=run),
                        patch.object(box, "exec_command") as ordinary,
                        patch.object(box, "verify_host_archive") as verify,
                        contextlib.redirect_stdout(output),
                        self.assertRaises(box.BoxError),
                    ):
                        box.sync_sessions(self.path, self.data)
                finally:
                    box._QUIET_SYNC_BINDING.reset(token)
                ordinary.assert_not_called()
                verify.assert_called_once()
                self.assertEqual(requests, sessions[:2])
                state = box.session_state(self.path)
                self.assertEqual(set(state["synced"]), set(sessions[:2]))
                self.assertEqual(state["synced"]["ses_failed"], old_digest)
                self.assertEqual(failed_path.read_bytes(), old_bytes)
                self.assertNotIn("Session history synced", output.getvalue())

    def test_manual_repeat_keeps_pending_checkpoint_and_repairs_only_local_copies(self):
        args = SimpleNamespace(
            command="sync", _opencode_release="2.4.1", import_session=None
        )
        infos = [self.session(f"ses_{name}") for name in ("first", "pending", "last")]
        exports = {
            entry["id"]: {"info": copy.deepcopy(entry), "messages": []}
            for entry in infos
        }
        host, imports = {}, []
        pending = False

        def export(_data, ident):
            if pending and ident == "ses_pending":
                raise box.HistoryPending(v2.UNSETTLED)
            return copy.deepcopy(exports[ident])

        def host_run(command, **_kwargs):
            if "import" in command:
                archived = box.read_json(Path(command[-2]))
                ident = archived["info"]["id"]
                archived["info"]["location"] = {
                    "directory": str(self.path.parent / "controller")
                }
                host[ident] = archived
                imports.append(ident)
            elif "export" in command:
                return self.result(host[command[-2]])
            elif "api" in command:
                return self.result({"data": [], "cursor": {}})
            elif "delete" in command:
                host.pop(command[-2])
            else:
                self.fail("unexpected destination operation")
            return self.result(None)

        def run(*, expected=None):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                if expected is None:
                    self.assertEqual(
                        box.launch_environment(self.path, self.data, args), 0
                    )
                else:
                    with self.assertRaises(box.HistoryPending):
                        box.launch_environment(self.path, self.data, args)
                    (line,) = stderr.getvalue().splitlines()
                    self.assertIn(expected, line)
                    self.assertIn("prior archives retained", line)
                    self.assertNotIn("retry with 'oc-box sync'", line)

        with (
            lifecycle_output.invocation("sync", self.root / "state"),
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(box, "check_private_output"),
            patch.object(box, "start_container"),
            patch.object(box, "prepare_runtime"),
            patch.object(box, "stop_container"),
            patch.object(
                workspace_service,
                "history_source",
                side_effect=lambda *_: contextlib.nullcontext(),
            ),
            patch.object(box, "list_container_sessions", return_value=infos),
            patch.object(box, "export_container_session", side_effect=export) as source,
            patch.object(box, "run", side_effect=host_run) as destination,
        ):
            run()
            old = box.session_state(self.path)
            archive = {
                name: self.path.parent / "history" / (ident + ".json")
                for name, ident in old["host_sessions"].items()
            }
            old_pending = archive["ses_pending"].read_bytes()
            pending = True
            exports["ses_last"]["info"]["title"] = "Changed without a new timestamp"
            run(expected="Newly verified: 1; already checkpointed: 1; pending: 1.")
            before_repeat = {name: path.stat().st_ino for name, path in archive.items()}
            destination.reset_mock()
            run(expected="Newly verified: 0; already checkpointed: 2; pending: 1.")
            self.assertEqual(
                {name: path.stat().st_ino for name, path in archive.items()},
                before_repeat,
            )
            # Repair an absent or corrupted private copy without replacing the
            # previously verified host archive or trusting the checkpoint alone.
            first, last = archive["ses_first"], archive["ses_last"]
            original_bytes = first.read_bytes(), last.read_bytes()
            first.unlink()
            last.write_bytes(b"X" * len(original_bytes[1]))
            run(expected="Newly verified: 0; already checkpointed: 2; pending: 1.")
            self.assertEqual((first.read_bytes(), last.read_bytes()), original_bytes)
            self.assertEqual(archive["ses_pending"].read_bytes(), old_pending)
            self.assertEqual(
                box.session_state(self.path)["synced"]["ses_pending"],
                old["synced"]["ses_pending"],
            )
            self.assertEqual(source.call_count, 12)
            destination.assert_not_called()
            self.assertEqual(len(imports), 4)

    def test_quiet_24_manual_sync_pins_proven_stopped_source_and_verifies_final_stop(
        self,
    ):
        data = {**self.data, "ready": True, "opencode_version": "2.4.1"}
        box.atomic_json(self.path, data)
        args = SimpleNamespace(
            command="sync", _opencode_release="2.4.1", import_session=None
        )
        ident = "a" * 64
        stopped = {
            "Id": ident,
            "State": {"Running": False, "Status": "exited", "Pid": 0, "ConmonPid": 0},
            "Config": {
                "Labels": {box.LABEL: data["id"]},
                "Entrypoint": ["/bin/sh"],
                "Cmd": ["-c", "exec sleep infinity"],
                "Env": ["PATH=" + box.history_reader.PATH],
            },
            "HostConfig": {"PidMode": "private", "RestartPolicy": {"Name": "no"}},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": data["project"],
                    "Destination": data["project"],
                },
                {
                    "Type": "volume",
                    "Name": data["home_volume"],
                    "Source": str(self.root / "volume"),
                    "Destination": data["home"],
                },
            ],
        }
        running = {
            **stopped,
            "State": {"Running": True, "Status": "running", "Pid": 123},
        }
        for case in (
            "name-rebound",
            "inspect-failed",
            "preparation-failed",
            "initially-running",
        ):
            with self.subTest(case=case):
                admitted = case != "initially-running"
                target = ident if admitted else data["container"]
                calls = []
                original_running = not admitted
                preparing = False
                lost_inspection = False

                def engine(command, **options):
                    nonlocal original_running, lost_inspection
                    action = tuple(command[1:])
                    calls.append(action)
                    if action[:2] == ("container", "inspect"):
                        if len(calls) == 1:  # Initial, read-only name resolution.
                            self.assertEqual(action[-1], data["container"])
                        else:
                            self.assertEqual(action[-1], target)
                        if (
                            preparing
                            and case == "inspect-failed"
                            and not lost_inspection
                        ):
                            lost_inspection = True
                            return subprocess.CompletedProcess(command, 1, "", "")
                        return self.result([running if original_running else stopped])
                    if action[0] != "start":
                        self.assertTrue(original_running)
                    if admitted:
                        self.assertEqual(options["env"], box.maintenance_host_env())
                    if action[0] == "start":
                        self.assertEqual(action[-1], ident)
                        original_running = True
                    elif action[0] == "stop":
                        self.assertEqual(action[-1], ident)
                        original_running = False
                    elif action[0] == "exec":
                        self.assertEqual(action[1], target)
                    else:
                        self.fail(f"unexpected synthetic engine action {action[0]}")
                    return self.result(None)

                def prepare(_path, supplied, _version, **_kwargs):
                    nonlocal preparing
                    preparing = True
                    self.assertEqual(supplied["container"], target)
                    # A process-local override introduced during preparation
                    # cannot send a bound command to a substitute engine.
                    if admitted:
                        with patch.dict(
                            os.environ,
                            {
                                "CONTAINER_HOST": "synthetic-other-engine",
                                "XDG_RUNTIME_DIR": str(
                                    self.root / "substitute-runtime"
                                ),
                            },
                        ):
                            command = box.exec_command(supplied, ["true"])
                        self.assertNotIn("CONTAINER_HOST", command.environment)
                        self.assertNotIn("XDG_RUNTIME_DIR", command.environment)
                        self.assertIn(ident, command)
                    if case == "preparation-failed":
                        raise box.BoxError("synthetic preparation failed")

                with (
                    lifecycle_output.invocation("sync", self.root / "state"),
                    contextlib.redirect_stdout(io.StringIO()),
                    patch.dict(
                        os.environ,
                        {
                            "OC_BOX_STATE_HOME": str(self.root / "state"),
                            "HOME": str(self.root / "host-home"),
                        },
                        clear=True,
                    ),
                    patch.object(box, "check_private_output"),
                    patch.object(box, "check_resource_controllers"),
                    patch.object(box, "prepare_runtime", side_effect=prepare),
                    patch.object(box, "run", side_effect=engine),
                    patch.object(box, "stop_container") as ordinary_stop,
                    patch.object(
                        workspace_service,
                        "history_source",
                        return_value=contextlib.nullcontext(),
                    ) as source,
                    patch.object(box, "sync_sessions") as sync,
                ):
                    if case.endswith("-failed"):
                        with self.assertRaises(box.BoxError):
                            box.launch_environment(self.path, data, args)
                    else:
                        self.assertEqual(
                            box.launch_environment(self.path, data, args), 0
                        )
                self.assertIsNone(box._QUIET_SYNC_BINDING.get())
                if case.endswith("-failed"):
                    source.assert_not_called()
                    sync.assert_not_called()
                else:
                    expected = {**data, "container": target}
                    source.assert_called_once_with(
                        box,
                        expected,
                        **({"stopped_source": True} if admitted else {}),
                    )
                    sync.assert_called_once_with(self.path, expected, announce=False)
                if admitted:
                    self.assertEqual(calls.count(("start", ident)), 1)
                    self.assertEqual(calls.count(("stop", "--time", "10", ident)), 1)
                    self.assertFalse(original_running)
                    ordinary_stop.assert_not_called()
                else:
                    ordinary_stop.assert_called_once_with(data)

    def test_persisted_options_refuse_scope_changes_before_backend(self):
        for options in (
            ["start", "--persisted"],
            ["sync", "--persisted", "--cpus", "2"],
            ["sync", "--persisted", "--pids-limit", "128"],
            ["sync", "--persisted", "--device", "/dev/example"],
        ):
            with (
                self.subTest(options=options),
                patch.object(box, "check_backend") as backend,
                self.assertRaisesRegex(box.BoxError, "--persisted applies only"),
            ):
                box.main(options)
            backend.assert_not_called()

    def test_persisted_sync_never_bypasses_legacy_replacement_enrollment(self):
        data = {
            **self.data,
            box.runtime_safeguard.entrypoint_migration.RECEIPT: {"synthetic": True},
        }
        args = SimpleNamespace(command="sync", persisted=True)
        with (
            contextlib.ExitStack() as context,
            patch.object(box.runtime_safeguard, "load", return_value=None),
            patch.object(box, "_launch_persisted_sync") as snapshot,
            patch.object(box, "detect_opencode_release") as probe,
            self.assertRaisesRegex(
                box.BoxError, "has not completed guarded runtime enrollment"
            ),
        ):
            box._locked_workspace_command(self.path, data, args, context, None)
        snapshot.assert_not_called()
        probe.assert_not_called()

    def test_other_history_refusals_still_abort_and_interrupted_count_is_validated(
        self,
    ):
        infos = [self.session("ses_first"), self.session("ses_second", 2)]
        with (
            patch.object(box, "list_container_sessions", return_value=infos),
            patch.object(
                box, "export_container_session", side_effect=box.BoxError(v2.EXPORT)
            ) as export,
        ):
            with self.assertRaises(box.BoxError) as raised:
                box.sync_sessions(self.path, self.data)
        self.assertEqual(str(raised.exception), v2.EXPORT)
        self.assertEqual(export.call_count, 1)
        source = {"info": self.session("ses_root"), "messages": []}
        remap = lambda ident, prefix: "arch_" + ident
        for value in (0, -1, True, "2", {}):
            export_value = {**source, v2.INTERRUPTED: {"assistant": value}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                box.opencode_compat.native_archive(export_value, self.data, remap)
        with self.assertRaises(ValueError):
            box.opencode_compat.native_archive(
                {**source, v2.INTERRUPTED: 3}, self.data, remap
            )


if __name__ == "__main__":
    unittest.main()
