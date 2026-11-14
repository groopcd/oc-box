#!/usr/bin/env python3
"""Private unmodified-native contracts for the V2-only HOST fixture.

Runs inside oc-box without Podman, without pretending to be a host, and without
a V1 executable. Checks the pinned selector, native seed/export/import helper,
synthetic provider/shell/MCP contract, rotated child-only env and same-session
history across native private-server restart. NOT host lifecycle acceptance.
"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from tools import test_v2_lifecycle_podman as fixture

legacy, safety, box = fixture.legacy, fixture.safety, fixture.box
bridge = legacy.history_io
require = safety.require


@contextlib.contextmanager
def working_directory(directory):
    previous = Path.cwd()
    try:
        os.chdir(directory)
        yield
    finally:
        os.chdir(previous)


def run(binary, version):
    with tempfile.TemporaryDirectory(prefix="oc-box-v2-only-native-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        for name in ("bin", "host-home", "source-a"):
            (root / name).mkdir(mode=0o700)
        wrapper, profile = fixture.prepare_native_cli(root, binary, version)
        # This is the actual shared host seed helper on an unmodified private
        # native server, not a handwritten projected export.
        with safety.private_output():
            seed = safety.seed_native_history(root, binary)
        ident = seed["info"]["id"]
        require(safety.private_json(root, "seed-baseline.json") == seed, "native-seed")
        # The selector cannot pass ambient host/provider/server credentials.
        require(
            legacy.check_native_version(root, wrapper, "2", expected=version)
            == version,
            "native-selector",
        )
        state = {
            "key": "fixture-" + secrets.token_urlsafe(24),
            "markers": set(),
            "requests": 0,
            "errors": [],
        }
        server, worker = legacy.make_provider(state)
        requests = []
        try:
            config = legacy.fixture_config(server.server_port)
            config["provider"]["fixture"]["options"]["baseURL"] = (
                f"http://127.0.0.1:{server.server_port}/v1"
            )
            target = root / "host-config-2/opencode"
            target.mkdir(mode=0o700, exist_ok=True)
            safety.write_json_new(target / "opencode.json", config)
            env = legacy.native_env(root, "2")
            overlay = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            overlay.update(box.container_build_config(version))
            # Seed permissions intentionally exercise ASK inheritance in the
            # host fixture. This offline provider session instead has native
            # allow rules at creation; it does NOT establish Build-hook coverage.
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay)
            previous = None
            native_session = None
            real_popen = subprocess.Popen

            def child_environment(command, **kwargs):
                if list(command)[1:2] == ["serve"]:
                    # Synthetic values enter only the private server's child
                    # env, never os.environ, CLI argv or config files.
                    kwargs["env"] = {**kwargs["env"], legacy.KEY: state["key"]}
                return real_popen(command, **kwargs)

            for attempt in range(2):
                if attempt:
                    state["key"] = "fixture-" + secrets.token_urlsafe(24)
                state["markers"].clear()
                before = state["requests"]
                with (
                    working_directory(root / "source-a"),
                    patch.dict(os.environ, env, clear=True),
                    patch.object(subprocess, "Popen", side_effect=child_environment),
                    safety.private_output(),
                    bridge.server(str(binary)) as request,
                ):
                    if native_session is None:
                        native_session = request(
                            "POST",
                            "/api/session",
                            {
                                "location": {"directory": str(root / "source-a")},
                                "agent": "build",
                                "permissions": [
                                    {"action": "*", "resource": "*", "effect": "allow"}
                                ],
                            },
                        )["data"]["id"]
                    else:
                        retained = bridge.checked_export(request, native_session)
                        legacy.verify_v2_progress(retained, previous)
                    # The unrelated imported seed remains byte-exact in history.
                    current_seed = bridge.checked_export(request, ident)
                    require(
                        current_seed["messages"] == seed["messages"],
                        "seed-preservation",
                    )
                    client_env = {
                        **env,
                        "OPENCODE_PASSWORD": request.endpoint["password"],
                    }
                    require(
                        legacy.KEY not in client_env and legacy.KEY not in os.environ,
                        "client-credential-env",
                    )
                    result = subprocess.run(
                        [
                            str(binary),
                            "run",
                            "--server",
                            request.endpoint["url"],
                            "--session",
                            native_session,
                            "--agent",
                            "build",
                            "--model",
                            "fixture/gpt-smoke",
                            "Exercise private synthetic shell and MCP contracts.",
                        ],
                        env=client_env,
                        cwd=root / "source-a",
                        input="",
                        capture_output=True,
                        text=True,
                        timeout=180,
                        check=False,
                    )
                    require(
                        result.returncode == 0
                        and "HOST_V2_ACCEPTANCE_OK" in result.stdout
                        and not state["errors"]
                        and state["markers"] == {"VAULT_SHELL_OK", "VAULT_MCP_OK"},
                        "native-provider-tools",
                    )
                    actual = bridge.checked_export(request, native_session)
                    if previous is not None:
                        legacy.verify_v2_progress(actual, previous)
                    require(
                        "HOST_V2_ACCEPTANCE_OK" in json.dumps(actual), "native-history"
                    )
                    # Exercise the actual export/import transport after model
                    # work, on the same ID, with complete messages unchanged.
                    request("DELETE", "/api/session/" + native_session)
                    imported = request(
                        "POST",
                        "/api/experimental/session/import",
                        {
                            **actual,
                            "location": {"directory": str(root / "source-a")},
                        },
                    )["data"]
                    require(imported["id"] == native_session, "native-roundtrip")
                    roundtrip = bridge.checked_export(request, native_session)
                    require(
                        roundtrip["messages"] == actual["messages"], "native-roundtrip"
                    )
                    previous = roundtrip
                requests.append(state["requests"] - before)
            require(fixture.binary_hash(binary) == profile["sha256"], "native-changed")
            return {
                "status": "passed",
                "kind": "offline-native-contract-only",
                "native": profile,
                "native_versions": {"2": version},
                "checks": [
                    "pinned-selector",
                    "native-seed-helper",
                    "native-export-import-exact",
                    "synthetic-provider-shell-mcp",
                    "child-only-rotated-credentials",
                    "same-session-server-restart",
                ],
                "synthetic_requests_per_owner": requests,
                "podman_ran": False,
                "host_acceptance": False,
                "legacy_v1_ran": False,
                "limits": "No lifecycle/backup/reset/vault/UI/Build-hook/real-provider/OAuth/network/security acceptance.",
            }
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


def main(argv=None):
    parser = fixture.QuietParser(description=__doc__)
    parser.add_argument("--opencode-v2", type=Path, required=True)
    parser.add_argument("--native-version", choices=fixture.VERSIONS, default="2.3.0")
    args = parser.parse_args(argv)
    try:
        binary = args.opencode_v2.resolve(strict=True)
        # Capture subprocess diagnostics privately; print only public contract
        # categories on failure, never raw native stderr or synthetic values.
        with safety.private_output():
            report = run(binary, args.native_version)
        print(json.dumps(report, indent=2))
        return 0
    except BaseException as error:
        print(
            "V2-only offline native contract failed: "
            + safety.safe_error_class(error)
            + ".",
            file=sys.stderr,
        )
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
