"""Exact native V2 host-fixture profiles; no runtime configuration mutation."""

import hashlib
from pathlib import Path
import subprocess

from oc_box import oc_box as box, opencode_compat as compat
from tools.test_v2_podman import native_env

DEFAULT_NATIVE_VERSION = "2.2.6"
NATIVE_VERSIONS = (DEFAULT_NATIVE_VERSION, "2.3.0")


def native_version(expected=DEFAULT_NATIVE_VERSION):
    if expected not in NATIVE_VERSIONS:
        raise ValueError("Unsupported native host fixture profile")
    return expected


def file_hash(binary):
    digest = hashlib.sha256()
    with Path(binary).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def native_identity(binary, expected=DEFAULT_NATIVE_VERSION):
    return {
        "version": native_version(expected),
        "path": str(Path(binary).resolve(strict=True)),
        "sha256": file_hash(binary),
    }


def check_native_version(root, binary, major, *, expected=DEFAULT_NATIVE_VERSION):
    """Keep the original private probe boundaries and require an exact release."""
    expected = native_version(expected)
    if major != "2":
        raise ValueError("Native host fixture profiles require V2")
    result = box.run(
        [str(binary), "--version"],
        env=native_env(root, major),
        stdin=subprocess.DEVNULL,
        capture=True,
        private=True,
        timeout=15,
    )
    try:
        found = compat.version(result.stdout)
    except ValueError:
        raise ValueError("Native host fixture version is invalid") from None
    if found != expected:
        raise ValueError(
            "Native host fixture executable does not match selected profile"
        )
    return found
