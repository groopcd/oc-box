import copy
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from tools import announce_release as social


def fixture(channel="stable"):
    version = "1.6.99" if channel == "stable" else "1.7.123.dev0"
    identity = {
        "distribution_version": version,
        "version": version + "+g" + "a" * 7,
        "commit": "a" * 40,
        "release_channel": channel,
        "tag": "v" + version,
    }
    release = {
        "id": 123,
        "tag_name": identity["tag"],
        "prerelease": channel == "devel",
        "draft": False,
        "published_at": "2026-11-16T00:00:00Z",
        "body": "## Highlights\n- **Private workspaces** with a bounded lifecycle.\n"
        "  Retains session history.\n- [Native tools](https://example.invalid)\n"
        "## Tests\n- This is not a feature.\n",
        "assets": [],
    }
    return identity, release


class MemoryGitHub:
    def __init__(self):
        self.files = {}
        self.writes = []
        self.fail_suffix = None

    def read_asset(self, release, name):
        data = self.files.get(name)
        return social.load(data) if data else None

    def asset(self, release, name):
        if name in self.files:
            data = self.files[name]
            return {
                "name": name,
                "size": len(data),
                "state": "uploaded",
                "digest": "sha256:" + social.sha(data),
            }
        return None

    def upload(self, release, name, data, content_type=None):
        if self.fail_suffix and name.endswith(self.fail_suffix):
            raise social.Refusal("fixture-upload")
        if name in self.files:
            raise social.Refusal("fixture-duplicate")
        self.files[name] = data
        self.writes.append(name)


class ReleaseSummaryTests(unittest.TestCase):
    def test_feature_block_and_continuation_not_tests(self):
        _, release = fixture()
        self.assertEqual(
            social.highlights(release["body"]),
            [
                "Private workspaces with a bounded lifecycle. Retains session history.",
                "Native tools",
            ],
        )

    def test_explicit_social_highlights_win_and_are_bounded_data(self):
        body = (
            "## Highlights\n- Other feature\n## LinkedIn highlights\n"
            "- $(touch /not-executed) `code` &amp; text\n"
            "- " + "x" * 2000 + "\n- third\n- fourth\n## Limits\n- no\n"
        )
        values = social.highlights(body)
        self.assertEqual(len(values), 3)
        self.assertIn("$(touch /not-executed)", values[0])
        self.assertLessEqual(len(values[1]), 350)

    def test_no_invented_highlights_and_code_fences_ignored(self):
        for body in ("", "## Tests\n- suite passed", "```\n## Highlights\n- code\n```"):
            with self.subTest(body=body), self.assertRaises(social.Refusal):
                social.highlights(body)

    def test_both_channels_version_link_and_explicit_devel_warning(self):
        for channel in ("stable", "devel"):
            with self.subTest(channel=channel):
                identity, release = fixture(channel)
                text, _ = social.content(identity, release)
                self.assertIn(identity["distribution_version"], text)
                self.assertIn("/releases/tag/" + identity["tag"], text)
                self.assertEqual(
                    "not a stable recommendation" in text, channel == "devel"
                )
                self.assertLessEqual(len(text), 2800)

    def test_control_and_markdown_cleanup(self):
        self.assertEqual(
            social.plain("a\x1b\u202e **b** ![x](https://x) [c](https://y)", 50),
            "a b c",
        )


class AnnouncementTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.image = Path(self.temporary.name) / "card.png"
        self.data = b"\x89PNG\r\n\x1a\nsynthetic-only"
        self.image.write_bytes(self.data)
        self.github = MemoryGitHub()
        self.buffer = Mock()
        self.identity, self.release = fixture()
        self.channel = "linkedin_fixture_123"
        self.text = social.content(self.identity, self.release)[0]
        self.post = {
            "id": "post_123",
            "channelId": self.channel,
            "text": self.text,
            "status": "sending",
        }
        self.buffer.create.return_value = self.post
        self.buffer.post.return_value = self.post
        self.download = patch.object(social, "request", return_value=self.data).start()
        self.addCleanup(patch.stopall)

    def run_announcement(self, **kwargs):
        return social.announce(
            self.github,
            self.buffer,
            self.identity,
            self.release,
            self.channel,
            self.image,
            **kwargs,
        )

    def test_success_for_both_channels_claim_precedes_create_and_receipt(self):
        for channel in ("stable", "devel"):
            with self.subTest(channel=channel):
                self.github = MemoryGitHub()
                self.identity, self.release = fixture(channel)
                text = social.content(self.identity, self.release)[0]
                post = {**self.post, "text": text}

                def create(*args):
                    self.assertTrue(self.github.writes[-1].endswith("-claim.json"))
                    self.assertFalse(
                        any(n.endswith("-receipt.json") for n in self.github.writes)
                    )
                    return post

                self.buffer.create.side_effect = create
                result = self.run_announcement()
                self.assertEqual(result["outcome"], "submitted-to-buffer")
                self.assertEqual(result["observed_buffer_status"], "sending")
                self.assertTrue(self.github.writes[-1].endswith("-receipt.json"))

    def test_rerun_uses_durable_receipt_without_creating_again(self):
        self.run_announcement()
        self.buffer.reset_mock()
        result = self.run_announcement()
        self.assertEqual(result["outcome"], "already-submitted")
        self.buffer.create.assert_not_called()
        self.buffer.channel.assert_not_called()

    def test_explicit_approved_text_does_not_use_or_modify_release_notes(self):
        self.release["body"] = "Existing release notes without a highlights block."
        original_body = self.release["body"]
        approved = "Introducing oc-box.\nTwo channels: stable and devel."
        self.buffer.create.return_value = {**self.post, "text": approved}
        social.announce(
            self.github,
            self.buffer,
            self.identity,
            self.release,
            self.channel,
            self.image,
            approved_text=approved,
        )
        self.assertEqual(self.buffer.create.call_args.args[1], approved)
        self.assertEqual(self.release["body"], original_body)
        self.buffer.create.reset_mock()
        social.announce(
            self.github,
            self.buffer,
            self.identity,
            self.release,
            self.channel,
            self.image,
            approved_text=approved,
        )
        self.buffer.create.assert_not_called()

    def test_claim_upload_failure_prevents_buffer_call(self):
        self.github.fail_suffix = "-claim.json"
        with self.assertRaises(social.Refusal):
            self.run_announcement()
        self.buffer.create.assert_not_called()

    def test_ambiguous_buffer_timeout_retains_claim_and_blocks_rerun(self):
        self.buffer.create.side_effect = social.Refusal("network-or-http-failure")
        with self.assertRaises(social.Refusal):
            self.run_announcement()
        self.buffer.create.reset_mock()
        with self.assertRaisesRegex(social.Refusal, "pending-claim"):
            self.run_announcement()
        self.buffer.create.assert_not_called()
        self.assertTrue(any(n.endswith("-claim.json") for n in self.github.files))

    def test_lost_receipt_can_only_reconcile_existing_post(self):
        self.github.fail_suffix = "-receipt.json"
        with self.assertRaises(social.Refusal):
            self.run_announcement()
        self.github.fail_suffix = None
        self.buffer.reset_mock()
        result = self.run_announcement(post_id="post_123")
        self.assertEqual(result["buffer_post_id"], "post_123")
        self.buffer.post.assert_called_once_with("post_123")
        self.buffer.create.assert_not_called()

    def test_wrong_reconciliation_channel_text_status_or_id_refused(self):
        self.buffer.create.side_effect = social.Refusal("fixture-timeout")
        with self.assertRaises(social.Refusal):
            self.run_announcement()
        for change in (
            {"channelId": "other"},
            {"text": "other"},
            {"status": "error"},
            {"status": "draft"},
            {"id": "other"},
        ):
            with self.subTest(change=change):
                self.buffer.post.return_value = {**self.post, **change}
                with self.assertRaises(social.Refusal):
                    self.run_announcement(post_id="post_123")

    def test_receipt_identity_tampering_is_not_silently_skipped(self):
        self.run_announcement()
        name = next(n for n in self.github.files if n.endswith("-receipt.json"))
        record = social.load(self.github.files[name])
        record["binding"]["commit"] = "b" * 40
        self.github.files[name] = social.encoded(record)
        with self.assertRaisesRegex(social.Refusal, "receipt-binding"):
            self.run_announcement()

    def test_unavailable_card_or_profile_prevents_claim_and_post(self):
        for failure in ("card", "profile"):
            with self.subTest(failure=failure):
                self.github = MemoryGitHub()
                self.download.return_value = (
                    b"wrong" if failure == "card" else self.data
                )
                self.buffer.channel.side_effect = (
                    social.Refusal("fixture") if failure == "profile" else None
                )
                with self.assertRaises(social.Refusal):
                    self.run_announcement()
                self.buffer.create.assert_not_called()
                self.assertFalse(
                    any(n.endswith("-claim.json") for n in self.github.files)
                )

    def test_invalid_claim_does_not_authorize_reconciliation(self):
        stem = social.prefix(social.binding(self.identity, self.release, self.channel))
        self.github.files[stem + "-claim.json"] = social.encoded({"binding": {}})
        with self.assertRaises(social.Refusal):
            self.run_announcement(post_id="post_123")
        self.buffer.create.assert_not_called()
        self.buffer.post.assert_not_called()


