#!/usr/bin/env python3
"""Install the external launcher and prebuilt manuals in ~/.local."""

from pathlib import Path
import sys

from installation import install

try:
    install(Path(__file__).resolve().parent)
except (OSError, ValueError) as exc:
    print(f"oc-box install: {exc}", file=sys.stderr)
    raise SystemExit(1)
