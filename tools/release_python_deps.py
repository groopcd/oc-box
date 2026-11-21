#!/usr/bin/env python3
"""List the Python dependencies installed into the audited release build env."""

import argparse
import re
import tomllib
from pathlib import Path


def requirements(project):
    extras = project["project"].get("optional-dependencies", {})
    return (
        "pip",
        "build",
        "twine",
        "Pillow>=13,<14",
        *project["build-system"]["requires"],
        *project["project"].get("dependencies", ()),
        *extras["vault"],
        *extras.get("tui", ()),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest", nargs="?", type=Path, default=Path("pyproject.toml")
    )
    args = parser.parse_args()
    with args.manifest.open("rb") as manifest:
        dependencies = requirements(tomllib.load(manifest))
    for dependency in dependencies:
        # Accept the index package/specifier subset used by this project, never
        # direct URLs, local paths, requirements-file options, or shell input.
        if not isinstance(dependency, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*"
            r"(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
            r"(?:(?:~=|==|!=|<=|>=|<|>)[A-Za-z0-9_.+!*]+"
            r"(?:,(?:~=|==|!=|<=|>=|<|>)[A-Za-z0-9_.+!*]+)*)?",
            dependency,
        ):
            raise SystemExit("Invalid index-only release dependency")
        print(dependency)


if __name__ == "__main__":
    main()