class BufferClientTests(unittest.TestCase):
    @patch.object(social, "request")
    def test_graphql_and_typed_mutation_errors_are_not_success(self, request):
        client = social.Buffer("synthetic-secret")
        for response in (
            {"errors": [{"message": "synthetic-secret"}], "data": {"createPost": {}}},
            {"data": {"createPost": {"message": "synthetic-secret"}}},
            {"data": None},
        ):
            request.return_value = social.encoded(response)
            with self.subTest(response=response), self.assertRaises(social.Refusal):
                client.create("channel", "text", "https://example.invalid/image.png")

    @patch.object(social, "request")
    def test_create_uses_variables_image_and_automatic_share_now(self, request):
        request.return_value = social.encoded(
            {"data": {"createPost": {"post": {"id": "post"}}}}
        )
        social.Buffer("synthetic-secret").create(
            "channel", '"quoted"\ntext', "https://example.invalid/card.png"
        )
        payload = social.load(request.call_args.kwargs["data"])
        self.assertNotIn('"quoted"', payload["query"])
        self.assertEqual(payload["variables"]["input"]["mode"], "shareNow")
        self.assertEqual(payload["variables"]["input"]["schedulingType"], "automatic")
        self.assertEqual(len(payload["variables"]["input"]["assets"]), 1)

    def test_channel_must_be_connected_personal_linkedin_not_a_page(self):
        channel = {
            "id": "channel",
            "service": "linkedin",
            "type": "profile",
            "isDisconnected": False,
            "isLocked": False,
            "isQueuePaused": False,
        }
        client = social.Buffer("synthetic-secret")
        for change in (
            {"service": "facebook"},
            {"type": "page"},
            {"isLocked": True},
            {"isDisconnected": True},
            {"isQueuePaused": True},
            {"id": "other"},
        ):
            with (
                self.subTest(change=change),
                patch.object(
                    client, "query", return_value={"channel": {**channel, **change}}
                ),
            ):
                with self.assertRaises(social.Refusal):
                    client.channel("channel")

    def test_profile_discovery_is_queries_only_and_omits_email(self):
        client = social.Buffer("synthetic-secret")
        client.query = Mock(
            side_effect=[
                {"account": {"organizations": [{"id": "org"}]}},
                {
                    "channels": [
                        {"id": "yes", "service": "linkedin", "type": "profile"},
                        {"id": "no", "service": "linkedin", "type": "page"},
                    ]
                },
            ]
        )
        self.assertEqual([v["id"] for v in client.channels()], ["yes"])
        for call in client.query.call_args_list:
            self.assertNotIn("mutation", call.args[0])
            self.assertNotIn("Email", call.args[0])


