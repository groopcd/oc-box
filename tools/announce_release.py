#!/usr/bin/env python3
"""Optional release-to-LinkedIn bridge through Buffer; no publishing by default.

Credentials are read only from the Actions step environment. No credential,
request body, signed URL or remote error body is logged. Mutation requests are
never retried. Release notes are data, never shell commands or model prompts.
"""

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__:
    from . import check_release, check_release_artifacts, release_card
else:
    import check_release
    import check_release_artifacts
    import release_card


REPOSITORY = "groopcd/oc-box"
API = "https://api.github.com/repos/" + REPOSITORY
TAG = re.compile(r"v\d+\.\d+\.\d+(?:\.dev0)?")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_JSON = 4 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_POST_UTF16 = 3000
# Trusted workflow preflight parses this literal without executing selected code.
# Version 1 requires exact reviewed text/card hashes before new devel mutations.
DEVEL_ANNOUNCEMENT_APPROVAL_POLICY = 1
POST_START = "<!-- linkedin-post:start -->"
POST_END = "<!-- linkedin-post:end -->"
TUI_IMAGE_SOURCE = Path(__file__).resolve().parents[1] / "docs/assets/readme-hero.png"
CHANNEL_FIELDS = (
    "id name displayName service type isDisconnected isLocked isQueuePaused"
)


class Refusal(Exception):
    """Fixed-category refusal; never include private remote exception text."""


def require(condition, category):
    if not condition:
        raise Refusal(category)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(data):
    return check_release_artifacts.load_json(data)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refusal("unexpected-redirect")


class PublicRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, response, code, message, headers, new_url):
        require(public_url(new_url), "public-download-origin")
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def public_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname
        in {
            "github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
        }
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def request(
    url,
    *,
    method="GET",
    token=None,
    data=None,
    limit=MAX_JSON,
    content_type="application/json",
    public=False,
):
    if public:
        require(token is None and method == "GET" and public_url(url), "public-request")
    else:
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            in {"api.github.com", "uploads.github.com", "api.buffer.com"}
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment,
            "api-origin",
        )
    headers = {
        "User-Agent": "oc-box-release-announcements",
        "Accept": "application/json",
    }
    if token:
        require(
            isinstance(token, str) and "\n" not in token and "\r" not in token,
            "credential",
        )
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = content_type
    opener = build_opener(
        ProxyHandler({}), PublicRedirect() if public else NoRedirect()
    )
    try:
        with opener.open(
            Request(url, data=data, headers=headers, method=method), timeout=30
        ) as reply:
            require(reply.status in (200, 201), "http-status")
            body = reply.read(limit + 1)
            require(len(body) <= limit, "response-size")
            return body
    except (HTTPError, URLError, OSError, HTTPException):
        # A mutation might already have succeeded. Never retry here.
        raise Refusal("network-or-http-failure") from None


class GitHub:
    def __init__(self, token=None):
        self.token = token

    def release(self, tag):
        require(isinstance(tag, str) and TAG.fullmatch(tag), "release-tag")
        value = load(request(API + "/releases/tags/" + tag, token=self.token))
        require(
            type(value) is dict
            and type(value.get("id")) is int
            and value["id"] > 0
            and value.get("tag_name") == tag
            and value.get("draft") is False
            and type(value.get("prerelease")) is bool
            and isinstance(value.get("published_at"), str)
            and isinstance(value.get("body"), str)
            and len(value["body"].encode()) <= 128 * 1024
            and isinstance(value.get("assets"), list)
            and len(value["assets"]) <= 1000,
            "release-metadata",
        )
        return value

    def asset(self, release, name):
        matches = [item for item in release["assets"] if item.get("name") == name]
        require(len(matches) <= 1, "duplicate-asset")
        return matches[0] if matches else None

    def read_asset(self, release, name):
        item = self.asset(release, name)
        if item is None:
            return None
        require(
            item.get("state") == "uploaded"
            and type(item.get("size")) is int
            and 0 < item["size"] <= MAX_JSON,
            "asset-metadata",
        )
        body = request(asset_url(release["tag_name"], name), public=True)
        require(len(body) == item["size"], "asset-size")
        if item.get("digest"):
            require(item["digest"] == "sha256:" + sha(body), "asset-digest")
        return load(body)

    def upload(self, release, name, data, content_type="application/json"):
        require(self.token, "github-write-token-required")
        require(re.fullmatch(r"linkedin-[A-Za-z0-9_.-]+", name), "asset-name")
        require(0 < len(data) <= MAX_IMAGE, "asset-size")
        # GitHub refuses a duplicate name atomically; never delete/overwrite it.
        result = load(
            request(
                f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release['id']}"
                "/assets?name=" + quote(name),
                method="POST",
                token=self.token,
                data=data,
                content_type=content_type,
            )
        )
        require(
            result.get("name") == name
            and result.get("state") == "uploaded"
            and result.get("size") == len(data),
            "asset-upload-unverified",
        )


