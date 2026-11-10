"""Stage complete user-local installations and atomically switch the launcher."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
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
ACTIVATION_MARKER = ".activation-pending.json"
# Keep the root install.py calling protocol unchanged for released updaters.
# New updater callers require this explicit publication-guard contract.
UPGRADE_PROTOCOL = 1


def require_settled_installation(target):
    # A killed publisher or failed rollback must never masquerade as an
    # up-to-date installation. Recovery bytes are deliberately retained.
    if any(target.glob(".activation-*")):
        raise ValueError(
            "Installation activation requires explicit recovery; retained .activation-* evidence must be inspected before reinstalling or updating."
        )


@contextlib.contextmanager
def install_lock(home=None):
    home = Path.home() if home is None else Path(home)
    target = home / ".local/share/oc-box"
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("The oc-box installation root must not be a symlink.")
    with (target / ".install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield target


def launcher(release):
    return (
        "#!/bin/sh\nexec python3 " + shlex.quote(str(release / "oc_box.py")) + ' "$@"\n'
    )


def active_release(home=None):
    """Check versioned launcher/pointer coherence without executing user code."""
    home = Path.home() if home is None else Path(home)
    target = home / ".local/share/oc-box"
    require_settled_installation(target)
    current = target / "current"
    if not current.exists() and not current.is_symlink():
        return None  # The legacy flat installer has no pinned-release pointer.
    if not current.is_symlink():
        raise ValueError("Installation pointers require explicit recovery.")
    release = current.resolve(strict=True)
    if release.parent != (target / "releases").resolve():
        raise ValueError("Installation pointers require explicit recovery.")
    binary = home / ".local/bin/oc-box"
    if (
        not binary.is_file()
        or binary.is_symlink()
        or binary.read_text() != launcher(release)
        or get_version_info(target) != get_version_info(release)
    ):
        raise ValueError("Installation launcher/metadata require explicit recovery.")
    return release


def restore_activation_entry(home, record):
    path = home / record["path"]
    kind = record["kind"]
    if kind == "absent":
        path.unlink(missing_ok=True)
        return
    backup = home / record["backup"]
    if kind == "legacy-directory":
        if backup.exists():
            path.unlink(missing_ok=True)
            os.replace(backup, path)
        elif not path.is_dir() or path.is_symlink():
            raise ValueError("Legacy manual recovery is incomplete.")
        return
    if (
        kind == "link"
        and path.is_symlink()
        and os.readlink(path) == os.readlink(backup)
    ):
        return
    if (
        kind == "file"
        and not path.is_symlink()
        and path.is_file()
        and path.read_bytes() == backup.read_bytes()
        and stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode)
    ):
        return
    # Stage restoration in the destination's own filesystem. Manuals or bin
    # directories need not share the release directory's filesystem.
    fd, temporary = tempfile.mkstemp(prefix=".oc-box-rollback-", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        if kind == "link":
            temporary.unlink()
            temporary.symlink_to(os.readlink(backup))
        else:
            shutil.copyfile(backup, temporary)
            temporary.chmod(stat.S_IMODE(backup.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def activation(home, target, release, paths):
    """Rollback handled failures; leave explicit evidence after an interruption.

    Removal of the pending marker is the commit point, after the pinned launcher,
    current pointer, metadata and compatibility resources have all been published.
    This is recoverable process-failure ordering, not power-loss certification.
    """
    recovery = Path(tempfile.mkdtemp(prefix=".activation-", dir=target))
    marker = target / ACTIVATION_MARKER
    records = []
    retain = False
    try:
        for number, path in enumerate(paths):
            record = {"path": str(path.relative_to(home)), "kind": "absent"}
            backup = recovery / str(number)
            if path.is_symlink():
                backup.symlink_to(os.readlink(path))
                record.update(kind="link", backup=str(backup.relative_to(home)))
            elif path.is_file():
                shutil.copyfile(path, backup)
                backup.chmod(stat.S_IMODE(path.stat().st_mode))
                record.update(kind="file", backup=str(backup.relative_to(home)))
            elif path.is_dir() and path == target / "docs":
                legacy = target / "legacy-docs"
                if legacy.exists() or legacy.is_symlink():
                    raise ValueError(
                        "Both legacy manual directories exist; inspect the installation."
                    )
                record.update(
                    kind="legacy-directory", backup=str(legacy.relative_to(home))
                )
            elif path.exists():
                raise ValueError("Unexpected nonregular installation destination.")
            records.append(record)
        atomic_file(
            recovery / "state.json",
            (json.dumps({"format": 1, "entries": records}, indent=2) + "\n").encode(),
            0o600,
        )
        atomic_file(
            marker,
            (
                json.dumps(
                    {"format": 1, "recovery": recovery.name, "release": release.name}
                )
                + "\n"
            ).encode(),
            0o600,
        )
        try:
            yield
            marker.unlink()
        except BaseException as original:
            # Retain independent recovery copies even if rollback itself fails.
            failures = []
            current_failed = False
            for record in reversed(records):
                if (
                    current_failed
                    and home / record["path"] == home / ".local/bin/oc-box"
                ):
                    # Keep the newer launcher if current could not be restored.
                    # Older updater code cannot understand our recovery marker;
                    # it must not see new metadata with the old selected binary.
                    continue
                try:
                    restore_activation_entry(home, record)
                except (OSError, ValueError) as error:
                    failures.append(type(error).__name__)
                    if home / record["path"] == target / "current":
                        current_failed = True
            if failures:
                retain = True
                raise ValueError(
                    "Installation activation failed and requires explicit recovery; retained .activation-* evidence and releases were not removed."
                ) from original
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                retain = True
                raise ValueError(
                    "Installation rollback completed but requires explicit recovery of its retained activation marker."
                ) from original
            raise
    finally:
        if not retain and not marker.exists() and not marker.is_symlink():
            shutil.rmtree(recovery)


def publish_release(home, target, release, build):
    man = home / ".local/share/man/man1/oc-box.1"
    info = home / ".local/share/info/oc-box.info"
    completion = home / ".local/share/bash-completion/completions/oc-box"
    binary = home / ".local/bin/oc-box"
    names = (*PROGRAM_FILES, "build-info.json", "docs", "completions")
    paths = [
        man,
        info,
        completion,
        *(target / name for name in names),
        binary,
        target / "current",
    ]
    with activation(home, target, release, paths):
        atomic_file(man, (release / "docs/oc-box.1").read_bytes())
        atomic_file(info, (release / "docs/oc-box.info").read_bytes())
        atomic_file(completion, (release / COMPLETION_FILE).read_bytes())
        for name in names:
            destination = target / name
            if destination.is_dir() and not destination.is_symlink():
                destination.rename(target / "legacy-docs")
            temporary = target / (".next-" + name)
            try:
                temporary.unlink(missing_ok=True)
                temporary.symlink_to(
                    "current/" + name,
                    target_is_directory=name in ("docs", "completions"),
                )
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        atomic_file(binary, launcher(release).encode(), 0o755)
        # Publish metadata last: even an older pinned updater must never observe
        # a new installed identity while the selected launcher is still old.
        link = target / ".current-next"
        try:
            link.unlink(missing_ok=True)
            link.symlink_to(release.relative_to(target), target_is_directory=True)
            os.replace(link, target / "current")
        finally:
            link.unlink(missing_ok=True)
        if (
            (target / "current").resolve(strict=True) != release
            or get_version_info(target) != build
            or binary.read_text() != launcher(release)
        ):
            raise ValueError("Installation activation identity could not be verified.")
    return man, info, completion, binary


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
    with install_lock(home):
        require_settled_installation(target)
        expected = os.environ.get("OC_BOX_UPGRADE_EXPECTED_COMMIT")
        if expected is not None:
            if (
                expected != build.get("commit")
                or type(build.get("commit_count")) is not int
                or build.get("dirty") is not False
            ):
                raise ValueError(
                    "Downloaded installation identity could not be verified."
                )
            # Only updater-originated publication is ordering-constrained. A
            # deliberate manual source reinstall may still select older code.
            active = active_release(home)
            current = get_version_info(target)
            if (
                current.get("commit_count") is not None
                and current["commit_count"] > build["commit_count"]
            ):
                print(
                    "Installed build advanced while downloading; leaving it installed."
                )
                return active or target
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
            man, info, completion, binary = publish_release(
                home, target, release, build
            )
            activated = True
        finally:
            if not activated and not any(target.glob(".activation-*")):
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
