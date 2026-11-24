#!/usr/bin/env python3
"""Preview the inline startup logo with synthetic status and native output."""

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import startup_output
from oc_box.oc_box import loading_stage
from oc_box.terminal_ui import run_tui


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("style", choices=("ascii", "graphics"))
    args = parser.parse_args()
    with startup_output.invocation():
        startup_output.configure(lambda: args.style)
        startup_output.line("oc-box: Synthetic workspace preparation", flush=True)
        with loading_stage("Testing the inline startup badge"):
            time.sleep(2.5)
        startup_output.line("oc-box: Opening a synthetic native client", flush=True)
        return run_tui(
            [
                sys.executable,
                "-B",
                "-c",
                "import time; time.sleep(2.5); "
                "print('Synthetic native output; no workspace was opened.')",
            ],
        )


if __name__ == "__main__":
    raise SystemExit(main())