def asset_url(tag, name):
    require(
        TAG.fullmatch(tag) and re.fullmatch(r"linkedin-[A-Za-z0-9_.-]+", name),
        "asset-selector",
    )
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"


class Buffer:
    def __init__(self, token):
        require(bool(token), "buffer-key-required")
        self.token = token

    def query(self, query, variables=None):
        result = load(
            request(
                "https://api.buffer.com",
                method="POST",
                token=self.token,
                data=encoded({"query": query, "variables": variables or {}}),
            )
        )
        require(
            type(result) is dict
            and not result.get("errors")
            and type(result.get("data")) is dict,
            "buffer-api-error",
        )
        return result["data"]

    def channels(self):
        organizations = self.query("query { account { organizations { id } } }")[
            "account"
        ]["organizations"]
        require(
            type(organizations) is list and len(organizations) <= 20, "organizations"
        )
        found = []
        for organization in organizations:
            values = self.query(
                "query($input: ChannelsInput!) { channels(input: $input) { "
                + CHANNEL_FIELDS
                + " } }",
                {"input": {"organizationId": organization["id"]}},
            )["channels"]
            require(type(values) is list and len(values) <= 100, "channels")
            found.extend(
                v
                for v in values
                if v.get("service") == "linkedin" and v.get("type") == "profile"
            )
        return found

    def channel(self, channel_id):
        require(
            isinstance(channel_id, str) and IDENTIFIER.fullmatch(channel_id),
            "channel-id",
        )
        value = self.query(
            "query($input: ChannelInput!) { channel(input: $input) { "
            + CHANNEL_FIELDS
            + " } }",
            {"input": {"id": channel_id}},
        )["channel"]
        require(
            value.get("id") == channel_id
            and value.get("service") == "linkedin"
            and value.get("type") == "profile"
            and value.get("isDisconnected") is False
            and value.get("isLocked") is False
            and value.get("isQueuePaused") is False,
            "linkedin-profile-unavailable",
        )
        return value

    def create(self, channel_id, text, image_url):
        value = self.query(
            """mutation($input: CreatePostInput!) {
              createPost(input: $input) {
                ... on PostActionSuccess { post { id channelId text status } }
                ... on MutationError { message }
              }
            }""",
            {
                "input": {
                    "channelId": channel_id,
                    "text": text,
                    "schedulingType": "automatic",
                    "mode": "shareNow",
                    "assets": [{"image": {"url": image_url}}],
                }
            },
        )["createPost"]
        require(type(value.get("post")) is dict, "buffer-post-refused")
        return value["post"]

    def post(self, post_id):
        require(isinstance(post_id, str) and IDENTIFIER.fullmatch(post_id), "post-id")
        return self.query(
            "query($input: PostInput!) { post(input: $input) { id channelId text status } }",
            {"input": {"id": post_id}},
        )["post"]


def plain(text, limit):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = html.unescape(re.sub(r"[`*_~]", "", text))
    text = " ".join(
        "".join(
            c if not unicodedata.category(c).startswith("C") else " " for c in text
        ).split()
    )
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def highlights(body):
    """Prefer a small explicit block; otherwise use the release Highlights."""
    for wanted in ("linkedin highlights", "highlights", "what's new"):
        items, active, level, fenced = [], False, 0, False
        for line in body.splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            if fenced:
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                if active and len(heading[1]) <= level:
                    break
                if heading[2].lower() == wanted:
                    active, level = True, len(heading[1])
                continue
            bullet = re.match(r"^\s*[-*+]\s+(.+)", line)
            if active and bullet:
                text = plain(bullet[1], 350)
                if text and len(items) < 3:
                    items.append(text)
            elif active and items and line.startswith(("  ", "\t")) and line.strip():
                items[-1] = plain(items[-1] + " " + line.strip(), 350)
        if items:
            return items
    # Do not invent features or summarize unrelated commits.
    raise Refusal("release-highlights-required")


