#!/usr/bin/env python3
"""Show quiet and verbose preparation with synthetic children and private state."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
FIXTURE = """
import sys
from oc_box import lifecycle_output, oc_box as box

def synthetic_preparation():
    lifecycle_output.bind_workspace("a" * 32)
    for task, label in (
        ("container-image", "Podman image"),
        ("baseline-packages", "baseline packages"),
        ("baseline-tools", "runtime tools"),
        ("native-cli-install", "OpenCode npm package"),
        ("plugin-dependencies", "plugin/npm dependencies"),
    ):
        box.run(
            [sys.executable, "-c", "import sys,time; "
             "print(sys.argv[1] + ': synthetic installation output', flush=True); "
             "print(sys.argv[1] + ': synthetic installer diagnostics', file=sys.stderr); "
             "time.sleep(.18)", label],
            timeout=5,
            preparation=task,
        )
    print("OpenCode would start here; no native client was launched.", flush=True)
    return 0

box.main = synthetic_preparation
raise SystemExit(box.entrypoint())
"""


def main():
    with tempfile.TemporaryDirectory(prefix="oc-box-preparation-demo-") as name:
        private = Path(name)
        environment = {
            "HOME": name,
            "PATH": os.defpath,
            "PYTHONPATH": str(SOURCE),
            "OC_BOX_STATE_HOME": str(private / "state"),
            "XDG_CONFIG_HOME": str(private / "config"),
            "TERM": "dumb",
        }
        cli = [sys.executable, "-B", "-m", "oc_box"]
        fixture = [sys.executable, "-B", "-c", FIXTURE]
        for title, command in (
            ("Quiet first start", [*fixture, "start"]),
            ("Read preparation details", [*cli, "logs", "--tail"]),
            ("Reset with live installer output", [*fixture, "reset", "--verbose"]),
        ):
            print(f"\n--- {title} ---", flush=True)
            subprocess.run(
                command, cwd=private, env=environment, check=True, timeout=20
            )


if __name__ == "__main__":
    main()
