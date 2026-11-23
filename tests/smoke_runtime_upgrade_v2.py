#!/usr/bin/env python3
"""Exercise owning preparation across a real npm upgrade, without Podman."""

import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
from unittest.mock import patch

from oc_box import oc_box as box, opencode_v2_io as bridge


OLD = "2.3.0"
REGRESSION = "2.4.0"
ENV_KEY = "OC_BOX_UPGRADE_FIXTURE_API_KEY"


def npm(*args, **kwargs):
    return subprocess.run(
        ["npm", *args], check=True, timeout=180, stdin=subprocess.DEVNULL, **kwargs
    )


def smoke(version, parent, network):
    with tempfile.TemporaryDirectory(prefix="native-upgrade-", dir=parent) as directory:
        root = Path(directory)
        prefix = root / "runtime"
        binary = prefix / "bin/opencode"
        project = root / "project"
        home = root / "workspace-home"
        for folder in (project, home, root / "controller"):
            folder.mkdir()
        data = {
            "id": "native-upgrade-fixture",
            "project": str(project),
            "home": str(home),
            "opencode_version": OLD,
            "credentials": {"mode": "env", "env": [], "vault": []},
        }
        registry = root / "workspace.json"
        config = home / ".config/opencode"
        lockfile = config / "package-lock.json"
        installs = []
        audited = {}

        def execute_inside(workspace, args):
            env = {
                **box.container_env(workspace),
                "PATH": os.environ["PATH"],
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                **network,
            }
            args = [
                str(prefix) if item == "/opt/oc-box/runtime" else item for item in args
            ]
            if args[0] == "npm" and "--package-lock-only" not in args:
                kind = "cli" if "--global" in args else "sdk"
                installs.append(kind)
                if kind == "sdk":
                    flags = ("--prefix", str(config), "--no-fund")
                    # Check the generated, public SDK-only graph before
                    # installing it. A registry/audit failure is not a pass.
                    if args[1] == "install":
                        npm(
                            "install", *flags, "--package-lock-only", "--ignore-scripts",
                            "--no-audit", env=env, cwd=project,
                        )
                    npm(
                        "audit", *flags, "--omit=dev", "--audit-level=info",
                        env=env, cwd=project,
                    )
                    audited[workspace["opencode_version"]] = json.loads(
                        lockfile.read_text()
                    )["packages"]
            return box.ContainerCommand(["env", "--chdir", str(project), *args], env)

        def verify_audited_lock(release):
            assert audited[release]
            assert json.loads(lockfile.read_text())["packages"] == audited[release]

        def verify_lease():
            command = execute_inside(
                data, [sys.executable, "-B", bridge.__file__, "lease", str(binary)]
            )
            with subprocess.Popen(
                command,
                env=command.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            ) as child:
                try:
                    assert select.select([child.stdout], [], [], 30)[0]
                    request = bridge.endpoint_request(json.loads(child.stdout.readline()))
                    catalog = request("GET", "/api/plugin")["data"]
                    assert any(
                        item.get("id") == "oc-box.build-permissions"
                        and item["state"]["status"] == "active"
                        for item in catalog
                    )
                    probe = request("POST", "/api/session", {
                        "title": "Synthetic environment check",
                        "location": {"directory": str(project)},
                    })["data"]["id"]
                    marker = project / ".synthetic-env"
                    marker.unlink(missing_ok=True)
                    request("POST", f"/api/session/{probe}/shell", {
                        "command": f'test "${ENV_KEY}" = synthetic && touch .synthetic-env',
                    })
                    assert marker.is_file(), "native shell did not inherit the owner environment"
                    assert not (config / "service.json").exists(), "unexpected shared-service config"
                    child.stdin.close()
                    assert child.wait(timeout=15) == 0
                finally:
                    if child.poll() is None:
                        child.stdin.close()
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)

        def native_session(method, route, payload=None):
            cwd = Path.cwd()
            try:
                os.chdir(project)
                env = {
                    **box.container_env(data),
                    "PATH": os.environ["PATH"],
                    "OPENCODE_DISABLE_MODELS_FETCH": "1",
                }
                with patch.dict(os.environ, env, clear=True), bridge.server(
                    str(binary)
                ) as request:
                    return request(method, route, payload)["data"]
            finally:
                os.chdir(cwd)

        with (
            patch.dict(os.environ, {
                "PATH": f"{binary.parent}:{os.environ['PATH']}", ENV_KEY: "synthetic",
            }),
            patch.object(box, "CONTAINER_OPENCODE", str(binary)),
            patch.object(box, "exec_command", side_effect=execute_inside),
        ):
            print(f"Preparing OpenCode {OLD}, then upgrading in place to {version}.",
                  flush=True)
            box.prepare_runtime(registry, data, OLD)
            verify_audited_lock(OLD)
            verify_lease()
            title = "Synthetic pre-upgrade session"
            session = native_session(
                "POST", "/api/session",
                {"title": title, "location": {"directory": str(project)}},
            )["id"]
            data["opencode_version"] = version
            box.prepare_runtime(registry, data, version)
            verify_audited_lock(version)
            assert native_session("GET", f"/api/session/{session}")["title"] == title
            assert installs == ["cli", "sdk", "cli", "sdk"], installs
            box.prepare_runtime(registry, data, version)
            assert installs == ["cli", "sdk", "cli", "sdk"], installs
            verify_audited_lock(version)
            verify_lease()
            print(
                f"PASS: {OLD} -> {version}, retained session, Build plugin, "
                "audited SDK and warm cache.",
                flush=True,
            )


if __name__ == "__main__":
    network = {
        key: value
        for key, value in os.environ.items()
        if key.lower() in ("http_proxy", "https_proxy", "no_proxy")
    }
    with tempfile.TemporaryDirectory(prefix="oc-box-native-npm-") as directory:
        root = Path(directory)
        home = root / "host-home"
        home.mkdir()
        with patch.dict(
            os.environ,
            {"HOME": str(home), "PATH": os.environ["PATH"], **network},
            clear=True,
        ):
            subprocess.run(["node", "--version"], check=True, timeout=15)
            npm("--version")
            releases = json.loads(
                npm(
                    "view", "@opencode/cli@2", "version", "--json", text=True,
                    stdout=subprocess.PIPE,
                ).stdout
            )
            latest = releases[-1] if isinstance(releases, list) else releases
            if not isinstance(latest, str) or not re.fullmatch(r"2\.\d+\.\d+", latest):
                raise ValueError("npm did not resolve a stable OpenCode 2.x release.")
            for target in dict.fromkeys((REGRESSION, latest)):
                smoke(target, root, network)
