import contextlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from oc_box import installation, oc_box
from tools import check_package
from tools.check_release import source_identity

APP = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_archive_validation_requires_sync_runtime_docs_and_regressions(self):
        build = {
            "version": "1.6.1+gabcdef0",
            "distribution_version": "1.6.1",
            "commit": "abcdef0" * 5 + "abcde",
            "commit_count": 30,
            "release_channel": "stable",
            "dirty": False,
        }
        wheel_names = {
            "oc_box/" + name
            for name in (
                *installation.PROGRAM_FILES,
                *("docs/" + name for name in installation.MANUAL_FILES),
                installation.COMPLETION_FILE,
                "build-info.json",
                "distribution.json",
            )
        }
        source_names = {
            "pyproject.toml",
            "setup.py",
            "install.py",
            "LICENSE",
            "NOTICE",
            "completions/oc-box",
            "build-info.json",
            "PKG-INFO",
            *(
                path.relative_to(APP).as_posix()
                for directory in ("src", "tests", "tools", "docs")
                for path in (APP / directory).rglob("*")
                if path.suffix in (".py", ".js", ".mjs", ".md", ".1", ".texi", ".info")
            ),
        }
        cases = (
            (None, None),
            *(
                ("wheel", "oc_box/" + name)
                for name in (
                    "oc_box.py",
                    "private_memory.py",
                    "opencode_compat.py",
                    "opencode_v2_io.py",
                    "workspace_service.py",
                    "version_info.py",
                    "build-info.json",
                    "distribution.json",
                    "docs/cli.md",
                    "docs/opencode-2.md",
                    "docs/oc-box.1",
                    "docs/oc-box.texi",
                    "docs/oc-box.info",
                )
            ),
            *(
                ("sdist", name)
                for name in (
                    "tests/test_session_sync.py",
                    "src/oc_box/private_memory.py",
                    "tests/test_private_memory.py",
                    "tests/test_private_output.py",
                    "tools/ci_ubuntu_apt.py",
                    "tests/test_ci_ubuntu_apt.py",
                    "tests/smoke_session_sync_v2.py",
                    "tests/smoke_v2.py",
                    "tools/check_release.py",
                    "tests/test_release_channels.py",
                    "src/oc_box/version_info.py",
                )
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="oc-box-package-archives-"
        ) as temporary:
            directory = Path(temporary)
            wheel = directory / "oc_box-1.6.1-py3-none-any.whl"
            source = directory / "oc_box-1.6.1.tar.gz"
            for kind, omitted in cases:
                with self.subTest(kind=kind, omitted=omitted):
                    with zipfile.ZipFile(wheel, "w") as archive:
                        archive.writestr(
                            "oc_box-1.6.1.dist-info/METADATA",
                            "Name: oc-box\nVersion: 1.6.1\nRequires-Python: >=3.10\n"
                            "License-Expression: GPL-3.0-or-later\n"
                            'Requires-Dist: cryptography>=44; extra == "vault"\n',
                        )
                        for name in sorted(wheel_names):
                            if kind != "wheel" or name != omitted:
                                contents = ""
                                if name.endswith("build-info.json"):
                                    contents = json.dumps(build)
                                elif name == "oc_box/version_info.py":
                                    contents = (
                                        'VERSION_SERIES = "1.6"\n'
                                        'RELEASE_CHANNEL = "stable"\n'
                                        "VERSION_BASE_COMMIT_COUNT = 29\n"
                                    )
                                elif name == "oc_box/distribution.json":
                                    contents = json.dumps(
                                        {
                                            "installer": "python-package",
                                            "name": "oc-box",
                                            "version": "1.6.1",
                                        }
                                    )
                                archive.writestr(
                                    name,
                                    contents,
                                )
                    with tarfile.open(source, "w:gz") as archive:
                        for name in sorted(source_names):
                            if kind != "sdist" or name != omitted:
                                content = b""
                                if name == "build-info.json":
                                    content = json.dumps(
                                        {
                                            key: value
                                            for key, value in build.items()
                                            if key != "distribution_version"
                                        }
                                    ).encode()
                                elif name == "src/oc_box/version_info.py":
                                    content = (
                                        b'VERSION_SERIES = "1.6"\n'
                                        b'RELEASE_CHANNEL = "stable"\n'
                                        b"VERSION_BASE_COMMIT_COUNT = 29\n"
                                    )
                                elif name == "PKG-INFO":
                                    content = b"Name: oc-box\nVersion: 1.6.1\n"
                                member = tarfile.TarInfo("oc_box-1.6.1/" + name)
                                member.size = len(content)
                                archive.addfile(member, io.BytesIO(content))
                    if omitted is None:
                        self.assertEqual(
                            check_package.check_archives(directory), (wheel, build)
                        )
                    else:
                        with self.assertRaises(AssertionError) as error:
                            check_package.check_archives(directory)
                        self.assertEqual(
                            str(error.exception), omitted.removeprefix("oc_box/")
                        )

    def test_artifact_channel_grammar_and_provenance_matrix(self):
        for channel in ("stable", "devel"):
            source = (
                'VERSION_SERIES = "1.7"\n'
                f'RELEASE_CHANNEL = "{channel}"\n'
                "VERSION_BASE_COMMIT_COUNT = 29\n"
            )
            public = "1.7.23" + (".dev0" if channel == "devel" else "")
            build = {
                "version": public + "+gabcdef0",
                "commit": "abcdef0" * 5 + "abcde",
                "commit_count": 52,
                "release_channel": channel,
                "dirty": False,
            }
            with self.subTest(channel=channel):
                check_package.check_build_identity(build, source, public)
                check_package.check_build_identity(
                    {**build, "dirty": True, "version": build["version"] + ".dirty"},
                    source,
                    public,
                )
            changes = (
                {"release_channel": "stable" if channel == "devel" else "devel"},
                {"commit_count": 53},
                {"commit_count": True},
                {"commit": "b" * 40},
                {"dirty": "false"},
                {"version": public + "+gwrong"},
            )
            for change in changes:
                with self.subTest(channel=channel, change=change):
                    with self.assertRaises(AssertionError):
                        check_package.check_build_identity(
                            {**build, **change}, source, public
                        )
            for invalid in (
                "1.7.23.dev1",
                "1.7.23rc1",
                "1.7.23a1",
                "1.7.23.post1",
                "1.7.23+local",
            ):
                with self.subTest(channel=channel, invalid=invalid):
                    with self.assertRaises(AssertionError):
                        check_package.check_build_identity(build, source, invalid)
        for identity in (
            'VERSION_SERIES = "1.7"\nRELEASE_CHANNEL = "rc"\nVERSION_BASE_COMMIT_COUNT = 29\n',
            'VERSION_SERIES = "1.7"\nRELEASE_CHANNEL = "stable"\nVERSION_BASE_COMMIT_COUNT = 30\n',
            'VERSION_SERIES = "1.7"\nRELEASE_CHANNEL = "stable"\nVERSION_BASE_COMMIT_COUNT = True\n',
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    source_identity(identity)

    def test_console_entrypoint_preserves_readable_error_and_interrupt_handling(self):
        with (
            patch.object(
                oc_box, "main", side_effect=oc_box.BoxError("fixture failure")
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(oc_box.entrypoint(), 1)
        self.assertEqual(output.getvalue(), "oc-box: fixture failure\n")
        with patch.object(oc_box, "main", side_effect=KeyboardInterrupt):
            self.assertEqual(oc_box.entrypoint(), 130)

    def test_package_metadata_and_workflow_keep_both_distribution_channels(self):
        metadata = (APP / "pyproject.toml").read_text()
        self.assertIn('oc-box = "oc_box.oc_box:entrypoint"', metadata)
        self.assertIn('license = "GPL-3.0-or-later"', metadata)
        self.assertIn('requires-python = ">=3.10"', metadata)
        self.assertIn('vault = ["cryptography>=44"]', metadata)
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        self.assertIn("name: pypi", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("tools/check_package.py dist", workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", workflow)
        self.assertIn(
            "https://github.com/groopcd/oc-box.git", (APP / "install.sh").read_text()
        )


if __name__ == "__main__":
    unittest.main()
