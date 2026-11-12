"""Pure session-payload contracts; no native, engine or host fixture execution."""

import copy
import json
from pathlib import Path
import unittest

from tools import test_sharing_podman as runner


class SharingSessionPayloadTests(unittest.TestCase):
    def test_default_payload_is_fixed_native_metadata_with_explicit_title(self):
        self.assertEqual(
            runner.sharing_session_payload("/synthetic/project"),
            {
                "location": {"directory": "/synthetic/project"},
                "title": "Synthetic host-sharing check",
                "agent": "build",
                "model": {"providerID": "fixture", "id": "share-model"},
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
            },
        )

    def test_explicit_permission_arrays_preserve_order_denials_and_empty_selection(
        self,
    ):
        cases = (
            [],
            [
                {"action": "*", "resource": "*", "effect": "allow"},
                {
                    "action": "oc_box_share_synthetic_read_file",
                    "resource": "*",
                    "effect": "deny",
                },
            ],
        )
        for rules in cases:
            with self.subTest(empty=not rules):
                before = copy.deepcopy(rules)
                payload = runner.sharing_session_payload("/synthetic/project", rules)
                self.assertEqual(payload["permissions"], before)
                self.assertEqual(rules, before)

    def test_default_permissions_and_nested_metadata_are_fresh_per_session(self):
        first = runner.sharing_session_payload("/synthetic/first")
        first["permissions"][0]["effect"] = "deny"
        first["location"]["directory"] = "/synthetic/changed"
        first["model"]["id"] = "changed"
        second = runner.sharing_session_payload("/synthetic/second")
        self.assertEqual(second["permissions"][0]["effect"], "allow")
        self.assertEqual(second["location"], {"directory": "/synthetic/second"})
        self.assertEqual(
            second["model"], {"providerID": "fixture", "id": "share-model"}
        )

    def test_absolute_path_metadata_is_serializable_and_relative_paths_are_refused(
        self,
    ):
        payload = runner.sharing_session_payload(Path("/synthetic/project"))
        self.assertEqual(payload["location"], {"directory": "/synthetic/project"})
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        with self.assertRaisesRegex(runner.Refusal, "^protocol$"):
            runner.sharing_session_payload("relative-project")

    def test_title_is_static_and_no_prompt_or_case_token_is_forwarded(self):
        token = "SHARING_CASE_" + "a" * 32
        for directory in ("/synthetic/first", "/synthetic/" + token):
            with self.subTest(token_in_directory=token in directory):
                payload = runner.sharing_session_payload(directory)
                self.assertEqual(payload["title"], "Synthetic host-sharing check")
                self.assertEqual(
                    set(payload), {"location", "title", "agent", "model", "permissions"}
                )
                metadata = {
                    key: value for key, value in payload.items() if key != "location"
                }
                self.assertNotIn(token, json.dumps(metadata))
                for field in ("text", "prompt", "token", "messages"):
                    self.assertNotIn(field, payload)


if __name__ == "__main__":
    unittest.main()
