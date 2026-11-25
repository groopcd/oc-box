"""Check host archive environment construction without touching host state."""

import json
import unittest

from oc_box.history_overlap_host_env import private_host_archive_env
from oc_box.history_overlap_order import OverlapRefusal


class HostArchiveEnvironmentTests(unittest.TestCase):
    def test_selects_same_data_home_and_only_explicit_private_roots(self):
        ordinary = {
            "XDG_DATA_HOME": "/synthetic/data",
            "HOME": "/unbound/home",
            "OPENCODE_CONFIG_CONTENT": "synthetic untrusted config",
            "OPENCODE_FLAGS": "ambient",
            "TEST_PROVIDER_TOKEN": "synthetic",
            "CONTAINER_HOST": "synthetic selector",
        }
        actual = private_host_archive_env(
            "2.4.1", ordinary, scratch="/synthetic/scratch"
        )
        self.assertEqual(actual["XDG_DATA_HOME"], ordinary["XDG_DATA_HOME"])
        self.assertEqual(actual["HOME"], "/synthetic/scratch/home")
        self.assertEqual(actual["OPENCODE_TEST_HOME"], actual["HOME"])
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
        ):
            self.assertTrue(actual[key].startswith("/synthetic/scratch/"))
        for key in (
            "OPENCODE_FLAGS",
            "TEST_PROVIDER_TOKEN",
            "CONTAINER_HOST",
            "OPENCODE_DB",
        ):
            self.assertNotIn(key, actual)
        config = json.loads(actual["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(
            {item["action"] for item in config["policies"]},
            {"plugin.use", "provider.use", "tool.use"},
        )
        self.assertTrue(all(item["effect"] == "deny" for item in config["policies"]))
        self.assertFalse(config["automations"])

    def test_ambiguous_or_alternate_database_selectors_do_not_opt_in(self):
        for ordinary in (
            {},
            {"XDG_DATA_HOME": "relative-data"},
            {"XDG_DATA_HOME": "/synthetic/../data"},
            {"XDG_DATA_HOME": "/synthetic/data", "OPENCODE_DB": "other.db"},
            {"XDG_DATA_HOME": "/synthetic/data", "OPENCODE_DISABLE_CHANNEL_DB": "1"},
        ):
            with self.subTest(ordinary=ordinary), self.assertRaises(OverlapRefusal):
                private_host_archive_env(
                    "2.4.1", ordinary, scratch="/synthetic/scratch"
                )

    def test_overlapping_declared_roots_or_non_stock_release_refuses(self):
        for release, scratch in (
            ("2.4.1", "/synthetic/data"),
            ("2.4.1", "/synthetic/data/tmp"),
            ("2.4.0", "/synthetic"),
            ("2.4.2", "/synthetic/scratch"),
            ("2.4.1+modified", "/synthetic/scratch"),
        ):
            with (
                self.subTest(release=release, scratch=scratch),
                self.assertRaises(OverlapRefusal),
            ):
                private_host_archive_env(
                    release, {"XDG_DATA_HOME": "/synthetic/data"}, scratch=scratch
                )
