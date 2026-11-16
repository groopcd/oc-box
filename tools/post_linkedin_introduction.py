#!/usr/bin/env python3
"""Send the exact approved first introduction, never a new software release.

One-off controller for the existing v1.6.94 release. Frozen input digests prevent
silent copy/image changes after approval. Reuses the normal durable claim and
receipt, so an uncertain result cannot trigger another createPost request.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from . import announce_release as social
else:
    import announce_release as social


TAG = "v1.6.94"
COMMIT = "9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22"
RELEASE_ID = 446216826
PROFILE_SHA = "3c59f81604371a60f2f0d6fbeafc9586ed7a60a5534d7f3652349ccdcc5645e5"
INPUTS = (
    Path(__file__).resolve().parents[1]
    / ".github/announcements/oc-box-introduction-20261116"
)
DIGESTS = {
    "post.txt": "e054aec515ac8920800790c035c0be4661b76420fc32074da788514781a1e0fb",
    "image.png": "0d17192f980c541755520056d6b382f08952170d9b7c2698f5d0a2797fa39632",
}


def approved_inputs():
    for name, digest in DIGESTS.items():
        data = social.check_release_artifacts.bounded_file(
            INPUTS / name, social.MAX_IMAGE
        )
        social.require(social.sha(data) == digest, "approved-input-changed")
    text = (INPUTS / "post.txt").read_text(encoding="utf-8").rstrip("\n")
    social.require(0 < len(text) <= 2800, "approved-text")
    return text, INPUTS / "image.png"


def target():
    value = os.environ.get("BUFFER_LINKEDIN_CHANNEL_ID")
    social.require(
        isinstance(value, str) and social.sha(value.encode()) == PROFILE_SHA,
        "approved-profile-required",
    )
    return value


def publish(source):
    social.require(
        os.environ.get("LINKEDIN_ANNOUNCEMENTS") == "true", "posting-disabled"
    )
    text, image = approved_inputs()
    channel_id = target()
    github = social.GitHub(os.environ.get("ANNOUNCEMENT_GITHUB_TOKEN"))
    release = github.release(TAG)
    social.require(
        release["id"] == RELEASE_ID and release["prerelease"] is False,
        "approved-release-required",
    )
    latest = social.load(
        social.request(social.API + "/releases/latest", token=github.token)
    )
    social.require(latest.get("id") == RELEASE_ID, "stable-release-changed")
    # The controller is new, but the advertised source is the existing stable
    # tag in a separate clean checkout. Old release code is parsed, not executed.
    identity = social.check_release.validate(
        source, publish=True, tag=TAG, release=release
    )
    social.require(
        identity["commit"] == COMMIT
        and identity["release_channel"] == "stable"
        and identity["distribution_version"] == "1.6.94",
        "approved-source-required",
    )
    with tempfile.TemporaryDirectory(prefix="oc-box-introduction-verify-") as directory:
        social.verify_pypi(identity, Path(directory))
        return social.announce(
            github,
            social.Buffer(os.environ.get("BUFFER_API_KEY")),
            identity,
            release,
            channel_id,
            image,
            approved_text=text,
        )


def status(post_id):
    text, _ = approved_inputs()
    channel_id = target()
    post = social.Buffer(os.environ.get("BUFFER_API_KEY")).post(post_id)
    social.require(
        post.get("id") == post_id
        and post.get("channelId") == channel_id
        and post.get("text") == text
        and post.get("status")
        in {
            "scheduled",
            "sending",
            "sent",
            "draft",
            "error",
            "needs_approval",
        },
        "introduction-post-unverified",
    )
    link = post.get("externalLink")
    if link:
        parsed = urlsplit(link)
        social.require(
            parsed.scheme == "https"
            and parsed.hostname in {"www.linkedin.com", "linkedin.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None,
            "post-link-origin",
        )
    return {
        "outcome": "buffer-status",
        "buffer_post_id": post_id,
        "status": post["status"],
        "linkedin_url": link or None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "status"))
    parser.add_argument("--source", type=Path, default=Path("released-source"))
    parser.add_argument("--post-id")
    args = parser.parse_args(argv)
    try:
        social.require(
            os.environ.get("GITHUB_REPOSITORY") == social.REPOSITORY,
            "canonical-repository-required",
        )
        social.require(
            (args.post_id is not None) == (args.action == "status"), "invalid-options"
        )
        result = (
            publish(args.source) if args.action == "publish" else status(args.post_id)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        social.Refusal,
        ValueError,
        OSError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.SubprocessError,
        HTTPException,
    ) as error:
        category = (
            str(error)
            if isinstance(error, social.Refusal)
            else "invalid-input-or-response"
        )
        print("Introduction refused: " + category, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
