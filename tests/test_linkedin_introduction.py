import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import announce_release as social
from tools import post_linkedin_introduction as intro


class IntroductionTests(unittest.TestCase):
    def test_exact_approved_files_and_no_new_release_claim(self):
        text, image = intro.approved_inputs()
        self.assertIn("Introducing oc-box", text)
        self.assertIn("Current stable: 1.6.94", text)
        self.assertIn("Two release channels", text)
        self.assertEqual(social.sha(image.read_bytes()), intro.DIGESTS["image.png"])

    def test_disabled_publish_and_wrong_profile_never_reach_network(self):
        for env in (
            {},
            {"LINKEDIN_ANNOUNCEMENTS": "true", "BUFFER_LINKEDIN_CHANNEL_ID": "other"},
        ):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env, clear=True),
                patch.object(social, "request") as network,
            ):
                with self.assertRaises(social.Refusal):
                    intro.publish(Path("unused"))
                network.assert_not_called()

    def test_publish_requires_existing_exact_stable_and_public_pypi_before_send(self):
        identity = {
            "commit": intro.COMMIT,
            "release_channel": "stable",
            "distribution_version": "1.6.94",
        }
        release = {"id": intro.RELEASE_ID, "prerelease": False}
        github = Mock()
        github.release.return_value = release
        events = []
        with (
            patch.dict(os.environ, {"LINKEDIN_ANNOUNCEMENTS": "true"}, clear=True),
            patch.object(intro, "target", return_value="profile"),
            patch.object(social, "GitHub", return_value=github),
            patch.object(
                social, "request", return_value=social.encoded({"id": intro.RELEASE_ID})
            ),
            patch.object(
                social.check_release, "validate", return_value=identity
            ) as validate,
            patch.object(
                social, "verify_pypi", side_effect=lambda *a: events.append("pypi")
            ) as pypi,
            patch.object(social, "Buffer"),
            patch.object(
                social,
                "announce",
                side_effect=lambda *a, **k: events.append("send") or k,
            ) as send,
        ):
            result = intro.publish(Path("released-source"))
            validate.assert_called_once_with(
                Path("released-source"), publish=True, tag=intro.TAG, release=release
            )
            self.assertEqual(events, ["pypi", "send"])
            self.assertEqual(result["approved_text"], intro.approved_inputs()[0])
            send.reset_mock()
            pypi.side_effect = ValueError("fixture")
            with self.assertRaises(ValueError):
                intro.publish(Path("released-source"))
            send.assert_not_called()
            pypi.side_effect = None
            validate.return_value = {**identity, "commit": "b" * 40}
            with self.assertRaises(social.Refusal):
                intro.publish(Path("released-source"))
            send.assert_not_called()

    def test_status_is_read_only_and_returns_only_verified_link(self):
        text, _ = intro.approved_inputs()
        buffer = Mock()
        buffer.post.return_value = {
            "id": "post_123",
            "channelId": "profile",
            "text": text,
            "status": "sent",
            "externalLink": "https://www.linkedin.com/feed/update/urn:li:share:123/",
        }
        with (
            patch.object(intro, "target", return_value="profile"),
            patch.object(social, "Buffer", return_value=buffer),
        ):
            result = intro.status("post_123")
            self.assertEqual(result["status"], "sent")
            self.assertNotIn("text", result)
            buffer.create.assert_not_called()
            buffer.post.return_value["externalLink"] = "https://evil.invalid/"
            with self.assertRaises(social.Refusal):
                intro.status("post_123")


if __name__ == "__main__":
    unittest.main()
