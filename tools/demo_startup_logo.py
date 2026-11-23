#!/usr/bin/env python3
"""Preview the real oc-box startup line with a synthetic child; no workspace."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box.terminal_ui import run_tui


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("style", choices=("ascii", "graphics"))
    args = parser.parse_args()
    return run_tui(
        [
            sys.executable,
            "-B",
            "-c",
            "import time; time.sleep(2.5); "
            "print('Synthetic startup complete; no workspace was opened.')",
        ],
        startup_logo=args.style,
    )


if __name__ == "__main__":
    raise SystemExit(main())
