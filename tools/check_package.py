#!/usr/bin/env python3
"""Verify built wheel/sdist contents and real isolated wheel CLI behavior.

Run with a directory containing one wheel and sdist; needs no runtime package
imports, credentials, OpenCode, or Podman. Installs only into a temporary venv.
"""

import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import venv
import zipfile

if __package__:
    from .check_release import source_identity
else:
    from check_release import source_identity


_STUDIO_STATIC_FILES = (
    "web_studio_shell.css",
    "web_studio_shell.mjs",
    "web_studio_transcript.css",
    "web_studio_transcript.mjs",
    "web_studio_composer.css",
    "web_studio_composer.mjs",
)

_NAMED_STATIC_FILES = (
    "web_named_login.html",
    "web_named_login.css",
    "web_named_login.mjs",
    "web_named_login_slot.mjs",
    "web_named_handoff_client.mjs",
    "web_pake_client.mjs",
)


_WEB_RUNTIME_FILES = (
    "web_sessions.py",
    "web_sessions.js",
    "web_sessions.css",
    *_STUDIO_STATIC_FILES,
    *_NAMED_STATIC_FILES,
    "web_studio_conversation.py",
    "web_studio_read.py",
    "web_studio_selection.py",
    "web_studio_send.py",
    "web_history.py",
    "web_tls.py",
    "web_local_origins.py",
    "web_accounts.py",
    "web_admission.py",
    "web_handoff.py",
    "web_native_pair.py",
    "web_named_native_profile.py",
    "web_named_local.py",
    "web_named_headers.py",
    "web_named_account_assets.py",
    "web_named_bootstrap.py",
    "web_named_foreground.py",
    "web_named_handoff.py",
    "web_named_http.py",
    "web_named_listeners.py",
    "web_named_owner.py",
    "web_named_protocol.py",
    "web_named_relay.py",
    "web_named_services.py",
    "web_named_site_lifecycle.py",
    "web_named_studio_chat.py",
    "web_named_studio_selection.py",
    "web_named_transport.py",
    "web_bootstrap.py",
    "web_account_ingress.py",
    "web_app_ingress.py",
    "web_pake_flow.py",
    "web_pake_profile.py",
    "web_pake_worker.py",
    "web_enrollment.py",
    "web_registration_worker.py",
)


def check_build_identity(build, source, version):
    """Prove public/channel/source provenance, including sdist rebuild identity."""
    series, channel, baseline = source_identity(source)
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev0)?", version), version
    assert build.get("release_channel") == channel, "release_channel"
    count = build.get("commit_count")
    assert type(count) is int and count >= baseline, "commit_count"
    commit = build.get("commit")
    assert isinstance(commit, str) and re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit
    ), "commit"
    assert type(build.get("dirty")) is bool, "dirty"
    expected = f"{series}.{count - baseline}" + (".dev0" if channel == "devel" else "")
    assert version == expected, "distribution_version"
    assert build.get("version") == expected + "+g" + commit[:7] + (
        ".dirty" if build["dirty"] else ""
    ), "version"


