"""Commit-derived release identity, embedded by install.py for installed copies."""

import json
from pathlib import Path
import re
import subprocess

if __package__:
    from .paths import source_root
else:
    from paths import source_root

VERSION_SERIES = "1.6"
# Retain the original stable patch-count baseline across minor-series changes.
# Absolute counts still order upgrades from installed 0.1/1.0-series launchers.
VERSION_BASE_COMMIT_COUNT = 29
AUTHOR_NAME = "Paulo Dias"
AUTHOR_EMAIL = "paulo.miguel.dias@gmail.com"
AI_CONTRIBUTION = "OpenAI coding assistant via OpenCode: implementation, debugging, tests, and documentation."
LICENSE = "GPL-3.0-or-later"


def public_version(build):
    """The public package/tag version, without source-local provenance."""
    return build["version"].split("+", 1)[0]


def unknown_version():
    return {
        "version": f"{VERSION_SERIES}.0+unknown",
        "commit": None,
        "commit_count": None,
        "dirty": False,
    }


def valid_embedded_identity(value):
    """Validate provenance types without rewriting older series or extra fields."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("version"), str)
        and re.fullmatch(
            r"\d+\.\d+\.\d+(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?", value["version"]
        )
        is not None
        and isinstance(value.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", value["commit"]) is not None
        and type(value.get("commit_count")) is int
        and value["commit_count"] > 0
        and type(value.get("dirty")) is bool
        and (
            "distribution_version" not in value
            or isinstance(value["distribution_version"], str)
        )
    )


def get_version_info(root):
    root = source_root(root)
    marker = root / ".git"
    if marker.exists() or marker.is_symlink():

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
            if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
                raise ValueError("Invalid source commit")
            dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
            return {
                "version": f"{VERSION_SERIES}.{patch}+g{commit[:7]}"
                + (".dirty" if dirty else ""),
                "commit": commit,
                "commit_count": count,
                "dirty": dirty,
            }
        except (OSError, ValueError, subprocess.SubprocessError):
            # Embedded metadata belongs to installed/sdist copies, not a way to
            # turn an unverifiable checkout into an older clean source identity.
            return unknown_version()
    try:
        value = json.loads((root / "build-info.json").read_text())
        if valid_embedded_identity(value):
            return value
    except (OSError, ValueError):
        pass
    return unknown_version()


def about_text(root):
    root = source_root(root)
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
