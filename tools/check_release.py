#!/usr/bin/env python3
"""Fail closed on release identity and channel lineage before PyPI publication.

Without --publish, validate a clean full-history source build on any branch.
Publishing additionally requires a published GitHub release JSON document
retrieved from the canonical repository by the workflow (not an event flag).
No network access, Git mutation, or publication is performed by this validator.
"""

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess


SOURCE = "src/oc_box/version_info.py"
CHANNEL_BRANCHES = {"stable": "master", "devel": "devel"}


def source_identity(text):
    """Read literal tracked constants without importing or executing source."""
    values = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "VERSION_SERIES",
                    "RELEASE_CHANNEL",
                    "VERSION_BASE_COMMIT_COUNT",
                ):
                    if target.id in values:
                        raise ValueError("Duplicate source identity constant")
                    values[target.id] = ast.literal_eval(node.value)
    series = values.get("VERSION_SERIES")
    channel = values.get("RELEASE_CHANNEL")
    if (
        not isinstance(series, str)
        or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", series)
        or not isinstance(channel, str)
        or channel not in CHANNEL_BRANCHES
        or type(values.get("VERSION_BASE_COMMIT_COUNT")) is not int
        or values["VERSION_BASE_COMMIT_COUNT"] != 29
    ):
        raise ValueError("Invalid tracked source release identity")
    return series, channel, values["VERSION_BASE_COMMIT_COUNT"]


def validate(root, *, publish=False, tag=None, release=None):
    root = Path(root).resolve()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    if git("rev-parse", "--show-toplevel") != str(root):
        raise ValueError("Run release validation from the repository root")
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Full Git history is required")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("A clean source checkout is required")
    # Read the committed blob, not an environment override or generated file.
    series, channel, baseline = source_identity(git("show", "HEAD:" + SOURCE))
    count = int(git("rev-list", "--count", "HEAD"))
    if count < baseline:
        raise ValueError("History predates the version baseline")
    commit = git("rev-parse", "HEAD")
    public = f"{series}.{count - baseline}" + (".dev0" if channel == "devel" else "")
    expected_tag = "v" + public
    if publish:
        if tag != expected_tag:
            raise ValueError("Release tag must be " + expected_tag)
        if not isinstance(release, dict):
            raise ValueError("An existing published GitHub release is required")
        if release.get("tag_name") != expected_tag or release.get("draft") is not False:
            raise ValueError("GitHub release tag/draft state does not match")
        if release.get("prerelease") is not (channel == "devel"):
            raise ValueError(
                "GitHub prerelease state does not match the source channel"
            )
        published = release.get("published_at")
        try:
            if not isinstance(published, str):
                raise ValueError
            timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError("GitHub release must already be published") from None
        if (
            git("rev-parse", "--verify", "refs/tags/" + expected_tag + "^{commit}")
            != commit
        ):
            raise ValueError("Release tag does not identify this source commit")
        branch = "refs/remotes/origin/" + CHANNEL_BRANCHES[channel]
        try:
            git("merge-base", "--is-ancestor", commit, branch)
        except subprocess.CalledProcessError:
            raise ValueError(
                "Release commit is not on the " + CHANNEL_BRANCHES[channel] + " lineage"
            ) from None
    elif tag is not None or release is not None:
        raise ValueError("Release inputs require explicit publication validation")
    return {
        "version": public + "+g" + commit[:7],
        "distribution_version": public,
        "commit": commit,
        "commit_count": count,
        "release_channel": channel,
        "tag": expected_tag,
        "publish_validated": publish,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--release-json", type=Path)
    args = parser.parse_args(argv)
    try:
        release = (
            json.loads(args.release_json.read_text())
            if args.release_json is not None
            else None
        )
        result = validate(
            args.root, publish=args.publish, tag=args.tag, release=release
        )
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        # Git output and release bodies are deliberately not echoed.
        parser.exit(
            1,
            "Release validation refused: "
            + (
                str(error)
                if isinstance(error, ValueError)
                and not isinstance(error, json.JSONDecodeError)
                else "invalid release inputs or Git state"
            )
            + "\n",
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
