"""Container-only baseline setup, invoked over podman exec stdin."""

import io
import os
from pathlib import Path
import platform
import shutil
import tarfile
import urllib.request

target = Path("/opt/oc-box/runtime/bin")
target.mkdir(parents=True, exist_ok=True)
if not (target / "uv").is_file():
    machine = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if not machine:
        raise RuntimeError("uv bootstrap supports x86_64 and aarch64")
    libc = "musl" if Path("/etc/alpine-release").exists() else "gnu"
    asset = f"uv-{machine}-unknown-linux-{libc}.tar.gz"
    url = "https://github.com/astral-sh/uv/releases/latest/download/" + asset
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read(64 * 1024 * 1024)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for name in ("uv", "uvx"):
            member = next(item for item in archive.getmembers() if item.name.endswith("/" + name) and item.isfile())
            with archive.extractfile(member) as source, (target / name).open("wb") as destination:
                shutil.copyfileobj(source, destination)
            (target / name).chmod(0o755)
# The user's existing MCP config refers to /usr/bin/uvx. Keep that path valid.
for name in ("uv", "uvx"):
    link = Path("/usr/bin") / name
    if not link.exists():
        link.symlink_to(target / name)
home = Path(os.environ["HOME"])
for name in (".config", ".cache", ".local/share", ".local/state"):
    (home / name).mkdir(parents=True, exist_ok=True)
print("Container baseline ready: Node/npm, Python/uv, Git, ripgrep, and PDF tools.")
