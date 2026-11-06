"""Build-time integration: full source identity, public PyPI version, resources.

Runtime has no setuptools dependency. Standard builds work from both Git and
the source distribution without version-bump files or local-version suffixes.
"""

import importlib.util
import json
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "oc_box_build_version", ROOT / "version_info.py"
)
assert spec is not None and spec.loader is not None
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)
BUILD = version.get_version_info(ROOT)
if BUILD.get("commit_count") is None or BUILD.get("commit") is None:
    raise RuntimeError(
        "Building oc-box requires full Git history or embedded source-distribution build metadata."
    )
PUBLIC_VERSION = f"{version.VERSION_SERIES}.{BUILD['commit_count']}"


class BuildPackage(build_py):
    def find_package_modules(self, package, package_dir):
        return [
            module
            for module in super().find_package_modules(package, package_dir)
            if module[1] != "setup"
        ]

    def run(self):
        super().run()
        package = Path(self.build_lib) / "oc_box"
        (package / "build-info.json").write_text(
            json.dumps({**BUILD, "distribution_version": PUBLIC_VERSION}, indent=2)
            + "\n"
        )
        (package / "distribution.json").write_text(
            json.dumps(
                {
                    "installer": "python-package",
                    "name": "oc-box",
                    "version": PUBLIC_VERSION,
                }
            )
            + "\n"
        )


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        # Git history is not part of an sdist; preserve it as metadata instead.
        (Path(base_dir) / "build-info.json").write_text(
            json.dumps(BUILD, indent=2) + "\n"
        )


setup(
    version=PUBLIC_VERSION,
    cmdclass={"build_py": BuildPackage, "sdist": SourceDistribution},
)
