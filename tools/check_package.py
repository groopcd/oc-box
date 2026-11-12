#!/usr/bin/env python3
"""Verify built wheel/sdist contents and real isolated wheel CLI behavior.

Run with a directory containing one wheel and sdist; needs no project checkout
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


def check_archives(directory):
    """Validate distribution contents without installing or running the wheel."""
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata_path = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        version = metadata["Version"]
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
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
            "workspace_control.py",
            "history_reader.py",
            "history_recovery.py",
            "session_terminal.py",
            "sharing.py",
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
            "terminal_ui.py",
            "status_report.py",
            "activity_log.py",
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
            "completions/oc-box",
            "build-info.json",
            "distribution.json",
        ):
            assert "oc_box/" + name in names, name
        assert "oc_box/setup.py" not in names
        assert not any(
            "/tests/" in name
            or name.endswith(".pyc")
            or ".oc-box-id" in name
            or "/.git/" in name
            for name in names
        )
        build = json.loads(wheel.read("oc_box/build-info.json"))
        assert build["distribution_version"] == version
        assert build["version"].startswith(version + "+g")
    with tarfile.open(sources[0], "r:gz") as source:
        root = source.getnames()[0].split("/", 1)[0]
        build_file = source.extractfile(root + "/build-info.json")
        assert build_file is not None
        original = json.load(build_file)
        assert original["commit"] == build["commit"]
        assert original["commit_count"] == build["commit_count"]
        for name in (
            "pyproject.toml",
            "setup.py",
            "install.py",
            "src/_oc_box_build.py",
            "tests/test_updater.py",
            "tests/test_session_terminal.py",
            "tests/test_sharing.py",
            "tests/test_sharing_transport.py",
            "tests/smoke_sharing_v2.py",
            "tests/test_session_sync.py",
            "tests/test_workspace_control.py",
            "tests/test_history_reader.py",
            "tests/test_history_recovery.py",
            "tests/smoke_history_recovery_v2.py",
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
            "tools/ci_ubuntu_apt.py",
            "tools/test_v2_podman.py",
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
            "src/oc_box/session_terminal.py",
            "src/oc_box/sharing.py",
            "src/oc_box/plugin_compat.py",
            "src/oc_box/plugin_bridge.js",
            "src/oc_box/paths.py",
            "docs/oc-box.info",
            "docs/opencode-2.md",
            "completions/oc-box",
            "LICENSE",
            "NOTICE",
        ):
            assert root + "/" + name in source.getnames(), name
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
        assert (
            run(python, "-m", "oc_box", "--version").strip()
            == "oc-box " + build["version"]
        )
        assert "Paulo Dias" in run(cli, "--about")
        assert "WORKSPACE ID" in run(cli, "list")
        assert "tumbleweed" in run(cli, "distros")
        assert json.loads(run(cli, "--status", "--json"))["workspaces"] == []
        assert "Python-package managed" in run(cli, "upgrade")
        assert "--import-session" in run(cli, "--help")
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
        assert "secrets" in run(cli, "--help")
        assert "--recipient" in run(cli, "backup", "--help")
        assert "--no-open" in run(cli, "web", "--help")
        assert "--read-only" in run(cli, "tui", "--help")
        assert "--acknowledge-risk" in run(cli, "share", "--help")
        assert "textual" not in run(
            python, "-c", "import sys,oc_box.tui; print(' '.join(sys.modules))"
        )
        assert "--maintenance" in run(cli, "--help")
        assert "--min-passphrase-length" in run(cli, "backup", "--help")
        assert "--identity" in run(cli, "restore", "--help")
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
from textual.widgets import Select
async def check():
    app=DashboardApp(DemoBackend(), read_only=True, demo=True)
    async with app.run_test(size=(80,24)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.report['workspaces'])==1
        assert len(app.logs.search('ready'))==1
        app.action_settings()
        await pilot.pause()
        assert app.screen.query_one('#pref-interval', Select).value==1
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