def check_archives(directory):
    """Validate distribution contents without installing or running the wheel."""
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata_paths = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_paths) == 1, "wheel METADATA"
        metadata_path = metadata_paths[0]
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        version = metadata["Version"]
        assert wheels[0].name.split("-")[:2] == ["oc_box", version], "wheel filename"
        assert sources[0].name == f"oc_box-{version}.tar.gz", "sdist filename"
        assert metadata["Name"] == "oc-box"
        assert metadata["Requires-Python"] == ">=3.10"
        assert metadata["License-Expression"] == "GPL-3.0-or-later"
        requirements = metadata.get_all("Requires-Dist") or []
        assert len(requirements) == 3, requirements
        assert any(
            r.startswith("cryptography>=44") and 'extra == "vault"' in r
            for r in requirements
        ), requirements
        for dependency, lower, upper in (
            ("textual", ">=8.2.8", "<9"),
            ("pyte", ">=0.8.2", "<0.9"),
        ):
            assert any(
                r.startswith(dependency)
                and lower in r
                and upper in r
                and 'extra == "tui"' in r
                for r in requirements
            ), requirements
        assert all("extra ==" in r for r in requirements), requirements
        for name in (
            "oc_box.py",
            "dependencies.py",
            "private_memory.py",
            "opencode_compat.py",
            "opencode_v2_io.py",
            "workspace_service.py",
            "web_attachments.py",
            "web_attachments.js",
            *_WEB_RUNTIME_FILES,
            "oc-box-mark.png",
            "workspace_control.py",
            "history_reader.py",
            "history_recovery.py",
            "native_tabs.py",
            "ui_state.py",
            "ui_state_server.js",
            "ui_state_tui.js",
            "ui_state_rpc.js",
            "ui_state_memory.js",
            "file_preview.py",
            "file_preview_io.py",
            "file_preview_tui.tsx",
            "file_preview_png.py",
            "file_preview_host.py",
            "file_open_formats.py",
            "session_terminal.py",
            "sharing.py",
            "attachments.py",
            "attachment_io.py",
            "host_drop.py",
            "host_drop_io.py",
            "host_drop_input.py",
            "workspace_preferences.py",
            "plugin_compat.py",
            "plugin_bridge.js",
            "backups.py",
            "backup_crypto.py",
            "backup_archive.py",
            "workspace_identity.py",
            "vault.py",
            "secret_prompt.py",
            "credentials.py",
            "bootstrap.py",
            "sync_config.py",
            "startup_output.py",
            "terminal_ui.py",
            "cli_help.py",
            "status_report.py",
            "activity_log.py",
            "journal_output.py",
            "log_config.py",
            "lifecycle_output.py",
            "logs_cli.py",
            "tui.py",
            "tui_app.py",
            "tui_clipboard.py",
            "tui_backend.py",
            "tui_status.py",
            "tui_settings.py",
            "tui_logs.py",
            "tui_sessions.py",
            "tui_terminal.py",
            "tui_storage.py",
            "tui_operations.py",
            "tui_management.py",
            "tui_notifications.py",
            "session_backups.py",
            "updater.py",
            "update_channels.py",
            "package_updates.py",
            "installation.py",
            "version_info.py",
            "paths.py",
            "LICENSE",
            "NOTICE",
            "docs/cli.md",
            "docs/oc-box.1",
            "docs/oc-box.texi",
            "docs/oc-box.info",
            "docs/opencode-2.md",
            "docs/host-plugins.md",
            "docs/tui.md",
            "docs/update-channels.md",
            "docs/web-shell.md",
            "completions/oc-box",
            "build-info.json",
            "distribution.json",
        ):
            assert "oc_box/" + name in names, name
        assert "oc_box/setup.py" not in names
        assert "oc_box/web_tls_cli.py" not in names
        assert "oc_box/web_tls_setup.py" not in names
        assert not any(name.endswith(".wasm") for name in names), (
            "unapproved web verifier"
        )
        web_runtime = {
            name: wheel.read("oc_box/" + name) for name in _WEB_RUNTIME_FILES
        }
        assert not any(
            "/tests/" in name
            or name.endswith(".pyc")
            or ".oc-box-id" in name
            or "/.git/" in name
            for name in names
        )
        build = json.loads(wheel.read("oc_box/build-info.json"))
        assert build["distribution_version"] == version
        version_source = wheel.read("oc_box/version_info.py").decode()
        check_build_identity(build, version_source, version)
        distribution = json.loads(wheel.read("oc_box/distribution.json"))
        assert distribution == {
            "installer": "python-package",
            "name": "oc-box",
            "version": version,
        }, "distribution.json"
    with tarfile.open(sources[0], "r:gz") as source:
        root = source.getnames()[0].split("/", 1)[0]
        assert root == "oc_box-" + version, "sdist root"
        build_file = source.extractfile(root + "/build-info.json")
        assert build_file is not None
        original = json.load(build_file)
        assert original == {
            name: value
            for name, value in build.items()
            if name != "distribution_version"
        }, "sdist build-info.json"
        for name in (
            "pyproject.toml",
            "setup.py",
            "install.py",
            "src/_oc_box_build.py",
            "src/oc_box/oc-box-mark.svg",
            "src/oc_box/oc-box-mark.png",
            "tests/test_updater.py",
            "tests/test_update_channels.py",
            "tests/test_channel_updater.py",
            "tests/test_channel_installer.py",
            "tests/test_channel_bootstrap.py",
            "tests/test_package_updates.py",
            "tests/test_upgrade_cli.py",
            "src/oc_box/update_channels.py",
            "src/oc_box/package_updates.py",
            "tests/test_session_terminal.py",
            "tests/test_web_attachments.py",
            "tests/web_attachments_browser.mjs",
            "tests/web_attachments_shell_dom.mjs",
            "tests/smoke_web_attachments_v2.py",
            "tests/test_sharing.py",
            "tests/test_attachments.py",
            "tests/test_attachment_io.py",
            "tests/test_host_drop.py",
            "tests/test_host_drop_io.py",
            "tests/test_host_drop_input.py",
            "tests/test_workspace_preferences.py",
            "tests/test_logs_cli.py",
            "tests/test_journal_output.py",
            "tests/test_log_config.py",
            "tests/test_cli_help.py",
            "tests/test_lifecycle_output.py",
            "tests/test_host_drop_preferences.py",
            "tests/smoke_host_drop_v2.py",
            "tests/smoke_attachments_v2.py",
            "tests/test_sharing_transport.py",
            "tests/smoke_sharing_v2.py",
            "tests/smoke_sharing_host_helpers.py",
            "tests/test_sharing_host_fixture.py",
            "tests/test_sharing_host_oracles.py",
            "tests/test_sharing_owner_control.py",
            "tests/test_sharing_archive_scan.py",
            "tests/test_sharing_pax_metadata.py",
            "tests/test_sharing_pax_reason.py",
            "tests/test_sharing_backup_probe.py",
            "tests/test_sharing_backup_diagnostic.py",
            "tests/test_sharing_fixture_provider.py",
            "tests/test_sharing_session_payload.py",
            "tests/test_session_sync.py",
            "tests/test_native_tabs.py",
            "tests/test_native_tab_routing.py",
            "tests/test_web_sessions.py",
            "tests/web_sessions_dom.mjs",
            "tests/smoke_web_sessions_v2.py",
            "tests/test_web_layout_runtime.mjs",
            "tests/web_studio_dom_fixture.mjs",
            "tests/smoke_web_layout_browser.py",
            "docs/mockups/web-manager.html",
            "tests/smoke_native_tabs_v2.py",
            "tests/smoke_native_preferences_v2.py",
            "tests/smoke_image_preview_preferences_v2.py",
            "tests/test_sync_config_preferences.py",
            "tests/test_history_source_lease.py",
            "tests/test_release_announcement_workflows.py",
            "tests/test_workspace_control.py",
            "tests/test_history_reader.py",
            "tests/test_history_recovery.py",
            "tests/smoke_history_recovery_v2.py",
            "tests/smoke_history_subagent_recovery_v2.py",
            "tests/test_private_memory.py",
            "tests/test_private_output.py",
            "tests/test_ci_ubuntu_apt.py",
            "tests/smoke_session_sync_v2.py",
            "tests/smoke_tui_v2.py",
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
            "tests/test_tui_app.py",
            "tests/test_tui_sessions.py",
            "tests/test_activity_log.py",
            "tests/smoke_v2.py",
            "tests/plugin_bridge_fixture.mjs",
            "tests/copied_plugin_bytes.mjs",
            "tools/check_package.py",
            "tools/check_release.py",
            "tools/check_release_artifacts.py",
            "tests/test_release_artifacts.py",
            "tests/test_native_host_profile.py",
            "tests/test_native_recovery_process.py",
            "tests/test_native_smoke_profiles.py",
            "tests/test_v2_only_host_fixture.py",
            "tools/native_host_profile.py",
            "tools/test_v2_lifecycle_native.py",
            "tools/test_v2_lifecycle_podman.py",
            "tests/test_release_channels.py",
            "tools/ci_ubuntu_apt.py",
            "tools/test_v2_podman.py",
            "tools/test_sharing_podman.py",
            "tools/sharing_archive_scan.py",
            "tools/sharing_archive_scan_original.py",
            "tools/sharing_backup_probe.py",
            "tools/sharing_fixture_provider.py",
            "tools/sharing_pax_reason.py",
            "tools/diagnose_sharing_backup.py",
            "tools/migrate_host_plugins.py",
            "tools/port_host_plugins.py",
            "tools/native_plugins/output-clamp/index.js",
            "tools/native_plugins/pdf-to-text/index.js",
            "tools/host_sandbox.js",
            "tests/test_v2_host_fixture.py",
            "src/oc_box/oc_box.py",
            "src/oc_box/private_memory.py",
            "src/oc_box/workspace_control.py",
            "src/oc_box/history_reader.py",
            "src/oc_box/history_recovery.py",
            "src/oc_box/opencode_compat.py",
            "src/oc_box/opencode_v2_io.py",
            "src/oc_box/workspace_service.py",
            "src/oc_box/ui_state.py",
            "src/oc_box/ui_state_memory.js",
            "src/oc_box/ui_state_rpc.js",
            "src/oc_box/ui_state_server.js",
            "src/oc_box/ui_state_tui.js",
            "src/oc_box/file_preview.py",
            "src/oc_box/file_preview_io.py",
            "src/oc_box/file_preview_tui.tsx",
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
            "tests/test_ui_state.py",
            "tests/ui_state_fixture.mjs",
            "tests/smoke_ui_state_v2.py",
            "docs/ui-state.md",
            "src/oc_box/session_terminal.py",
            "src/oc_box/sharing.py",
            "src/oc_box/attachments.py",
            "src/oc_box/attachment_io.py",
            "src/oc_box/host_drop.py",
            "src/oc_box/host_drop_io.py",
            "src/oc_box/host_drop_input.py",
            "src/oc_box/workspace_preferences.py",
            "src/oc_box/startup_output.py",
            "src/oc_box/lifecycle_output.py",
            "src/oc_box/logs_cli.py",
            "src/oc_box/journal_output.py",
            "src/oc_box/log_config.py",
            "src/oc_box/cli_help.py",
            "src/oc_box/plugin_compat.py",
            "src/oc_box/plugin_bridge.js",
            "src/oc_box/paths.py",
            "src/oc_box/version_info.py",
            "docs/oc-box.info",
            "docs/web-shell.md",
            "docs/opencode-2.md",
            "docs/sharing-host-acceptance.md",
            "docs/sharing-handoff.md",
            "completions/oc-box",
            "LICENSE",
            "NOTICE",
        ):
            assert root + "/" + name in source.getnames(), name
        for name, content in web_runtime.items():
            relative = "src/oc_box/" + name
            assert root + "/" + relative in source.getnames(), relative
            module = source.extractfile(root + "/" + relative)
            assert module is not None and module.read() == content, relative
        assert not any(name.endswith(".wasm") for name in source.getnames()), (
            "unapproved web verifier"
        )
        version_file = source.extractfile(root + "/src/oc_box/version_info.py")
        assert version_file is not None
        sdist_version_source = version_file.read().decode()
        assert sdist_version_source == version_source, "version_info.py"
        check_build_identity(original, sdist_version_source, version)
        package_file = source.extractfile(root + "/PKG-INFO")
        assert package_file is not None
        package = BytesParser().parsebytes(package_file.read())
        assert package["Name"] == "oc-box" and package["Version"] == version, "PKG-INFO"
    return wheels[0], build


