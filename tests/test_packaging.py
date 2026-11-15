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
            "version": "1.7.1.dev0+gabcdef0",
            "distribution_version": "1.7.1.dev0",
            "commit": "abcdef0" * 5 + "abcde",
            "commit_count": 30,
            "release_channel": "devel",
            "dirty": False,
        }
        version = build["distribution_version"]
        version_source = (
            'VERSION_SERIES = "1.7"\nRELEASE_CHANNEL = "devel"\n'
            "VERSION_BASE_COMMIT_COUNT = 29\n"
        )
        wheel_names = {
            "oc_box/" + name
            for name in (
                *installation.PROGRAM_FILES,
                *("docs/" + name for name in installation.MANUAL_FILES),
                installation.COMPLETION_FILE,
                "build-info.json",
                "distribution.json",
                "update_channels.py",
                "package_updates.py",
                "docs/update-channels.md",
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
            # Synthetic archives exercise membership independently of whether
            # the parallel updater/CLI ports have been integrated into checkout.
            "tests/test_update_channels.py",
            "tests/test_channel_updater.py",
            "tests/test_channel_installer.py",
            "tests/test_channel_bootstrap.py",
            "tests/test_package_updates.py",
            "tests/test_upgrade_cli.py",
            "src/oc_box/update_channels.py",
            "src/oc_box/package_updates.py",
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
                    "workspace_control.py",
                    "history_reader.py",
                    "history_recovery.py",
                    "sharing.py",
                    "activity_log.py",
                    "tui_app.py",
                    "tui_terminal.py",
                    "tui_clipboard.py",
                    "tui_storage.py",
                    "tui_operations.py",
                    "tui_management.py",
                    "tui_notifications.py",
                    "session_backups.py",
                    "update_channels.py",
                    "package_updates.py",
                    "docs/update-channels.md",
                    "docs/tui.md",
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
                    "tests/test_update_channels.py",
                    "tests/test_channel_updater.py",
                    "tests/test_channel_installer.py",
                    "tests/test_channel_bootstrap.py",
                    "tests/test_package_updates.py",
                    "tests/test_upgrade_cli.py",
                    "src/oc_box/update_channels.py",
                    "src/oc_box/package_updates.py",
                    "src/oc_box/version_info.py",
                    "tools/check_release.py",
                    "tools/check_release_artifacts.py",
                    "tests/test_release_channels.py",
                    "tests/test_release_artifacts.py",
                    "tests/test_workspace_control.py",
                    "tests/test_history_reader.py",
                    "tests/test_history_recovery.py",
                    "tests/smoke_history_recovery_v2.py",
                    "src/oc_box/private_memory.py",
                    "src/oc_box/workspace_control.py",
                    "src/oc_box/history_reader.py",
                    "src/oc_box/history_recovery.py",
                    "tests/test_private_memory.py",
                    "tests/test_private_output.py",
                    "tools/ci_ubuntu_apt.py",
                    "tests/test_ci_ubuntu_apt.py",
                    "tests/smoke_session_sync_v2.py",
                    "tests/smoke_v2.py",
                    "tests/smoke_tui_v2.py",
                    "tests/test_sharing.py",
                    "tests/test_sharing_transport.py",
                    "tests/smoke_sharing_v2.py",
                    "tests/smoke_session_backups_v2.py",
                    "tests/smoke_session_backup_transport_v2.py",
                    "tests/test_tui_management.py",
                    "tests/test_tui_loading.py",
                    "tests/test_tui_delete_notifications.py",
                    "tests/test_tui_notifications.py",
                    "tests/test_tui_operation_results.py",
                    "tests/test_tui_operations.py",
                    "tests/test_tui_storage.py",
                    "tests/test_session_backups.py",
                    "tests/test_interactive_host_service_port.py",
                )
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="oc-box-package-archives-"
        ) as temporary:
            directory = Path(temporary)
            wheel = directory / f"oc_box-{version}-py3-none-any.whl"
            source = directory / f"oc_box-{version}.tar.gz"
            for kind, omitted in cases:
                with self.subTest(kind=kind, omitted=omitted):
                    with zipfile.ZipFile(wheel, "w") as archive:
                        archive.writestr(
                            f"oc_box-{version}.dist-info/METADATA",
                            f"Name: oc-box\nVersion: {version}\nRequires-Python: >=3.10\n"
                            "License-Expression: GPL-3.0-or-later\n"
                            'Requires-Dist: cryptography>=44; extra == "vault"\n'
                            'Requires-Dist: textual<9,>=8.2.8; extra == "tui"\n'
                            'Requires-Dist: pyte<0.9,>=0.8.2; extra == "tui"\n',
                        )
                        for name in sorted(wheel_names):
                            if kind != "wheel" or name != omitted:
                                content = ""
                                if name.endswith("build-info.json"):
                                    content = json.dumps(build)
                                elif name == "oc_box/version_info.py":
                                    content = version_source
                                elif name == "oc_box/distribution.json":
                                    content = json.dumps(
                                        {
                                            "installer": "python-package",
                                            "name": "oc-box",
                                            "version": version,
                                        }
                                    )
                                archive.writestr(name, content)
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
                                    content = version_source.encode()
                                elif name == "PKG-INFO":
                                    content = (
                                        f"Name: oc-box\nVersion: {version}\n".encode()
                                    )
                                member = tarfile.TarInfo(f"oc_box-{version}/" + name)
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
        self.assertIn('tui = ["textual>=8.2.8,<9", "pyte>=0.8.2,<0.9"]', metadata)
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        self.assertIn("name: pypi", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("tools/check_package.py dist", workflow)
        self.assertIn("tools/check_package.py dist --with-vault --with-tui", workflow)
        self.assertIn("'textual>=8.2.8,<9' 'pyte>=0.8.2,<0.9'", workflow)
        self.assertIn("--mirror azure --force-ipv4", workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", workflow)
        self.assertIn(
            "https://github.com/groopcd/oc-box.git", (APP / "install.sh").read_text()
        )


if __name__ == "__main__":
    unittest.main()
