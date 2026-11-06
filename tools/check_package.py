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


def check(directory, *, with_vault=False):
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
        assert (
            len(requirements) == 1
            and requirements[0].startswith("cryptography>=44")
            and 'extra == "vault"' in requirements[0]
        ), requirements
        for name in (
            "oc_box.py",
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
            "updater.py",
            "installation.py",
            "LICENSE",
            "NOTICE",
            "docs/cli.md",
            "docs/oc-box.1",
            "docs/oc-box.info",
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
            "tests/test_updater.py",
            "tools/check_package.py",
            "docs/oc-box.info",
            "LICENSE",
            "NOTICE",
        ):
            assert root + "/" + name in source.getnames(), name
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
            wheels[0].resolve(),
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
        assert "secrets" in run(cli, "--help")
        assert "--recipient" in run(cli, "backup", "--help")
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
                str(wheels[0].resolve()) + "[vault]",
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
    arguments = parser.parse_args()
    check(arguments.distribution_dir, with_vault=arguments.with_vault)
