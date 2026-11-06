#!/usr/bin/env python3
"""Disposable-distro CI: install mapped packages and verify their real providers.

Run only in an empty test container with --install. Never manages real oc-box
workspaces or tries nested Podman. Standard-library-only, no Python dependencies.
"""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dependencies as deps


def verify(distro, install=False):
    assert deps.host_distro() == distro, (distro, deps.host_distro())
    keys = sorted(set(deps.COMMON) | set(deps.PACKAGES[distro]))
    packages = deps.package_names(distro, keys)
    print("Testing", distro, "providers:", " ".join(packages), flush=True)
    if install:
        assert os.geteuid() == 0, "Use only in a disposable root test container"
        # Execute the same mapped command, with the test-only yes flag and no
        # sudo (minimal images run as root and do not necessarily ship sudo).
        command = deps.install_commands(distro, keys)[0].replace("sudo ", "")
        if distro in ("tumbleweed", "leap"):
            command = command.replace("zypper ", "zypper --non-interactive ")
        elif distro in ("ubuntu", "debian", "fedora"):
            command = command.replace(" install ", " install -y ")
        elif distro == "arch":
            command = command.replace("pacman ", "pacman --noconfirm ")
        subprocess.run(
            ["/bin/sh", "-ec", command],
            check=True,
            timeout=1200,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        # Also execute the real workspace command. Install providers together
        # first so SUSE's solver does not select mutually exclusive BusyBox
        # pager alternatives before the optional manual tools are requested.
        subprocess.run(
            ["/bin/sh", "-ec", deps.baseline_install(distro)],
            check=True,
            timeout=1200,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    # Older Leap may default to an obsolete Python even though packages exist.
    assert deps.probe("python")[0], sys.version
    for key in keys:
        present, detail = deps.probe(key)
        assert present, (distro, key, detail)
        print("PASS:", key, detail, flush=True)
    assert deps.probe("memfd")[0]
    # Verify a real standard-age envelope, not just package database metadata.
    with tempfile.TemporaryDirectory(prefix="oc-box-distro-age-") as temporary:
        root = Path(temporary)
        key = root / "identity"
        subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
        key.chmod(0o600)
        recipient = subprocess.check_output(
            ["age-keygen", "-y", str(key)], text=True
        ).strip()
        ciphertext = subprocess.check_output(
            ["age", "-r", recipient], input=b"synthetic-distro-fixture"
        )
        cleartext = subprocess.check_output(
            ["age", "-d", "-i", str(key)], input=ciphertext
        )
        assert cleartext == b"synthetic-distro-fixture"
    selected = [
        "maintenance",
        "backup",
        "backup-info",
        "restore",
        "storage",
        "github-upgrade",
        "bootstrap",
        "docs-build",
        "manuals",
        "overlay",
    ]
    data = deps.report(selected)
    assert all(feature["available"] for feature in data["features"]), data
    original_probe = deps.probe

    def missing_age(key):
        return (
            (False, "Command unavailable: " + key)
            if key in ("age", "age-keygen")
            else original_probe(key)
        )

    with patch.object(deps, "probe", side_effect=missing_age):
        data = deps.report(["backup-info", "storage"])
    backup, storage = data["features"]
    assert not backup["available"] and storage["available"]
    assert backup["packages"] == ["age"], backup
    assert deps.MANAGERS[distro][0] in backup["install_commands"][0]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert deps.cli(["--feature", "backup-info", "--json"]) == 0
    assert json.loads(output.getvalue())["features"][0]["available"]
    print(
        "PASS:",
        distro,
        "all mapped packages, baseline tools, age round-trip and scoped diagnostics",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", choices=deps.DISTROS, required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    verify(args.distro, args.install)
