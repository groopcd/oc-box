"""Synthetic session history only; no native shared service or host credentials."""

import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from oc_box import backup_crypto as crypto
from oc_box import session_backups as engine
from oc_box import tui_sessions


def transcript(
    ident="ses_root", *, parent=None, directory="/source", text="SYNTHETIC_SECRET"
):
    info = {
        "id": ident,
        "projectID": "global",
        "location": {"directory": directory},
        "title": "Synthetic",
        "time": {"created": 1, "updated": 2},
        "cost": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }
    if parent:
        info["parentID"] = parent
    return {
        "info": info,
        "messages": [
            {
                "id": "msg_" + ident[4:],
                "type": "user",
                "text": text,
                "time": {"created": 1},
            }
        ],
    }


def page(values, cursor=None):
    return {"data": values, "cursor": {"next": cursor, "previous": None}}


class Native:
    def __init__(self, *values):
        self.values = {value["info"]["id"]: copy.deepcopy(value) for value in values}
        self.calls, self.active, self.jobs = [], {}, []
        self.hook, self.import_hook = None, None

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.hook:
            value = self.hook(method, path, body)
            if value is not None:
                return value
        if path == "/api/session/active":
            return {"data": copy.deepcopy(self.active)}
        if path == "/api/experimental/job":
            return {"data": copy.deepcopy(self.jobs)}
        parsed = urlsplit(path)
        if parsed.path == "/api/session":
            query = parse_qs(parsed.query)
            assert (
                "directory" not in query
                and "parentID" not in query
                and "archived" not in query
            )
            return page([copy.deepcopy(item["info"]) for item in self.values.values()])
        if path == "/api/experimental/session/import":
            assert method == "POST"
            if self.import_hook:
                value = self.import_hook(body)
                if value is not None:
                    return value
            ident = body["info"]["id"]
            if ident in self.values:
                raise RuntimeError("SYNTHETIC_PRIVATE_NATIVE_ERROR")
            self.values[ident] = copy.deepcopy(
                {key: body[key] for key in ("info", "messages")}
            )
            return {"data": copy.deepcopy(body["info"])}
        if path.startswith("/api/experimental/session/") and path.endswith("/export"):
            return {"data": copy.deepcopy(self.values[unquote(path.split("/")[-2])])}
        if parsed.path.endswith("/message"):
            ident = unquote(parsed.path.split("/")[-2])
            return page(copy.deepcopy(self.values[ident]["messages"]))
        if parsed.path.startswith("/api/session/") and method == "GET":
            return {
                "data": copy.deepcopy(
                    self.values[unquote(parsed.path.split("/")[-1])]["info"]
                )
            }
        raise AssertionError("Unexpected fixture operation")

    def catalog(self, *, ownership="exclusive", managed_ids=()):
        return tui_sessions.SessionCatalog(
            self,
            "host",
            "synthetic-store",
            ownership=None if ownership is None else lambda *_: ownership,
            managed_ids=managed_ids,
        )

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


