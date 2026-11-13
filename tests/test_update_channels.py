import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oc_box import update_channels as channels


class ChannelTests(unittest.TestCase):
    def test_numeric_version_order_and_strict_grammar(self):
        versions = ["1.6.99", "1.6.100", "1.7.1.dev0", "1.7.1", "1.7.2.dev0"]
        self.assertEqual(sorted(versions, key=channels.parse_version), versions)
        self.assertEqual(channels.parse_version("1.7.1.dev0+gabcdef0"), (1, 7, 1, 0))
        for invalid in (
            "v1.7.1",
            "1.7",
            "1.7.1.dev1",
            "1.7.1rc1",
            "1.7.01",
            "../master",
            None,
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(channels.ChannelError),
            ):
                channels.parse_version(invalid)

    def test_published_inventory_filters_and_orders_versions(self):
        def release(version, prerelease=False, draft=False):
            return {"tag_name": "v" + version, "prerelease": prerelease, "draft": draft}

        records = [
            release("1.6.99"),
            release("1.6.100"),
            release("1.7.1.dev0", True),
            release("9.0.0", draft=True),
            release("8.0.0", True),
            release("1.8.0.dev0", False),
            release("1.7.1.dev0+gfoo", True),
        ]
        with patch.object(channels, "fetch_json", return_value=records):
            result = channels.available_releases()
        self.assertEqual(result["stable"]["version"], "1.6.100")
        self.assertEqual(result["devel"]["version"], "1.7.1.dev0")

    def test_complete_bounded_pagination_is_required(self):
        empty = {"tag_name": "unrelated", "draft": False, "prerelease": False}
        with patch.object(
            channels, "fetch_json", side_effect=[[empty] * 100, []]
        ) as fetch:
            self.assertEqual(
                channels.available_releases(), {"stable": None, "devel": None}
            )
            self.assertEqual(fetch.call_count, 2)
        with patch.object(channels, "fetch_json", return_value=[empty] * 100):
            with self.assertRaisesRegex(channels.ChannelError, "pagination"):
                channels.available_releases()

    def test_preferences_are_private_scoped_and_preserve_unrelated_values(self):
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": work}),
        ):
            target = Path(work) / "oc-box/config.json"
            self.assertEqual(channels.selected_channel(), "stable")
            channels.save_settings({"pass_env": ["SETTING"], "auto_update": False})
            channels.remember_channel("devel")
            channels.remember_channel("stable", owner="package:fixture")
            self.assertEqual(channels.selected_channel(), "devel")
            self.assertEqual(
                channels.selected_channel(owner="package:fixture"), "stable"
            )
            self.assertEqual(channels.selected_channel("stable"), "stable")
            self.assertEqual(channels.selected_channel(), "devel")
            settings = json.loads(target.read_text())
            self.assertEqual(settings["pass_env"], ["SETTING"])
            self.assertIs(settings["auto_update"], False)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            for content in ('{"update_channel":"feature/x"}', "{broken", "[]"):
                target.write_text(content)
                with self.assertRaises(channels.ChannelError):
                    channels.selected_channel()
                self.assertEqual(target.read_text(), content)

    def test_anonymous_http_refuses_arbitrary_endpoints(self):
        for url in (
            "http://pypi.org/simple/oc-box/",
            "https://evil.invalid/",
            channels.GITHUB_RELEASES + "/../config",
        ):
            with self.subTest(url=url), self.assertRaises(channels.ChannelError):
                channels.fetch_json(url)


if __name__ == "__main__":
    unittest.main()
