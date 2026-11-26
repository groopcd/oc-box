import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from oc_box import installation, oc_box, web_named_account_assets
from tools import check_package
from tools.check_release import source_identity

APP = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_guarded_discard_imports_from_actual_flat_install_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            for name in installation.RUNTIME_FILES:
                if name.endswith(".py"):
                    (target / name).write_bytes(
                        installation.source_file(APP, name).read_bytes()
                    )
            output = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import guarded_discard, stale_cleanup; "
                    "assert stale_cleanup.disposal.Engine is guarded_discard.Engine",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(output.returncode, 0, output.stderr)

    def test_exact_named_assets_are_present_without_compiled_payloads(self):
        source_names = {
            web_named_account_assets._DOCUMENT[0],
            *(row[4] for row in web_named_account_assets._SOURCES),
        }
        self.assertEqual(source_names, set(check_package._NAMED_STATIC_FILES))
        toml = (APP / "pyproject.toml").read_text()
        manifest = (APP / "MANIFEST.in").read_text()
        for name in source_names:
            with self.subTest(asset=name):
                self.assertIn(name, installation.RUNTIME_FILES)
                self.assertIn(f'"{name}"', toml)
                self.assertIn(f"src/oc_box/{name}", manifest)
        self.assertFalse(
            any(name.endswith(".wasm") for name in installation.RUNTIME_FILES)
        )
        self.assertNotIn("*.wasm", manifest)
        self.assertNotIn("*.wasm", toml)

    def test_studio_assets_are_in_the_flat_copy_inventory(self):
        for name in check_package._STUDIO_STATIC_FILES:
            with self.subTest(asset=name):
                self.assertIn(name, installation.PROGRAM_FILES)
                self.assertEqual(
                    installation.source_file(APP, name), APP / "src/oc_box" / name
                )

    def test_flat_install_can_import_the_private_overlap_pass(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            with contextlib.redirect_stdout(io.StringIO()):
                installation.install(APP, home=home)
            flat = (home / ".local/share/oc-box/current").resolve(strict=True)
            self.assertTrue((flat / "guarded_devices.py").is_file())
            fatal = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import oc_box, history_overlap_pass; "
                    "assert callable(oc_box._bind_first_history_sync)",
                ],
                cwd=flat,
                env={"HOME": name, "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(fatal.returncode, 0, fatal.stderr)

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
        studio_assets = {
            "oc_box/" + name: (APP / "src/oc_box" / name).read_bytes()
            for name in check_package._STUDIO_STATIC_FILES
        }
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
            "src/oc_box/oc-box-mark.svg",
            "src/oc_box/oc-box-mark.png",
            "docs/mockups/web-manager.html",
            *("src/oc_box/" + name for name in check_package._WEB_RUNTIME_FILES),
            *(
                path.relative_to(APP).as_posix()
                for directory in ("src", "tests", "tools", "docs")
                for path in (APP / directory).rglob("*")
                if path.suffix
                in (".py", ".js", ".tsx", ".mjs", ".md", ".1", ".texi", ".info")
            ),
        }
        cases = (
            (None, None),
            ("sdist-bytes", "src/oc_box/web_studio_shell.mjs"),
            *(
                ("wheel", "oc_box/" + name)
                for name in (
                    "oc_box.py",
                    "private_memory.py",
                    "opencode_compat.py",
                    "opencode_v2_io.py",
                    "workspace_service.py",
                    "web_attachments.py",
                    "web_attachments.js",
                    *check_package._WEB_RUNTIME_FILES,
                    "oc-box-mark.png",
                    "version_info.py",
                    "build-info.json",
                    "distribution.json",
                    "workspace_control.py",
                    "guarded_devices.py",
                    "workspace_podman.py",
                    "history_overlap_archive.py",
                    "history_overlap_binding.py",
                    "history_overlap_domains.py",
                    "history_overlap_engine.py",
                    "history_overlap_engine_config.py",
                    "history_overlap_guest.py",
                    "history_overlap_host_env.py",
                    "history_overlap_host_scratch.py",
                    "history_overlap_io.py",
                    "history_overlap_order.py",
                    "history_overlap_pass.py",
                    "history_overlap_remote.py",
                    "history_overlap_transaction.py",
                    "history_overlap_writer.py",
                    "history_reader.py",
                    "history_recovery.py",
                    "history_volume_scratch.py",
                    "history_write_intent.py",
                    "native_tabs.py",
                    "ui_state.py",
                    "ui_state_memory.js",
                    "ui_state_rpc.js",
                    "ui_state_server.js",
                    "ui_state_tui.js",
                    "file_preview.py",
                    "file_preview_io.py",
                    "file_preview_tui.tsx",
                    "activity_timeline_tui.tsx",
                    "activity_timeline_events.mjs",
                    "inline_timestamps.mjs",
                    "inline_timestamps_tui.mjs",
                    "file_preview_png.py",
                    "file_preview_host.py",
                    "file_open_formats.py",
                    "sharing.py",
                    "attachments.py",
                    "attachment_io.py",
                    "host_drop.py",
                    "host_drop_io.py",
                    "host_drop_input.py",
                    "workspace_preferences.py",
                    "startup_output.py",
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
                    "docs/web-shell.md",
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
                    "src/oc_box/oc-box-mark.svg",
                    "src/oc_box/oc-box-mark.png",
                    "tests/test_session_sync.py",
                    "tests/test_web_attachments.py",
                    "tests/web_attachments_browser.mjs",
                    "tests/web_attachments_shell_dom.mjs",
                    "tests/smoke_web_attachments_v2.py",
                    "tests/smoke_web_attachments_browser.py",
                    "tests/test_native_tabs.py",
                    "tests/test_native_tab_routing.py",
                    "tests/test_web_sessions.py",
                    "tests/web_sessions_dom.mjs",
                    "tests/smoke_web_sessions_v2.py",
                    "tests/test_web_layout_runtime.mjs",
                    "tests/web_studio_dom_fixture.mjs",
                    "tests/smoke_web_layout_browser.py",
                    "docs/mockups/web-manager.html",
                    *(
                        "src/oc_box/" + name
                        for name in check_package._WEB_RUNTIME_FILES
                    ),
                    "tests/smoke_native_tabs_v2.py",
                    "tests/smoke_native_preferences_v2.py",
                    "tests/smoke_activity_timeline_v2.py",
                    "tests/opencode_activity_timeline.test.mjs",
                    "tests/opencode_inline_timestamps.test.mjs",
                    "tests/smoke_image_preview_preferences_v2.py",
                    "tests/test_sync_config_preferences.py",
                    "tests/test_history_source_lease.py",
                    "tests/test_release_announcement_workflows.py",
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
                    "tests/smoke_history_subagent_recovery_v2.py",
                    "tests/smoke_history_source_v24.py",
                    "src/oc_box/private_memory.py",
                    "src/oc_box/ui_state.py",
                    "src/oc_box/ui_state_memory.js",
                    "src/oc_box/ui_state_rpc.js",
                    "src/oc_box/ui_state_server.js",
                    "src/oc_box/ui_state_tui.js",
                    "src/oc_box/file_preview.py",
                    "src/oc_box/file_preview_io.py",
                    "src/oc_box/file_preview_tui.tsx",
                    "src/oc_box/activity_timeline_tui.tsx",
                    "src/oc_box/activity_timeline_events.mjs",
                    "src/oc_box/inline_timestamps.mjs",
                    "src/oc_box/inline_timestamps_tui.mjs",
                    "src/oc_box/file_preview_png.py",
                    "src/oc_box/file_preview_host.py",
                    "src/oc_box/file_open_formats.py",
                    "tests/test_file_preview.py",
                    "tests/test_file_preview_io.py",
                    "tests/test_file_preview_png.py",
                    "tests/test_file_preview_host.py",
                    "tests/test_file_open_formats.py",
                    "tests/test_file_open_relay.py",
                    "tests/smoke_file_preview_v2.py",
                    "tests/smoke_image_preview_x11_v2.py",
                    "tests/smoke_host_clipboard_x11_v2.py",
                    "tests/test_ui_state.py",
                    "tests/ui_state_fixture.mjs",
                    "tests/smoke_ui_state_v2.py",
                    "docs/ui-state.md",
                    "src/oc_box/workspace_control.py",
                    *(
                        "src/oc_box/" + module
                        for module in check_package._HISTORY_OVERLAP_RUNTIME
                    ),
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
                    "tests/test_attachments.py",
                    "tests/test_attachment_io.py",
                    "tests/test_host_drop.py",
                    "tests/test_host_drop_io.py",
                    "tests/test_host_drop_input.py",
                    "tests/test_workspace_preferences.py",
                    "tests/test_host_drop_preferences.py",
                    "tests/smoke_host_drop_v2.py",
                    "tests/smoke_attachments_v2.py",
                    "src/oc_box/attachments.py",
                    "src/oc_box/attachment_io.py",
                    "src/oc_box/host_drop.py",
                    "src/oc_box/host_drop_io.py",
                    "src/oc_box/host_drop_input.py",
                    "src/oc_box/workspace_preferences.py",
                    "src/oc_box/startup_output.py",
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
                                content = studio_assets.get(name, b"")
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
                                content = studio_assets.get(
                                    name.removeprefix("src/"), b""
                                )
                                if kind == "sdist-bytes" and name == omitted:
                                    content = b"Unexpected Studio asset bytes"
                                elif name == "build-info.json":
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
            patch.object(sys, "argv", ["oc-box"]),
            patch.object(
                oc_box, "main", side_effect=oc_box.BoxError("fixture failure")
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(oc_box.entrypoint(), 1)
        self.assertEqual(output.getvalue(), "oc-box: fixture failure\n")
        with (
            patch.object(sys, "argv", ["oc-box"]),
            patch.object(oc_box, "main", side_effect=KeyboardInterrupt),
        ):
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
        self.assertIn("python tools/release_python_deps.py", workflow)
        self.assertIn("--upgrade -r", workflow)
        self.assertIn("--mirror azure --force-ipv4", workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", workflow)
        self.assertIn(
            "https://github.com/groopcd/oc-box.git", (APP / "install.sh").read_text()
        )

    @unittest.skipIf(sys.version_info < (3, 11), "release tool uses stdlib tomllib")
    def test_release_dependency_generator_reads_optional_extras(self):
        resolved_inputs = subprocess.run(
            [sys.executable, "tools/release_python_deps.py"],
            cwd=APP,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
        self.assertIn("textual>=8.2.8,<9", resolved_inputs)
        self.assertIn("pyte>=0.8.2,<0.9", resolved_inputs)

    @unittest.skipIf(sys.version_info < (3, 11), "release tool uses stdlib tomllib")
    def test_release_dependency_generator_rejects_non_index_inputs(self):
        with tempfile.TemporaryDirectory() as work:
            manifest = Path(work) / "pyproject.toml"
            for requirement in (
                "demo @ https://example.invalid/demo.whl",
                "-r /tmp/other",
            ):
                with self.subTest(requirement=requirement):
                    manifest.write_text(
                        '[build-system]\nrequires=["wheel"]\n'
                        "[project.optional-dependencies]\nvault=["
                        + json.dumps(requirement)
                        + "]\n"
                    )
                    result = subprocess.run(
                        [sys.executable, "tools/release_python_deps.py", str(manifest)],
                        cwd=APP,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(requirement, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
