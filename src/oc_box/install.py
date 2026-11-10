#!/usr/bin/env python3
"""Install the external launcher and prebuilt manuals in ~/.local."""

from pathlib import Path
import argparse
import sys

if __package__:
    from .installation import install
else:
    from installation import install


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--with-vault",
            action="store_true",
            help="install optional cryptography into a private per-release runtime (no system/user-site packages)",
        )
        parser.add_argument(
            "--with-tui",
            action="store_true",
            help="install optional TUI dependencies into this immutable release's private runtime",
        )
        args = parser.parse_args(argv)
        install(
            Path(__file__).resolve().parent,
            with_vault=args.with_vault,
            with_tui=args.with_tui,
        )
    except (OSError, ValueError) as exc:
        print(f"oc-box install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
