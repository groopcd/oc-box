"""Bounded native tab-state reading and pure, source-bound session selection."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import native_tabs as tabs


class NativeTabsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oc-box-native-tabs-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir(mode=0o700)
        self.preferences = self.home / ".config/opencode/cli.json"
        self.state = self.home / ".local/state/opencode/latest/tui/tabs.json"
        self.directory = "/example/project"

    def write(self, file, value):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value if isinstance(value, str) else json.dumps(value))
        file.chmod(0o600)

    def saved(self, ids, *, directory=None):
        return {
            "global": {"tabs": [], "unread": {}},
            "cwd": {
                directory or self.directory: {
                    "tabs": [{"sessionID": ident} for ident in ids],
                    "unread": {},
                }
            },
        }

    def read(self, release="2.3.0"):
        return tabs.read_snapshot(str(self.home), self.directory, release)

    def session(self, ident, *, directory=None, parent=None, archived=None):
        return {
            "id": ident,
            "location": {"directory": directory or self.directory},
            "parentID": parent,
            "time": {"updated": 900, "archived": archived},
        }

    def test_stable_v2_source_contract_matrix_and_missing_scope(self):
        for release in (
            "2.0.0",
            "2.2.5",
            "2.2.6",
            "2.2.7",
            "2.3.0",
            "2.3.2",
            "2.4.0",
            "2.99.123",
        ):
            with self.subTest(release=release):
                self.assertEqual(self.read(release).status, "missing")
                self.write(self.state, self.saved(["ses_keep"]))
                self.assertEqual(self.read(release).tabs, ("ses_keep",))
                self.write(self.state, {"schema": 2, "tabSets": []})
                self.assertEqual(self.read(release).reason, "invalid-state")
                self.write(self.state, self.saved([], directory="/other"))
                self.assertEqual(self.read(release).reason, "scope-absent")
                self.state.unlink()
        for release in (
            "1.19.5",
            "3.0.0",
            "2.3",
            "2.03.2",
            "2.3.02",
            "2.3.٢",
            "2.3.2-preview",
            "2.3.2+custom",
            "2.3.2\n",
            "invalid",
            None,
            [],
        ):
            with self.subTest(release=release):
                self.assertEqual(self.read(release).reason, "unsupported-version")

    def test_native_empty_scope_is_valid_not_missing_and_preserves_input(self):
        self.write(self.state, self.saved([]))
        before = self.state.read_bytes()
        snapshot = self.read()
        inventory = [self.session("ses_closed")]
        self.assertEqual(snapshot.status, "valid")
        self.assertEqual(snapshot.tabs, ())
        self.assertIsNone(
            tabs.choose(snapshot, inventory, "ses_closed", directory=self.directory)
        )
        self.assertEqual(self.state.read_bytes(), before)

    def test_default_cwd_jsonc_and_global_scope_read_order_without_active_guess(self):
        value = self.saved(["ses_second", "ses_first"])
        value["global"]["tabs"] = [{"sessionID": "ses_global", "title": "Synthetic"}]
        self.write(self.state, value)
        self.write(
            self.preferences,
            '{/* comment */"tabs":{"scope":"cwd",},"unknown":"https://example/a//b",}',
        )
        self.assertEqual(self.read().tabs, ("ses_second", "ses_first"))
        self.write(self.preferences, {"tabs": {"scope": "global"}})
        self.assertEqual(self.read().tabs, ("ses_global",))
        self.assertEqual(self.read().scope, "global")
        self.assertNotIn("active", asdict(self.read()))

    def test_disabled_native_tabs_allow_legacy_but_invalid_scope_does_not(self):
        for settings in ({"mode": "off"}, {"enabled": False}):
            with self.subTest(settings=settings):
                self.write(self.preferences, {"tabs": settings})
                self.assertEqual(self.read().reason, "disabled")
        self.write(self.preferences, {"tabs": {"mode": "on", "enabled": False}})
        self.assertEqual(self.read().reason, "absent")
        self.write(self.preferences, {"tabs": {"mode": "off", "scope": "bad"}})
        self.assertEqual(self.read().status, "invalid")

    def test_malformed_preferences_never_authorize_legacy_fallback(self):
        values = (
            "not JSON",
            "[]",
            '{"tabs":{"scope":null}}',
            '{"tabs":{"scope":"cwd","scope":"global"}}',
            '{"tabs":{"mode":true}}',
            '{"tabs":{"mode":null}}',
            '{"tabs":{"enabled":null}}',
            '{"tabs":[]}',
            '{"tabs":{"scope":"cwd"},"bad":NaN}',
        )
        for value in values:
            with self.subTest(value=value):
                self.write(self.preferences, value)
                self.assertEqual(self.read().reason, "invalid-preferences")
                with self.assertRaisesRegex(tabs.TabError, "invalid-snapshot"):
                    tabs.choose(
                        self.read(),
                        [self.session("ses_closed")],
                        "ses_closed",
                        directory=self.directory,
                    )

    def test_malformed_state_active_scope_and_byte_bounds_fail_closed(self):
        invalid = (
            "not JSON",
            "null",
            {"global": {}, "cwd": []},
            {"global": {}, "cwd": {self.directory: None}},
            {"global": {}, "cwd": {self.directory: {"tabs": []}}},
            self.saved(["ses_same", "ses_same"]),
            self.saved(["bad"]),
            self.saved(["ses_" + "x" * tabs.MAX_ID]),
            self.saved(["ses_" + str(index) for index in range(tabs.MAX_TABS + 1)]),
        )
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                self.write(self.state, value)
                self.assertEqual(self.read().reason, "invalid-state")
        self.write(self.state, " " * (tabs.MAX_STATE + 1))
        self.assertEqual(self.read().status, "invalid")
        self.write(self.preferences, " " * (tabs.MAX_PREFERENCES + 1))
        self.assertEqual(self.read().status, "invalid")

    def test_symlink_parents_and_final_paths_are_refused(self):
        for target in ("preferences", "state"):
            file = getattr(self, target)
            self.write(file, self.saved([]) if target == "state" else {})
            saved = file.with_suffix(".retained")
            file.rename(saved)
            file.symlink_to(saved)
            self.assertEqual(self.read().reason, "unsafe-path")
            file.unlink()
            saved.rename(file)
            parent = file.parent
            moved = parent.with_name(parent.name + "-retained")
            parent.rename(moved)
            parent.symlink_to(moved, target_is_directory=True)
            self.assertEqual(self.read().reason, "unsafe-path")
            parent.unlink()
            moved.rename(parent)
        moved = self.home.with_name("retained-home")
        self.home.rename(moved)
        self.home.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.read().reason, "unsafe-path")

    def test_missing_private_home_is_not_a_missing_native_snapshot(self):
        self.home.rmdir()
        self.assertEqual(self.read().reason, "unsafe-path")

    def test_final_type_hardlink_permissions_and_unreadability_are_refused(self):
        self.state.parent.mkdir(parents=True)
        os.mkfifo(self.state)
        self.assertEqual(self.read().reason, "unsafe-path")
        self.state.unlink()
        self.write(self.state, self.saved([]))
        linked = self.state.with_name("linked.json")
        os.link(self.state, linked)
        self.assertEqual(self.read().reason, "unsafe-path")
        linked.unlink()
        self.state.chmod(0o666)
        self.assertEqual(self.read().reason, "unsafe-path")
        self.state.chmod(0o600)
        self.state.parent.chmod(0o777)
        self.assertEqual(self.read().reason, "unsafe-path")
        self.state.parent.chmod(0o700)
        with patch.object(tabs.os, "open", side_effect=PermissionError()):
            self.assertEqual(self.read().reason, "unsafe-path")

    def test_owner_checks_cover_home_parents_and_final_files(self):
        self.write(self.preferences, {})
        self.write(self.state, self.saved([]))
        actual = tabs.os.fstat
        for target in (
            self.home,
            self.preferences.parent,
            self.preferences,
            self.home / ".local",
            self.state.parent,
            self.state,
        ):
            identity = target.stat().st_ino

            def changed(descriptor, identity=identity):
                info = actual(descriptor)
                if info.st_ino == identity:
                    values = {
                        key: getattr(info, key)
                        for key in dir(info)
                        if key.startswith("st_")
                    }
                    values["st_uid"] = os.geteuid() + 1
                    return SimpleNamespace(**values)
                return info

            with (
                self.subTest(target=target.name),
                patch.object(tabs.os, "fstat", side_effect=changed),
            ):
                self.assertEqual(self.read().reason, "unsafe-path")

    def test_stat_change_during_read_is_invalid(self):
        self.write(self.preferences, {})
        self.write(self.state, self.saved([]))
        actual = tabs.os.read
        changed = False

        def mutate(descriptor, size):
            nonlocal changed
            result = actual(descriptor, size)
            if result and not changed:
                changed = True
                with self.preferences.open("ab") as output:
                    output.write(b" ")
            return result

        with patch.object(tabs.os, "read", side_effect=mutate):
            self.assertEqual(self.read().reason, "unsafe-path")

    def test_selection_revalidation_filters_only_choice_not_history_inventory(self):
        inventory = [
            self.session("ses_closed"),
            self.session("ses_first"),
            self.session("ses_second"),
            self.session("ses_child", parent="ses_first"),
            self.session("ses_archived", archived=0),
            self.session("ses_foreign", directory="/other"),
        ]
        before = copy.deepcopy(inventory)
        snapshot = tabs.Snapshot(
            "valid",
            ("ses_foreign", "ses_archived", "ses_child", "ses_first", "ses_second"),
        )
        self.assertEqual(
            tabs.choose(snapshot, inventory, "ses_closed", directory=self.directory),
            "ses_first",
        )
        self.assertEqual(
            tabs.choose(snapshot, inventory, "ses_second", directory=self.directory),
            "ses_second",
        )
        self.assertEqual(
            tabs.revalidate(
                snapshot, inventory, "ses_closed", directory=self.directory
            ),
            "ses_first",
        )
        self.assertIsNone(
            tabs.revalidate(
                tabs.Snapshot("valid"),
                inventory,
                "ses_closed",
                directory=self.directory,
            )
        )
        self.assertIsNone(
            tabs.revalidate(
                tabs.Snapshot("invalid"),
                inventory,
                "ses_closed",
                directory=self.directory,
            )
        )
        missing = tabs.Snapshot("missing", reason="absent")
        self.assertEqual(
            tabs.choose(missing, inventory, "ses_closed", directory=self.directory),
            "ses_closed",
        )
        self.assertIsNone(
            tabs.revalidate(missing, inventory, None, directory=self.directory)
        )
        self.assertEqual(inventory, before)

    def test_invalid_or_duplicate_root_inventory_is_not_partially_accepted(self):
        good = self.session("ses_open")
        invalid = [None, [None], [good, good], [{**good, "time": None}]]
        for field in ("updated", "archived"):
            for value in (float("nan"), float("inf"), -1, True, "private", 10**400):
                invalid.append([{**good, "time": {**good["time"], field: value}}])
        for inventory in invalid:
            with (
                self.subTest(inventory=type(inventory).__name__),
                self.assertRaisesRegex(tabs.TabError, "invalid-inventory"),
            ):
                tabs.choose(
                    tabs.Snapshot("valid", ("ses_open",)),
                    inventory,
                    "ses_open",
                    directory=self.directory,
                )
        for snapshot in (
            tabs.Snapshot("bad"),
            tabs.Snapshot("valid", ("not-a-session",)),
            tabs.Snapshot("valid", ("ses_open", "ses_open")),
        ):
            with self.assertRaisesRegex(tabs.TabError, "invalid-snapshot"):
                tabs.choose(snapshot, [good], "ses_open", directory=self.directory)

    def test_inspect_uses_explicit_existing_connection_not_context_or_native_cli(self):
        connection = {
            "workspace": "a" * 32,
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-private-password",
        }
        data = {
            "id": connection["workspace"],
            "home": str(self.home),
            "opencode_version": "2.3.2",
        }
        snapshot = tabs.Snapshot("valid", ("ses_open",))
        box = SimpleNamespace(
            BoxError=RuntimeError,
            workspace_service=SimpleNamespace(
                validate_endpoint=Mock(),
                client_command=Mock(return_value=["private-file-probe"]),
            ),
            container_project=Mock(return_value=self.directory),
            run=Mock(
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(asdict(snapshot))
                )
            ),
            list_container_sessions=Mock(side_effect=AssertionError("not allowed")),
            run_v2_history=Mock(side_effect=AssertionError("not allowed")),
        )
        self.assertEqual(tabs.inspect(box, data, connection=connection), snapshot)
        command = box.workspace_service.client_command.call_args.args[3]
        self.assertEqual(command[:5], ["python3", "-I", "-S", "-B", "-c"])
        self.assertNotIn(connection["password"], " ".join(command))
        self.assertEqual(json.loads(command[-1])["home"], str(self.home))
        self.assertEqual(json.loads(command[-1])["release"], "2.3.2")
        self.assertEqual(
            box.workspace_service.client_command.call_args.args[2], connection
        )
        self.assertEqual(
            box.run.call_args.kwargs,
            {"capture": True, "private": True, "check": False, "timeout": 2},
        )
        for failure in (
            "{}",
            "raw-private-data",
            "[]",
            json.dumps(
                {"status": "missing", "tabs": [], "scope": "cwd", "reason": "transport"}
            ),
        ):
            box.run.return_value.stdout = failure
            self.assertEqual(
                tabs.inspect(box, data, connection=connection).reason, "transport"
            )
        box.run.side_effect = subprocess.TimeoutExpired("private", 2)
        self.assertEqual(
            tabs.inspect(box, data, connection=connection).reason, "transport"
        )
        box.run.reset_mock(side_effect=True)
        for timeout in (float("inf"), float("nan"), True, -1, 0, 10**400):
            self.assertEqual(
                tabs.inspect(box, data, connection=connection, timeout=timeout).reason,
                "transport",
            )
        for release, status in (("1.19.5", "missing"), ("3.0.0", "invalid")):
            self.assertEqual(
                tabs.inspect(
                    box, {**data, "opencode_version": release}, connection=connection
                ).status,
                status,
            )
        box.run.assert_not_called()
        with self.assertRaises(TypeError):
            tabs.inspect(box, data)


if __name__ == "__main__":
    unittest.main()
