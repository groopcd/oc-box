#!/usr/bin/env python3
"""Generate public, invocation-scoped Ubuntu APT configuration; never install.

Callers must provide traversable parent directories for APT's _apt sandbox.
Only the fresh output directory and its public files get explicit permissions.
The CA input must be an existing public bundle, not a private key or credential.
"""

import argparse
import os
from pathlib import Path
import re
import shlex
import sys


MIRROR = "https://mirrors.edge.kernel.org/ubuntu"
CODENAMES = ("noble", "resolute")


class ConfigError(ValueError):
    """A fixed, safe-to-print configuration failure."""


def _ubuntu_codename(os_release):
    try:
        text = Path(os_release).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("cannot read Ubuntu os-release") from None
    values = {}
    try:
        for line in text.splitlines():
            fields = shlex.split(line, comments=True, posix=True)
            if not fields:
                continue
            if len(fields) != 1 or "=" not in fields[0]:
                raise ValueError
            key, value = fields[0].split("=", 1)
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or key in values:
                raise ValueError
            values[key] = value
    except ValueError:
        raise ConfigError("invalid Ubuntu os-release") from None
    if values.get("ID") != "ubuntu":
        raise ConfigError("automatic detection requires Ubuntu")
    return values.get("VERSION_CODENAME") or values.get("UBUNTU_CODENAME")


def _apt_path(path):
    value = str(path)
    if any(
        char in '\\"'
        or ord(char) < 32
        or ord(char) == 127
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise ConfigError("output path cannot be safely quoted for APT")
    return '"' + value + '"'


def generate(
    output: Path,
    *,
    codename=None,
    os_release=Path("/etc/os-release"),
    ca_bundle=Path("/etc/ssl/certs/ca-certificates.crt"),
) -> Path:
    """Return an absolute apt.conf path in a new directory, without system edits.

    An explicit codename selects a target Ubuntu image without reading the
    generating host's os-release. Parents must already exist and be traversable
    by _apt; no permissions on existing paths are changed.
    """
    if codename is None:
        codename = _ubuntu_codename(os_release)
    if codename not in CODENAMES:
        raise ConfigError("supported Ubuntu codenames are noble and resolute")

    # Do not resolve the final component: even dangling symlinks must be refused.
    output = Path(os.path.abspath(output))
    sources = output / "ubuntu.sources"
    sourceparts = output / "sources.list.d"
    ca_copy = output / "ca-certificates.crt"
    settings = {
        "Dir::Etc::sourcelist": _apt_path(sources),
        "Dir::Etc::sourceparts": _apt_path(sourceparts),
        "Acquire::https::CaInfo": _apt_path(ca_copy),
        "APT::Update::Error-Mode": '"any"',
        "Acquire::Retries": '"2"',
        "Acquire::http::Timeout": '"20"',
        "Acquire::https::Timeout": '"20"',
        "Acquire::https::Verify-Peer": '"true"',
        "Acquire::https::Verify-Host": '"true"',
        "Acquire::AllowInsecureRepositories": '"false"',
        "Acquire::AllowWeakRepositories": '"false"',
        "Acquire::AllowDowngradeToInsecureRepositories": '"false"',
        "APT::Get::AllowUnauthenticated": '"false"',
        "Acquire::Check-Valid-Until": '"true"',
        "Acquire::Check-Date": '"true"',
    }
    if output.exists() or output.is_symlink():
        raise ConfigError("output already exists; choose a fresh directory")
    try:
        ca_bundle = Path(ca_bundle)
        if not ca_bundle.is_file():
            raise OSError
        ca_bytes = ca_bundle.read_bytes()
    except (OSError, ValueError):
        raise ConfigError("cannot read public CA bundle") from None
    if not ca_bytes:
        raise ConfigError("public CA bundle must be an existing nonempty file")

    suites = " ".join(
        codename + suffix for suffix in ("", "-updates", "-backports", "-security")
    )
    source_text = (
        f"Types: deb\nURIs: {MIRROR}\nSuites: {suites}\n"
        "Components: main restricted universe multiverse\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
    )
    config_text = "".join(f"{key} {value};\n" for key, value in settings.items())
    config = output / "apt.conf"
    try:
        output.mkdir(mode=0o755)
        output.chmod(0o755)
        sourceparts.mkdir(mode=0o755)
        sourceparts.chmod(0o755)
        for path, data in (
            (sources, source_text.encode("utf-8")),
            (ca_copy, ca_bytes),
            (config, config_text.encode("utf-8")),
        ):
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o644)
    except FileExistsError:
        raise ConfigError("output already exists; choose a fresh directory") from None
    except OSError:
        raise ConfigError("cannot create APT configuration") from None
    return config


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "ci_ubuntu_apt: invalid arguments (use --help)\n")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codename", choices=CODENAMES)
    args = parser.parse_args(argv)
    try:
        config = generate(args.output, codename=args.codename)
    except ConfigError as exc:
        print(f"ci_ubuntu_apt: {exc}", file=sys.stderr)
        return 1
    print(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
