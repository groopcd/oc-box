"""Read-only, feature-scoped host prerequisites and distro package guidance.

No package manager, network request, Podman operation, or password prompt runs
here. A command found on PATH is not proof of working rootless isolation.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

DISTROS = ("tumbleweed", "leap", "ubuntu", "debian", "fedora", "arch", "alpine")
MANAGERS = {
    "tumbleweed": ("zypper", "install"),
    "leap": ("zypper", "install"),
    "ubuntu": ("apt", "install"),
    "debian": ("apt", "install"),
    "fedora": ("dnf", "install"),
    "arch": ("pacman", "-Syu", "--needed"),
    "alpine": ("apk", "add"),
}
# Shared package names, then distro-specific command providers. OS package names
# are fixed catalog data; /etc/os-release contents never become shell syntax.
COMMON = {
    "podman": "podman",
    "pasta": "passt",
    "age": "age",
    "age-keygen": "age",
    "git": "git",
    "curl": "curl",
    "du": "coreutils",
    "mktemp": "coreutils",
    "bash": "bash",
    "tar": "tar",
    "gzip": "gzip",
    "rg": "ripgrep",
    "node": "nodejs",
    "npm": "npm",
    "ca-certificates": "ca-certificates",
    "catatonit": "catatonit",
    "conmon": "conmon",
    "oci-runtime": "crun",
    "fuse-overlayfs": "fuse-overlayfs",
    "makeinfo": "texinfo",
}
PACKAGES = {
    "tumbleweed": {
        "python": "python3",
        "newuidmap": "shadow",
        "newgidmap": "shadow",
        "pdftotext": "poppler-tools",
        "man": "man",
        "info": "info",
    },
    "leap": {
        "python": "python3",
        "newuidmap": "shadow",
        "newgidmap": "shadow",
        "pdftotext": "poppler-tools",
        "man": "man",
        "info": "info",
    },
    "ubuntu": {
        "python": "python3",
        "newuidmap": "uidmap",
        "newgidmap": "uidmap",
        "pdftotext": "poppler-utils",
        "man": "man-db",
        "info": "info",
    },
    "debian": {
        "python": "python3",
        "newuidmap": "uidmap",
        "newgidmap": "uidmap",
        "pdftotext": "poppler-utils",
        "man": "man-db",
        "info": "info",
    },
    "fedora": {
        "python": "python3",
        "newuidmap": "shadow-utils",
        "newgidmap": "shadow-utils",
        "pdftotext": "poppler-utils",
        "man": "man-db",
        "info": "info",
        "curl": "curl-minimal",
    },
    "arch": {
        "python": "python",
        "newuidmap": "shadow",
        "newgidmap": "shadow",
        "pdftotext": "poppler",
        "man": "man-db",
        "info": "texinfo",
    },
    "alpine": {
        "python": "python3",
        "newuidmap": "shadow-subids",
        "newgidmap": "shadow-subids",
        "pdftotext": "poppler-utils",
        "man": "mandoc",
        "info": "texinfo",
    },
}
BASELINE = (
    "bash",
    "ca-certificates",
    "curl",
    "git",
    "python",
    "rg",
    "tar",
    "gzip",
    "node",
    "npm",
    "pdftotext",
)
RUNTIME = ("linux", "python", "podman", "newuidmap", "newgidmap")
CREATE = (*RUNTIME, "pasta", "catatonit", "conmon", "oci-runtime")
FEATURES = {
    "launch": ("OpenCode launch/reset and history sync", (*CREATE, "opencode")),
    "maintenance": ("Container maintenance, stop, repair and purge", RUNTIME),
    "backup": (
        "Encrypted full/portable backup",
        (*RUNTIME, "age", "age-keygen", "memfd"),
    ),
    "backup-info": (
        "Encrypted backup inspection (no Podman needed)",
        ("linux", "python", "age", "age-keygen", "memfd"),
    ),
    "restore": ("Encrypted workspace restore", (*CREATE, "age", "age-keygen", "memfd")),
    "vault": (
        "Optional encrypted credential vault",
        ("linux", "python", "cryptography"),
    ),
    "github-upgrade": ("GitHub/source installation and upgrades", ("python", "git")),
    "bootstrap": (
        "One-line installer download",
        ("python", "git", "curl", "mktemp", "ca-certificates"),
    ),
    "storage": ("Complete host/private-home disk usage", ("du",)),
    "overlay": ("Optional FUSE overlay storage driver", ("fuse-overlayfs",)),
    "manuals": ("Read installed man/Info manuals", ("man", "info")),
    "docs-build": ("Contributor manual regeneration", ("python", "makeinfo")),
}
PRIVATE_PATHS = {
    "catatonit": ("/usr/libexec/podman/catatonit", "/usr/lib/podman/catatonit"),
    "conmon": ("/usr/libexec/podman/conmon", "/usr/lib/podman/conmon"),
}


class DependencyError(Exception):
    pass


def host_distro(path=Path("/etc/os-release")):
    try:
        values = {}
        for line in path.read_text().splitlines():
            if line.startswith(("ID=", "VERSION_ID=")):
                key, raw = line.split("=", 1)
                words = shlex.split(raw)
                values[key] = words[0] if len(words) == 1 else ""
        ident = values.get("ID", "")
    except (OSError, ValueError):
        return None
    return {
        "opensuse-tumbleweed": "tumbleweed",
        "opensuse-slowroll": "tumbleweed",
        "opensuse-leap": "leap",
        "opensuse": "leap",
        "archlinux": "arch",
        **{name: name for name in DISTROS},
    }.get(ident)


def package_names(distro, keys):
    mapping = {**COMMON, **PACKAGES[distro]}
    return sorted({mapping[key] for key in keys if key in mapping})


def install_commands(distro, keys):
    if distro not in DISTROS:
        return []
    packages = package_names(distro, keys)
    if not packages:
        return []
    prefix = "sudo "
    command = prefix + shlex.join([*MANAGERS[distro], *packages])
    if distro in ("ubuntu", "debian"):
        command = "sudo apt update && " + command
    elif distro in ("tumbleweed", "leap"):
        command = "sudo zypper refresh && " + command
    return [command]


def executable(key):
    names = ("crun", "runc") if key == "oci-runtime" else (key,)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for path in PRIVATE_PATHS.get(key, ()):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def probe(key):
    if key == "linux":
        return platform.system() == "Linux", "Linux host required"
    if key == "python":
        return sys.version_info >= (3, 10), "Running Python must be 3.10 or newer"
    if key == "memfd":
        try:
            fd = os.memfd_create("oc-box-doctor", os.MFD_CLOEXEC)
            os.close(fd)
            return True, "Linux anonymous memory-file support"
        except (AttributeError, OSError):
            return (
                False,
                "Linux memfd support is required; check kernel/container restrictions",
            )
    if key == "cryptography":
        if __package__:
            from .vault import VaultError, crypto
        else:
            from vault import VaultError, crypto
        try:
            crypto()
            return (
                True,
                "Optional cryptography APIs available in this Python installation",
            )
        except (ImportError, ValueError, RuntimeError, VaultError):
            return False, "Install the vault extra in this oc-box installation"
    if key == "ca-certificates":
        import ssl

        try:
            present = ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
            return present, "Trusted HTTPS CA certificates for downloads"
        except (OSError, ssl.SSLError):
            return False, "HTTPS trust store unavailable"
    found = executable(key)
    if not found:
        return False, "Command unavailable: " + key
    if key == "du":
        try:
            result = subprocess.run(
                [found, "--version"],
                capture_output=True,
                timeout=5,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
            )
            if result.returncode or b"GNU coreutils" not in result.stdout:
                return (
                    False,
                    "GNU du required; BusyBox du lacks the byte-counting options used here",
                )
        except (OSError, subprocess.SubprocessError):
            return False, "Cannot verify GNU du"
    return True, "Command available: " + key


def extra_guidance(keys):
    lines = []
    if "opencode" in keys:
        lines.append("Install host OpenCode 1.x: https://opencode.ai/docs/#install")
    if "cryptography" in keys:
        lines.extend(
            [
                "uv: uv tool install --force 'oc-box[vault]'",
                "pipx: pipx inject oc-box 'cryptography>=44'",
                "GitHub/source: rerun install.py --with-vault (or the curl installer with --with-vault)",
            ]
        )
    return lines


def missing_message(label, keys, *, distro=None):
    distro = host_distro() if distro is None else distro
    lines = [
        label
        + " unavailable; missing/incompatible prerequisites: "
        + ", ".join(keys)
        + "."
    ]
    lines.extend(
        "Install on this OS: " + command for command in install_commands(distro, keys)
    )
    if distro is None:
        lines.append(
            "Unrecognized host distro; install the listed command providers with your OS package manager."
        )
    lines.extend(extra_guidance(keys))
    lines.append("Run 'oc-box doctor' for feature-specific dependency details.")
    return "\n".join(lines)


def require(feature):
    label, keys = FEATURES[feature]
    missing = [key for key in keys if not probe(key)[0]]
    if missing:
        raise DependencyError(missing_message(label, missing))


def report(selected=None):
    distro = host_distro()
    cache = {}
    features = []
    for name in selected or FEATURES:
        label, keys = FEATURES[name]
        results = []
        for key in keys:
            if key not in cache:
                cache[key] = probe(key)
            available, detail = cache[key]
            results.append({"id": key, "available": available, "detail": detail})
        missing = [item["id"] for item in results if not item["available"]]
        features.append(
            {
                "id": name,
                "label": label,
                "available": not missing,
                "dependencies": results,
                "missing": missing,
                "packages": package_names(distro, missing) if distro else [],
                "install_commands": install_commands(distro, missing),
                "guidance": extra_guidance(missing),
            }
        )
    return {
        "schema_version": 1,
        "scope": "current-container"
        if os.environ.get("OC_BOX_CONTAINER") == "1"
        else "host",
        "distro": distro,
        "features": features,
        "notes": [
            "Reports the OS running doctor, not a selected workspace distro.",
            "Checks prerequisites only; rootless Podman, subordinate IDs, cgroup delegation, terminal access and filesystem support are checked by feature commands.",
            "Container baseline tools (Bash, certificates, curl, Git, Python, ripgrep, tar/gzip, Node/npm and PDF tools) are installed automatically inside each workspace; uv and matching OpenCode are bootstrapped there.",
            "Optional feature failures do not disable unrelated functionality; FUSE overlay is optional when kernel overlay works.",
        ],
    }


def parser():
    result = argparse.ArgumentParser(
        prog="oc-box doctor",
        description="Read-only dependency report with feature-specific OS installation guidance.",
    )
    result.add_argument(
        "--json", action="store_true", help="emit a machine-readable dependency report"
    )
    result.add_argument(
        "--feature",
        action="append",
        choices=tuple(FEATURES),
        help="check one feature; repeatable; default reports all features",
    )
    return result


def cli(argv):
    args = parser().parse_args(argv)
    data = report(args.feature)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(
            f"Dependencies on {data['scope']} OS: {data['distro'] or 'unrecognized distro'}"
        )
        for feature in data["features"]:
            status = "available" if feature["available"] else "unavailable"
            print(f"\n{feature['id']}: {status} — {feature['label']}")
            for item in feature["dependencies"]:
                if not item["available"]:
                    print("  Missing/incompatible: " + item["detail"])
            for command in feature["install_commands"]:
                print("  Install on this OS: " + command)
            for line in feature["guidance"]:
                print("  " + line)
            if (
                feature["packages"] == []
                and feature["missing"]
                and data["distro"] is None
            ):
                print(
                    "  Unrecognized distro; use its package manager for the listed providers."
                )
        print("\n" + "\n".join(data["notes"]))
    checked = args.feature or ["launch"]
    return int(
        any(not item["available"] for item in data["features"] if item["id"] in checked)
    )