class GatesTests(unittest.TestCase):
    def test_refusals_precede_paths_tools_and_transport(self):
        with (
            patch.object(engine, "_parent") as paths,
            patch.object(engine, "_load") as load,
        ):
            for name in ("backup", "prepare_restore", "restore"):
                for flags, reason in (
                    ({"read_only": True}, "read-only"),
                    ({"demo": True}, "demo-disabled"),
                ):
                    args = {
                        "backup": (None, "ses_root", "/bad"),
                        "prepare_restore": (None, "/bad", "/target"),
                        "restore": (None, None),
                    }[name]
                    with (
                        self.subTest(name=name, flags=flags),
                        self.assertRaises(engine.SessionBackupError) as caught,
                    ):
                        getattr(engine, name)(*args, **flags)
                    self.assertEqual(caught.exception.reason, reason)
            with self.assertRaises(engine.SessionBackupError):
                engine.inspect("/bad", demo=True)
            paths.assert_not_called()
            load.assert_not_called()

    def test_flat_runtime_import(self):
        package = str(Path(engine.__file__).parent)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0,sys.argv[1]); import session_backups; "
                "assert session_backups.FORMAT == 'oc-box-session-backup'; "
                "assert 'backup_crypto' not in sys.modules",
                package,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, "Flat import failed")


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age required")
class SessionBackupsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = crypto.small(["age-keygen"])
        cls.recipient = (
            crypto.small(["age-keygen", "-y"], data=cls.private).decode().strip()
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-session-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.identity = self.root / "identity"
        self.identity.write_bytes(self.private)
        self.identity.chmod(0o600)
        self.output = self.root / "history.ocsession"
        self.source = Native(
            transcript(), transcript("ses_child", parent="ses_root", directory="/other")
        )

    def backup(self, native=None, **options):
        return engine.backup(
            (native or self.source).catalog(),
            "ses_root",
            self.output,
            recipients=[self.recipient],
            **options,
        )

    def plan(self, native, **options):
        catalog = native.catalog(**options)
        return catalog, engine.prepare_restore(
            catalog, self.output, "/target", identities=[self.identity]
        )

    def refused(self, reason, action):
        with self.assertRaises(engine.SessionBackupError) as caught:
            action()
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
        self.assertNotIn("PRIVATE_NATIVE_ERROR", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        return caught.exception

    def entries(self):
        with tarfile.open(self.output) as archive:
            return {item.name: archive.extractfile(item).read() for item in archive}

    def repack(self, entries, *, members=None):
        with (
            self.output.open("wb") as destination,
            tarfile.open(
                fileobj=destination, mode="w|", format=tarfile.USTAR_FORMAT
            ) as archive,
        ):
            for name, value in members or list(entries.items()):
                info = tarfile.TarInfo(name)
                info.mode, info.size = 0o600, len(value)
                archive.addfile(info, io.BytesIO(value))

    def altered_payload(self, alter):
        """Re-encrypt intentionally malformed authenticated synthetic history."""
        entries = self.entries()
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            root = Path(temporary)
            for name, value in entries.items():
                (root / name).write_bytes(value)
            private = crypto.load_key(root / "key.age", [self.identity])
            payload = json.loads(
                engine._decrypt(root / "sessions.age", private, engine.MAX_HISTORY)
            )
            alter(payload)
            (root / "sessions.age").unlink()
            crypto.encrypt_bytes(
                json.dumps(payload).encode(),
                self.recipient_for(private),
                root / "sessions.age",
            )
            manifest = json.loads(
                engine._decrypt(root / "manifest.age", private, engine.MAX_METADATA)
            )
            manifest["components"]["sessions.age"] = crypto.digest(
                root / "sessions.age"
            )
            (root / "manifest.age").unlink()
            crypto.encrypt_bytes(
                json.dumps(manifest).encode(),
                self.recipient_for(private),
                root / "manifest.age",
            )
            self.repack({name: (root / name).read_bytes() for name in engine.ENTRIES})

    @staticmethod
    def recipient_for(private):
        return crypto.small(["age-keygen", "-y"], data=private).decode().strip()

    def test_encrypted_tree_round_trip_and_metadata_only_inspection(self):
        before = copy.deepcopy(self.source.values)
        review = self.backup()
        self.assertEqual(review["session_count"], 2)
        self.assertEqual(review["message_count"], 2)
        self.assertEqual(set(self.entries()), set(engine.ENTRIES))
        self.assertNotIn(b"SYNTHETIC_SECRET", self.output.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_nlink, 1)
        inspection = engine.inspect(
            self.output, identities=[self.identity], read_only=True
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(inspection))
        self.assertEqual(inspection["sessions"][1]["location"]["directory"], "/other")
        target = Native(transcript("ses_existing"))
        original = copy.deepcopy(target.values)
        catalog, plan = self.plan(target)
        self.assertEqual(target.mutations(), [])
        detached = plan.review
        detached["created_ids"].clear()
        self.assertEqual(len(plan.review["created_ids"]), 2)
        result = engine.restore(catalog, plan)
        self.assertFalse(result["atomic"])
        self.assertEqual(len(result["created_ids"]), 2)
        self.assertEqual(target.values["ses_existing"], original["ses_existing"])
        root, child = [target.values[ident] for ident in result["created_ids"]]
        self.assertEqual(child["info"]["parentID"], root["info"]["id"])
        for item in (root, child):
            self.assertRegex(item["info"]["id"], r"^ses_[0-9a-f]{32}$")
            self.assertRegex(item["messages"][0]["id"], r"^msg_[0-9a-f]{32}$")
            self.assertEqual(item["info"]["permissions"], [])
            self.assertEqual(item["info"]["location"], {"directory": "/target"})
            self.assertEqual(item["messages"][0]["text"], "SYNTHETIC_SECRET")
        self.assertEqual(self.source.values, before)
        self.assertEqual(self.source.mutations(), [])
        self.refused("invalid-review", lambda: engine.restore(catalog, plan))
        self.assertEqual(set(self.root.iterdir()), {self.identity, self.output})

    def test_unknown_ownership_activity_and_invalid_inventories_refuse(self):
        cases = (
            (None, {}, [], "ownership-unknown"),
            ("exclusive", {"ses_root": {"type": "running"}}, [], "session-active"),
            (
                "exclusive",
                {},
                [
                    {
                        "id": "job_fixture",
                        "sessionID": "ses_root",
                        "kind": "shell",
                        "status": "completed",
                    }
                ],
                "session-active",
            ),
        )
        for owner, active, jobs, reason in cases:
            native = Native(transcript())
            native.active, native.jobs = active, jobs
            self.refused(
                reason,
                lambda: engine.backup(
                    native.catalog(ownership=owner),
                    "ses_root",
                    self.output,
                    recipients=[self.recipient],
                ),
            )
            self.assertFalse(self.output.exists())
        native = Native(transcript())
        native.hook = lambda _m, p, _b: (
            {"data": []} if urlsplit(p).path == "/api/session" else None
        )
        self.refused("invalid-inventory", lambda: self.backup(native))

    def test_readonly_backup_does_not_apply_managed_child_or_terminal_delete_fences(
        self,
    ):
        catalog = self.source.catalog(managed_ids=["ses_root"])
        catalog._previews["unrelated-delete-token"] = {"untouched": True}
        engine.backup(catalog, "ses_root", self.output, recipients=[self.recipient])
        self.assertEqual(
            catalog._previews, {"unrelated-delete-token": {"untouched": True}}
        )
        self.assertFalse(
            any(path.endswith("/terminal") for _, path, _ in self.source.calls)
        )

    def test_snapshot_and_cross_directory_children_revalidated(self):
        for change in ("metadata", "child", "late-child", "message", "origin"):
            native = Native(transcript())
            catalog = native.catalog()
            count = [0]

            def hook(_method, path, _body):
                if path.endswith("/export"):
                    count[0] += 1
                    if change == "metadata":
                        native.values["ses_root"]["info"]["time"]["updated"] += 1
                    elif change == "child" or (
                        change == "late-child" and count[0] == 2
                    ):
                        native.values["ses_added"] = transcript(
                            "ses_added", parent="ses_root", directory="/other"
                        )
                    elif change == "message" and count[0] == 2:
                        native.values["ses_root"]["messages"][0]["text"] = "CHANGED"
                    elif change == "origin":
                        catalog.store = "other"

            native.hook = hook
            with (
                self.subTest(change=change),
                self.assertRaises(engine.SessionBackupError),
            ):
                engine.backup(
                    catalog, "ses_root", self.output, recipients=[self.recipient]
                )
            self.assertFalse(self.output.exists())

    def test_unsettled_tail_shell_and_incomplete_message_pagination(self):
        for kind in ("assistant", "shell", "compaction", "cursor"):
            native = Native(transcript())
            if kind == "cursor":
                native.hook = lambda _m, p, _b: (
                    page([], "more") if urlsplit(p).path.endswith("/message") else None
                )
                reason = "incomplete-history"
            else:
                native.values["ses_root"]["messages"].append(
                    {
                        "id": "msg_pending",
                        "type": kind,
                        "time": {"created": 3},
                        "status": "running",
                    }
                )
                reason = "unsettled-history"
            with self.subTest(kind=kind):
                self.refused(reason, lambda: self.backup(native))

    def test_interrupted_count_retained_without_unfinished_turn(self):
        native = Native(transcript())
        pending = {"id": "msg_interrupted", "type": "assistant", "time": {"created": 0}}
        native.hook = lambda _m, p, _b: (
            page([pending] + copy.deepcopy(native.values["ses_root"]["messages"]))
            if urlsplit(p).path.endswith("/message")
            else None
        )
        self.assertEqual(self.backup(native)["interrupted_assistant_count"], 1)
        target = Native()
        catalog, plan = self.plan(target)
        result = engine.restore(catalog, plan)
        restored = target.values[result["created_ids"][0]]
        self.assertEqual(
            restored["info"]["metadata"]["oc-box.session-backup"]["interrupted"],
            {"assistant": 1},
        )
        self.assertEqual(len(restored["messages"]), 1)

    def test_native_error_text_and_partial_commit_are_not_false_success(self):
        self.backup()
        for stage in ("before", "after", "conflict", "corrupt", "inventory"):
            target = Native(transcript("ses_existing"))
            before = copy.deepcopy(target.values["ses_existing"])
            catalog, plan = self.plan(target)
            count = [0]

            def imported(body):
                count[0] += 1
                if count[0] == 1:
                    return None
                ident = body["info"]["id"]
                if stage in ("after", "corrupt", "inventory"):
                    target.values[ident] = copy.deepcopy(
                        {key: body[key] for key in ("info", "messages")}
                    )
                if stage in ("before", "after"):
                    raise RuntimeError("SYNTHETIC_PRIVATE_NATIVE_ERROR")
                if stage == "corrupt":
                    target.values[ident]["messages"][0]["text"] = "WRONG"
                if stage == "inventory":
                    target.values["ses_foreign"] = transcript("ses_foreign")
                return {"data": copy.deepcopy(body["info"])}

            target.import_hook = imported
            with (
                self.subTest(stage=stage),
                self.assertRaises(engine.SessionBackupError) as caught,
            ):
                engine.restore(catalog, plan)
            error = caught.exception
            self.assertNotIn("PRIVATE_NATIVE_ERROR", str(error))
            self.assertEqual(len(error.attempted_ids), 2)
            self.assertEqual(
                len(error.created_ids), 1 if stage in ("before", "conflict") else 2
            )
            self.assertEqual(target.values["ses_existing"], before)
            self.assertFalse(
                any(method in ("DELETE", "PATCH") for method, _, _ in target.calls)
            )

    def test_restore_plan_is_bound_integrity_checked_quiet_and_single_use(self):
        self.backup()
        for change in (
            "target",
            "payload",
            "review",
            "inventory",
            "activity",
            "closed",
            "pid",
        ):
            target = Native(transcript("ses_existing"))
            catalog, plan = self.plan(target)
            connection = catalog
            if change == "target":
                connection = target.catalog()
            elif change == "payload":
                plan._payload[0]["info"]["permissions"] = [{"action": "*"}]
            elif change == "review":
                plan._review["destination"]["directory"] = "/wrong"
            elif change == "inventory":
                target.values["ses_added"] = transcript("ses_added")
            elif change == "activity":
                target.active = {"ses_existing": {"type": "running"}}
            elif change == "closed":
                plan.close()
            elif change == "pid":
                plan._pid = -1
            with (
                self.subTest(change=change),
                self.assertRaises(engine.SessionBackupError),
            ):
                engine.restore(connection, plan)
            self.assertEqual(target.mutations(), [])

    def test_privilege_metadata_and_foreign_parent_not_reactivated(self):
        native = Native(transcript(parent="ses_foreign"))
        native.values["ses_root"]["info"].update(
            {
                "agent": "unsafe",
                "model": {"providerID": "unsafe"},
                "config": {"danger": True},
                "auth": {"secret": "SYNTHETIC_SECRET"},
                "jobs": ["unsafe"],
                "fork": {"sessionID": "ses_foreign"},
                "revert": {},
                "metadata": {
                    "oc-box.archive": {"privilege": "unsafe"},
                    "untrusted": "unsafe",
                },
            }
        )
        self.backup(native)
        target = Native(transcript("ses_foreign"))
        catalog, plan = self.plan(target)
        engine.restore(catalog, plan)
        info = next(
            value["info"]
            for ident, value in target.values.items()
            if ident != "ses_foreign"
        )
        self.assertFalse(
            set(info)
            & {"parentID", "agent", "model", "config", "auth", "jobs", "fork", "revert"}
        )
        self.assertEqual(set(info["metadata"]), {"oc-box.session-backup"})

    def test_existing_output_symlinks_hardlinks_and_unsafe_input_refuse(self):
        self.output.write_bytes(b"UNCHANGED")
        self.refused("output-exists", lambda: self.backup())
        self.assertEqual(self.source.calls, [])
        self.assertEqual(self.output.read_bytes(), b"UNCHANGED")
        self.output.unlink()
        self.backup()
        link = self.root / "link"
        link.symlink_to(self.output)
        with self.assertRaises(engine.SessionBackupError):
            engine.inspect(link, identities=[self.identity])
        link.unlink()
        os.link(self.output, link)
        self.refused(
            "unsafe-input",
            lambda: engine.inspect(self.output, identities=[self.identity]),
        )
        link.unlink()
        self.output.chmod(0o644)
        self.refused(
            "unsafe-input",
            lambda: engine.inspect(self.output, identities=[self.identity]),
        )
        self.output.chmod(0o600)
        directory_link = self.root / "parent-link"
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(engine.SessionBackupError):
            engine.inspect(
                directory_link / self.output.name, identities=[self.identity]
            )

    def test_tampered_ciphertext_validated_before_history_decryption_or_import(self):
        self.backup()
        entries = self.entries()
        value = bytearray(entries["sessions.age"])
        value[-1] ^= 1
        entries["sessions.age"] = bytes(value)
        self.repack(entries)
        original = engine._decrypt
        seen = []

        def decrypt(path, *args):
            seen.append(path.name)
            return original(path, *args)

        target = Native()
        with patch.object(engine, "_decrypt", side_effect=decrypt):
            self.refused("ciphertext-mismatch", lambda: self.plan(target))
        self.assertEqual(seen, ["manifest.age"])
        self.assertEqual(target.calls, [])

    def test_envelope_names_duplicates_links_extensions_and_trailing_data(self):
        self.backup()
        entries = self.entries()
        cases = (
            [("../key.age", entries["key.age"])] + list(entries.items())[1:],
            list(entries.items()) + [("sessions.age", b"extra")],
            [
                ("key.age", b"key"),
                ("key.age", b"duplicate"),
                ("sessions.age", b"history"),
            ],
        )
        for members in cases:
            self.repack(entries, members=members)
            with self.assertRaises(engine.SessionBackupError):
                engine.inspect(self.output, identities=[self.identity])
        self.repack(entries)
        with self.output.open("ab") as stream:
            stream.write(b"NOT_A_SECOND_ARCHIVE")
        self.refused(
            "invalid-envelope",
            lambda: engine.inspect(self.output, identities=[self.identity]),
        )
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.XHDTYPE,
        ):
            with (
                self.output.open("wb") as stream,
                tarfile.open(
                    fileobj=stream, mode="w|", format=tarfile.USTAR_FORMAT
                ) as archive,
            ):
                info = tarfile.TarInfo("key.age")
                info.type, info.mode, info.linkname = kind, 0o600, "/outside"
                archive.addfile(info)
            with self.assertRaises(engine.SessionBackupError):
                engine.inspect(self.output, identities=[self.identity])

    def test_session_and_history_limits(self):
        with patch.object(engine, "MAX_SESSIONS", 1):
            self.refused("session-limit", lambda: self.backup())
        with patch.object(engine, "MAX_HISTORY", 32):
            self.refused("history-limit", lambda: self.backup())
        self.backup()
        with patch.object(engine, "MAX_CIPHERTEXT", 1):
            self.refused(
                "invalid-envelope",
                lambda: engine.inspect(self.output, identities=[self.identity]),
            )
        with patch.object(engine, "MAX_HISTORY", 32):
            self.refused(
                "history-limit",
                lambda: engine.inspect(self.output, identities=[self.identity]),
            )

    def test_fresh_id_collision_refuses_without_import_or_existing_changes(self):
        self.backup()
        target = Native(transcript("ses_" + "a" * 32))
        original = copy.deepcopy(target.values)
        with patch.object(engine.secrets, "token_hex", return_value="a" * 32):
            self.refused("identity-collision", lambda: self.plan(target))
        self.assertEqual(target.values, original)
        self.assertEqual(target.mutations(), [])

    def test_malformed_assistant_content_and_unknown_types_refuse_fixed(self):
        for content in (None, 42, ["text"], [{"type": "tool", "state": None}]):
            payload = {
                "source": {"origin": "host", "store": "synthetic", "id": "ses_root"},
                "sessions": [transcript()],
            }
            payload["sessions"][0]["messages"][0].update(
                type="assistant", content=content, time={"created": 1, "completed": 2}
            )
            self.refused("invalid-message", lambda: engine._validate_tree(payload))
        payload["sessions"][0]["messages"][0]["type"] = []
        self.refused("invalid-message", lambda: engine._validate_tree(payload))

    def test_output_race_never_replaces_existing_file_and_cleans_ciphertext_stage(self):
        original_link = engine.os.link

        def raced_link(source, destination, **kwargs):
            self.output.write_bytes(b"CONCURRENT_EXISTING")
            return original_link(source, destination, **kwargs)

        with patch.object(engine.os, "link", side_effect=raced_link):
            self.refused("output-exists", lambda: self.backup())
        self.assertEqual(self.output.read_bytes(), b"CONCURRENT_EXISTING")
        self.assertEqual(set(self.root.iterdir()), {self.identity, self.output})

    def test_crypto_failure_never_publishes_output_or_plaintext(self):
        with patch.object(
            crypto, "encrypt_bytes", side_effect=crypto.BackupError("SYNTHETIC_SECRET")
        ):
            self.refused("encryption-failed", lambda: self.backup())
        self.assertFalse(self.output.exists())
        self.assertEqual(set(self.root.iterdir()), {self.identity})

    def test_nonregular_input_is_opened_nonblocking_then_refused(self):
        os.mkfifo(self.output, 0o600)
        self.refused(
            "unsafe-input",
            lambda: engine.inspect(self.output, identities=[self.identity]),
        )

    def test_authenticated_duplicate_ids_and_invalid_tree_refuse_before_target_io(self):
        for alter in (
            lambda p: p["sessions"].append(p["sessions"][0]),
            lambda p: p["sessions"][1]["messages"][0].update(id="msg_root"),
            lambda p: p["sessions"][1]["info"].update(parentID="ses_missing"),
        ):
            if self.output.exists():
                self.output.unlink()
            self.backup()
            self.altered_payload(alter)
            target = Native()
            with self.assertRaises(engine.SessionBackupError):
                self.plan(target)
            self.assertEqual(target.calls, [])

    def test_process_local_warning_and_large_history_above_old_crypto_limit(self):
        native = Native(transcript(text="X" * (crypto.MAX_SMALL + 100)))
        review = engine.backup(
            native.catalog(ownership="process-local"),
            "ses_root",
            self.output,
            recipients=[self.recipient],
        )
        self.assertEqual(review["warning"], tui_sessions.PROCESS_LOCAL_WARNING)
        target = Native()
        catalog, plan = self.plan(target, ownership="process-local")
        result = engine.restore(catalog, plan)
        self.assertEqual(result["warning"], tui_sessions.PROCESS_LOCAL_WARNING)
        self.assertEqual(
            len(target.values[result["created_ids"][0]]["messages"][0]["text"]),
            crypto.MAX_SMALL + 100,
        )
        self.assertEqual(crypto.MAX_SMALL, 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
