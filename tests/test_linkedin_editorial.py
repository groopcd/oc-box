from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import linkedin_editorial as editorial


ARTICLE = "https://example.com/benchmarks/"
PDF = b"%PDF-1.4\n%%EOF\n"
COVER = b"\x89PNG\r\n\x1a\nexample"


class EditorialTest(unittest.TestCase):
    def setUp(self):
        self.env = {
            "GITHUB_REPOSITORY": "owner/editorial",
            "GITHUB_SHA": "a" * 40,
            "EDITORIAL_CHANNEL_ID": "personal-profile",
            "EDITORIAL_ALLOWED_HOSTS": "example.com raw.githubusercontent.com",
            "EDITORIAL_KIND": "article",
            "EDITORIAL_TITLE": "Measured results",
            "EDITORIAL_CAPTION": "Exact text.\r\n\n" + ARTICLE,
            "EDITORIAL_ARTICLE_URL": ARTICLE,
            "EDITORIAL_ENABLED": "true",
            "BUFFER_API_KEY": "mock-buffer",
            "EDITORIAL_GITHUB_TOKEN": "mock-github",
        }
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)

    def enable_pdf(self):
        self.env.update(
            EDITORIAL_KIND="pdf",
            EDITORIAL_PDF_URL=f"https://example.com/deck-{editorial.digest(PDF)}.pdf",
            EDITORIAL_THUMBNAIL_URL=f"https://example.com/cover-{editorial.digest(COVER)}.png",
        )

    def preview(self):
        path = self.directory / "preview"
        editorial.run("preview", self.env, path)
        self.env["EDITORIAL_APPROVAL"] = (path / "approval.sha256").read_text().strip()
        return path

    def test_article_preview_never_calls_remote_services(self):
        with patch.object(editorial, "request") as fetch:
            path = self.preview()
            manifest, payload, _ = editorial.prepare(self.env)
        fetch.assert_not_called()
        caption = self.env["EDITORIAL_CAPTION"].replace("\r\n", "\n").encode()
        self.assertEqual((path / "linkedin-caption.txt").read_bytes(), caption)
        self.assertEqual(
            editorial.digest((path / "approval.json").read_bytes()),
            self.env["EDITORIAL_APPROVAL"],
        )
        self.assertEqual(
            manifest["request_sha256"], editorial.digest(editorial.encoded(payload))
        )
        self.assertEqual(
            payload["metadata"]["linkedin"]["linkAttachment"]["url"], ARTICLE
        )
        self.assertNotIn("assets", payload)

    def test_pdf_preview_includes_reviewed_bytes_and_one_document(self):
        self.enable_pdf()
        with patch.object(
            editorial, "request", side_effect=[PDF, COVER, PDF, COVER]
        ) as fetch:
            path = self.preview()
            manifest, payload, _ = editorial.prepare(self.env)
        self.assertEqual((path / "linkedin-document.pdf").read_bytes(), PDF)
        self.assertEqual((path / "linkedin-thumbnail.png").read_bytes(), COVER)
        self.assertEqual(manifest["pdf"]["sha256"], editorial.digest(PDF))
        self.assertEqual(len(payload["assets"]), 1)
        self.assertEqual(
            payload["assets"][0]["document"]["title"], self.env["EDITORIAL_TITLE"]
        )
        self.assertIn(ARTICLE, payload["text"])
        self.assertNotIn("metadata", payload)
        self.assertTrue(
            all("token" not in call.kwargs for call in fetch.call_args_list)
        )

    def test_url_refusals_do_not_fetch(self):
        self.enable_pdf()
        for url in (
            "http://example.com/a.pdf",
            "https://user@example.com/a.pdf",
            "https://example.com:443/a.pdf",
            "https://unapproved.test/a.pdf",
            "https://example.com/a.pdf?token=hidden",
            "https://example.com/a.pdf#anchor",
            "https://example.com/a.pdf\n",
        ):
            with self.subTest(url=url), patch.object(editorial, "request") as fetch:
                self.env["EDITORIAL_PDF_URL"] = url
                with self.assertRaises(editorial.Refusal):
                    editorial.prepare(self.env)
                fetch.assert_not_called()

    def test_changed_pdf_is_rejected_before_claim(self):
        self.enable_pdf()
        with patch.object(editorial, "request", side_effect=[PDF, COVER]):
            self.preview()
        with (
            patch.object(editorial, "request", return_value=b"%PDF-1.5\n%%EOF\n"),
            patch.object(editorial, "claim") as claim,
            patch.object(editorial, "buffer_query") as query,
        ):
            with self.assertRaisesRegex(editorial.Refusal, "full SHA-256"):
                editorial.run("publish", self.env, self.directory / "publish")
        query.assert_not_called()
        claim.assert_not_called()

    def test_refuses_unapproved_or_disabled_before_remote_calls(self):
        self.preview()
        for name, value, message in (
            ("EDITORIAL_CAPTION", "Changed caption", "approved inputs"),
            ("EDITORIAL_CHANNEL_ID", "different-profile", "approved inputs"),
            ("EDITORIAL_ENABLED", "false", "disabled"),
            ("EDITORIAL_APPROVAL", "", "approval SHA-256"),
        ):
            with self.subTest(name=name), patch.object(editorial, "request") as request:
                with self.assertRaisesRegex(editorial.Refusal, message):
                    editorial.run(
                        "publish", self.env | {name: value}, self.directory / "publish"
                    )
                request.assert_not_called()

    def channel(self):
        return {
            "channel": {
                "id": self.env["EDITORIAL_CHANNEL_ID"],
                "service": "linkedin",
                "type": "profile",
                "isDisconnected": False,
                "isLocked": False,
                "isQueuePaused": False,
            }
        }

    def test_publish_submits_once_only_after_reserving_claim(self):
        self.preview()
        routes = []

        def fake(url, *, data, token, **kwargs):
            if url == editorial.BUFFER and "channel(input:" in data["query"]:
                routes.append("channel")
                self.assertEqual(token, self.env["BUFFER_API_KEY"])
                return editorial.encoded({"data": self.channel()})
            if url == "https://api.github.com/repos/owner/editorial/git/refs":
                routes.append("reserve")
                self.assertEqual(token, self.env["EDITORIAL_GITHUB_TOKEN"])
                self.assertEqual(
                    data["ref"],
                    "refs/tags/linkedin-editorial/" + self.env["EDITORIAL_APPROVAL"],
                )
                self.assertEqual(data["sha"], self.env["GITHUB_SHA"])
                return b"{}"
            routes.append("create")
            self.assertEqual(url, editorial.BUFFER)
            self.assertEqual(token, self.env["BUFFER_API_KEY"])
            self.assertEqual(data["variables"]["input"]["mode"], "shareNow")
            return editorial.encoded(
                {
                    "data": {
                        "createPost": {
                            "post": {
                                "id": "test-post",
                                "channelId": self.env["EDITORIAL_CHANNEL_ID"],
                                "status": "buffer",
                            }
                        }
                    }
                }
            )

        with patch.object(editorial, "request", side_effect=fake):
            path = self.directory / "publish"
            editorial.run("publish", self.env, path)
        self.assertEqual(routes, ["channel", "reserve", "create"])
        self.assertIn(b'"post_id": "test-post"', (path / "receipt.json").read_bytes())

    def test_existing_or_failed_claim_does_not_send_create(self):
        self.preview()
        path = self.directory / "publish"
        with patch.object(
            editorial,
            "request",
            side_effect=[
                editorial.encoded({"data": self.channel()}),
                editorial.Refusal("HTTP 422"),
            ],
        ) as network:
            with self.assertRaisesRegex(editorial.Refusal, "no Buffer create was sent"):
                editorial.run("publish", self.env, path)
        self.assertEqual(network.call_count, 2)
        self.assertFalse((path / "receipt.json").exists())

    def test_uncertain_buffer_request_never_retries(self):
        self.preview()
        with patch.object(
            editorial,
            "request",
            side_effect=[
                editorial.encoded({"data": self.channel()}),
                b"{}",
                editorial.Refusal("network failure"),
            ],
        ) as network:
            with self.assertRaisesRegex(editorial.Refusal, "claim remains"):
                editorial.run("publish", self.env, self.directory / "publish")
        self.assertEqual(network.call_count, 3)

    def test_status_is_read_only(self):
        self.env["EDITORIAL_POST_ID"] = "existing-post"
        value = {
            "data": {
                "post": {
                    "id": "existing-post",
                    "channelId": self.env["EDITORIAL_CHANNEL_ID"],
                    "status": "sent",
                    "externalLink": "https://www.linkedin.com/feed/update/test-post/",
                }
            }
        }
        with patch.object(
            editorial, "request", return_value=editorial.encoded(value)
        ) as fetch:
            editorial.run("status", self.env, self.directory / "status")
        fetch.assert_called_once()
        self.assertTrue(fetch.call_args.kwargs["data"]["query"].startswith("query"))
        self.assertFalse((self.directory / "status").exists())


if __name__ == "__main__":
    unittest.main()