def check(directory, *, with_vault=False, with_tui=False):
    wheel, build = check_archives(directory)
    version = build["distribution_version"]
    with tempfile.TemporaryDirectory(prefix="oc-box-wheel-check-") as temporary:
        work = Path(temporary)
        environment = work / "venv"
        home = work / "home"
        home.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        # A positive allowlist also makes leaked parent provider credentials
        # unavailable to test commands or package installation subprocesses.
        env = {
            "PATH": str(environment / "bin") + ":/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "PYTHONNOUSERSITE": "1",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "OC_BOX_CONTAINER": "0",
        }

        def run(*command):
            result = subprocess.run(
                list(map(str, command)),
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        run(
            python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            wheel.resolve(),
        )
        cli = environment / "bin/oc-box"
        assert run(cli, "--version").strip() == "oc-box " + build["version"]
        overview = run(cli, "--help")
        assert "COMMAND --help" in overview
        assert "--image-preview" not in overview and "--delete-sessions" not in overview
        web_help = run(cli, "web", "--help")
        assert "Open Studio" in web_help and "native one-use link" in web_help
        assert (
            "--workspace-layout" in web_help and "--no-workspace-layout" not in web_help
        )
        assert all(word not in web_help for word in ("--auth", "--bind", "web tls"))
        assert "web-tls-local" not in run(cli, "doctor", "--help")
        tls = subprocess.run(
            [cli, "web", "tls", "setup", "localhost"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert tls.returncode == 2 and "unrecognized arguments" in tls.stderr
        log_help = run(cli, "logs", "--help")
        for option in (
            "--level",
            "--verbose",
            "--table",
            "--limit",
            "--follow",
            "--json",
            "--journald",
            "--path",
            "--log-dir",
        ):
            assert option in log_help, option
        begin_help = run(cli, "start", "--help")
        assert "--image-preview" in begin_help and "--delete-sessions" not in begin_help
        assert "--host-file-open" in begin_help and "--no-host-file-open" in begin_help
        run(
            python,
            "-I",
            "-c",
            "from oc_box import oc_box as box; "
            "assert not box.effective_host_file_open(box.parser().parse_args([]), {})",
        )
        for option in (
            "--journald",
            "--no-journald",
            "--log-dir",
            "--lifecycle-diagnostics",
            "--verbose",
        ):
            assert option in begin_help, option
        sync_help = run(cli, "sync", "--help")
        assert (
            "--lifecycle-diagnostics" in sync_help
            and "--verbose" in sync_help
            and "--image-preview" not in sync_help
        )
        logdir = work / "one-invocation-log"
        assert run(cli, "logs", "--log-dir", str(logdir), "--path").strip() == str(
            logdir / "activity.jsonl"
        )
        assert not run(cli, "logs", "--log-dir", str(logdir), "--verbose=ALL", "--json")
        assert not logdir.exists()
        assert (
            run(python, "-m", "oc_box", "--version").strip()
            == "oc-box " + build["version"]
        )
        assert "Paulo Dias" in run(cli, "--about")
        assert "WORKSPACE ID" in run(cli, "list")
        assert "tumbleweed" in run(cli, "distros")
        assert json.loads(run(cli, "--status", "--json"))["workspaces"] == []
        assert "No oc-box workspaces registered." in run(cli, "status", "--table")
        # Ownership detection is offline. Explicit upgrade delegates to the
        # verified package manager; artifact checks must not query or mutate it.
        assert "Python-package managed" in run(
            python,
            "-c",
            "from oc_box import oc_box,updater; "
            "assert updater.package_managed(oc_box.APP); "
            "print(updater.package_upgrade_message())",
        )
        upgrade_help = run(cli, "upgrade", "--help")
        assert "--devel" in upgrade_help and "--stable" in upgrade_help
        assert "--import-session" in begin_help
        completion = environment / "share/bash-completion/completions/oc-box"
        assert completion.is_file()
        assert run(cli, "completion", "bash") == completion.read_text()
        assert "complete -F _oc_box oc-box" in completion.read_text()
        assert "--portable" in run(
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'source "$1"; COMP_WORDS=(oc-box backup --por); COMP_CWORD=2; _oc_box; printf "%s\\n" "${COMPREPLY[@]}"',
            "completion-check",
            completion,
        )
        doctor = json.loads(run(cli, "doctor", "--feature", "github-upgrade", "--json"))
        assert doctor["schema_version"] == 1
        assert doctor["features"][0]["id"] == "github-upgrade"
        private = json.loads(
            run(cli, "doctor", "--feature", "private-output", "--json")
        )
        assert private["features"][0]["available"]
        # Older uv-managed interpreters can omit the binding despite a capable
        # libc/kernel. Exercise the installed package, not checkout imports.
        run(
            python,
            "-c",
            "import os,stat; from oc_box import private_memory; "
            "os.__dict__.pop('memfd_create',None); os.__dict__.pop('MFD_CLOEXEC',None); "
            "fd=private_memory.create('oc-box-package-check'); "
            "assert stat.S_ISREG(os.fstat(fd).st_mode); "
            "assert not os.get_inheritable(fd); "
            "os.write(fd,b'synthetic-package-check'); os.lseek(fd,0,0); "
            "assert os.read(fd,64)==b'synthetic-package-check'; os.close(fd)",
        )
        assert "secrets" in overview
        backup_help = run(cli, "backup", "--help")
        assert all(
            flag in backup_help
            for flag in ("--recipient", "--host", "--opencode-auth-export")
        )
        assert "--no-open" in run(cli, "web", "--help")
        assert "--read-only" in run(cli, "tui", "--help")
        assert "--acknowledge-risk" in run(cli, "share", "--help")
        assert "textual" not in run(
            python, "-c", "import sys,oc_box.tui; print(' '.join(sys.modules))"
        )
        assert "--maintenance" in run(cli, "terminal", "--help")
        assert "--min-passphrase-length" in backup_help
        restore_help = run(cli, "restore", "--help")
        assert all(
            flag in restore_help
            for flag in ("--identity", "--host", "--include-opencode")
        )
        assert "--identity" in run(cli, "backup-info", "--help")
        assert "init" in run(cli, "secrets", "--help")
        assert "--min-passphrase-length" in run(cli, "secrets", "--help")
        # Default package installation keeps crypto optional and never prompts.
        assert "cryptography" not in run(
            python,
            "-c",
            "import sys; import oc_box.vault; print(' '.join(sys.modules))",
        )
        data = run(
            python,
            "-c",
            "import json,oc_box.oc_box as b; from oc_box import workspace_identity; from pathlib import Path; print(json.dumps({'root':str(b.APP),'bootstrap':(b.APP/'bootstrap.py').is_file(),'identity':workspace_identity.snapshot(b.APP)[0]}))",
        )
        assert json.loads(data)["bootstrap"]
        # A colliding generic module name elsewhere on sys.path must not hijack
        # the namespaced installed launcher or its lazy imports.
        (work / "workspace_identity.py").write_text(
            "raise RuntimeError('unrelated top-level module imported')\n"
        )
        assert "tumbleweed" in run(python, "-m", "oc_box", "distros")
        fake_bin = work / "fake-bin"
        fake_bin.mkdir()
        podman_log = work / "podman.jsonl"
        podman = fake_bin / "podman"
        podman.write_text(
            "#!"
            + str(python)
            + "\n"
            + "import json,sys\n"
            + "with open("
            + repr(str(podman_log))
            + ",'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n"
            + "if sys.argv[1:2]==['info']: print('true'); sys.exit(0)\n"
            + "if sys.argv[1:3]==['container','exists'] or sys.argv[1:3]==['volume','exists']: sys.exit(1)\n"
            + "sys.exit(125)\n"
        )
        podman.chmod(0o755)
        registry = home / ".local/state/oc-box/workspaces" / ("a" * 32)
        registry.mkdir(parents=True)
        project = home / "project"
        project.mkdir()
        marker = project / ".oc-box-id"
        marker.write_text("a" * 32 + "\n")
        (registry / "workspace.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "id": "a" * 32,
                    "project": str(project),
                    "project_identity": [project.stat().st_dev, project.stat().st_ino],
                    "distro": "tumbleweed",
                    "image": "fixture",
                    "container": "oc-box-" + "a" * 32,
                    "home_volume": "oc-box-home-" + "a" * 32,
                    "home": str(home),
                    "cpus": "4",
                    "memory": "8g",
                    "pids": 2048,
                }
            )
        )
        env["PATH"] = str(fake_bin) + ":" + env["PATH"]
        assert "a" * 32 in run(cli, "list")
        # CI is nonroot; local container checks may need a simulated host UID
        # for the fake Podman operations only. Core CLI tests above are unpatched.
        backend_cli = [cli]
        if os.geteuid() == 0:
            backend_cli = [
                python,
                "-c",
                "import sys; from oc_box import oc_box as b; b.os.geteuid=lambda:1000; sys.exit(b.entrypoint())",
            ]
        resource = json.loads(
            run(*backend_cli, "--status", "--workspace", "a" * 32, "--json")
        )
        assert resource["workspaces"][0]["state"] == "missing"
        assert "Container stopped" in run(*backend_cli, "stop", "--workspace", "a" * 32)
        repair = run(*backend_cli, "repair", "--workspace", "a" * 32)
        assert "Workspace identity repaired" in repair or "already matches" in repair
        assert marker.exists()
        assert "Credential forwarding mode: env" in run(
            cli, "credentials", "--workspace", "a" * 32
        )
        assert "Credential forwarding mode: selected" in run(
            cli,
            "credentials",
            "--workspace",
            "a" * 32,
            "--credential-mode=selected",
            "--vault-secret=EXAMPLE_TOKEN",
        )
        assert "EXAMPLE_TOKEN" in run(cli, "credentials", "--workspace", "a" * 32)
        assert all(
            call[0] in ("info", "container", "volume")
            for call in map(json.loads, podman_log.read_text().splitlines())
        )
        assert (environment / "share/man/man1/oc-box.1").is_file()
        assert (environment / "share/info/oc-box.info").is_file()
        # Declared console entrypoint retains normal readable validation errors.
        invalid = subprocess.run(
            [str(cli), "--check"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert invalid.returncode == 1 and "Traceback" not in invalid.stderr
        assert not (home / ".local/bin/oc-box").exists(), (
            "Package install created a competing global launcher"
        )
        assert not (home / ".local/share/oc-box").exists(), (
            "Package management called the GitHub installer"
        )
        if with_vault:
            run(
                python,
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                str(wheel.resolve()) + "[vault]",
            )
            # Prompt mocking is only at the input boundary; exercise real
            # installed CLI routing, cipher/KDF, persistence, and name selection.
            exercise = """import contextlib,io,json,os
from pathlib import Path
from unittest.mock import patch
from oc_box import oc_box as box, vault
phrase='eight888'
value='synthetic package test credential'
with patch.object(vault,'hidden',side_effect=[phrase,phrase]), contextlib.redirect_stdout(io.StringIO()):
    assert box.main(['secrets','init'])==0
with patch.object(vault,'hidden',side_effect=[phrase,value,value]), contextlib.redirect_stdout(io.StringIO()):
    assert box.main(['secrets','set','EXAMPLE_TOKEN'])==0
raw=vault.vault_path().read_text()
assert value not in raw and phrase not in raw and 'EXAMPLE_TOKEN' not in raw
with patch.object(vault,'hidden',return_value=phrase):
    selected=vault.select(['EXAMPLE_TOKEN'])
assert selected=={'EXAMPLE_TOKEN':value}
assert 'EXAMPLE_TOKEN' not in os.environ
print('PASS: installed vault extra encrypts, unlocks, and selects without exposing values to os.environ or disk.')
"""
            print(run(python, "-c", exercise).strip())
        if with_tui:
            run(
                python,
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                str(wheel.resolve()) + "[tui]",
            )
            exercise = """import asyncio
from oc_box.tui import DemoBackend
from oc_box.tui_app import DashboardApp
from textual.widgets import Input
async def check():
    app=DashboardApp(DemoBackend(), read_only=True, demo=True)
    async with app.run_test(size=(80,24)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.report['workspaces'])==1
        assert len(app.logs.search('ready'))==1
        app.action_settings()
        await pilot.pause()
        assert app.screen.query_one('#pref-interval', Input).value=='1'
        await pilot.press('escape')
asyncio.run(check())
print('PASS: installed TUI extra renders the synthetic dashboard without Podman or user state.')
"""
            print(run(python, "-c", exercise).strip())
    print(
        f"PASS: oc-box {version} wheel/sdist metadata, packaged resources/manuals, isolated CLI, workspace/status tools, and safe package-manager upgrades."
    )
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution_dir", type=Path)
    parser.add_argument(
        "--with-vault",
        action="store_true",
        help="also resolve/test the optional crypto extra inside the temporary venv",
    )
    parser.add_argument(
        "--with-tui",
        action="store_true",
        help="also resolve/test the optional TUI extra in the temporary venv",
    )
    arguments = parser.parse_args()
    check(
        arguments.distribution_dir,
        with_vault=arguments.with_vault,
        with_tui=arguments.with_tui,
    )
