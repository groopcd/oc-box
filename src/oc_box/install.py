#!/usr/bin/env python3
"""Install the external launcher and prebuilt manuals in ~/.local."""

from pathlib import Path
import argparse
import os
import re
import subprocess
import sys

if __package__:
    from .installation import install
    from .paths import source_root
    from .version_info import get_version_info, public_version
else:
    from installation import install
    from paths import source_root
    from version_info import get_version_info, public_version


def _verified_bootstrap_checkout(version, commit):
    """Recheck the public bootstrap's pinned checkout before enrolling policy."""
    root = source_root(Path(__file__).resolve().parent)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root.parent),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    try:
        if (
            git("config", "--local", "--get", "remote.origin.url")
            != "https://github.com/groopcd/oc-box.git"
            or git("rev-parse", "--verify", f"refs/tags/v{version}^{{commit}}")
            != commit
            or git("rev-parse", "--verify", "HEAD") != commit
            or git("status", "--porcelain", "--untracked-files=all")
        ):
            return False
        git("merge-base", "--is-ancestor", commit, "refs/remotes/origin/devel")
        return True
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return False


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
        channels = parser.add_mutually_exclusive_group()
        channels.add_argument(
            "--stable", dest="channel", action="store_const", const="stable"
        )
        channels.add_argument(
            "--devel", dest="channel", action="store_const", const="devel"
        )
        parser.add_argument("--release-version", help=argparse.SUPPRESS)
        parser.add_argument("--verified-release-commit", help=argparse.SUPPRESS)
        parser.add_argument("--expect-active-installation", help=argparse.SUPPRESS)
        args = parser.parse_args(argv)
        # Legacy no-argument downloaded installers remain valid. Bootstrap
        # selectors additionally bind the requested published identity.
        verified_release = False
        if args.channel is not None or args.release_version is not None:
            if __package__:
                from .update_channels import (
                    ChannelError,
                    remember_channel,
                    selected_channel,
                    parse_version,
                    version_channel,
                )
            else:
                from update_channels import (
                    ChannelError,
                    remember_channel,
                    selected_channel,
                    parse_version,
                    version_channel,
                )
            build = get_version_info(Path(__file__).resolve().parent)
            try:
                actual_channel = version_channel(build["version"])
            except ChannelError as exc:
                raise ValueError(
                    "Installer source has no valid release identity."
                ) from exc
            if args.channel is not None and actual_channel != args.channel:
                raise ValueError(
                    "Installer source does not match the requested channel."
                )
            if (
                args.release_version is not None
                and public_version(build) != args.release_version
            ):
                raise ValueError(
                    "Installer source does not match the published version."
                )
            # Only the updated, state-preserving bootstrap supplies this
            # checkout attestation after fetching the verified public refs.
            # Older bootstraps and source installs keep the legacy default.
            verified_release = (
                args.channel == "devel"
                and args.release_version is not None
                and build.get("dirty") is False
                and isinstance(build.get("commit"), str)
                and re.fullmatch(r"[a-f0-9]{40}", build["commit"]) is not None
                and args.verified_release_commit == build["commit"]
                and _verified_bootstrap_checkout(args.release_version, build["commit"])
            )
            try:
                selected_channel(args.channel)
            except ChannelError as exc:
                raise ValueError(str(exc)) from exc
            previous = Path.home() / ".local/share/oc-box/build-info.json"
            if previous.exists():
                try:
                    import json

                    with previous.open("rb") as stream:
                        raw = stream.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError()
                    installed = json.loads(raw)
                    older = parse_version(build["version"]) < parse_version(
                        installed["version"]
                    )
                except (OSError, ValueError, KeyError, TypeError, ChannelError) as exc:
                    raise ValueError(
                        "Cannot verify the existing launcher identity before replacement."
                    ) from exc
                if older:
                    print(
                        "Warning: channel selection will downgrade the launcher; "
                        "workspace/session state is not rolled back.",
                        flush=True,
                    )
        install(
            Path(__file__).resolve().parent,
            with_vault=args.with_vault,
            with_tui=args.with_tui,
            **({"verified_release": True} if verified_release else {}),
            **(
                {"expected_active": args.expect_active_installation}
                if args.expect_active_installation is not None
                else {}
            ),
        )
        if args.channel is not None:
            try:
                remember_channel(args.channel)
            except ChannelError as exc:
                raise ValueError(
                    "Release installed, but channel preference could not be saved."
                ) from exc
    except (OSError, ValueError) as exc:
        print(f"oc-box install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
