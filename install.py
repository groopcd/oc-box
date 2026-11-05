#!/usr/bin/env python3
"""Install the external launcher without editing OpenCode config."""

from pathlib import Path
import json
import shlex
import shutil
import subprocess

from version_info import get_version_info

source = Path(__file__).resolve().parent
target = Path.home() / ".local/share/oc-box"
binary = Path.home() / ".local/bin/oc-box"
target.mkdir(parents=True, exist_ok=True)
binary.parent.mkdir(parents=True, exist_ok=True)
for name in (
    "oc_box.py",
    "bootstrap.py",
    "sync_config.py",
    "terminal_ui.py",
    "status_report.py",
    "version_info.py",
    "README.md",
    "LICENSE",
    "NOTICE",
):
    if source != target:
        shutil.copy2(source / name, target / name)
(target / "build-info.json").write_text(
    json.dumps(get_version_info(source), indent=2) + "\n"
)
binary.write_text(
    "#!/bin/sh\nexec python3 " + shlex.quote(str(target / "oc_box.py")) + ' "$@"\n'
)
binary.chmod(0o755)
for name in ("cli.md", "oc-box.1", "oc-box.texi", "oc-box.info"):
    destination = target / "docs" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != target:
        shutil.copy2(source / "docs" / name, destination)
man = Path.home() / ".local/share/man/man1/oc-box.1"
info = Path.home() / ".local/share/info/oc-box.info"
for source_file, destination in (
    (target / "docs/oc-box.1", man),
    (target / "docs/oc-box.info", info),
):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, destination)
install_info = shutil.which("install-info") or shutil.which(
    "install-info", path="/usr/sbin:/sbin"
)
if install_info:
    try:
        indexed = subprocess.run(
            [install_info, "--info-dir=" + str(info.parent), str(info)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if indexed.returncode:
            print(
                "Note: Info index update failed; use 'info --file " + str(info) + "'."
            )
    except (OSError, subprocess.TimeoutExpired):
        print(
            "Note: Info index update unavailable; use 'info --file " + str(info) + "'."
        )
print(f"Installed {binary}")
print(f"Installed manual pages: {man} and {info}")
print(
    "Read with 'man oc-box' or 'info oc-box'; full reference: "
    + str(target / "docs/cli.md")
)
print(
    "Run 'oc-box distros', then run 'oc-box' from a dedicated project directory as your regular user."
)
