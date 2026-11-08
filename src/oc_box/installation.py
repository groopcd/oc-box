"""Stage complete user-local installations and atomically switch the launcher."""

import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import venv

if __package__:
    from .paths import runtime_root, source_root
    from .version_info import get_version_info
else:
    from paths import runtime_root, source_root
    from version_info import get_version_info

RUNTIME_FILES = (
    "oc_box.py",
    "dependencies.py",
    "opencode_compat.py",
    "opencode_v2_io.py",
    "workspace_service.py",
    "session_terminal.py",
    "plugin_compat.py",
    "plugin_bridge.js",
    "backups.py",
    "backup_crypto.py",
    "backup_archive.py",
    "vault.py",
    "secret_prompt.py",
    "credentials.py",
    "__init__.py",
    "__main__.py",
    "bootstrap.py",
    "sync_config.py",
    "terminal_ui.py",
    "status_report.py",
    "version_info.py",
    "paths.py",
    "workspace_identity.py",
    "installation.py",
    "updater.py",
    "install.py",
)
RESOURCE_FILES = (
    "install.sh",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "NOTICE",
)
PROGRAM_FILES = RUNTIME_FILES + RESOURCE_FILES
MANUAL_FILES = (
    "cli.md",
    "oc-box.1",
    "oc-box.texi",
    "oc-box.info",
    "opencode-2.md",
    "host-plugins.md",
)
COMPLETION_FILE = "completions/oc-box"


def source_file(source, name):
    source = source_root(source)
    return (runtime_root(source) if name in RUNTIME_FILES else source) / name