class NetworkAndEntryTests(unittest.TestCase):
    def test_public_download_cannot_have_a_token_or_bad_origin(self):
        for url in (
            "http://github.com/x",
            "https://github.com.evil.invalid/x",
            "https://github.com@evil.invalid/x",
            "https://github.com:443/x",
            "https://127.0.0.1/x",
        ):
            with self.subTest(url=url), self.assertRaises(social.Refusal):
                social.request(url, public=True)
        with self.assertRaises(social.Refusal):
            social.request(
                "https://github.com/x", public=True, token="synthetic-secret"
            )
        with self.assertRaises(social.Refusal):
            social.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil.invalid"
            )

    @patch.object(social, "build_opener")
    def test_mutation_network_failure_is_not_retried(self, build):
        build.return_value.open.side_effect = URLError("synthetic-secret")
        with self.assertRaisesRegex(social.Refusal, "network-or-http-failure"):
            social.request(
                "https://api.buffer.com",
                method="POST",
                token="synthetic-secret",
                data=b"{}",
            )
        self.assertEqual(build.return_value.open.call_count, 1)

    @patch.object(social, "request")
    @patch.object(social.check_release_artifacts, "check_archives")
    def test_public_pypi_verification_binds_downloads_and_embedded_identity(
        self, archives, request
    ):
        identity, _ = fixture()
        files = []
        data = b"synthetic archive"
        for name in ("oc_box-1.6.99-py3-none-any.whl", "oc_box-1.6.99.tar.gz"):
            files.append(
                {
                    "filename": name,
                    "url": "https://files.pythonhosted.org/" + name,
                    "yanked": False,
                    "size": len(data),
                    "digests": {"sha256": social.sha(data)},
                }
            )
        metadata = {"info": {"version": "1.6.99"}, "urls": files}
        for change in (None, "digest", "origin", "yanked", "missing"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as work:
                value = copy.deepcopy(metadata)
                if change == "digest":
                    value["urls"][0]["digests"]["sha256"] = "b" * 64
                if change == "origin":
                    value["urls"][0]["url"] = "https://evil.invalid/file"
                if change == "yanked":
                    value["urls"][0]["yanked"] = True
                if change == "missing":
                    value["urls"].pop()
                request.side_effect = [social.encoded(value), data, data]
                if change is None:
                    social.verify_pypi(identity, Path(work))
                    archives.assert_called_with(Path(work), identity)
                else:
                    with self.assertRaises(social.Refusal):
                        social.verify_pypi(identity, Path(work))

    def test_disabled_or_wrong_repository_never_reaches_network(self):
        for environment in (
            {},
            {"GITHUB_REPOSITORY": "someone/fork"},
            {"GITHUB_REPOSITORY": social.REPOSITORY},
        ):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                patch.object(social, "request") as network,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(social.main(["announce", "--tag", "v1.6.99"]), 1)
                network.assert_not_called()

    def test_errors_do_not_log_credentials_or_raw_body(self):
        env = {
            "GITHUB_REPOSITORY": social.REPOSITORY,
            "BUFFER_API_KEY": "synthetic-secret",
        }
        errors, output = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                social.Buffer, "channels", side_effect=ValueError("synthetic-secret")
            ),
            redirect_stderr(errors),
            redirect_stdout(output),
        ):
            self.assertEqual(social.main(["channels"]), 1)
        self.assertNotIn("synthetic-secret", errors.getvalue() + output.getvalue())

    def test_preview_has_no_buffer_or_release_asset_mutations(self):
        identity, release = fixture()
        env = {"GITHUB_REPOSITORY": social.REPOSITORY}
        with (
            tempfile.TemporaryDirectory() as work,
            patch.dict(os.environ, env, clear=True),
            patch.object(social.GitHub, "release", return_value=release),
            patch.object(social.check_release, "validate", return_value=identity),
            patch.object(social.release_card, "render_card") as render,
            patch.object(social.GitHub, "upload") as upload,
            patch.object(social, "Buffer") as buffer,
            redirect_stdout(io.StringIO()),
        ):
            output = Path(work) / "preview"
            self.assertEqual(
                social.main(
                    ["preview", "--tag", identity["tag"], "--output", str(output)]
                ),
                0,
            )
            self.assertTrue((output / "linkedin-post.txt").exists())
            render.assert_called_once()
            upload.assert_not_called()
            buffer.assert_not_called()


