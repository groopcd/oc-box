#!/usr/bin/env python3
"""List the Python dependencies installed into the audited release build env."""

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
    with Path("pyproject.toml").open("rb") as manifest:
        dependencies = requirements(tomllib.load(manifest))
    for dependency in dependencies:
        if (
            not dependency
            or dependency != dependency.strip()
            or "\n" in dependency
            or "\r" in dependency
            or dependency.startswith("-")
        ):
            raise SystemExit("Invalid release dependency")
        print(dependency)


if __name__ == "__main__":
    main()
