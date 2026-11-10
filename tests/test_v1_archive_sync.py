"""V1 schema-path archiving and verified, retryable host replacement."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box


class V1ArchiveSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-v1-archive-")
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
        self.data["opencode_version"] = "1.19.5"
        box.atomic_json(self.path, self.data)
        self.source = {
            "info": {
                "id": "ses_source",
                "directory": str(self.project),
                "title": "Saved work",
                "time": {"created": 1, "updated": 2},
                "metadata": {"user": "retained"},
            },
            "messages": [
                {
                    "info": {
                        "id": "msg_user",
                        "sessionID": "ses_source",
                        "role": "user",
                    },
                    "parts": [
                        {
                            "id": "prt_user",
                            "sessionID": "ses_source",
                            "messageID": "msg_user",
                            "type": "text",
                            "text": "before",
                        }
                    ],
                },
                {
                    "info": {
                        "id": "msg_assistant",
                        "sessionID": "ses_source",
                        "parentID": "msg_user",
                        "role": "assistant",
                    },
                    "parts": [
                        {
                            "id": "prt_tool",
                            "sessionID": "ses_source",
                            "messageID": "msg_assistant",
                            "type": "tool",
                            "tool": "fixture",
                            "state": {
                                "status": "completed",
                                "input": {},
                                "output": "saved",
                            },
                        }
                    ],
                },
            ],
        }

    def result(self, value=None, *, code=0, stderr=""):
        return subprocess.CompletedProcess([], code, json.dumps(value), stderr)

    def test_only_explicit_schema_identity_paths_are_remapped(self):
        opaque = {
            key: value
            for key, value in zip(
                ("id", "sessionID", "messageID", "parentID", "partID"),
                ("msg_user", "ses_source", "msg_assistant", "msg_user", "prt_tool"),
            )
        }
        opaque["nested"] = [
            {"sessionID": "ses_source"},
            {"metadata": {"id": "msg_user"}},
        ]
        self.source["info"]["metadata"] = copy.deepcopy(opaque)
        self.source["info"]["revert"] = {
            "messageID": "msg_user",
            "partID": "prt_user",
            "diff": "msg_user",
        }
        for message in self.source["messages"]:
            message["info"]["metadata"] = copy.deepcopy(opaque)
            for part in message["parts"]:
                part["metadata"] = copy.deepcopy(opaque)
        state = self.source["messages"][1]["parts"][0]["state"]
        state.update(
            input=copy.deepcopy(opaque),
            metadata=copy.deepcopy(opaque),
            output=json.dumps(opaque),
        )
        self.source["messages"][0]["parts"].append(
            {
                "id": "prt_subtask",
                "sessionID": "ses_source",
                "messageID": "msg_user",
                "type": "subtask",
                "prompt": "unchanged",
                "description": "fixture",
                "agent": "build",
                "metadata": copy.deepcopy(opaque),
            }
        )
        before = copy.deepcopy(self.source)
        archived = box.archive_session(self.source, self.data)
        remap = lambda ident, kind: box.archive_id(self.data["id"], ident, kind)
        self.assertEqual(self.source, before)
        self.assertEqual(archived["info"]["metadata"], opaque)
        self.assertEqual(
            archived["info"]["revert"],
            {
                "messageID": remap("msg_user", "msg"),
                "partID": remap("prt_user", "prt"),
                "diff": "msg_user",
            },
        )
        for old, new in zip(before["messages"], archived["messages"]):
            self.assertEqual(new["info"]["id"], remap(old["info"]["id"], "msg"))
            self.assertEqual(new["info"]["sessionID"], remap("ses_source", "ses"))
            self.assertEqual(new["info"]["metadata"], opaque)
            if "parentID" in old["info"]:
                self.assertEqual(
                    new["info"]["parentID"], remap(old["info"]["parentID"], "msg")
                )
            for old_part, new_part in zip(old["parts"], new["parts"]):
                expected = copy.deepcopy(old_part)
                expected.update(
                    id=remap(old_part["id"], "prt"),
                    sessionID=remap("ses_source", "ses"),
                    messageID=remap(old_part["messageID"], "msg"),
                )
                self.assertEqual(new_part, expected)

    def sync_fixture(self):
        """Independent mutable host view, including successful no-op/error modes."""
        archived = box.archive_session(self.source, self.data)
        ident = archived["info"]["id"]
        self.target = self.path.parent / "history" / (ident + ".json")
        box.atomic_json(self.target, archived)
        state = {
            "sessions": ["ses_source"],
            "last": "ses_source",
            "host_sessions": {"ses_source": ident},
            "synced": {
                "ses_source": hashlib.sha256(self.target.read_bytes()).hexdigest()
            },
        }
        box.atomic_json(box.session_file(self.path), state)
        host = copy.deepcopy(archived)
        host["info"]["directory"] = str(self.path.parent / "controller")
        self.host = host
        self.ident = ident
        self.journal = self.path.parent / "v1-replacements" / (ident + ".json")
        self.journal.unlink(missing_ok=True)
        self.mode = "success"
        self.calls = []
        self.source["messages"][0]["parts"][0]["text"] += "\nnew progress"
        self.source["info"]["title"] += " updated"

    def native(self, command, **kwargs):
        self.calls.append(command)
        if "list" in command:
            self.fail("V1 roots-only inventory cannot authorize archive absence")
        if "delete" in command:
            if self.mode == "delete refused" or self.host is None:
                return self.result(code=1, stderr="synthetic-private-output")
            if self.mode != "delete noop":
                self.host = None
            return self.result()
        if "import" in command:
            if self.mode == "import failed":
                raise box.BoxError("synthetic import failure")
            if self.host is None and self.mode != "import noop":
                self.host = box.read_json(Path(command[-1]))
                self.host["info"]["directory"] = str(self.path.parent / "controller")
                if self.mode == "metadata changed":
                    self.host["info"]["metadata"] = {"user": "changed"}
                elif self.mode == "title changed":
                    self.host["info"]["title"] = "stale"
            return self.result()
        if "export" in command:
            return self.result(self.host)
        self.fail("unexpected transport command")

    def sync(self):
        with (
            patch.object(
                box,
                "list_container_sessions",
                return_value=[{"id": "ses_source", "directory": str(self.project)}],
            ),
            patch.object(
                box,
                "export_container_session",
                side_effect=lambda *_: copy.deepcopy(self.source),
            ),
            patch.object(box, "run", side_effect=self.native),
            patch("sys.stdout", io.StringIO()),
        ):
            box.sync_sessions(self.path, self.data)

    def test_failed_or_noop_delete_and_import_never_advance_checkpoint_or_archive(self):
        for mode in (
            "delete refused",
            "delete noop",
            "import noop",
            "metadata changed",
            "title changed",
            "unverified absent",
        ):
            with self.subTest(mode=mode):
                self.source["messages"][0]["parts"][0]["text"] = "before"
                self.source["info"]["title"] = "Saved work"
                self.sync_fixture()
                before_state = box.session_file(self.path).read_bytes()
                before_archive = self.target.read_bytes()
                self.mode = mode
                if mode == "unverified absent":
                    self.host = None
                with self.assertRaises((box.BoxError, ValueError)) as raised:
                    self.sync()
                self.assertNotIn("synthetic-private-output", str(raised.exception))
                self.assertEqual(box.session_file(self.path).read_bytes(), before_state)
                self.assertEqual(self.target.read_bytes(), before_archive)
                if mode in ("delete refused", "unverified absent"):
                    self.assertFalse(any("import" in call for call in self.calls))
                    self.assertFalse(self.journal.exists())

    def test_successful_retry_after_delete_and_failed_import_verifies_before_checkpoint(
        self,
    ):
        self.sync_fixture()
        before = box.session_file(self.path).read_bytes()
        archive_before = self.target.read_bytes()
        self.mode = "import failed"
        with self.assertRaises(box.BoxError):
            self.sync()
        self.assertIsNone(self.host)
        self.assertEqual(box.session_file(self.path).read_bytes(), before)
        self.assertEqual(self.target.read_bytes(), archive_before)
        record = box.read_json(self.journal)
        self.assertEqual(record["workspace"], self.data["id"])
        self.assertEqual(record["source"], "ses_source")
        self.assertEqual(record["archive"], self.ident)
        self.assertNotEqual(
            record["digest"], hashlib.sha256(archive_before).hexdigest()
        )
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.journal.parent.stat().st_mode & 0o777, 0o700)
        self.mode = "success"
        self.calls.clear()
        self.sync()
        self.assertFalse(any("delete" in command for command in self.calls))
        self.assertTrue(any("export" in command for command in self.calls))
        self.assertNotEqual(box.session_file(self.path).read_bytes(), before)
        self.assertEqual(
            self.host["messages"],
            box.archive_session(self.source, self.data)["messages"],
        )
        self.assertEqual(box.read_json(self.target)["messages"], self.host["messages"])
        self.assertFalse(self.journal.exists())
        self.calls.clear()
        self.sync()
        self.assertTrue(any("export" in command for command in self.calls))
        self.assertFalse(
            any("import" in command or "delete" in command for command in self.calls)
        )

    def test_v1_cached_checkpoint_does_not_hide_unverified_old_host_content(self):
        unchanged = copy.deepcopy(self.source)
        self.sync_fixture()
        self.source = unchanged
        self.host["messages"][0]["parts"][0]["text"] = "stale host content"
        checkpoint = box.session_file(self.path).read_bytes()
        archived = self.target.read_bytes()
        with self.assertRaisesRegex(box.BoxError, "Host archive verification failed"):
            self.sync()
        self.assertFalse(
            any("import" in command or "delete" in command for command in self.calls)
        )
        self.assertEqual(box.session_file(self.path).read_bytes(), checkpoint)
        self.assertEqual(self.target.read_bytes(), archived)

    def test_restored_namespace_uses_verified_initial_import_not_absent_delete(self):
        self.sync_fixture()
        state = box.session_state(self.path)
        state["host_sessions"]["ses_source"] = "ses_previous_workspace_archive"
        box.atomic_json(box.session_file(self.path), state)
        self.host = None
        self.sync()
        self.assertFalse(any("delete" in command for command in self.calls))
        self.assertTrue(any("import" in command for command in self.calls))
        self.assertEqual(
            box.session_state(self.path)["host_sessions"]["ses_source"], self.ident
        )

    def cached_archive_fixture(self, damage):
        unchanged = copy.deepcopy(self.source)
        self.sync_fixture()
        self.source = unchanged
        archived = self.target.read_bytes()
        checkpoint = box.session_file(self.path).read_bytes()
        if damage == "missing":
            self.target.unlink()
        else:
            self.target.write_bytes(b"corrupt private recovery copy")
        return archived, checkpoint

    def test_cached_success_restores_missing_and_corrupt_private_archives(self):
        for damage in ("missing", "corrupt"):
            for receipt in (False, True):
                with self.subTest(target=damage, receipt=receipt):
                    archived, checkpoint = self.cached_archive_fixture(damage)
                    source_before = copy.deepcopy(self.source)
                    host_before = copy.deepcopy(self.host)
                    associations = copy.deepcopy(
                        box.session_state(self.path)["host_sessions"]
                    )
                    if receipt:
                        box.atomic_json(
                            self.journal,
                            {
                                "version": 1,
                                "workspace": self.data["id"],
                                "source": "ses_source",
                                "archive": self.ident,
                                "digest": hashlib.sha256(archived).hexdigest(),
                            },
                        )
                    self.sync()
                    self.assertEqual(self.target.read_bytes(), archived)
                    self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
                    self.assertTrue(any("export" in command for command in self.calls))
                    self.assertFalse(
                        any(
                            "import" in command or "delete" in command
                            for command in self.calls
                        )
                    )
                    self.assertEqual(
                        box.session_file(self.path).read_bytes(), checkpoint
                    )
                    self.assertEqual(
                        box.session_state(self.path)["host_sessions"], associations
                    )
                    self.assertEqual(self.source, source_before)
                    self.assertEqual(self.host, host_before)
                    self.assertFalse(self.journal.exists())
                    self.assertEqual(
                        list(self.target.parent.glob(".pending-*.json")), []
                    )

    def test_cached_recovery_copy_is_not_replaced_after_host_or_journal_refusal(self):
        for damage in ("missing", "corrupt"):
            for refusal in ("host", "journal"):
                with self.subTest(target=damage, refusal=refusal):
                    _, checkpoint = self.cached_archive_fixture(damage)
                    if refusal == "host":
                        self.host["messages"][0]["parts"][0]["text"] = (
                            "unexpected host content"
                        )
                    else:
                        box.atomic_json(self.journal, {"invalid": "recovery record"})
                    target_before = (
                        self.target.read_bytes() if self.target.exists() else None
                    )
                    host_before = copy.deepcopy(self.host)
                    source_before = copy.deepcopy(self.source)
                    with self.assertRaises(box.BoxError):
                        self.sync()
                    self.assertEqual(
                        self.target.read_bytes() if self.target.exists() else None,
                        target_before,
                    )
                    self.assertEqual(
                        box.session_file(self.path).read_bytes(), checkpoint
                    )
                    self.assertEqual(self.host, host_before)
                    self.assertEqual(self.source, source_before)
                    self.assertFalse(
                        any(
                            "import" in command or "delete" in command
                            for command in self.calls
                        )
                    )
                    if refusal == "journal":
                        self.assertEqual(
                            box.read_json(self.journal), {"invalid": "recovery record"}
                        )

    def test_unmatched_or_corrupt_replacement_evidence_never_authorizes_retry(self):
        for field in (
            "workspace",
            "source",
            "archive",
            "digest",
            "version",
            "unexpected",
            "invalid JSON",
            "changed export",
        ):
            with self.subTest(field=field):
                self.sync_fixture()
                self.mode = "import failed"
                with self.assertRaises(box.BoxError):
                    self.sync()
                record = box.read_json(self.journal)
                if field == "invalid JSON":
                    self.journal.write_text("private malformed evidence")
                elif field == "version":
                    record["version"] = True
                    box.atomic_json(self.journal, record)
                elif field == "changed export":
                    self.source["messages"][0]["parts"][0]["text"] += "more progress"
                else:
                    record[field] = "different synthetic value"
                    box.atomic_json(self.journal, record)
                before = self.journal.read_bytes()
                checkpoint = box.session_file(self.path).read_bytes()
                archived = self.target.read_bytes()
                self.mode = "success"
                self.calls.clear()
                with self.assertRaisesRegex(box.BoxError, "recovery does not match"):
                    self.sync()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.journal.read_bytes(), before)
                self.assertEqual(box.session_file(self.path).read_bytes(), checkpoint)
                self.assertEqual(self.target.read_bytes(), archived)

    def test_delete_before_record_failure_remains_pending_without_guessed_absence(self):
        self.sync_fixture()
        checkpoint = box.session_file(self.path).read_bytes()
        archived = self.target.read_bytes()
        atomic = box.atomic_json

        def write(path, value):
            if path == self.journal:
                raise OSError("private synthetic failure")
            return atomic(path, value)

        with patch.object(box, "atomic_json", side_effect=write):
            with self.assertRaisesRegex(
                box.BoxError, "recovery record could not be saved"
            ):
                self.sync()
        self.assertIsNone(self.host)
        self.assertFalse(self.journal.exists())
        self.assertFalse(any("import" in command for command in self.calls))
        self.calls.clear()
        with self.assertRaisesRegex(box.BoxError, "deletion was not verified"):
            self.sync()
        self.assertFalse(any("import" in command for command in self.calls))
        self.assertEqual(box.session_file(self.path).read_bytes(), checkpoint)
        self.assertEqual(self.target.read_bytes(), archived)

    def test_initial_import_success_noop_is_not_accepted_without_valid_reexport(self):
        self.sync_fixture()
        self.host = None
        box.atomic_json(
            box.session_file(self.path),
            {"last": "ses_source", "sessions": ["ses_source"]},
        )
        self.mode = "import noop"
        before = self.target.read_bytes()
        with self.assertRaises((box.BoxError, ValueError)):
            self.sync()
        self.assertNotIn("synced", box.session_state(self.path))
        self.assertEqual(self.target.read_bytes(), before)

    def test_verified_archive_accepts_only_existing_v1_raw_normalization(self):
        archived = box.archive_session(self.source, self.data)
        archived["messages"][1]["parts"][0]["state"].update(
            raw="obsolete", metadata={"raw": "keep"}, input={"raw": "keep"}
        )
        for field in ("obsolete raw", "input", "metadata", "output", "pending raw"):
            with self.subTest(field=field):
                original = copy.deepcopy(archived)
                imported = copy.deepcopy(original)
                imported["info"]["directory"] = str(self.path.parent / "controller")
                state = imported["messages"][1]["parts"][0]["state"]
                if field in ("obsolete raw", "pending raw"):
                    del state["raw"]
                    if field == "pending raw":
                        original["messages"][1]["parts"][0]["state"]["status"] = (
                            "pending"
                        )
                        state["status"] = "pending"
                else:
                    state[field] = "changed"
                with patch.object(box, "run", return_value=self.result(imported)):
                    if field == "obsolete raw":
                        box.verify_host_archive(self.path, self.data, original)
                    else:
                        with self.assertRaises(box.BoxError):
                            box.verify_host_archive(self.path, self.data, original)

    def test_v1_archive_info_contract_preserves_fields_but_accepts_controller_binding(
        self,
    ):
        source = copy.deepcopy(self.source)
        source["info"].update(
            version="1.19.5",
            slug="native-slug",
            summary={"additions": 1},
            cost=1.5,
            tokens={"input": 2},
            share={"url": "https://synthetic.invalid/"},
            revert={"messageID": "msg_user", "partID": "prt_user"},
            time={"created": 1, "updated": 2, "archived": 3, "compacting": 4},
        )
        archived = box.archive_session(source, self.data)
        bound = copy.deepcopy(archived)
        bound["info"].update(
            directory=str(self.path.parent / "controller"),
            projectID="global",
            path="native-derived",
        )
        with patch.object(box, "run", return_value=self.result(bound)):
            box.verify_host_archive(self.path, self.data, archived)
        for field in (
            "title",
            "version",
            "slug",
            "metadata",
            "summary",
            "cost",
            "tokens",
            "share",
            "revert",
            "time",
            "parentID",
            "permission",
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(bound)
                changed["info"][field] = "unexpected"
                with patch.object(box, "run", return_value=self.result(changed)):
                    with self.assertRaisesRegex(
                        box.BoxError, "Host archive verification failed"
                    ):
                        box.verify_host_archive(self.path, self.data, archived)
