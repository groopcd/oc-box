"""Commit-derived release identity, embedded by install.py for installed copies."""

import json
from pathlib import Path
import subprocess

VERSION_SERIES = "1.0"
# The first stable release is the 29th full-history commit. Keep the absolute
# count in build metadata for compatibility with installed 0.1-series updaters.
VERSION_BASE_COMMIT_COUNT = 29
AUTHOR_NAME = "Paulo Dias"
AUTHOR_EMAIL = "paulo.miguel.dias@gmail.com"
AI_CONTRIBUTION = "OpenAI coding assistant via OpenCode: implementation, debugging, tests, and documentation."
LICENSE = "GPL-3.0-or-later"


def public_version(build):
    """The public package/tag version, without source-local provenance."""
    return build["version"].split("+", 1)[0]


def get_version_info(root):
    root = Path(root).resolve()
    if (root / ".git").exists():

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()

        try:
            if git("rev-parse", "--is-shallow-repository") != "false":
                raise ValueError("Versioning requires full Git history")
            count = int(git("rev-list", "--count", "HEAD"))
            patch = count - VERSION_BASE_COMMIT_COUNT
            if patch < 0:
                raise ValueError("History predates this version series")
            commit = git("rev-parse", "HEAD")
            dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
            return {
                "version": f"{VERSION_SERIES}.{patch}+g{commit[:7]}"
                + (".dirty" if dirty else ""),
                "commit": commit,
                "commit_count": count,
                "dirty": dirty,
            }
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        value = json.loads((root / "build-info.json").read_text())
        if isinstance(value, dict) and isinstance(value.get("version"), str):
            return value
    except (OSError, ValueError):
        pass
    return {
        "version": f"{VERSION_SERIES}.0+unknown",
        "commit": None,
        "commit_count": None,
        "dirty": False,
    }


def about_text(root):
    build = get_version_info(root)
    return "\n".join(
        (
            "oc-box " + build["version"],
            "Rootless Podman development environments for unmodified mainline OpenCode.",
            "",
            "Author: " + AUTHOR_NAME,
            "Email: " + AUTHOR_EMAIL,
            "AI contribution: " + AI_CONTRIBUTION,
            "Copyright (C) 2026 " + AUTHOR_NAME,
            "License: " + LICENSE + " (GNU GPL version 3 or any later version)",
            "Free software; you may redistribute it under the GPL. No warranty.",
            "License text: " + str(Path(root).resolve() / "LICENSE"),
            "",
            "Source commit: " + (build.get("commit") or "unavailable"),
        )
    )
