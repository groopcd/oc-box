#!/usr/bin/env python3
"""Install the external launcher without editing OpenCode config."""

from pathlib import Path
import json
import shlex
import shutil

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
print(f"Installed {binary}")
print(
    "Run 'oc-box distros', then run 'oc-box' from a dedicated project directory as your regular user."
)