def atomic_file(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".oc-box-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def optional_crypto_runtime(release):
    """Install the maintained optional library without touching host Python.

    Resolve dependencies through a throwaway venv, then copy their wheels into
    this release's private directory. No project/user/system site installation.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(release),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    with tempfile.TemporaryDirectory(prefix="oc-box-crypto-build-") as temporary:
        tools = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(tools)
        result = subprocess.run(
            [
                str(tools / "bin/python"),
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--target",
                str(release / "vault-deps"),
                "cryptography>=44",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise ValueError(
                "Optional vault crypto installation failed; no launcher was switched. Install a supported cryptography wheel or use the unchanged env-file workflow."
            )


def install(source, home=None, *, with_vault=False):
    source = source_root(source)
    if (source / "distribution.json").is_file():
        raise ValueError(
            "This copy is managed by a Python package installer; use pipx/uv/pip to upgrade, not install.py. The GitHub installer remains available separately."
        )
    home = Path.home() if home is None else Path(home)
    target = home / ".local/share/oc-box"
    # Validate all inputs before changing an existing installation.
    inputs = [
        *(source_file(source, name) for name in PROGRAM_FILES),
        *(source / "docs" / name for name in MANUAL_FILES),
        source / COMPLETION_FILE,
    ]
    for path in inputs:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or nonregular installation file: {path.name}")
    build = get_version_info(source)
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("The oc-box installation root must not be a symlink.")
    releases = target / "releases"
    releases.mkdir(exist_ok=True)
    if releases.is_symlink():
        raise ValueError("The oc-box releases directory must not be a symlink.")
    for name in (*PROGRAM_FILES, "build-info.json", "current"):
        destination = target / name
        if destination.is_dir() and not destination.is_symlink():
            raise ValueError(f"Unexpected directory in installation: {name}")
    if (target / "completions").exists() and not (target / "completions").is_symlink():
        raise ValueError(
            "Unexpected completion directory in installation; inspect it before reinstalling."
        )
    if (
        (target / "docs").is_dir()
        and not (target / "docs").is_symlink()
        and (target / "legacy-docs").exists()
    ):
        raise ValueError(
            "Both legacy manual directories exist; inspect the installation."
        )
    with (target / ".install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        release = Path(
            tempfile.mkdtemp(
                prefix=(build.get("commit") or "local")[:12] + "-", dir=releases
            )
        )
        activated = False
        try:
            for name in PROGRAM_FILES:
                shutil.copyfile(source_file(source, name), release / name)
                (release / name).chmod(0o644)
            (release / "docs").mkdir()
            for name in MANUAL_FILES:
                shutil.copyfile(source / "docs" / name, release / "docs" / name)
                (release / "docs" / name).chmod(0o644)
            (release / "completions").mkdir()
            shutil.copyfile(source / COMPLETION_FILE, release / COMPLETION_FILE)
            (release / COMPLETION_FILE).chmod(0o644)
            atomic_file(
                release / "build-info.json",
                (json.dumps(build, indent=2) + "\n").encode(),
            )
            if with_vault:
                try:
                    optional_crypto_runtime(release)
                except subprocess.TimeoutExpired as exc:
                    raise ValueError(
                        "Optional vault dependency download timed out; existing installation retained."
                    ) from exc
                atomic_file(
                    release / "vault-support.json", b'{"optional_crypto": true}\n'
                )
            # Readers/install-info are optional: ship prebuilt manuals.
            man = home / ".local/share/man/man1/oc-box.1"
            info = home / ".local/share/info/oc-box.info"
            atomic_file(man, (release / "docs/oc-box.1").read_bytes())
            atomic_file(info, (release / "docs/oc-box.info").read_bytes())
            completion = home / ".local/share/bash-completion/completions/oc-box"
            atomic_file(completion, (release / COMPLETION_FILE).read_bytes())
            binary = home / ".local/bin/oc-box"
            # A running launcher keeps this exact immutable release path. It
            # never lazy-loads modules from a half-updated or newer directory.
            wrapper = (
                "#!/bin/sh\nexec python3 "
                + shlex.quote(str(release / "oc_box.py"))
                + ' "$@"\n'
            )
            link = target / ".current-next"
            link.unlink(missing_ok=True)
            link.symlink_to(release.relative_to(target), target_is_directory=True)
            os.replace(link, target / "current")
            activated = True
            # Preserve documented/legacy installed paths as pointers into the
            # current release, including build metadata and manual sources.
            for name in (*PROGRAM_FILES, "build-info.json", "docs", "completions"):
                destination = target / name
                if destination.is_dir() and not destination.is_symlink():
                    # The old installer used a docs directory; keep it intact
                    # until it can be renamed, then replace only our directory.
                    if name != "docs":
                        raise ValueError(
                            f"Unexpected directory in installation: {name}"
                        )
                    legacy = target / "legacy-docs"
                    if legacy.exists():
                        raise ValueError(
                            "Both legacy manual directories exist; inspect the installation."
                        )
                    destination.rename(legacy)
                temporary = target / (".next-" + name)
                temporary.unlink(missing_ok=True)
                temporary.symlink_to(
                    "current/" + name,
                    target_is_directory=name in ("docs", "completions"),
                )
                os.replace(temporary, destination)
            atomic_file(binary, wrapper.encode(), 0o755)
        finally:
            if not activated:
                shutil.rmtree(release)
    utility = shutil.which("install-info") or shutil.which(
        "install-info", path="/usr/sbin:/sbin"
    )
    if utility:
        try:
            result = subprocess.run(
                [utility, "--info-dir=" + str(info.parent), str(info)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode:
                print(
                    "Note: Info index update failed; use 'info --file "
                    + str(info)
                    + "'."
                )
        except (OSError, subprocess.TimeoutExpired):
            print(
                "Note: Info index update unavailable; use 'info --file "
                + str(info)
                + "'."
            )
    print(f"Installed {binary} ({build['version']})")
    print(f"Installed manual pages: {man} and {info}")
    print(f"Installed Bash completion: {completion}")
    print("Enable in this Bash shell: source <(oc-box completion bash)")
    print(
        "Read with 'man oc-box' or 'info oc-box'; full reference: "
        + str(target / "docs/cli.md")
    )
    print("Ensure ~/.local/bin is on PATH. Use 'oc-box upgrade' for future updates.")
    return release