class EnabledEntryGateTests(unittest.TestCase):
    def setUp(self):
        self.identity, self.release = fixture()
        self.env = {
            "GITHUB_REPOSITORY": social.REPOSITORY,
            "LINKEDIN_ANNOUNCEMENTS": "true",
            "BUFFER_API_KEY": "synthetic-secret",
            "BUFFER_LINKEDIN_CHANNEL_ID": "profile_123",
        }
        self.github, self.buffer = MemoryGitHub(), Mock()
        self.events = []
        text = social.content(self.identity, self.release)[0]
        self.post = {
            "id": "post_123",
            "channelId": "profile_123",
            "text": text,
            "status": "sent",
        }
        self.data = b"\x89PNG\r\n\x1a\nsynthetic-only"

        def validate(*args, **kwargs):
            self.events.append("source")
            return self.identity

        def published(*args):
            self.events.append("pypi")

        def render(output, **kwargs):
            output.write_bytes(self.data)

        upload = self.github.upload

        def observed_upload(release, name, data, content_type=None):
            self.events.append("claim" if name.endswith("-claim.json") else "asset")
            return upload(release, name, data, content_type)

        def create(*args):
            self.assertLess(self.events.index("source"), self.events.index("pypi"))
            self.assertLess(self.events.index("pypi"), self.events.index("claim"))
            self.events.append("create")
            return self.post

        self.github.upload = observed_upload
        self.github.release = Mock(return_value=self.release)
        self.buffer.create.side_effect = create
        self.buffer.post.return_value = self.post
        self.validate = patch.object(
            social.check_release, "validate", side_effect=validate
        ).start()
        self.pypi = patch.object(social, "verify_pypi", side_effect=published).start()
        patch.object(social, "GitHub", return_value=self.github).start()
        patch.object(social, "Buffer", return_value=self.buffer).start()
        patch.object(social.release_card, "render_card", side_effect=render).start()
        patch.object(social, "request", return_value=self.data).start()
        patch.dict(os.environ, self.env, clear=True).start()
        self.addCleanup(patch.stopall)

    def run_main(self, action="announce", *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return social.main([action, "--tag", self.identity["tag"], *extra])

    def test_enabled_success_requires_publish_validation_before_claim_or_post(self):
        self.assertEqual(self.run_main(), 0)
        self.validate.assert_called_once_with(
            ".", publish=True, tag=self.identity["tag"], release=self.release
        )
        self.assertEqual(self.events[:3], ["source", "pypi", "asset"])
        self.assertIn("create", self.events)
        self.buffer.create.assert_called_once()

    def test_source_or_pypi_refusal_prevents_every_mutation_in_both_modes(self):
        for gate in ("source", "pypi"):
            for mode in ("announce", "reconcile"):
                with self.subTest(gate=gate, mode=mode):
                    self.validate.side_effect = (
                        ValueError("fixture")
                        if gate == "source"
                        else lambda *a, **k: self.identity
                    )
                    self.pypi.side_effect = (
                        ValueError("fixture") if gate == "pypi" else None
                    )
                    self.assertEqual(
                        self.run_main(
                            mode,
                            *(["--post-id", "post_123"] if mode == "reconcile" else []),
                        ),
                        1,
                    )
                    self.assertEqual(self.github.writes, [])
                    self.buffer.create.assert_not_called()
                    self.buffer.post.assert_not_called()
                    self.buffer.channel.assert_not_called()

    def test_enabled_reconcile_validates_source_and_pypi_without_create(self):
        bound = social.binding(self.identity, self.release, "profile_123")
        name = social.prefix(bound) + "-claim.json"
        self.github.files[name] = social.encoded(
            {
                "binding": bound,
                "text_sha256": social.sha(self.post["text"].encode()),
                "image_sha256": social.sha(self.data),
            }
        )
        self.assertEqual(self.run_main("reconcile", "--post-id", "post_123"), 0)
        self.validate.assert_called_once_with(
            ".", publish=True, tag=self.identity["tag"], release=self.release
        )
        self.pypi.assert_called_once()
        self.buffer.create.assert_not_called()
        self.buffer.post.assert_called_once_with("post_123")


class PublicPackageIdentityTests(unittest.TestCase):
    """Run the actual archive validator through the announcement's PyPI gate."""

    def packages(self, directory, identity, source, alteration):
        build = {**identity}
        if alteration == "commit":
            build["commit"] = "b" * 40
        if alteration == "channel":
            build["release_channel"] = "devel"
        archived_source = source + (b"\n# modified" if alteration == "source" else b"")
        version = identity["distribution_version"]
        metadata = f"Name: oc-box\nVersion: {version}\n\n".encode()
        wheel = directory / f"oc_box-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("oc_box/version_info.py", archived_source)
            archive.writestr("oc_box/build-info.json", social.encoded(build))
            archive.writestr(
                "oc_box/distribution.json",
                social.encoded(
                    {
                        "installer": "python-package",
                        "name": "oc-box",
                        "version": version,
                    }
                ),
            )
            archive.writestr(f"oc_box-{version}.dist-info/METADATA", metadata)
        sdist = directory / f"oc_box-{version}.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            for name, data in {
                "src/oc_box/version_info.py": archived_source,
                "build-info.json": social.encoded(build),
                "PKG-INFO": metadata,
            }.items():
                member = tarfile.TarInfo(f"oc_box-{version}/" + name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return [wheel, sdist]

    def test_public_downloads_refuse_different_embedded_commit_channel_or_source(self):
        source = (
            b'VERSION_SERIES = "1.6"\nRELEASE_CHANNEL = "stable"\n'
            b'VERSION_BASE_COMMIT_COUNT = 29\nraise RuntimeError("never execute")\n'
        )
        identity, _ = fixture()
        identity.update(
            commit_count=128,
            dirty=False,
            publish_validated=True,
            source_sha256=hashlib.sha256(source).hexdigest(),
        )
        for alteration in (None, "commit", "channel", "source"):
            with (
                self.subTest(alteration=alteration),
                tempfile.TemporaryDirectory() as work,
            ):
                root = Path(work)
                built, downloaded = root / "built", root / "downloaded"
                built.mkdir()
                downloaded.mkdir()
                paths = self.packages(built, identity, source, alteration)
                files = [
                    {
                        "filename": p.name,
                        "url": "https://files.pythonhosted.org/" + p.name,
                        "size": p.stat().st_size,
                        "yanked": False,
                        "digests": {"sha256": social.sha(p.read_bytes())},
                    }
                    for p in paths
                ]
                metadata = {
                    "info": {"version": identity["distribution_version"]},
                    "urls": files,
                }
                responses = [
                    json.dumps(metadata).encode(),
                    *(p.read_bytes() for p in paths),
                ]
                with patch.object(social, "request", side_effect=responses):
                    if alteration is None:
                        social.verify_pypi(identity, downloaded)
                    else:
                        with self.assertRaises(ValueError):
                            social.verify_pypi(identity, downloaded)


if __name__ == "__main__":
    unittest.main()