def full_post(body):
    """Extract exact public copy, excluding only marker framing line breaks."""
    if POST_START not in body and POST_END not in body:
        return None
    require(body.count(POST_START) == body.count(POST_END) == 1, "post-block")
    match = re.search(
        r"^"
        + re.escape(POST_START)
        + r"\r?\n([\s\S]*?)\r?\n"
        + re.escape(POST_END)
        + r"\r?$",
        body,
        re.MULTILINE,
    )
    require(match is not None, "post-block")
    # GitHub's editor may use CRLF; the reviewed/exported post is always UTF-8/LF.
    text = match[1].replace("\r\n", "\n")
    require(
        bool(text.strip())
        and all(c == "\n" or not unicodedata.category(c).startswith("C") for c in text),
        "post-control-or-empty",
    )
    return text


def content(identity, release):
    features = highlights(release["body"])
    channel = identity["release_channel"]
    label = (
        "stable release"
        if channel == "stable"
        else "development preview — experimental"
    )
    heading = f"oc-box {identity['distribution_version']} · {label}"
    link = (
        f"Release notes: https://github.com/{REPOSITORY}/releases/tag/{identity['tag']}"
    )
    warning = "This is a development prerelease, not a stable recommendation."
    custom = full_post(release["body"])
    if custom is not None:
        # Full copy is never truncated, reformatted, or extended after review.
        require(custom.split("\n")[0] == heading, "post-release-heading")
        require(link in custom.split("\n"), "post-release-link")
        if channel == "devel":
            require(warning in custom.split("\n"), "post-devel-warning")
        require(len(custom.encode("utf-16-le")) // 2 <= MAX_POST_UTF16, "post-length")
        return custom, features
    text = heading + "\n\n"
    text += "\n".join("• " + item for item in features)
    if channel == "devel":
        text += "\n\n" + warning
    text += "\n\n" + link
    text += "\n\n#OpenSource #Linux #DeveloperTools"
    require(len(text) <= 2800, "post-length")
    require(len(text.encode("utf-16-le")) // 2 <= MAX_POST_UTF16, "post-length")
    return text, features


def render_image(output, identity, release, features):
    """Choose only the generic card or the committed README TUI screenshot."""
    selectors = [
        line
        for line in release["body"].replace("\r\n", "\n").splitlines()
        if "<!-- linkedin-image:" in line
    ]
    if not selectors:
        release_card.render_card(
            output,
            version=identity["distribution_version"],
            channel=identity["release_channel"],
            highlights=features,
        )
        return
    require(selectors == ["<!-- linkedin-image:readme-tui -->"], "card-selector")
    require(
        TUI_IMAGE_SOURCE.is_file() and not TUI_IMAGE_SOURCE.is_symlink(), "card-image"
    )
    with TUI_IMAGE_SOURCE.open("rb") as stream:
        data = stream.read(MAX_IMAGE + 1)
    require(
        0 < len(data) <= MAX_IMAGE and data.startswith(b"\x89PNG\r\n\x1a\n"),
        "card-image",
    )
    output.write_bytes(data)


def approved_content(
    identity, text, image, expected_text_sha256, expected_image_sha256
):
    """Devel creation needs exact reviewed bytes before *any* remote mutation."""
    expected = (expected_text_sha256, expected_image_sha256)
    if identity["release_channel"] != "devel" and expected == (None, None):
        return  # Preserve optional stable automation.
    require(
        all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in expected),
        "approval-hashes-required",
    )
    require(
        sha(text.encode("utf-8")) == expected_text_sha256
        and sha(image) == expected_image_sha256,
        "approved-content-changed",
    )


def verify_pypi(identity, directory):
    """Verify public package bytes/embedded source; never execute downloaded code."""
    version = identity["distribution_version"]
    metadata = load(
        request(f"https://pypi.org/pypi/oc-box/{version}/json", public=True)
    )
    require(metadata.get("info", {}).get("version") == version, "pypi-version")
    files = metadata.get("urls")
    require(type(files) is list and len(files) == 2, "pypi-files")
    expected = {f"oc_box-{version}-py3-none-any.whl", f"oc_box-{version}.tar.gz"}
    seen = set()
    for item in files:
        name, url = item.get("filename"), item.get("url")
        require(
            name in expected and name not in seen and item.get("yanked") is False,
            "pypi-file",
        )
        require(
            isinstance(url, str)
            and public_url(url)
            and urlsplit(url).hostname == "files.pythonhosted.org"
            and type(item.get("size")) is int
            and 0 < item["size"] <= check_release_artifacts.MAX_ARCHIVE,
            "pypi-download",
        )
        data = request(url, public=True, limit=check_release_artifacts.MAX_ARCHIVE)
        require(
            len(data) == item["size"]
            and sha(data) == item.get("digests", {}).get("sha256"),
            "pypi-digest",
        )
        (directory / name).write_bytes(data)
        seen.add(name)
    require(seen == expected, "pypi-files")
    check_release_artifacts.check_archives(directory, identity)


def binding(identity, release, channel_id):
    require(
        isinstance(channel_id, str) and IDENTIFIER.fullmatch(channel_id), "channel-id"
    )
    return {
        "schema": 1,
        "repository": REPOSITORY,
        "release_id": release["id"],
        "tag": identity["tag"],
        "commit": identity["commit"],
        "release_channel": identity["release_channel"],
        "buffer_channel_sha256": sha(channel_id.encode()),
    }


def prefix(bound):
    return f"linkedin-{bound['release_channel']}-{bound['buffer_channel_sha256'][:16]}"


def matched_record(record, bound):
    require(type(record) is dict and record.get("binding") == bound, "receipt-binding")
    require(
        re.fullmatch(r"[a-f0-9]{64}", record.get("text_sha256", "")) is not None
        and re.fullmatch(r"[a-f0-9]{64}", record.get("image_sha256", "")) is not None,
        "receipt-digest",
    )


def valid_post(post, channel_id, text_sha, post_id=None):
    require(
        type(post) is dict
        and isinstance(post.get("id"), str)
        and IDENTIFIER.fullmatch(post["id"])
        and (post_id is None or post["id"] == post_id)
        and post.get("channelId") == channel_id
        and isinstance(post.get("text"), str)
        and sha(post["text"].encode()) == text_sha
        and post.get("status") in {"scheduled", "sending", "sent"},
        "buffer-post-unverified",
    )


def announce(
    github,
    buffer,
    identity,
    release,
    channel_id,
    image,
    *,
    post_id=None,
    expected_text_sha256=None,
    expected_image_sha256=None,
):
    """Durable claim BEFORE createPost; a missing receipt always stops a rerun."""
    bound = binding(identity, release, channel_id)
    stem = prefix(bound)
    claim_name, receipt_name = stem + "-claim.json", stem + "-receipt.json"
    receipt = github.read_asset(release, receipt_name)
    if receipt is not None:
        matched_record(receipt, bound)
        require(
            isinstance(receipt.get("buffer_post_id"), str)
            and IDENTIFIER.fullmatch(receipt["buffer_post_id"]),
            "receipt-post",
        )
        return {
            "outcome": "already-submitted",
            "buffer_post_id": receipt["buffer_post_id"],
        }
    claim = github.read_asset(release, claim_name)
    if claim is not None:
        matched_record(claim, bound)
        require(post_id is not None, "pending-claim-reconcile-in-buffer")
        buffer.channel(channel_id)
        post = buffer.post(post_id)  # Read-only reconciliation; never calls create.
        valid_post(post, channel_id, claim["text_sha256"], post_id)
    else:
        require(post_id is None, "reconciliation-claim-required")
        text, _ = content(identity, release)
        data = image.read_bytes()
        require(
            0 < len(data) <= MAX_IMAGE and data.startswith(b"\x89PNG\r\n\x1a\n"),
            "card-image",
        )
        approved_content(
            identity, text, data, expected_text_sha256, expected_image_sha256
        )
        buffer.channel(channel_id)  # Never auto-select another connected account.
        claim = {
            "binding": bound,
            "text_sha256": sha(text.encode()),
            "image_sha256": sha(data),
        }
        image_name = stem + "-" + sha(data)[:16] + ".png"
        existing = github.asset(release, image_name)
        if existing is None:
            github.upload(release, image_name, data, "image/png")
        else:
            require(
                existing.get("state") == "uploaded"
                and existing.get("size") == len(data)
                and existing.get("digest") in (None, "", "sha256:" + sha(data)),
                "existing-card",
            )
        # Buffer fetches media anonymously; verify public bytes before claiming
        # the one permitted createPost attempt.
        require(
            request(
                asset_url(identity["tag"], image_name), public=True, limit=MAX_IMAGE
            )
            == data,
            "public-card-unavailable",
        )
        github.upload(release, claim_name, encoded(claim))
        post = buffer.create(channel_id, text, asset_url(identity["tag"], image_name))
        valid_post(post, channel_id, claim["text_sha256"])
    result = {
        **claim,
        "buffer_post_id": post["id"],
        "observed_buffer_status": post["status"],
    }
    github.upload(release, receipt_name, encoded(result))
    return {
        "outcome": "submitted-to-buffer",
        "buffer_post_id": post["id"],
        "observed_buffer_status": post["status"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("channels", "preview", "announce", "reconcile")
    )
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--post-id")
    parser.add_argument("--expected-text-sha256")
    parser.add_argument("--expected-image-sha256")
    args = parser.parse_args(argv)
    try:
        require(
            os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
            "canonical-repository-required",
        )
        require(
            args.action == "announce"
            or (
                args.expected_text_sha256 is None and args.expected_image_sha256 is None
            ),
            "invalid-options",
        )
        if args.action == "channels":
            require(args.tag is None and args.post_id is None, "invalid-options")
            rows = Buffer(os.environ.get("BUFFER_API_KEY")).channels()
            # Only public profile labels/IDs and availability; no account email or token.
            print(json.dumps({"linkedin_profiles": rows}, ensure_ascii=True))
            return 0
        require(isinstance(args.tag, str) and TAG.fullmatch(args.tag), "release-tag")
        require(
            (args.post_id is not None) == (args.action == "reconcile"),
            "invalid-options",
        )
        if args.action in ("announce", "reconcile"):
            require(
                os.environ.get("LINKEDIN_ANNOUNCEMENTS") == "true", "posting-disabled"
            )
        github = GitHub(os.environ.get("ANNOUNCEMENT_GITHUB_TOKEN"))
        release = github.release(args.tag)
        identity = check_release.validate(
            ".", publish=True, tag=args.tag, release=release
        )
        text, features = content(identity, release)
        with tempfile.TemporaryDirectory(prefix="oc-box-announcement-") as work:
            work = Path(work)
            if args.action == "preview":
                require(
                    args.output is not None and not args.output.exists(),
                    "new-preview-directory-required",
                )
                args.output.mkdir(parents=True)
                render_image(
                    args.output / "linkedin-card.png", identity, release, features
                )
                # The text file hashes the actual Buffer payload (no added newline).
                (args.output / "linkedin-post.txt").write_bytes(text.encode("utf-8"))
                manifest = {
                    "schema": 1,
                    "repository": REPOSITORY,
                    "release_id": release["id"],
                    "tag": identity["tag"],
                    "commit": identity["commit"],
                    "release_channel": identity["release_channel"],
                    "text_sha256": sha(text.encode("utf-8")),
                    "image_sha256": sha(
                        (args.output / "linkedin-card.png").read_bytes()
                    ),
                    "text_utf16_units": len(text.encode("utf-16-le")) // 2,
                }
                (args.output / "linkedin-approval.json").write_bytes(encoded(manifest))
                print("Preview generated; no assets uploaded or posts created.")
            else:
                verify_pypi(identity, work)
                image = work / "card.png"
                render_image(image, identity, release, features)
                result = announce(
                    github,
                    Buffer(os.environ.get("BUFFER_API_KEY")),
                    identity,
                    release,
                    os.environ.get("BUFFER_LINKEDIN_CHANNEL_ID"),
                    image,
                    post_id=args.post_id,
                    expected_text_sha256=args.expected_text_sha256,
                    expected_image_sha256=args.expected_image_sha256,
                )
                print(json.dumps(result, sort_keys=True))
        return 0
    except (
        Refusal,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
        HTTPException,
    ):
        # No raw response, credentials, exception text or release body in logs.
        error = sys.exc_info()[1]
        category = (
            str(error) if isinstance(error, Refusal) else "invalid-input-or-response"
        )
        print("Release announcement refused: " + category, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
